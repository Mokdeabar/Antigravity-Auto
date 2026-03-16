"""
temporal_planner.py — V19 Temporal Planning Engine (Product Manager)

Ingests high-level EPIC.md feature requests and decomposes them into a
Directed Acyclic Graph (DAG) of atomic, sequentially committed micro-tasks.

Each node in the DAG is a single Git transaction — small enough to fit in the
Gemini Worker's context window, testable in isolation, and independently
committable.

Execution Flow:
  1. Ingest EPIC.md → Local Manager decomposes → JSON DAG
  2. Execute unblocked nodes sequentially via V15 Git transactions
  3. On success: commit becomes the new baseline, mark node complete
  4. On failure: reflect via V17, then replan remainder (MAX_REPLAN_COUNT=3)
  5. Workspace hash validation before every node execution

State persists to `.ag-memory/epic_state.json` so the agent can resume
after crashes.
"""

from __future__ import annotations

import hashlib
from . import config
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("supervisor.temporal_planner")

_MEMORY_DIR = Path(__file__).resolve().parent.parent / ".ag-memory"
_EPIC_STATE_PATH = _MEMORY_DIR / "epic_state.json"
MAX_REPLAN_COUNT = 3  # V60: used as a fallback default; instances compute dynamically


class TaskNode:
    """A single atomic task in the DAG."""

    def __init__(
        self,
        task_id: str,
        description: str,
        dependencies: List[str] = None,
        status: str = "pending",  # pending | running | complete | failed | skipped
        knowledge_gaps: List[str] = None,
    ):
        self.task_id = task_id
        self.description = description
        self.dependencies = dependencies or []
        self.knowledge_gaps = knowledge_gaps or []
        self.status = status
        self.result: str = ""
        self.commit_sha: str = ""
        self.retry_count: int = 0      # V40: Track retries for re-queuing
        self.max_retries: int = 2      # V40: Max re-queue attempts per task
        self.priority: int = 0         # V44: 0=normal, 100=user instruction (next task)
        self.started_at: float = 0.0   # Watchdog: wall-clock time when status -> running
        self.acceptance_criteria: str = ""  # V58: Verifiable conditions for audit cross-check

    # V23: UI keyword detection for Visual Phase triggering
    _UI_KEYWORDS = [
        "component", "frontend", "view", "page", "layout", "ui",
        "button", "form", "modal", "dialog", "sidebar", "navbar",
        "header", "footer", "card", "dashboard", "chart", "table",
        "css", "style", "theme", "responsive", "animation", "menu",
        "checkout", "login", "signup", "html", "template", "render",
    ]

    @property
    def is_visual(self) -> bool:
        """Check if this node is a frontend/UI task that needs Visual QA."""
        lower = self.description.lower()
        return any(kw in lower for kw in self._UI_KEYWORDS)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "dependencies": self.dependencies,
            "knowledge_gaps": self.knowledge_gaps,
            "status": self.status,
            "result": self.result,
            "commit_sha": self.commit_sha,
            "retry_count": self.retry_count,
            "priority": self.priority,
            "started_at": self.started_at,
            "acceptance_criteria": self.acceptance_criteria,  # V58
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskNode":
        node = cls(
            task_id=data["task_id"],
            description=data["description"],
            dependencies=data.get("dependencies", []),
            status=data.get("status", "pending"),
            knowledge_gaps=data.get("knowledge_gaps", []),
        )
        node.result = data.get("result", "")
        node.commit_sha = data.get("commit_sha", "")
        node.retry_count = data.get("retry_count", 0)
        node.priority = data.get("priority", 0)
        node.started_at = data.get("started_at", 0.0)
        node.acceptance_criteria = data.get("acceptance_criteria", "")  # V58
        return node


class TemporalPlanner:
    """
    Decomposes epics into DAGs and executes them as atomic Git transactions.
    """

    # V74: Restructured with section headers (§5.1)
    DECOMPOSITION_PROMPT = (
        "== ROLE ==\n"
        "You are an expert technical project planner operating at SENIOR ARCHITECT level. "
        "You will receive a high-level feature request (epic). Decompose it into the COMPLETE "
        "set of atomic tasks needed to fully implement EVERY requirement to an award-winning standard.\n\n"
        "== RULES ==\n"
        "1. Each task must modify at most 2-3 files.\n"
        "2. Each task must be independently testable.\n"
        "3. Dependencies must form a DAG (no cycles).\n"
        "4. task_id must be short alphanumeric (e.g., 't1', 't2', ... 't95', etc.).\n"
        "5. First task must have empty dependencies.\n"
        "6. TASK COUNT — SCALE TO ACTUAL SCOPE (NO ARTIFICIAL CAPS):\n"
        "   There is NO hard cap on task count. Create as many tasks as the project GENUINELY needs.\n"
        "   - Simple isolated features: 5-15 tasks.\n"
        "   - Medium features or refactors: 15-30 tasks.\n"
        "   - Full applications or major goals: AIM FOR 50-95 tasks across 3 categories:\n"
        "     * CATEGORY A — FUNCTIONALITY (~50 tasks): Core features, data models, logic,\n"
        "       APIs, routing, state management, forms, auth, integrations, error handling.\n"
        "     * CATEGORY B — STYLING & UI/UX (~30 tasks): Layout, typography, color system,\n"
        "       animations, micro-interactions, responsive design, component polish, icons,\n"
        "       dark mode, empty/error/loading states, 2026 visual excellence.\n"
        "     * CATEGORY C — LIGHTHOUSE & PERFORMANCE (~15 tasks): Performance (FCP, LCP,\n"
        "       TBT, CLS, SI), accessibility (WCAG 2.1 AA), best practices (CSP, HTTPS,\n"
        "       source maps), SEO (meta, robots.txt, structured data), image optimization,\n"
        "       HTTP/2+, cache headers, code splitting, tree-shaking, font loading.\n"
        "   - Each task in the description should note which category it belongs to: [FUNC], [UI/UX], or [PERF].\n"
        "   - Bug fixes belong to whichever category the bug affects.\n"
        "   - If NOTHING is built yet (greenfield): maximize coverage — every layer,\n"
        "     every feature, every integration point, every visual detail gets its own task.\n"
        "   - If building on EXISTING code: analyze what exists, focus on BIGGEST IMPACT\n"
        "     first — missing features > broken features > incomplete features > improvements\n"
        "     > refinements > polish.\n"
        "7. PRIORITY ORDER — biggest impact tasks FIRST in the DAG:\n"
        "   Tier 1: Core architecture, data models, critical broken functionality\n"
        "   Tier 2: Major features, key user flows, primary UI components\n"
        "   Tier 3: Secondary features, responsive breakpoints, animations\n"
        "   Tier 4: Polish, micro-interactions, edge cases, accessibility WCAG sweep\n"
        "   Tier 5: Performance optimization, Lighthouse metrics, SEO, caching\n"
        "8. Break complex systems into layers: core/engine first, then features, then UI/polish.\n"
        "9. Each task description must be DETAILED and SPECIFIC — include which files to create/modify, "
        "what functions to implement, what data structures to use.\n"
        "10. For each task, include a 'knowledge_gaps' array listing any external "
        "libraries, APIs, or frameworks the agent needs documentation for. "
        "Each entry should be a search query. Use an empty array if no gaps exist.\n"
        "11. CONTENT & PAGE FLOW: When creating user-facing content (copy, headings, CTAs, \n"
        "    descriptions, onboarding flows, landing pages, etc.):\n"
        "    - Content must be HIGHLY TAILORED to the TARGET AUDIENCE's language, tone, \n"
        "      and cultural expectations, not generic placeholder text.\n"
        "    - Content must FIT the PROJECT'S PURPOSE and brand identity.\n"
        "    - Page flow (section order, information hierarchy, user journey) must follow \n"
        "      PRESENT-DAY BEST PRACTICES for that specific type of project \n"
        "      (e.g., SaaS landing page, e-commerce, portfolio, dashboard, game).\n"
        "    - Include dedicated tasks for content writing and page flow optimization \n"
        "      when the project involves user-facing pages.\n"
        "    - ALL content MUST read as authentically HUMAN-WRITTEN. Zero AI tells.\n"
        "    - NEVER use em dashes or en dashes (U+2014, U+2013). Use commas, periods, \n"
        "      colons, parentheses, or double-hyphens -- instead.\n"
        "    - BANNED words: delve, utilize, leverage, streamline, robust, seamless, elevate, \n"
        "      foster, holistic, harness, cutting-edge, game-changer, pivotal, multifaceted, \n"
        "      plethora, a myriad of. Also ban filler phrases like 'it is important/worth \n"
        "      noting', 'at the end of the day', 'first and foremost', 'in conclusion'.\n"
        "    - Vary sentence length. No consecutive sentences starting the same way. \n"
        "      Write in the natural voice of the target audience.\n"
        "13. DEPENDENCY VERSIONS: When your task descriptions reference any npm or pip package,\n"
        "    you MUST verify and use the current latest stable version — not a version from memory.\n"
        "    Run `npm view <pkg> version` in /workspace to confirm before writing the task.\n"
        "    Example: 'Install react@19.1.0, react-dom@19.1.0 (verified latest as of today)'\n"
        "    This prevents the upgrade cycle that wastes a full DAG task later.\n"
        "    Pin exact versions in package.json additions (no ^ or ~ prefixes).\n"

        "== QUALITY STANDARDS ==\n"
        "12. UI/UX VISUAL EXCELLENCE — 2026 AWWWARDS SITE OF THE YEAR STANDARD:\n"
        "    Every DAG with a user-facing interface MUST include AT LEAST 15 tasks\n"
        "    dedicated to visual excellence and interaction design. These are CORE REQUIREMENTS.\n"
        "    A site that works but looks generic is FAILED. Categories to cover:\n\n"

        "    a) CUSTOM SVG ICONS & ILLUSTRATIONS: Hand-crafted inline SVG icons that\n"
        "       match the project visual identity. No generic icon libraries.\n"
        "       Create unique, meaningful iconography. Animated SVGs where appropriate.\n\n"

        "    b) MICRO-INTERACTIONS & ANIMATIONS: Hover effects with transforms and\n"
        "       opacity transitions. Scroll-triggered reveals (IntersectionObserver or\n"
        "       CSS scroll-driven animations with view-timeline/scroll-timeline).\n"
        "       Button press feedback (scale + shadow). Page transition animations.\n"
        "       Loading skeleton screens. Staggered list entry animations.\n"
        "       Cursor-following effects, parallax, or magnetic buttons where fitting.\n"
        "       CSS @starting-style for clean enter transitions. View Transitions API\n"
        "       for cross-page or cross-state morphing.\n\n"

        "    c) LAYOUT & SPATIAL DESIGN: Intentional whitespace rhythm using 4px/8px grid.\n"
        "       CSS Grid and Flexbox mastery. CSS Grid lanes for masonry-style layouts where fitting.\n"
        "       Asymmetric layouts that break the grid tastefully.\n"
        "       Full-bleed sections alternating with contained content. Responsive\n"
        "       breakpoints that look stunning at EVERY viewport width (use clamp() and\n"
        "       fluid sizing, not just 3 fixed breakpoints). CSS anchor positioning for\n"
        "       tooltips and popovers. Container queries for component-level responsiveness.\n\n"

        "    d) TYPOGRAPHY & COLOR SYSTEM: Premium Google Fonts (Inter, Outfit, Space\n"
        "       Grotesk, Clash Display, Plus Jakarta Sans). Type scale with clear hierarchy.\n"
        "       Fluid typography (clamp()). Color palette with semantic tokens, gradients,\n"
        "       and accent colors built on OKLCH or LCH color space.\n"
        "       Dark/light mode using CSS light-dark() and color-mix().\n"
        "       Relative colors for tint/shade generation. text-wrap: balance for headings.\n\n"

        "    e) COMPONENT POLISH & STATES: Glass-morphism cards, layered box-shadows.\n"
        "       Focus rings (visible, accessible). Active, disabled, hover, loading states.\n"
        "       Empty states with illustrations. Error states with personality.\n"
        "       Loading states with branded skeleton screens.\n"
        "       Beautiful form inputs, toggles, sliders, and select menus.\n"
        "       CSS @scope for modular component styling without conflicts.\n\n"

        "    f) PERFORMANCE-AWARE VISUALS: will-change for animated properties.\n"
        "       Composited-only animations (transform, opacity) — avoid animating\n"
        "       width, height, top, left, box-shadow, filter on main thread.\n"
        "       content-visibility: auto for off-screen sections.\n"
        "       Lazy-loaded images with loading='lazy' and explicit width/height.\n"
        "       AVIF/WebP with <picture> fallbacks. Preload critical assets.\n\n"

        "    The aesthetic must be 2026 Awwwards Site of the Year quality.\n"
        "    Domain-appropriate (finance=clean/trustworthy, creative=bold/expressive,\n"
        "    dev-tools=precise/dark, wellness=warm/organic, education=clear/engaging).\n"
        "    The user must be WOWED at first glance. Premium feel in every pixel.\n\n"

        "13. LIGHTHOUSE 100/100 SCORE — ALL 4 CATEGORIES:\n"
        "    Every web project MUST include tasks targeting a perfect Lighthouse score:\n\n"

        "    PERFORMANCE (target: 100):\n"
        "    - FCP < 1.8s: Eliminate render-blocking resources, inline critical CSS,\n"
        "      defer non-critical JS, font-display: swap, preconnect to origins.\n"
        "    - LCP < 2.5s: Optimize LCP element (largest image/text), preload LCP image,\n"
        "      set explicit width/height on images, avoid lazy-loading above-fold images.\n"
        "    - TBT = 0ms: Code-split JS bundles, defer heavy computation, use web workers\n"
        "      for CPU-intensive tasks, avoid long main-thread tasks (>50ms).\n"
        "    - CLS = 0: Set width/height on all images/videos, use aspect-ratio CSS,\n"
        "      avoid injecting content above existing content, font metric overrides.\n"
        "    - SI < 3.4s: Optimize paint sequence, reduce network payload, efficient caching.\n"
        "    - Enable HTTP/2 or HTTP/3. Set cache-control headers (max-age >= 1y for hashed assets).\n"
        "    - Tree-shake and code-split JS. Aim for <200KB total JS transferred.\n"
        "    - Compress all text (gzip/brotli). Minimize DOM size (<1500 elements).\n\n"

        "    ACCESSIBILITY (target: 100, WCAG 2.1 AA minimum):\n"
        "    - All buttons/links have accessible names (aria-label where needed).\n"
        "    - Heading hierarchy is sequential (h1 > h2 > h3, no skipping).\n"
        "    - Color contrast >= 4.5:1 for text, >= 3:1 for large text and UI components.\n"
        "    - All images have descriptive alt text (or alt='' for decorative).\n"
        "    - All videos have <track kind='captions'>.\n"
        "    - Keyboard navigable (tabindex, focus management, skip links).\n"
        "    - <html lang> set. Viewport meta present and allows zoom.\n"
        "    - Touch targets >= 44x44px with >= 8px spacing.\n"
        "    - Semantic HTML5 landmarks (<main>, <nav>, <header>, <footer>).\n"
        "    - ARIA attributes are valid and correctly applied.\n\n"

        "    BEST PRACTICES (target: 100):\n"
        "    - No console errors. Properly handle API errors.\n"
        "    - Serve source maps for production JS bundles.\n"
        "    - Use HTTPS everywhere. Set CSP headers (script-src, style-src).\n"
        "    - Set HSTS header (Strict-Transport-Security: max-age=31536000).\n"
        "    - Set X-Frame-Options or CSP frame-ancestors.\n"
        "    - Set Cross-Origin-Opener-Policy (COOP) header.\n"
        "    - Use Trusted Types for DOM XSS prevention.\n"
        "    - No deprecated APIs. Images served at correct aspect ratios.\n"
        "    - Charset declared in first 1024 bytes.\n\n"

        "    SEO (target: 100):\n"
        "    - <title> tag present, unique, under 60 chars.\n"
        "    - <meta name='description'> present, compelling, 120-155 chars.\n"
        "    - Valid robots.txt (NOT serving HTML — must be plain text with User-agent/Allow/Disallow).\n"
        "    - Canonical URL set. hreflang valid if multilingual.\n"
        "    - All links are crawlable (real href, not JS-only navigation).\n"
        "    - Structured data (JSON-LD) for relevant content types.\n"
        "    - HTTP 200 status code. No blocked resources.\n\n"
        "== ENVIRONMENT & TOOLING ==\n"
        "14. TOOLING VERSIONS \u2014 ALWAYS USE THE LATEST STABLE RELEASES (as of 2026):\n"
        "    When scaffolding projects, installing dependencies, or configuring build tools:\n"
        "    - Node.js: 22 LTS minimum (sandbox runs Node 22).\n"
        "    - npm: latest (10.x). Prefer npm unless the project already uses yarn/pnpm.\n"
        "    - Vite: latest (6.x). Use `npm create vite@latest` NOT `vite@4` or `vite@5`.\n"
        "    - TypeScript: latest stable (5.x). Always enable strict mode in tsconfig.\n"
        "    - React: 19.x (NOT 18.x). Use React 19 APIs where appropriate.\n"
        "    - Next.js: 15.x with App Router (NOT Pages Router for new projects).\n"
        "    - Tailwind CSS: v4.x if used (uses @import not @tailwind directives).\n"
        "    - ESLint: v9.x flat config (eslint.config.js) NOT legacy .eslintrc.*.\n"
        "    - NEVER pin to outdated major versions -- use ^ (caret) ranges in package.json.\n"
        "    - Set engines:{node:>=22} in package.json for new projects.\n\n"
        "    - FULL-STACK PROJECTS: always scaffold .env files for ALL services.\n"
        "      Frontend .env: VITE_API_URL / NEXT_PUBLIC_API_URL pointing to backend port.\n"
        "      Backend .env: PORT, NODE_ENV/DEBUG, CORS_ORIGIN pointing to frontend port.\n"
        "      Multiple backends get sequential ports (4000, 4001, ...).\n"
        "      Include DATABASE_URL/REDIS_URL/MONGODB_URI in backend .env when used.\n"
        "      NEVER hardcode localhost URLs/ports in source code -- always read from env vars.\n\n"

        "15. PHASED DEVELOPMENT — CROSS-PHASE MAXIMUM OUTPUT:\n"
        "    When the goal includes project phase information (## ⚙️ PROJECT PHASES):\n"
        "    a) Read ALL phases — not just the active one. Every phase with PENDING tasks needs DAG nodes.\n"
        "    b) Generate DAG tasks for ALL pending tasks across ALL phases listed.\n"
        "       Do NOT restrict to a single phase — cover Phases 1 through N.\n"
        "    c) Your task descriptions should map to the listed phase tasks.\n"
        "       Cover ALL listed pending tasks — do not skip any phase or any task.\n"
        "    d) If a later phase's task has no dependency on unfinished work, it can start now.\n"
        "       Make these tasks independent (empty dependencies) so they run in parallel.\n"
        "    e) Tasks from different phases with no interdependency should run concurrently.\n"
        "    f) Generate up to 100 DAG nodes total. Aim for the MAXIMUM the project needs.\n\n"
        "16. EXECUTION ENVIRONMENT — YOU ARE INSIDE A LINUX DOCKER CONTAINER:\n"
        "    You do NOT run on the host machine. Your tools execute inside a sandboxed\n"
        "    Linux container with full shell access. This means:\n\n"
        "    ✅ YOU CAN AND MUST:\n"
        "    - Run `npm install`, `npm run dev`, `npm run build`, `npx`, `yarn`, `pnpm`\n"
        "    - Run `node`, `python3`, `pip install`, `pip3`, `poetry install`\n"
        "    - Run `bash`, `sh`, `git`, `curl`, `wget`, `apt-get install`\n"
        "    - Run `psql`, `redis-cli`, `mongosh`, `docker` (if available)\n"
        "    - Start dev servers, run test suites, execute migrations\n"
        "    - Install system packages via apt-get\n\n"
        "    ❌ NEVER WRITE:\n"
        "    - 'Cannot run npm/bash/node autonomously'\n"
        "    - 'User action required to run npm install'\n"
        "    - 'Shell access is not available'\n"
        "    - 'Tests could not be run autonomously'\n"
        "    These statements are ALWAYS WRONG. You have full container shell access.\n\n"
        "    IMPORTANT: The host machine may run Windows/PowerShell — this is irrelevant.\n"
        "    You operate inside the Linux sandbox. PowerShell.exe does not exist IN your\n"
        "    environment, but bash and all Linux commands do.\n\n"
        "    🚫 DO NOT CREATE TASKS FOR ANY OF THE FOLLOWING — the Supervisor handles these AUTOMATICALLY:\n"
        "    The Supervisor has its own native infrastructure management layer that runs in parallel\n"
        "    with your DAG. Creating tasks for any of the following causes CONFLICTS, DUPLICATE\n"
        "    PROCESSES, and WASTES precious DAG slots on work that is already handled:\n\n"
        "    - npm install / yarn install / pnpm install / pip install / poetry install\n"
        "    - Starting, restarting, or checking the dev server (npm run dev, vite, next dev, etc.)\n"
        "    - npm run build / tsc --build / vite build (build verification is automatic)\n"
        "    - Port checks, curl health checks, or waiting for a server to respond\n"
        "    - Creating or initializing a git repository (git init, git commit)\n"
        "    - Creating package.json scaffold / project init (unless the project has NO package.json yet)\n\n"
        "    YOUR TASKS MUST BE EXCLUSIVELY about writing/modifying CODE, CONFIG, and CONTENT.\n"
        "    Focus on: implementing features, fixing bugs, adding components, writing styles,\n"
        "    configuring tools (vite.config.ts, tsconfig.json, etc.), writing tests.\n\n"

        '== OUTPUT FORMAT ==\n'
        'Output strict JSON: {"tasks": [{"task_id": "t1-FUNC", "description": "[FUNC] ...", '
        '"dependencies": [], "knowledge_gaps": ["Moyasar payment API integration"]}, ...]}\n'
        'Task ID FORMAT: tX-TAG where TAG is FUNC, UIUX, or PERF based on category. '
        'Examples: t1-FUNC, t2-UIUX, t3-PERF, t4-FUNC, t5-UIUX\n\n'
        '== NEGATIVE EXAMPLE (DO NOT DO THIS) ==\n'
        'BAD: {"task_id": "t1", "description": "Set up project and install deps", "dependencies": []}\n'
        'WHY BAD: Too vague, covers infrastructure the Supervisor handles, no specific files.\n'
        'GOOD: {"task_id": "t1-FUNC", "description": "[FUNC] Create src/lib/db.ts with Prisma '
        'client singleton and typed query helpers. Create prisma/schema.prisma with User and '
        'Project models.", "dependencies": [], "knowledge_gaps": ["Prisma x.x client API"]}'
    )

    REPLAN_PROMPT = (
        "You are a strict technical project planner. A multi-step plan has encountered "
        "a failure. You must rewrite the REMAINING tasks to route around the blocker.\n\n"
        "RULES:\n"
        "1. Do NOT change any completed tasks.\n"
        "2. The failed task's lesson tells you what to avoid.\n"
        "3. Rewrite only the pending tasks to find an alternative approach.\n"
        "4. Maintain valid DAG structure (no cycles).\n"
        "5. Keep task_ids consistent where possible.\n\n"
        'Output strict JSON: {"tasks": [{"task_id": "...", "description": "...", '
        '"dependencies": []}, ...]}'
    )

    def __init__(self, local_manager, workspace_path: str):
        self._manager = local_manager
        self._workspace = workspace_path
        self._nodes: Dict[str, TaskNode] = {}
        self._epic_text: str = ""
        self._replan_count: int = 0
        # V60: Dynamic replan budget — scales with DAG size so large workloads
        # get proportional retries. Computed when nodes are first loaded.
        # Formula: max(3, total_nodes // 10) — a 40-task DAG gets 4; an 80-task gets 8.
        self._max_replan_count: int = MAX_REPLAN_COUNT
        # V60: Critical-path descendant counts — cached after first compute.
        self._descendant_counts: dict[str, int] | None = None
        self._workspace_hash: str = ""
        # V44: Continuous task ID counter across DAG phases
        self._task_offset: int = 0
        # V44: Persist all user prompts for DAG context
        self._user_prompts: list[str] = []
        # V44: Map original IDs → remapped tX IDs for dependency resolution
        self._id_aliases: Dict[str, str] = {}
        # V54: When True, _save_state() is a no-op — used for the boot
        # display planner so it never contaminates epic_state.json.
        self.ephemeral: bool = False

        # V58: File-claim registry — maps file basename → task_id that last wrote it.
        # Used by register_file_writes() + inject_file_conflict_deps() to serialize
        # concurrent tasks that would otherwise overwrite each other's work.
        self._file_last_writer: Dict[str, str] = {}

        # V40 FIX: Per-project state path instead of global.
        # Previous bug: all projects shared one epic_state.json, so switching
        # projects would overwrite or load wrong state.
        self._state_dir = Path(workspace_path) / ".ag-supervisor"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_dir / "epic_state.json"

        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # ────────────────────────────────────────────────
    # V58: Layer 1 — File-Conflict Prevention
    # ────────────────────────────────────────────────

    def register_file_writes(self, task_id: str, files_changed: list) -> None:
        """
        Record that task_id wrote to the given files.
        Called from _pool_worker immediately after a task completes.
        Stores basename → task_id so inject_file_conflict_deps can scan pending nodes.
        """
        if not files_changed:
            return
        import os as _os
        for f in files_changed:
            basename = _os.path.basename(str(f))
            if basename:
                self._file_last_writer[basename] = task_id
        logger.debug(
            "[FileConflict] Registered %d file write(s) for %s: %s",
            len(files_changed), task_id,
            [_os.path.basename(str(f)) for f in files_changed[:5]],
        )

    def inject_file_conflict_deps(self, completed_task_id: str, files_changed: list) -> int:
        """
        After task_id completes, scan all pending nodes whose descriptions mention
        any of the written files. For each such node, add completed_task_id as a
        dependency — serializing them so they cannot overwrite the completed task's work.

        Returns the number of dependency injections made.
        """
        if not files_changed:
            return 0

        import os as _os
        basenames = {_os.path.basename(str(f)).lower() for f in files_changed if f}
        if not basenames:
            return 0

        injected = 0
        for node in self._nodes.values():
            if node.status not in ("pending",):
                continue  # Only affect tasks that haven't started yet
            if node.task_id == completed_task_id:
                continue
            if completed_task_id in node.dependencies:
                continue  # Already dependent

            desc_lower = node.description.lower()
            if any(bn in desc_lower for bn in basenames):
                node.dependencies.append(completed_task_id)
                injected += 1
                logger.info(
                    "[FileConflict] Injected dep %s → %s (shared file: %s)",
                    completed_task_id, node.task_id,
                    next((bn for bn in basenames if bn in desc_lower), "?"),
                )

        if injected:
            self._save_state()

        return injected

    # ────────────────────────────────────────────────
    # Epic Ingestion
    # ────────────────────────────────────────────────

    def load_epic(self, epic_path: str | None = None) -> Tuple[bool, str]:
        """Load an EPIC.md file from the workspace root."""
        path = Path(epic_path) if epic_path else Path(self._workspace) / "EPIC.md"
        if not path.exists():
            return False, f"Epic file not found: {path}"

        self._epic_text = path.read_text(encoding="utf-8").strip()
        if not self._epic_text:
            return False, "Epic file is empty."

        logger.info("📋 Loaded epic from %s (%d chars)", path.name, len(self._epic_text))
        return True, self._epic_text

    # ────────────────────────────────────────────────
    # DAG Decomposition
    # ────────────────────────────────────────────────

    async def decompose_epic(self, epic_text: str | None = None) -> Tuple[bool, str]:
        """
        Route the epic to the best available LLM for DAG decomposition.

        V41: For complex goals (>2000 chars), skip Ollama and go straight
        to Gemini CLI — Ollama's 8K context can't produce good decompositions
        for large epics. Also increased epic text limit from 3000 to 8000 chars.
        """
        text = epic_text or self._epic_text
        if not text:
            return False, "No epic text to decompose."

        # Store the epic text for replanning later
        self._epic_text = text

        # V53: Gemini ALWAYS goes first for decomposition.
        # Ollama's 8K context is insufficient for full epics and consistently
        # returns {} — wasting 2+ minutes before falling through to Gemini anyway.
        # Ollama is kept as a last-resort fallback only if Gemini is unavailable.
        raw = None

        # Tier 1: Gemini CLI (primary decomposer — large context, high quality)
        try:
            from .gemini_advisor import ask_gemini
            # Build rich project context so Gemini knows what already exists
            _project_context = ""
            try:
                from pathlib import Path as _DPath
                _ws = _DPath(self._workspace)

                # ── Context notes — read DIRECTLY from authoritative source ──────
                # CRITICAL: SUPERVISOR_MANDATE.md is often 20,000+ chars with notes
                # starting at char 7,000+. [:4000] truncation NEVER reached them.
                # Read context_notes.json directly — compact, always complete.
                _cn_path = _ws / ".ag-supervisor" / "context_notes.json"
                if _cn_path.exists():
                    try:
                        _notes_raw = json.loads(_cn_path.read_text(encoding="utf-8"))
                        if _notes_raw:
                            _cn_lines = [
                                "\n\nPERSISTENT CONTEXT NOTES — USER-DEFINED HARD CONSTRAINTS "
                                "(these are MANDATORY requirements that MUST be reflected in every generated task):\n\n"
                            ]
                            for _ci, _cn in enumerate(_notes_raw, 1):
                                _cn_lines.append(f"{_ci}. {_cn['text']}\n\n")
                            _project_context += "".join(_cn_lines)
                    except Exception:
                        pass

                # ── V68: research.md — use @-file reference instead of inline ────
                # Previously inlined up to 200K chars, causing 268K+ prompts that
                # timed out. The Gemini CLI reads @-referenced files at its cwd.
                _research_path = _ws / "research.md"
                if not _research_path.exists():
                    _research_path = _ws / "RESEARCH.md"
                if _research_path.exists():
                    _project_context = (
                        "\n\nRESEARCH FINDINGS — AUTHORITATIVE DOMAIN RESEARCH\n"
                        "These findings represent deep research into the problem space. "
                        "Every generated task MUST serve the ideal end-product described here. "
                        "Build exactly what the research says the perfect product should be.\n"
                        f"Read the full research document: @{_research_path.name}\n\n"
                    ) + _project_context
                    logger.info(
                        "📋  [DecomposeEpic] Using @%s file reference (avoids inline bloat).",
                        _research_path.name,
                    )

                # ── SUPERVISOR_MANDATE.md — extract just the mission section ─────
                # Notes are already injected above; avoid duplicating by only
                # taking the mission text (not the full mandate with notes appended).
                _mandate = _ws / "SUPERVISOR_MANDATE.md"
                if _mandate.exists():
                    _mtext = _mandate.read_text(encoding="utf-8")
                    _ms = _mtext.find("## YOUR MISSION")
                    _nc = _mtext.find("\n\n## Persistent Context Notes")
                    if _ms != -1:
                        _me = _nc if _nc != -1 else _ms + 3000
                        _project_context += (
                            "\n\nPROJECT MISSION (from SUPERVISOR_MANDATE.md):\n"
                            + _mtext[_ms:_me].strip() + "\n"
                        )
                    else:
                        _project_context += (
                            "\n\nSUPERVISOR MANDATE:\n" + _mtext[:2500] + "\n"
                        )

                # ── V68: PROJECT_STATE.md — use @-file reference instead of inline ─
                # Previously inlined up to 50K chars.
                _ps = _ws / "PROJECT_STATE.md"
                if _ps.exists():
                    _project_context += (
                        "\n\nCURRENT PROJECT STATE (read this carefully — do not recreate completed work):\n"
                        "Read the full project state: @PROJECT_STATE.md\n"
                    )

                # File tree (small, keep inline)
                _all_files = sorted(
                    str(f.relative_to(_ws))
                    for f in _ws.rglob("*")
                    if f.is_file()
                    and "node_modules" not in str(f)
                    and ".git" not in str(f)
                    and "__pycache__" not in str(f)
                )
                if _all_files:
                    _project_context += (
                        "\nEXISTING FILES IN PROJECT:\n"
                        + "\n".join(f"  - {f}" for f in _all_files[:80]) + "\n"
                    )

                # DAG history (small, keep inline)
                _hist = _ws / "dag_history.jsonl"
                if not _hist.exists():
                    _hist = _ws / ".ag-supervisor" / "dag_history.jsonl"
                if _hist.exists():
                    _lines = _hist.read_text(encoding="utf-8").strip().split("\n")
                    _prev_tasks = []
                    for _line in _lines[-3:]:
                        try:
                            _entry = json.loads(_line)
                            for _v in _entry.get("nodes", {}).values():
                                _prev_tasks.append(
                                    f"  [{_v['status'].upper()}] {_v['task_id']}: "
                                    f"{_v['description'][:200]}"
                                )
                        except Exception:
                            pass
                    if _prev_tasks:
                        _project_context += (
                            "\nPREVIOUSLY COMPLETED/ATTEMPTED TASKS:\n"
                            + "\n".join(_prev_tasks) + "\n"
                        )
            except Exception:
                pass  # Non-critical — decompose without context

            _context_instruction = ""
            if _project_context:
                _context_instruction = (
                    "\n\nIMPORTANT: This project already has existing code and prior work. "
                    "You MUST respect ALL constraints listed in the SUPERVISOR MANDATE above "
                    "(including any Persistent Context Notes — these are user-defined hard rules). "
                    "Review the project state and completed tasks. "
                    "Create tasks ONLY for what is MISSING, BROKEN, or needs IMPROVEMENT. "
                    "Do NOT recreate tasks for work that is already done. "
                    "Focus on the HIGHEST-IMPACT tasks that add the most value.\n"
                )

            gemini_prompt = (
                f"{self.DECOMPOSITION_PROMPT}\n\n"
                f"EPIC:\n{text[:100000]}\n"
                f"{_project_context}"
                f"{_context_instruction}\n"
                "Output strict JSON only. No markdown, no explanation.\n"
                "There is NO hard cap on task count. For full applications, aim for 50-95 tasks "
                "across Functionality, UI/UX, and Lighthouse categories. "
                "For simple features, use only as many as genuinely needed."
            )
            _gemini_attempts = 3
            for _attempt in range(1, _gemini_attempts + 1):
                try:
                    logger.info(
                        "📋  [Planner→Gemini] Decomposition attempt %d/%d (%d chars): %.500s…",
                        _attempt, _gemini_attempts, len(gemini_prompt), gemini_prompt,
                    )
                    raw = await ask_gemini(gemini_prompt, timeout=180, cwd=self._workspace, model=config.GEMINI_FALLBACK_MODEL)
                    if raw and raw != "{}":
                        logger.info(
                            "📋  [Planner←Gemini] Decomposition response (%d chars): %.300s…",
                            len(raw), raw,
                        )
                        break  # Success — stop retrying
                    else:
                        logger.warning(
                            "📋  [Planner] Gemini attempt %d/%d returned empty — retrying …",
                            _attempt, _gemini_attempts,
                        )
                except Exception as _exc:
                    logger.warning(
                        "📋  [Planner] Gemini attempt %d/%d failed: %s%s",
                        _attempt, _gemini_attempts, _exc,
                        " — retrying …" if _attempt < _gemini_attempts else " — giving up.",
                    )
                    raw = None
                if _attempt < _gemini_attempts:
                    import asyncio as _aio
                    await _aio.sleep(5 * _attempt)  # 5s, 10s between retries
        except Exception as exc:
            logger.warning("📋  Gemini decomposition setup failed: %s. Trying LiteBrain fallback.", exc)
            raw = None


        # Tier 2: Retry with Pro model (V74: never use Lite for planning)
        if (not raw or raw == "{}"):
            _retry_prompt = (
                f"{self.DECOMPOSITION_PROMPT}\n\n"
                f"EPIC:\n{text[:100000]}\n"
                "Output strict JSON only. No markdown, no explanation."
            )
            logger.info(
                "📋  [Planner→Gemini] Retry decomposition with Pro model (%d chars).\n"
                "    Prompt: %.200s…",
                len(_retry_prompt), _retry_prompt,
            )
            try:
                raw = await ask_gemini(
                    _retry_prompt,
                    timeout=180,
                    cwd=self._workspace,
                    model=config.GEMINI_FALLBACK_MODEL,
                )
                if raw:
                    logger.info(
                        "📋  [Planner←Gemini] Retry decomposition response (%d chars): %.300s…",
                        len(raw), raw,
                    )
            except Exception as exc:
                logger.error("📋  Pro retry decomposition also failed: %s", exc)
                raw = None

        if not raw or raw == "{}":
            return False, "Decomposition returned empty from both Gemini attempts."

        return self._parse_dag(raw)



    @classmethod
    def from_brain(cls, brain, workspace_path: str) -> "TemporalPlanner":
        """
        V38: Factory that creates a TemporalPlanner using an OllamaLocalBrain
        instance (from headless_executor.py) instead of a LocalManager.

        Adapts the OllamaLocalBrain.ask() interface to the ask_local_model()
        interface the planner expects. If brain is None, the planner will
        fall back to Gemini for decomposition.
        """
        class _BrainAdapter:
            """Minimal adapter: OllamaLocalBrain.ask() → ask_local_model()."""
            def __init__(self, brain):
                self._brain = brain

            async def ask_local_model(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
                result = await self._brain.ask(user_prompt, system=system_prompt, temperature=temperature)
                return result or "{}"

        adapter = _BrainAdapter(brain) if brain else None
        planner = cls(adapter, workspace_path)
        # V51: Auto-load saved DAG state from disk so tasks survive restarts.
        # Previously, from_brain() created an empty planner — all pending/failed
        # tasks from the previous session were silently lost.
        planner.load_state()
        return planner

    def _parse_dag(self, raw_json: str) -> Tuple[bool, str]:
        """Parse and validate the JSON DAG from the LLM."""
        try:
            # Strip markdown fences (Gemini CLI often wraps JSON in ```json blocks)
            import re
            cleaned = re.sub(r"```json?\s*", "", raw_json)
            cleaned = re.sub(r"```\s*", "", cleaned).strip()
            data = json.loads(cleaned)
            tasks = data.get("tasks", [])
        except (json.JSONDecodeError, AttributeError) as e:
            # V40 FIX: Try to recover partial tasks from truncated JSON.
            # Gemini often truncates large responses mid-string.
            logger.warning("📋  [Planner] JSON parse failed: %s. Attempting truncated recovery.", e)
            try:
                import re
                # Find all task-like objects in the raw text
                task_pattern = r'\{\s*"task_id"\s*:\s*"[^"]+"\s*,\s*"description"\s*:\s*"[^"]+"[^}]*\}'
                task_matches = re.findall(task_pattern, cleaned, re.DOTALL)
                if task_matches:
                    recovered_json = '{"tasks": [' + ','.join(task_matches) + ']}'
                    data = json.loads(recovered_json)
                    tasks = data.get("tasks", [])
                    logger.info(
                        "📋  [Planner] Recovered %d tasks from truncated JSON.",
                        len(tasks),
                    )
                else:
                    return False, f"Invalid JSON from decomposition: {e}"
            except Exception as recovery_err:
                logger.warning("📋  [Planner] Truncated recovery also failed: %s", recovery_err)
                return False, f"Invalid JSON from decomposition: {e}"

        if not tasks:
            return False, "Decomposition returned zero tasks."

        # (No hard cap — the prompt asks for 50-95 tasks for full apps)
        # V44: Remap LLM-generated task IDs to continue from the last offset.
        # LLM always generates t1, t2, t3... — we remap to t(offset+1), t(offset+2)...
        import re as _re_remap
        id_remap = {}
        for t in tasks:
            old_id = t.get("task_id", "")
            # Extract numeric suffix from IDs like t1, t2, t15
            m = _re_remap.match(r'^t(\d+)$', old_id)
            if m:
                new_num = self._task_offset + int(m.group(1))
                new_id = f"t{new_num}"
                id_remap[old_id] = new_id
                t["task_id"] = new_id
            # else: non-standard ID, keep as-is

        # Remap dependencies too
        for t in tasks:
            t["dependencies"] = [id_remap.get(d, d) for d in t.get("dependencies", [])]

        # V44: Update offset for next DAG phase
        max_num = 0
        for t in tasks:
            m = _re_remap.match(r'^t(\d+)$', t.get("task_id", ""))
            if m:
                max_num = max(max_num, int(m.group(1)))
        if max_num > 0:
            self._task_offset = max_num

        # Validate DAG structure
        ids = {t["task_id"] for t in tasks}

        # V44: Preserve completed/failed nodes from previous DAG phases
        # (don't replace self._nodes — merge new into existing)
        archived_nodes = {
            tid: n for tid, n in self._nodes.items()
            if n.status in ("complete", "failed", "skipped")
        }
        self._nodes = dict(archived_nodes)  # Keep history

        for t in tasks:
            tid = t.get("task_id", "")
            desc = t.get("description", "")
            deps = t.get("dependencies", [])

            if not tid or not desc:
                return False, f"Task missing task_id or description: {t}"

            # V53 FIX: Prune dangling deps instead of hard-failing the entire DAG.
            # Gemini sometimes references IDs that were renumbered or simply omitted.
            # A hard return False rejects the whole decomposition and crashes the supervisor.
            # Warn and remove the unknown dep — the task still runs, just without that dep.
            all_known_ids = ids | set(archived_nodes.keys())
            clean_deps = []
            for dep in deps:
                if dep in all_known_ids:
                    clean_deps.append(dep)
                else:
                    logger.warning(
                        "📋  [Planner] Task %s references unknown dep '%s' — dropping dangling reference",
                        tid, dep,
                    )
            deps = clean_deps

            gaps = t.get("knowledge_gaps", [])
            node = TaskNode(tid, desc, deps, knowledge_gaps=gaps)
            node.acceptance_criteria = t.get("acceptance_criteria", "")  # V58: Store for audit
            self._nodes[tid] = node


        # Verify no cycles (topological sort)
        if not self._is_dag():
            return False, "Decomposition contains a cycle — invalid DAG."

        # V60: Set dynamic replan budget now that we know the DAG size.
        # Larger DAGs need more runway: a 40-task DAG gets 4 replans, 80 gets 8.
        self._max_replan_count = max(5, len(tasks) // 5)
        # Invalidate critical-path cache — DAG topology just changed.
        self._descendant_counts = None

        self._save_state()
        logger.info(
            "📋 Decomposed epic into %d atomic tasks (offset=%d, total nodes=%d, max_replan=%d).",
            len(tasks), self._task_offset, len(self._nodes), self._max_replan_count,
        )
        return True, f"DAG created with {len(tasks)} tasks."

    def _is_dag(self) -> bool:
        """Verify the graph is acyclic using Kahn's algorithm.
        
        V53 FIX: Use setdefault() for the adjacency list so a dependency that
        is referenced by a node but is not itself a known node ID never causes
        a KeyError. Such dangling references are already pruned by _parse_dag()
        but this makes the cycle check doubly resilient.
        """
        known = set(self._nodes)
        in_degree = {tid: 0 for tid in known}
        adj: Dict[str, List[str]] = {tid: [] for tid in known}

        for node in self._nodes.values():
            for dep in node.dependencies:
                if dep not in known:
                    continue  # Dangling ref — already warned in _parse_dag
                adj.setdefault(dep, []).append(node.task_id)
                in_degree[node.task_id] += 1

        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        visited = 0

        while queue:
            tid = queue.pop(0)
            visited += 1
            for child in adj.get(tid, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        return visited == len(self._nodes)

    # V60: Critical-path analysis — cached per DAG load.
    def _compute_critical_path(self) -> dict[str, int]:
        """
        Compute transitive descendant count for each node.

        A higher count means more downstream tasks depend on this node,
        making it a bottleneck.  Scheduling bottlenecks first minimises
        total DAG completion time when running parallel workers.

        Result is cached in self._descendant_counts (invalidated when nodes change).
        """
        if self._descendant_counts is not None:
            return self._descendant_counts

        # Build adjacency: parent → list[child]
        children: dict[str, list[str]] = {tid: [] for tid in self._nodes}
        for node in self._nodes.values():
            for dep in node.dependencies:
                if dep in children:
                    children[dep].append(node.task_id)

        # Memoised DFS: count of all unique descendants
        memo: dict[str, int] = {}

        def _count(tid: str, visited: set) -> int:
            if tid in memo:
                return memo[tid]
            total = 0
            for child in children.get(tid, []):
                if child not in visited:
                    visited.add(child)
                    total += 1 + _count(child, visited)
            memo[tid] = total
            return total

        for tid in self._nodes:
            _count(tid, {tid})

        self._descendant_counts = memo
        return memo

    # ────────────────────────────────────────────────
    # Execution Queries
    # ────────────────────────────────────────────────

    def get_next_unblocked(self) -> TaskNode | None:
        """Return the first pending task whose dependencies are all complete."""
        for node in self._nodes.values():
            if node.status != "pending":
                continue
            deps_met = all(
                self._nodes[dep].status == "complete"
                for dep in node.dependencies
                if dep in self._nodes
            )
            if deps_met:
                return node
        return None

    def get_parallel_batch(self, max_workers: int = 3) -> List[TaskNode]:
        """
        V40: Return ALL unblocked nodes (up to max_workers) for parallel execution.
        A dependency is 'satisfied' if it is:
          - complete
          - failed AND has exhausted all retries (skip-on-fail)
          - not in the DAG at all (removed by replan)

        V41: Sort by priority descending so user-injected tasks (priority=1)
        execute before regular tasks (priority=0).
        """
        unblocked = []
        for node in self._nodes.values():
            if node.status != "pending":
                continue
            deps_met = True
            for dep in node.dependencies:
                if dep not in self._nodes:
                    continue  # Removed by replan
                dep_node = self._nodes[dep]
                if dep_node.status == "complete":
                    continue
                if (dep_node.status == "failed"
                        and dep_node.retry_count >= dep_node.max_retries):
                    continue  # Exhausted — let dependent try anyway
                deps_met = False
                break
            if deps_met:
                unblocked.append(node)

        # V41: High-priority (user-injected) tasks first
        # V60: Secondary sort by critical-path descendant count so bottleneck nodes
        # (those with the most downstream dependents) are scheduled first.
        _dc = self._compute_critical_path()
        unblocked.sort(
            key=lambda n: (n.priority, _dc.get(n.task_id, 0)),
            reverse=True,
        )

        # V51/V53: Build-health tasks run SOLO relative to EACH OTHER.
        # They modify deps/configs so concurrent build tasks cause conflicts.
        # V68: Normal tasks are NO LONGER blocked by a running BUILD task.
        # This saves the entire BUILD duration (~10+ min) of idle time.
        _bh_running = any(
            (n.task_id.startswith("build-health") or re.match(r'^t\d+-BUILD$', n.task_id))
            and n.status == "running"
            for n in self._nodes.values()
        )

        # Separate build-health tasks from normal tasks
        normal = [n for n in unblocked
                  if not n.task_id.startswith("build-health")
                  and not re.match(r'^t\d+-BUILD$', n.task_id)]
        bh_pending = [n for n in unblocked
                      if n.task_id.startswith("build-health")
                      or re.match(r'^t\d+-BUILD$', n.task_id)]

        # Guard A: If a build-health task is running, don't start another one.
        # But DO allow normal tasks to proceed.
        if _bh_running:
            # Only normal tasks can run alongside a BUILD task
            return normal[:max_workers] if normal else []

        # Guard B: If the highest-priority unblocked task is build-health,
        # run it solo (no other build tasks, but normal tasks CAN join).
        if bh_pending and (not normal or bh_pending[0].priority >= normal[0].priority):
            # Start the build task, and fill remaining slots with normal tasks
            result = [bh_pending[0]]
            result.extend(normal[:max_workers - 1])
            return result

        # Normal tasks are ready — return up to max_workers of them.
        return normal[:max_workers]


    def get_completed_summary(self) -> str:
        """Build a brief summary of completed prerequisite steps for context scoping."""
        completed = [
            n for n in self._nodes.values() if n.status == "complete"
        ]
        if not completed:
            return ""

        lines = ["[COMPLETED PREREQUISITE STEPS]"]
        for c in completed:
            lines.append(f"  ✓ {c.task_id}: {c.description}")
        return "\n".join(lines)

    # V46: Hash of last written PROGRESS.md to skip redundant writes
    _progress_hash: str = ""

    def write_progress_file(self, project_path: str) -> str:
        """V46: Write a PROGRESS.md checklist to disk for Gemini CLI to reference.

        Instead of inlining all completed task summaries into each prompt
        (which wastes prompt tokens), we write a file that the CLI can
        read on-demand via @PROGRESS.md.

        V46: Skips disk write if content is unchanged (hash dedup).
        V54: Prepends a CAPABILITIES header so Gemini doesn't forget it has
             full container shell access between tasks.
        Returns the path to the written file.
        """
        import os, hashlib
        progress_path = os.path.join(project_path, "PROGRESS.md")
        lines = [
            "# Task Progress",
            "",
            "## Execution Environment",
            "- You are running INSIDE a Linux Docker container with full shell access.",
            "- `bash`, `npm`, `node`, `npx`, `pip`, `python3`, `git`, `curl` are all available.",
            "- NEVER write 'cannot run npm/bash autonomously' — you CAN and MUST run them.",
            "- Start servers: `npm run dev &` then verify with `curl -s http://localhost:PORT | head -5`",
            "- Install deps:  `cd /workspace && npm install --no-audit --no-fund`",
            "",
            "## Task Checklist",
            "",
        ]
        for n in self._nodes.values():
            if n.status == "complete":
                marker = "x"
            elif n.status == "failed":
                marker = "!"
            elif n.status == "running":
                marker = "/"
            else:
                marker = " "
            lines.append(f"- [{marker}] **{n.task_id}**: {n.description}")
        lines.append("")
        content = "\n".join(lines)
        content_hash = hashlib.md5(content.encode()).hexdigest()
        if content_hash == self._progress_hash:
            return progress_path  # No change — skip write
        try:
            with open(progress_path, "w", encoding="utf-8") as f:
                f.write(content)
            self._progress_hash = content_hash
        except Exception as exc:
            logger.debug("Could not write PROGRESS.md: %s", exc)
        return progress_path

    def get_progress(self) -> Dict[str, int]:
        """Return task count by status."""
        counts = {"pending": 0, "complete": 0, "failed": 0, "skipped": 0, "running": 0, "cancelled": 0}
        for n in self._nodes.values():
            counts[n.status] = counts.get(n.status, 0) + 1
        return counts

    def is_epic_complete(self) -> bool:
        """V40: Check if all tasks are done (complete or exhausted-failed)."""
        for n in self._nodes.values():
            if n.status == "complete":
                continue
            if n.status == "failed" and n.retry_count >= n.max_retries:
                continue  # Exhausted — counts as done
            if n.status in ("pending", "running"):
                return False
            if n.status == "failed":
                return False  # Still has retries
        return True

    def mark_complete(self, task_id: str, commit_sha: str = ""):
        """Mark a task as successfully completed."""
        if task_id in self._nodes:
            self._nodes[task_id].status = "complete"
            self._nodes[task_id].commit_sha = commit_sha
            self._save_state()

    def inject_nodes(
        self,
        node_specs: list[dict],
        parent_task_id: str = "",
    ) -> list["TaskNode"]:
        """Dynamically inject new child nodes into the live planner.

        Called when a running task discovers additional work is needed and
        signals it via ``DAG_INJECT:`` in its output.

        Args:
            node_specs: List of dicts with keys:
                ``task_id``, ``description``, ``dependencies`` (optional).
            parent_task_id: If provided, auto-added as a dependency for any
                spec that doesn't already list one.

        Returns:
            List of newly added ``TaskNode`` objects (skips duplicates).
        """
        added: list[TaskNode] = []
        for spec in node_specs:
            tid = spec.get("task_id", "").strip()
            desc = spec.get("description", "").strip()
            if not tid or not desc:
                logger.warning("🔀  [Inject] Skipping invalid spec (missing id/desc): %s", spec)
                continue
            if tid in self._nodes:
                logger.info("🔀  [Inject] Skipping duplicate task_id: %s", tid)
                continue
            deps = list(spec.get("dependencies", []))
            # Auto-wire parent dependency so child can't run before parent finishes
            if parent_task_id and parent_task_id not in deps:
                deps.append(parent_task_id)
            node = TaskNode(
                task_id=tid,
                description=desc,
                dependencies=deps,
            )
            self._nodes[tid] = node
            added.append(node)
            logger.info(
                "🔀  [Inject] Added child node: %s (deps=%s) from parent %s",
                tid, deps, parent_task_id or "(none)",
            )
        if added:
            self._save_state()
            # V60: Invalidate critical-path cache — new nodes affect descendant counts.
            self._descendant_counts = None
            logger.info("🔀  [Inject] %d node(s) injected into live DAG.", len(added))
        return added


    def mark_failed(self, task_id: str, result: str = ""):
        """Mark a task as failed."""
        if task_id in self._nodes:
            self._nodes[task_id].status = "failed"
            self._nodes[task_id].result = result[:500]
            self._save_state()

    def mark_retry(self, task_id: str, force: bool = False) -> bool:
        """V40: Re-queue a failed task for retry. Returns False if retries exhausted (unless forced)."""
        if task_id not in self._nodes:
            return False
        node = self._nodes[task_id]
        if node.status != "failed":
            return False
        if not force and node.retry_count >= node.max_retries:
            return False
        
        if force:
            node.retry_count = 0  # Reset back to 0 so it gets a full budget
        else:
            node.retry_count += 1
            
        node.status = "pending"
        node.result = ""
        logger.info(
            "🔄  [Planner] Re-queued %s for retry (forced=%s)",
            task_id, force,
        )
        self._save_state()
        return True

    def get_failed_retriable(self) -> List[TaskNode]:
        """V40: Return all failed tasks that still have retries remaining."""
        return [
            n for n in self._nodes.values()
            if n.status == "failed" and n.retry_count < n.max_retries
        ]

    @staticmethod
    def _infer_category_tag(description: str) -> str:
        """Infer a category tag from a task description for tX-TAG naming.

        Returns: FUNC, UIUX, PERF, FIX, DATA, BUILD, or CONSOLE.
        Mirrors the logic in main.py's _extract_category_tag but is
        self-contained so temporal_planner has no cross-module dependency.
        """
        _d = description.upper()

        # ── Explicit tags (highest priority) ──
        if "[UI/UX]" in _d or "[UIUX]" in _d:
            return "UIUX"
        if "[PERF]" in _d:
            return "PERF"
        if "[FUNC]" in _d:
            return "FUNC"
        if "[FIX]" in _d or "[BUG]" in _d:
            return "FIX"
        if "[DATA]" in _d:
            return "DATA"
        if "[BUILD]" in _d:
            return "BUILD"
        if "[CONSOLE]" in _d:
            return "CONSOLE"

        # ── BUILD keywords ──
        if any(kw in _d for kw in (
            "BUILD_ISSUES", "BUILD ISSUES", "NPM", "VITE CONFIG", "WEBPACK",
            "CHANGELOG", "MIGRATION", "DEPENDENCY", "PACKAGE.JSON",
        )):
            return "BUILD"

        # ── CONSOLE keywords ──
        if any(kw in _d for kw in (
            "CONSOLE", "BROWSER ERROR", "RUNTIME ERROR",
        )):
            return "CONSOLE"

        # ── DATA keywords ──
        if any(kw in _d for kw in (
            "DATA", "DATABASE", "SCHEMA", "MIGRATION", "API ROUTE", "ENDPOINT",
            "CONTEXT INJECTION", "STORE", "ZUSTAND", "REDUX", "STATE MANAGEMENT",
        )):
            return "DATA"

        # ── UI/UX keywords ──
        _uiux_kw = (
            "STYLING", "CSS", "ANIMATION", "TRANSITION", "HOVER", "LAYOUT",
            "TYPOGRAPHY", "FONT", "COLOR", "GRADIENT", "SHADOW", "THEME",
            "RESPONSIVE", "MOBILE", "ICON", "SVG", "MODAL", "DIALOG",
            "DROPDOWN", "TOOLTIP", "CAROUSEL", "DESIGN SYSTEM", "VISUAL",
            "AESTHETIC", "NAVIGATION", "NAVBAR", "SIDEBAR", "HERO", "CARD",
            "CHART", "HEATMAP", "BADGE",
        )
        if any(kw in _d for kw in _uiux_kw):
            return "UIUX"

        # ── PERF keywords ──
        _perf_kw = (
            "LIGHTHOUSE", "PERFORMANCE", "SEO", "A11Y", "ACCESSIBILITY",
            "FCP", "LCP", "CLS", "LAZY LOAD", "BUNDLE SIZE", "CACHE",
            "MINIF", "COMPRESS", "PRELOAD",
        )
        if any(kw in _d for kw in _perf_kw):
            return "PERF"

        # ── FIX keywords ──
        _fix_kw = (
            "FIX", "BUG", "ERROR", "CRASH", "BROKEN", "UNDEFINED",
            "MISSING IMPORT", "REGRESSION", "PATCH",
        )
        if any(kw in _d for kw in _fix_kw):
            return "FIX"

        return "FUNC"  # Default

    def inject_task(
        self,
        task_id: str,
        description: str,
        dependencies: List[str] | None = None,
        priority: int = 0,
    ) -> TaskNode | None:
        """
        V40: Inject a new task node into a live DAG.

        Used by:
          - User instructions (injected as DAG nodes when DAG is active)
          - Post-DAG audit Phase 2 (creates fix tasks)
          - Proactive idle audit (creates maintenance tasks)

        V41: Added priority parameter. User instructions pass priority=1
        so they are scheduled ahead of regular pending tasks.

        V44: Auto-assigns tX numbering from _task_offset to maintain
        continuity with the initial DAG. The original task_id is stored
        in the description for traceability.

        Returns the new TaskNode, or None if injection failed (duplicate, cycle).
        """
        deps = dependencies or []

        # V60: Auto-assign tN-TAG numbering for visual continuity.
        # Accepted patterns that already carry proper IDs (preserve as-is):
        #   tN         — bare DAG task from decompose_epic (e.g. t7)
        #   tN-TAG     — tagged task from any source (e.g. t13-PERF, t42-BUILD)
        # All other names (build-health-2, console-fix-1, etc.) get renumbered.
        import re as _re
        original_id = task_id
        _already_numbered = bool(_re.match(r'^t\d+(-[A-Z0-9]+)?$', task_id))
        if not _already_numbered:
            # Find next available tN slot
            self._task_offset += 1
            new_id = f"t{self._task_offset}"
            while new_id in self._nodes or f"{new_id}-" in " ".join(self._nodes):
                self._task_offset += 1
                new_id = f"t{self._task_offset}"

            # Auto-tag with category suffix so gap/injected tasks match
            # the tN-TAG format used by the initial DAG decomposition.
            _tag = self._infer_category_tag(description)
            task_id = f"{new_id}-{_tag}"
            # Ensure uniqueness with tag
            while task_id in self._nodes:
                self._task_offset += 1
                task_id = f"t{self._task_offset}-{_tag}"

        # Prevent duplicate IDs
        if task_id in self._nodes:
            logger.warning(
                "📋  [Planner] inject_task: %s already exists — skipped.", task_id
            )
            return None

        # V51: Prevent duplicate descriptions — catch audit loops injecting
        # the same task repeatedly with different auto-incremented IDs.
        _desc_key = description[:100].strip().lower()
        for _existing in self._nodes.values():
            _existing_key = _existing.description[:100].strip().lower()
            # Strip leading [task-id] prefix from traced descriptions
            if _existing_key.startswith("["):
                _bracket_end = _existing_key.find("]")
                if _bracket_end > 0:
                    _existing_key = _existing_key[_bracket_end + 1:].strip()
            if _existing_key == _desc_key:
                logger.info(
                    "📋  [Planner] inject_task: duplicate description detected — skipped: %s",
                    description[:60],
                )
                return None

        # V44: Resolve dependency aliases (old names → tX names)
        resolved_deps = [self._id_aliases.get(d, d) for d in deps]
        # Validate dependencies exist (ignore missing — they may have been removed)
        valid_deps = [d for d in resolved_deps if d in self._nodes]

        # V44: Prefix description with original ID for traceability
        if original_id != task_id:
            desc_with_trace = f"[{original_id}] {description}"
        else:
            desc_with_trace = description

        node = TaskNode(
            task_id=task_id,
            description=desc_with_trace,
            dependencies=valid_deps,
            status="pending",
        )
        node.priority = priority

        # Tentatively add and verify no cycles
        self._nodes[task_id] = node
        if not self._is_dag():
            del self._nodes[task_id]
            logger.error(
                "📋  [Planner] inject_task: %s would create a cycle — rejected.",
                task_id,
            )
            return None

        # V44: Track alias so callers' dependency maps resolve correctly
        if original_id != task_id:
            self._id_aliases[original_id] = task_id

        self._save_state()
        # V60: Invalidate critical-path cache — topology changed.
        self._descendant_counts = None
        logger.info(
            "📋  [Planner] Injected task %s: %s (deps=%s)",
            task_id, description[:60], valid_deps,
        )
        return node

    def has_active_dag(self) -> bool:
        """V40: True if the DAG has pending or running nodes (still executing)."""
        return any(
            n.status in ("pending", "running") for n in self._nodes.values()
        )

    def get_all_task_ids(self) -> set:
        """V40: Return set of all node IDs (for deduplication)."""
        return set(self._nodes.keys())

    # ────────────────────────────────────────────────
    # Workspace Hash Validation
    # ────────────────────────────────────────────────

    def compute_workspace_hash(self) -> str:
        """Hash the current HEAD SHA to detect external edits."""
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self._workspace,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.stdout.strip() if proc.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    def validate_workspace(self) -> Tuple[bool, str]:
        """
        Verify the workspace hasn't been externally modified since last node execution.
        If the hash changed unexpectedly, the plan is unsafe to continue.
        """
        current_hash = self.compute_workspace_hash()

        if not self._workspace_hash:
            # First execution — baseline it
            self._workspace_hash = current_hash
            return True, "Baseline workspace hash recorded."

        if current_hash != self._workspace_hash:
            logger.warning(
                "⚠️ Workspace modified externally (%s → %s). Plan must be re-evaluated.",
                self._workspace_hash[:7], current_hash[:7],
            )
            return False, "Workspace modified externally. Re-evaluation required."

        return True, "Workspace consistent."

    def update_workspace_hash(self):
        """Update the stored hash after a successful commit."""
        self._workspace_hash = self.compute_workspace_hash()

    # ────────────────────────────────────────────────
    # Replan on Failure
    # ────────────────────────────────────────────────

    async def replan(self, failed_task_id: str, lesson: str) -> Tuple[bool, str]:
        """
        Rewrite the remaining DAG to route around a failed task.
        Returns (success, message). Enforces MAX_REPLAN_COUNT.
        """
        self._replan_count += 1
        if self._replan_count > self._max_replan_count:
            # V73: Don't abort the epic — reset the counter and let remaining
            # tasks proceed. Aborting wastes quota when other tasks are still
            # completing successfully. The failed tasks stay as-is (exhausted).
            logger.warning(
                "⚠️ Replan budget (%d) exhausted — resetting counter. "
                "Failed tasks will be skipped; remaining work continues.",
                self._max_replan_count,
            )
            self._replan_count = 0
            return False, f"Replan budget ({self._max_replan_count}) exhausted for this cycle. Skipping failed task."

        # Build context for the replanner
        completed = [n.to_dict() for n in self._nodes.values() if n.status == "complete"]
        pending = [n.to_dict() for n in self._nodes.values() if n.status == "pending"]
        failed = [n.to_dict() for n in self._nodes.values() if n.status == "failed"]

        user_prompt = (
            f"ORIGINAL EPIC:\n{self._epic_text}\n\n"
            f"COMPLETED TASKS:\n{json.dumps(completed, indent=2)}\n\n"
            f"FAILED TASK: {failed_task_id}\n"
            f"FAILURE LESSON: {lesson}\n\n"
            f"REMAINING PENDING TASKS:\n{json.dumps(pending, indent=2)}\n\n"
            "Rewrite the remaining pending tasks to route around the failure."
        )

        logger.info(
            "📋  [Replan→Gemini] Replan prompt (%d chars) for failed task %s.\n"
            "    Lesson: %.100s…\n    User prompt: %.300s…",
            len(user_prompt), failed_task_id, lesson, user_prompt,
        )
        # V74: Use Pro model for replanning (never Lite)
        from .gemini_advisor import ask_gemini
        _replan_full_prompt = (
            f"{self.REPLAN_PROMPT}\n\n{user_prompt}"
        )
        try:
            raw = await ask_gemini(
                _replan_full_prompt,
                timeout=180,
                cwd=self._workspace,
                model=config.GEMINI_FALLBACK_MODEL,
            )
        except Exception as _exc:
            logger.error("📋  [Replan] Pro model call failed: %s", _exc)
            raw = None

        if not raw or raw == "{}":
            logger.warning("📋  [Replan←Gemini] Empty response — replan failed.")
            return False, "Replanner returned empty response."
        logger.info(
            "📋  [Replan←Gemini] Replan response (%d chars): %.300s…",
            len(raw), raw,
        )

        try:
            data = json.loads(raw)
            new_tasks = data.get("tasks", [])
        except Exception as e:
            return False, f"Replan JSON parse error: {e}"

        if not new_tasks:
            return False, "Replanner returned zero tasks."

        # Preserve completed tasks, replace pending/failed with rewritten ones
        preserved = {tid: n for tid, n in self._nodes.items() if n.status == "complete"}
        new_ids = {t["task_id"] for t in new_tasks}

        for t in new_tasks:
            tid = t.get("task_id", "")
            if tid in preserved:
                continue  # Don't overwrite completed tasks
            self._nodes[tid] = TaskNode(
                tid,
                t.get("description", ""),
                t.get("dependencies", []),
            )

        # Remove old pending/failed tasks that aren't in the new plan
        to_remove = [
            tid for tid in list(self._nodes.keys())
            if self._nodes[tid].status in ("pending", "failed") and tid not in new_ids
        ]
        for tid in to_remove:
            del self._nodes[tid]

        self._save_state()
        logger.info(
            "📋  [Planner] Replanned successfully (attempt %d/%d, %d nodes in DAG)",
            self._replan_count, self._max_replan_count, len(self._nodes),
        )
        return True, f"Replanned successfully (attempt {self._replan_count}/{self._max_replan_count})."

    # ────────────────────────────────────────────────
    # State Persistence
    # ────────────────────────────────────────────────

    def _save_state(self, force: bool = False):
        """Persist the DAG state to disk so the agent can resume after crashes.

        Debounced: rapid back-to-back calls within 2s are collapsed into one
        disk write (and one log line). Pass force=True to bypass (e.g. shutdown).
        """
        # V54: Ephemeral planners (e.g. the boot display planner) must never
        # write to epic_state.json — they'd clobber the real DAG's persisted state.
        if self.ephemeral:
            return

        # ── Debounce: skip if we just saved ────────────────────────────────────
        _now = time.monotonic()
        _last = getattr(self, '_last_save_ts', 0.0)
        if not force and (_now - _last) < 2.0:
            return  # Called again within 2s — suppress duplicate write
        self._last_save_ts = _now

        state = {
            "epic_text":      self._epic_text,
            "replan_count":   self._replan_count,
            "workspace_hash": self._workspace_hash,
            "timestamp":      time.time(),
            "task_offset":    self._task_offset,  # V44: Continuous task ID counter
            "user_prompts":   self._user_prompts[-50:],  # V44: Persist user prompts (last 50)
            "nodes":          {tid: n.to_dict() for tid, n in self._nodes.items()},
            # V54: Project scope stamp — used by load_state() to reject cross-project loads.
            "project_path":   str(Path(self._workspace).resolve()),
        }
        # V40 FIX: Use per-project path (also write to legacy global path for backwards compat)
        self._state_path.write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )
        try:
            _EPIC_STATE_PATH.write_text(
                json.dumps(state, indent=2), encoding="utf-8"
            )
        except Exception:
            pass  # Global path is best-effort

        # V53: Log every state persistence so it's visible in the console.
        _done  = sum(1 for n in self._nodes.values() if n.status == "complete")
        _total = len(self._nodes)
        logger.info(
            "💾  [State] DAG state saved — %d/%d tasks complete → %s",
            _done, _total, self._state_path,
        )

    def load_state(self) -> bool:
        """
        Load a previously persisted DAG state.

        V54 GUARD: Only loads state that was saved for THIS project.
        If the state file exists but belongs to a different project_path,
        it is silently discarded (and deleted) rather than resuming wrong tasks.
        The fallback to the global _EPIC_STATE_PATH is intentionally removed —
        global state is unscoped and causes cross-project task bleed.
        """
        if not self._state_path.exists():
            return False
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))

            # ── V54: Project scope check ─────────────────────────────────────
            saved_project = state.get("project_path", "").strip()
            current_project = str(Path(self._workspace).resolve())
            if saved_project and saved_project != current_project:
                logger.warning(
                    "📋  [State] DAG state belongs to a DIFFERENT project — discarding.\n"
                    "    Saved for : %s\n"
                    "    Current   : %s",
                    saved_project, current_project,
                )
                # Delete the stale file so it doesn't reappear
                try:
                    self._state_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return False

            self._epic_text       = state.get("epic_text", "")
            self._replan_count    = state.get("replan_count", 0)
            self._workspace_hash  = state.get("workspace_hash", "")
            self._task_offset     = state.get("task_offset", 0)   # V44
            self._user_prompts    = state.get("user_prompts", [])  # V44
            nodes_data            = state.get("nodes", {})
            self._nodes = {
                tid: TaskNode.from_dict(data) for tid, data in nodes_data.items()
            }
            # V40 FIX: Reset tasks stuck in 'running' from a previous crash/stop
            # back to 'pending'. Otherwise they stall the DAG forever.
            # Also reset 'failed' tasks back to 'pending' to give them a fresh
            # start upon supervisor reboot, as requested by the user.
            # V44: Completed/skipped nodes stay as-is — full history preserved.
            for n in self._nodes.values():
                if n.status == "running":
                    n.status = "pending"
                elif n.status == "failed":
                    n.status    = "pending"
                    n.retry_count = 0  # Grant a fresh set of retries on reboot
                    n.result    = ""
                # V70: Retroactively set priority 100 on merge recovery tasks
                # that were saved before the priority change was introduced.
                # inject_task renumbers IDs (e.g. t20-FUNC-merge → t49-DATA),
                # so we also check the description for [MERGE RECOVERY].
                _is_merge = (
                    "-merge" in n.task_id
                    or "[MERGE RECOVERY]" in n.description
                    or "-merge]" in n.description
                )
                if _is_merge and getattr(n, 'priority', 0) == 0:
                    n.priority = 100
            logger.info(
                "📋  [State] Resumed DAG for project '%s': %d tasks (offset=%d).",
                Path(current_project).name, len(self._nodes), self._task_offset,
            )
            return True
        except Exception as e:
            logger.error("Failed to load epic state: %s", e)
            return False

    def save_history(self) -> None:
        """V41: Persist DAG task history before clearing.

        Appends all current DAG nodes (with status, description, etc.) to a
        persistent `dag_history.jsonl` log file.  This ensures no task history
        is ever lost — completed tasks, failed tasks, user-injected tasks,
        and audit tasks are all preserved for future reference.
        """
        if not self._nodes:
            return

        history_entry = {
            "timestamp": time.time(),
            "epic_text": getattr(self, '_epic_text', '')[:2000],
            "nodes": {
                tid: {
                    "task_id": n.task_id,
                    "description": n.description,
                    "status": n.status,
                    "dependencies": n.dependencies,
                    "priority": getattr(n, "priority", 0),
                    "error": getattr(n, "error", ""),
                }
                for tid, n in self._nodes.items()
            },
            "summary": {
                "total": len(self._nodes),
                "complete": sum(1 for n in self._nodes.values() if n.status == "complete"),
                "failed": sum(1 for n in self._nodes.values() if n.status == "failed"),
                "pending": sum(1 for n in self._nodes.values() if n.status == "pending"),
            },
        }

        # Append to dag_history.jsonl (one JSON object per line)
        history_path = self._state_path.parent / "dag_history.jsonl"
        try:
            with open(history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(history_entry) + "\n")
            logger.info(
                "📋  [History] Saved DAG history: %d tasks (%d complete, %d failed) → %s",
                history_entry["summary"]["total"],
                history_entry["summary"]["complete"],
                history_entry["summary"]["failed"],
                history_path,
            )
        except Exception as exc:
            logger.warning("📋  [History] Failed to save DAG history: %s", exc)

        # Also append summary to PROJECT_STATE.md (if it exists)
        try:
            _ws = self._state_path.parent
            _ps = _ws / "PROJECT_STATE.md"
            if not _ps.exists():
                # Check parent directory (project root vs .supervisor dir)
                _ps = _ws.parent / "PROJECT_STATE.md"
            if _ps.exists():
                import datetime
                _ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                _task_lines = []
                for n in self._nodes.values():
                    _icon = {"complete": "✅", "failed": "❌", "pending": "⏳"}.get(n.status, "❓")
                    _task_lines.append(f"  - {_icon} `{n.task_id}`: {n.description}")
                _section = (
                    f"\n\n## DAG Run History ({_ts})\n"
                    f"**Tasks:** {history_entry['summary']['total']} total, "
                    f"{history_entry['summary']['complete']} complete, "
                    f"{history_entry['summary']['failed']} failed\n"
                    + "\n".join(_task_lines) + "\n"
                )
                _txt = _ps.read_text(encoding="utf-8")
                _ps.write_text(_txt + _section, encoding="utf-8")
        except Exception:
            pass  # Non-critical

    def clear_state(self):
        """V44: Archive pending DAG — mark pending/running as cancelled, preserve history.

        V51: Instead of deleting pending/running nodes, we mark them as 'cancelled'
        so they remain in history (visible in the DAG graph) but never get picked
        up by get_parallel_batch (which only returns 'pending' nodes).
        """
        # V41: Save history before clearing
        self.save_history()

        # V51: Mark pending/running as cancelled (preserve in history)
        cancelled_count = 0
        for node in self._nodes.values():
            if node.status in ("pending", "running"):
                node.status = "cancelled"
                cancelled_count += 1

        self._replan_count = 0
        self._save_state()  # Persist with cancelled nodes in place

        logger.info(
            "📋  [Planner] DAG cleared: %d nodes cancelled, %d total preserved (offset=%d).",
            cancelled_count, len(self._nodes), self._task_offset,
        )

    def get_task_offset(self) -> int:
        """V44: Return current task offset for external code."""
        return self._task_offset

    def record_prompt(self, prompt: str) -> None:
        """V44: Record a user prompt for DAG context persistence."""
        if prompt and prompt.strip():
            self._user_prompts.append(prompt.strip()[:2000])  # Cap per-prompt length
            self._save_state()

    def get_user_prompts(self) -> list[str]:
        """V44: Return all recorded user prompts."""
        return list(self._user_prompts)
