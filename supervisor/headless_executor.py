"""
headless_executor.py — Headless Task Execution Engine.

"Host Intelligence, Sandboxed Hands" Architecture:
    The HOST runs the brain (Gemini CLI + Ollama).
    The SANDBOX is a dumb terminal (file I/O + shell exec only).

    ┌─────────────────────────────────────────────────────────┐
    │  HOST (Intelligence)                                     │
    │  ─ Gemini CLI (cloud, authenticated AI Ultra session)    │
    │  ─ Ollama (local LLM, ~200ms for triage)                 │
    │  ─ Prompt construction, output parsing                   │
    │  ─ ZERO credentials leak into the sandbox                │
    ├─────────────────────────────────────────────────────────┤
    │  SANDBOX (Execution)                                     │
    │  ─ File read/write via docker exec                       │
    │  ─ Shell command execution via docker exec               │
    │  ─ Dev server, tests, linters — all inside container     │
    │  ─ Non-root user, no API keys, no Gemini CLI installed   │
    └─────────────────────────────────────────────────────────┘

Graceful degradation: if Ollama is unavailable, everything works
via Gemini CLI alone (just slower for lightweight decisions).

Context gathering uses structured API calls via the ToolServer.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict

import aiohttp

from . import config
from .sandbox_manager import SandboxManager, SandboxError, CommandResult
from .tool_server import ToolServer, ShellExecResult, DevServerResult, GitStatusResult

logger = logging.getLogger("supervisor.headless_executor")



# ─────────────────────────────────────────────────────────────
# V58: Layer 2 — Current-file state injection helper
# ─────────────────────────────────────────────────────────────

_FILE_MENTION_RE = re.compile(
    r"[\`'\"]?([\w/\\.-]+\.(?:ts|tsx|js|jsx|mjs|py|md|json|css|scss|html|svelte|vue|go|rs))\b",
    re.IGNORECASE,
)
_MAX_FILES_INJECTED = 5
# V60: _MAX_BYTES_PER_FILE and _MAX_TOTAL_BYTES removed — dead code since
# L2 injection was replaced with @filename hints (headless_executor.py:65).


def _inject_current_file_states(prompt: str, project_path: str) -> str:
    """
    V59: Layer 2 (revised) — File-reference hint injection.

    Scans the prompt for source file mentions and prepends a short instruction
    telling Gemini CLI to READ those files from the project before making
    changes.  We intentionally do NOT dump file contents into the prompt:
    Gemini CLI has full filesystem access (--yolo mode, CWD = project root)
    and reads files natively via its own tools.  Pre-loading them into the
    prompt balloons context and causes silent hangs on large files.
    """
    if not project_path or project_path == ".":
        return prompt

    from pathlib import Path as _Path
    proj = _Path(project_path).resolve()
    if not proj.is_dir():
        return prompt

    # Extract unique file mentions that actually exist in the project
    mentions = list(dict.fromkeys(m.group(1) for m in _FILE_MENTION_RE.finditer(prompt)))
    if not mentions:
        return prompt

    existing: list[str] = []
    seen: set = set()
    for mention in mentions[:_MAX_FILES_INJECTED]:
        for candidate in (proj / mention, proj / os.path.basename(mention)):
            try:
                resolved = candidate.resolve()
                resolved.relative_to(proj)  # must be inside project
            except Exception:
                continue
            key = str(resolved)
            if key in seen or not resolved.is_file():
                continue
            rel = str(resolved.relative_to(proj)).replace("\\", "/")
            existing.append(rel)
            seen.add(key)
            break

    if not existing:
        return prompt

    # Use Gemini CLI native @filename syntax — the CLI reads these files
    # into context via its own read_many_files tool (March 2026 best practice).
    at_refs = " ".join(f"@{f}" for f in existing)
    hint = (
        f"{at_refs}\n"
        "[FILE GUARD] You have been given the current state of the above file(s). "
        "MERGE your changes into the existing content — never overwrite with a shorter version.\n\n"
    )
    logger.info(
        "[L2/Hint] Added @-file references for %d file(s): %s",
        len(existing), ", ".join(existing),
    )
    return hint + prompt


# ─────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    """Result of a coding task executed inside the sandbox."""
    status: str = "unknown"  # success, error, timeout, partial
    output: str = ""
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    exit_code: int = -1
    duration_s: float = 0.0
    prompt_used: str = ""
    _rate_limited: bool = False
    # V56: TS regression guard
    ts_regressions: list[str] = field(default_factory=list)
    # V57: Acceptance criteria (from DAG node)
    acceptance_criteria: str = ""
    # V57: True if task completed but modified zero files (potential hallucination)
    zero_change_warning: bool = False
    # V57: True if task touched a config file (vite/next/package.json/tailwind)
    touched_config: bool = False
    # V57: Self-critique — set when pool_worker should inject a review task.
    # We do NOT call ask_gemini from inside execute_task (nested subprocess risk).
    # Instead, flag it here and let pool_worker inject a safe review-task.
    needs_self_review: bool = False
    self_review_context: str = ""  # serialized prompt snippet for the review task
    # V58: Dynamic DAG expansion — nodes to inject into the live planner
    dag_injections: list[dict] = field(default_factory=list)
    # V60: True when Gemini was killed for inactivity AND wrote zero files.
    # main.py uses this to craft a targeted retry prompt instead of the
    # generic "fix the issues" message (which is useless for pure timeouts).
    silent_timeout: bool = False

    @property
    def success(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["success"] = self.success
        return d


@dataclass
class ExecutionContext:
    """
    Structured snapshot of the sandbox's current state.

    Replaces context_engine.py's ContextSnapshot which gathered state
    by scraping the IDE's DOM. This gathers state via direct API calls.
    """
    # Agent output
    last_agent_output: str = ""
    agent_status: str = "unknown"  # working, idle, error, waiting

    # Workspace state
    workspace_files: list[str] = field(default_factory=list)
    recently_changed_files: list[str] = field(default_factory=list)

    # Dev server
    dev_server_running: bool = False
    dev_server_port: int = 0
    dev_server_url: str = ""

    # Code quality
    diagnostics_errors: int = 0
    diagnostics_warnings: int = 0
    diagnostic_details: list[dict] = field(default_factory=list)

    # Git state
    git_branch: str = ""
    git_modified: list[str] = field(default_factory=list)
    git_clean: bool = True

    # Project state
    project_state_content: str = ""

    # Process info
    running_processes: list[dict] = field(default_factory=list)

    # Meta
    gathered_at: float = 0.0
    confidence: float = 1.0  # Always 1.0 — API calls are deterministic

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────
# Lite Brain — Gemini Lite first, Ollama fallback (V67)
# ─────────────────────────────────────────────────────────────

class OllamaLocalBrain:
    """
    V67: Gemini Lite-first intelligence for lightweight tasks.

    Strategy: Try Gemini Lite (~5000 RPD, 1M context) first via
    ask_gemini() from gemini_advisor.py. Fall back to local Ollama
    only if Gemini Lite is unavailable (quota exhausted, error, etc.).

    This class name is kept as OllamaLocalBrain for backward
    compatibility — all existing callsites work unchanged.

    Gemini Lite has a 1M token context window, so no truncation
    is applied to prompts (unlike the old Ollama 8K path).
    """

    def __init__(self, host: str | None = None, model: str | None = None):
        # Ollama fallback config (used only when Gemini Lite fails)
        self.host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2:3b")
        self._available: bool | None = None
        self._available_checked_at: float = 0.0
        self._session: aiohttp.ClientSession | None = None
        # V67: Track Ollama availability separately from overall is_available()
        self._ollama_available: bool | None = None
        self._ollama_checked_at: float = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._session

    async def close(self) -> None:
        """Properly close the aiohttp session to prevent resource leaks."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def is_available(self) -> bool:
        """V67: Always True — Gemini Lite is cloud-based and always reachable.

        The old Ollama-only check could return False at boot (service still
        starting), blocking the entire brain. Gemini Lite has no cold-start
        delay. Ollama is checked lazily in _ask_ollama_fallback() only when
        Gemini Lite fails.
        """
        return True

    async def _is_ollama_available(self) -> bool:
        """Check if local Ollama is running (used for fallback only)."""
        import time as _time
        if self._ollama_available is not None and (_time.time() - self._ollama_checked_at) < 300:
            return self._ollama_available
        self._ollama_checked_at = _time.time()
        try:
            session = await self._get_session()
            async with session.get(f"{self.host}/api/tags") as resp:
                self._ollama_available = resp.status == 200
                if self._ollama_available:
                    data = await resp.json()
                    models = [m.get("name", "") for m in data.get("models", [])]
                    logger.debug("🧠  [Ollama] Available (fallback). Models: %s", models[:5])
                    if self.model not in models:
                        text_models = [m for m in models if "llama" in m.lower() and "llava" not in m.lower()]
                        if text_models:
                            self.model = text_models[0]
                return self._ollama_available
        except Exception as exc:
            logger.debug("🧠  [Ollama] Fallback not available: %s", exc)
            self._ollama_available = False
            return False

    async def warm_up(self) -> None:
        """V67: No-op — Gemini Lite is cloud-based, no VRAM warm-up needed.

        Ollama warm-up is skipped since it's only a fallback now.
        """
        logger.debug("🧠  [LiteBrain] warm_up() is a no-op — Gemini Lite is cloud-based.")

    async def _ask_gemini_lite(self, prompt: str, system: str = "") -> str | None:
        """Try Gemini Lite via the existing Gemini CLI infrastructure.

        Uses the standard timeout and retry behavior — Gemini Lite is treated
        the same as other Gemini models. Model is forced to the lite tier to
        avoid burning Pro/Flash quota on lightweight intelligence tasks.
        """
        try:
            from .gemini_advisor import _call_gemini_async
            from . import config as _cfg

            lite_model = getattr(_cfg, "GEMINI_DEFAULT_LITE", "gemini-3.1-flash-lite-preview")
            full_prompt = f"{system}\n\n{prompt}" if system else prompt

            logger.info(
                "🧠  [LiteBrain→Gemini] Prompt (%d chars, model=%s): %.200s…",
                len(full_prompt), lite_model, full_prompt,
            )
            response = await _call_gemini_async(
                full_prompt,
                timeout=getattr(_cfg, "GEMINI_TIMEOUT_SECONDS", 180),
                model=lite_model,
            )
            if response:
                logger.info(
                    "🧠  [LiteBrain←Gemini] Response (%d chars): %.200s…",
                    len(response), response,
                )
                return response
            return None
        except Exception as exc:
            logger.info(
                "🧠  [LiteBrain] Gemini Lite failed (%s: %s) — will try Ollama fallback.",
                type(exc).__name__, str(exc)[:120],
            )
            return None

    async def _ask_ollama_fallback(self, prompt: str, system: str = "", temperature: float = 0.1) -> str | None:
        """Original Ollama HTTP API path — used as fallback when Gemini Lite fails."""
        if not await self._is_ollama_available():
            return None

        try:
            logger.info(
                "🧠  [LiteBrain→Ollama] Fallback prompt (%d chars, model=%s): %.200s…",
                len(prompt), self.model, prompt,
            )
            session = await self._get_session()
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "keep_alive": "5m",
                "options": {"temperature": temperature},
            }
            if system:
                payload["system"] = system

            async with session.post(f"{self.host}/api/generate", json=payload) as resp:
                if resp.status != 200:
                    logger.warning("🧠  [LiteBrain←Ollama] HTTP %d — no response", resp.status)
                    return None
                data = await resp.json()
                response = data.get("response", "")
                logger.info(
                    "🧠  [LiteBrain←Ollama] Fallback response (%d chars): %.200s…",
                    len(response), response,
                )
                return response
        except Exception as exc:
            logger.debug("🧠  [LiteBrain] Ollama fallback also failed: %s", exc)
            return None

    async def ask(self, prompt: str, system: str = "", temperature: float = 0.1) -> str | None:
        """
        V67: Gemini Lite first, Ollama fallback.

        Send a prompt to Gemini Lite (1M context, ~5000 RPD). If that fails
        (quota, error, timeout), fall back to local Ollama.

        Returns the response text, or None if both are unavailable.
        """
        # Tier 1: Gemini Lite (primary)
        response = await self._ask_gemini_lite(prompt, system=system)
        if response:
            return response

        # Tier 2: Ollama (fallback — only if Gemini Lite failed)
        return await self._ask_ollama_fallback(prompt, system=system, temperature=temperature)

    async def ask_json(self, prompt: str, system: str = "") -> dict | None:
        """Ask with JSON parsing. Gemini Lite first, Ollama fallback."""
        response = await self.ask(
            prompt,
            system=system + "\nRespond with valid JSON only. No markdown, no explanation.",
        )
        if not response:
            return None
        try:
            cleaned = re.sub(r"```json?\s*", "", response)
            cleaned = re.sub(r"```\s*", "", cleaned)
            return json.loads(cleaned.strip())
        except (json.JSONDecodeError, ValueError):
            return None

    async def classify_task(self, prompt: str) -> dict:
        """
        Classify a task's complexity and requirements.

        V67: No truncation — Gemini Lite has 1M context window.
        """
        result = await self.ask_json(
            f"Classify this coding task:\n\n{prompt}",
            system=(
                "You classify coding tasks. Return JSON with: "
                "complexity (simple/medium/complex), "
                "needs_gemini (true if this needs a powerful cloud LLM, false if a shell command suffices), "
                "category (coding/testing/analysis/setup/other), "
                "estimated_duration_s (integer seconds)."
            ),
        )
        return result or {
            "complexity": "medium",
            "needs_gemini": True,
            "category": "coding",
            "estimated_duration_s": 60,
        }

    async def analyze_errors(self, errors: list[dict], context: str = "") -> str | None:
        """
        Quick analysis of diagnostic errors.

        V67: No truncation — Gemini Lite has 1M context window.
        """
        if not errors:
            return None
        error_text = json.dumps(errors[:5], indent=2)
        return await self.ask(
            f"Analyze these code errors and suggest the most likely fix:\n\n"
            f"Errors:\n{error_text}\n\nContext:\n{context}",
            system="You are a senior developer. Be concise. Give actionable fix suggestions.",
        )

    async def decide_action(self, context_summary: str) -> dict:
        """
        Decide what action to take based on current context.

        V67: No truncation — Gemini Lite has 1M context window.
        """
        result = await self.ask_json(
            f"Based on this project state, what should the supervisor do next?\n\n{context_summary}",
            system=(
                "You are an autonomous coding supervisor. Decide the next action. "
                "Return JSON with: action (execute_task/fix_errors/run_tests/start_server/wait/escalate), "
                "reason (brief explanation)."
            ),
        )
        if not result:
            logger.warning(
                "🧠  [LiteBrain] decide_action returned no result — defaulting to 'wait'."
            )
        return result or {"action": "wait", "reason": "LiteBrain decision unavailable — idling"}

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()


# ─────────────────────────────────────────────────────────────
# Core: HeadlessExecutor
# ─────────────────────────────────────────────────────────────

def _log_npm_output(raw: str, source: str = "npm") -> None:
    """
    Parse npm install stdout and emit useful progress lines to the logger.
    All logger.info calls appear in the UI WebSocket log stream in real time.

    Emits:
      - Each "added X packages" / "changed X packages" summary line
      - Individual package install lines (npm v7+ format: + pkgname@ver)
      - WARN/deprecated lines
      - ERR! lines as warnings
    """
    if not raw.strip():
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if s.startswith("npm warn") or "deprecated" in low:
            logger.warning("📦  [%s] ⚠️  %s", source, s[:160])
        elif s.startswith("npm ERR!") or s.startswith("npm error"):
            logger.warning("📦  [%s] ❌ %s", source, s[:160])
        elif s.startswith("added ") or s.startswith("changed ") or s.startswith("removed ") \
                or s.startswith("updated ") or s.startswith("audited "):
            logger.info("📦  [%s] %s", source, s[:120])
        elif s.startswith("+ ") or s.startswith("- "):
            # npm v6 per-package lines: + pkgname@1.2.3
            logger.info("📦  [%s] %s", source, s[:120])


class HeadlessExecutor:

    """
    Host Intelligence, Sandboxed Hands.

    The HOST runs intelligence (Gemini CLI + Ollama).
    The SANDBOX is a dumb terminal (file I/O + shell exec only).

    Gemini CLI runs on the host using the user's authenticated session.
    ZERO credentials enter the Docker container.

    Usage:
        sandbox = SandboxManager()
        await sandbox.create(project_path)
        tools = ToolServer(sandbox)
        executor = HeadlessExecutor(tools, sandbox)

        # Execute a task (Gemini runs on HOST, actions in SANDBOX)
        result = await executor.execute_task("Create a hello.py that prints Hello World")

        # Gather context (reads from SANDBOX via docker exec)
        ctx = await executor.gather_context()
    """

    def __init__(self, tool_server: ToolServer, sandbox: SandboxManager):
        self.tools = tool_server
        self.sandbox = sandbox
        self.local_brain = OllamaLocalBrain()
        self._last_task_result: TaskResult | None = None
        self._task_history: list[TaskResult] = []
        # Prevent concurrent dev server starts (reinstall + retry can both
        # call start_dev_server; the lock ensures only one runs at a time)
        self._dev_server_lock: asyncio.Lock = asyncio.Lock()
        # Run tooling self-upgrade exactly once per session
        self._tooling_upgraded: bool = False
        # V54: Major dep auto-upgrade runs only ONCE per session.
        # build_health_check() is called at boot, after each coherence gate,
        # and after dev-server restarts — without this flag the same packages
        # e.g. @react-three/fiber, vite, zustand would be upgraded 3× per run.
        self._dep_upgrade_done: bool = False
        # Multi-backend lifecycle: list of {folder, framework, port, running, log, cmd}
        self._backends: list[dict] = []
        self._backend_port_base: int = 4000
        # Active data services: ['postgres', 'redis', 'mongodb']
        self._services: list[str] = []
        # V55: Flash session-skip — if Flash fails, record the timestamp.
        # Any subsequent Flash call within _FLASH_SKIP_WINDOW seconds goes
        # straight to Pro without burning ~35s on a doomed Flash attempt.
        self._flash_failed_at: float = 0.0
        self._FLASH_SKIP_WINDOW: int = 1800  # 30 minutes

    @property
    def last_result(self) -> TaskResult | None:
        """Return the result of the last executed task."""
        return self._last_task_result

    # ── Tooling Version Management ────────────────────────────

    async def _upgrade_sandbox_tooling(self) -> None:
        """
        V55: Run once per session — upgrades npm, vite, typescript inside the
        sandbox to their latest published versions, then logs the active
        versions to the Glass Brain so the operator can see what's running.
        Uses a container-lifetime stamp file so it's a no-op on the second
        call within the same container (session restart without new sandbox).
        """
        if self._tooling_upgraded:
            return
        self._tooling_upgraded = True

        # V55: Check container-lifetime stamp to skip redundant upgrades.
        # The stamp is written inside the Docker container, so it resets
        # whenever a new container is created.
        _stamp = "/tmp/.tooling_upgraded"
        try:
            _stamp_check = await self.sandbox.exec_command(
                f"test -f {_stamp} && cat {_stamp} || echo 'MISSING'",
                timeout=5,
            )
            _stamp_out = (_stamp_check.stdout or "").strip()
            if _stamp_out and _stamp_out != "MISSING":
                logger.info("🔧  [Boot] Tooling already upgraded this container (%s) — skipping.", _stamp_out)
                return
        except Exception:
            pass  # Stamp check failed — proceed with upgrade

        logger.info("🔧  [Boot] Upgrading sandbox tooling to latest versions …")
        try:
            # V55: Use POSIX-safe sh syntax (no bash-only ${PIPESTATUS}).
            # PIPESTATUS is bash-only and causes 'Bad substitution' in /bin/sh.
            # Instead, capture exit code via `set -o pipefail` (POSIX) or a tmp file.
            upgrade = await self.sandbox.exec_command(
                "npm install -g npm@latest vite@latest typescript@latest "
                "@vitejs/plugin-react@latest @vitejs/plugin-vue@latest "
                "--legacy-peer-deps --no-fund --no-audit 2>&1"
                " && echo 'OK' > " + _stamp + " || true",
                timeout=90,
            )
            if upgrade.exit_code != 0:
                logger.warning("🔧  [Boot] Tooling upgrade had warnings (non-fatal): %s", (upgrade.stderr or "")[:200])
            else:
                # Write stamp with timestamp
                await self.sandbox.exec_command(
                    f"date -u '+%Y-%m-%dT%H:%M:%SZ' > {_stamp} 2>/dev/null || true",
                    timeout=5,
                )
        except Exception as exc:
            logger.warning("🔧  [Boot] Tooling upgrade skipped: %s", exc)
            return

        # Log the now-active versions
        try:
            ver = await self.sandbox.exec_command(
                "node --version && npm --version && npx --yes vite --version 2>/dev/null || true",
                timeout=20,
            )
            lines = (ver.stdout or "").strip().splitlines()
            node_v = lines[0] if len(lines) > 0 else "?"
            npm_v  = lines[1] if len(lines) > 1 else "?"
            vite_v = lines[2] if len(lines) > 2 else "?"
            logger.info(
                "🔧  [Boot] Sandbox tooling ready — Node %s | npm %s | Vite %s",
                node_v, npm_v, vite_v,
            )
        except Exception:
            pass

    # ── Backend & Service Detection ───────────────────────────

    async def _detect_backends(self) -> list[dict]:
        """
        V54: Scan the workspace for backend server packages/files.
        Returns a list of dicts: [{folder, framework, start_cmd_template}]
        Each entry represents one independent backend process.
        Port assignment happens in _start_dev_server_impl().
        """
        candidates = []

        # Candidate folders to probe (in priority order)
        probe_dirs = ["server", "api", "backend", "services", "app/api", "apps/api",
                      "apps/server", "packages/api", "src/server"]

        for folder in probe_dirs:
            pkg_path  = f"{folder}/package.json"
            req_path  = f"{folder}/requirements.txt"
            manage_py = f"{folder}/manage.py"
            app_py    = f"{folder}/app.py"
            main_py   = f"{folder}/main.py"

            # ── Node / JS backend ──────────────────────
            if await self.sandbox.file_exists(pkg_path):
                try:
                    raw = await self.sandbox.exec_command(f"cat {pkg_path} 2>/dev/null", timeout=4)
                    pkg = json.loads(raw.stdout or "{}")
                    scripts = pkg.get("scripts", {})
                    # Look for a runnable server script
                    start_key = next(
                        (k for k in ("dev", "start", "serve", "server") if k in scripts),
                        None,
                    )
                    if start_key:
                        candidates.append({
                            "folder": folder,
                            "framework": "node",
                            "start_script": start_key,
                            "pkg": pkg,
                        })
                        logger.info("🖥️  [Backend] Found Node backend in %s (script: %s)", folder, start_key)
                except Exception:
                    pass

            # ── Python backend ─────────────────────────
            elif await self.sandbox.file_exists(manage_py):
                candidates.append({"folder": folder, "framework": "django"})
                logger.info("🖥️  [Backend] Found Django backend in %s", folder)

            elif await self.sandbox.file_exists(app_py) or await self.sandbox.file_exists(main_py):
                entry = "app" if await self.sandbox.file_exists(app_py) else "main"
                raw = await self.sandbox.exec_command(
                    f"head -30 {folder}/{entry}.py 2>/dev/null", timeout=4
                )
                head = (raw.stdout or "").lower()
                fw = "fastapi" if ("fastapi" in head or "uvicorn" in head) else "flask"
                candidates.append({"folder": folder, "framework": fw, "entry": entry})
                logger.info("🖥️  [Backend] Found %s backend in %s", fw, folder)

            elif await self.sandbox.file_exists(req_path):
                raw = await self.sandbox.exec_command(f"cat {req_path} 2>/dev/null", timeout=4)
                reqs = (raw.stdout or "").lower()
                fw = "fastapi" if "fastapi" in reqs else ("flask" if "flask" in reqs else "python")
                candidates.append({"folder": folder, "framework": fw, "entry": "main"})
                logger.info("🖥️  [Backend] Found Python backend in %s (%s)", folder, fw)

        # ── Root-level backend with frontend in sub-folder ────
        # e.g. Express at root + Vite in client/
        if not candidates:
            root_pkg_exists = await self.sandbox.file_exists("package.json")
            has_frontend_subdir = any([
                await self.sandbox.file_exists(d + "/package.json")
                for d in ("client", "frontend", "web", "ui")
            ])
            if root_pkg_exists and has_frontend_subdir:
                try:
                    raw = await self.sandbox.exec_command("cat package.json 2>/dev/null", timeout=4)
                    pkg = json.loads(raw.stdout or "{}")
                    scripts = pkg.get("scripts", {})
                    if any(k in scripts for k in ("server", "api", "backend")):
                        key = next(k for k in ("server", "api", "backend") if k in scripts)
                        candidates.append({
                            "folder": ".",
                            "framework": "node",
                            "start_script": key,
                            "pkg": pkg,
                        })
                        logger.info("🖥️  [Backend] Found root-level Node backend (script: %s)", key)
                except Exception:
                    pass

        return candidates

    async def _detect_services(self) -> list[str]:
        """
        V54: Sniff the workspace for data service dependencies.
        Returns a list of service names to start: ['postgres', 'redis', 'mongodb'].
        """
        services: set[str] = set()

        # Files to scan for service signals
        scan_files = [
            "package.json", "requirements.txt", ".env.example", ".env.template",
            "docker-compose.yml", "docker-compose.yaml", "prisma/schema.prisma",
            "server/package.json", "backend/package.json", "api/package.json",
            "server/requirements.txt", "backend/requirements.txt",
        ]

        # Postgres signals
        pg_signals = [
            "pg", "postgres", "postgresql", "prisma", "typeorm", "sequelize",
            "knex", "drizzle", "django.db", "psycopg", "asyncpg",
            "sqlalchemy", "DATABASE_URL", "POSTGRES",
        ]
        # Redis signals
        redis_signals = [
            "redis", "ioredis", "bull", "bullmq", "celery", "django-redis",
            "redis-py", "aioredis", "REDIS_URL", "REDIS",
        ]
        # MongoDB signals
        mongo_signals = [
            "mongoose", "mongodb", "mongoclient", "pymongo", "motor",
            "beanie", "MONGODB_URI", "MONGO_URI", "mongo",
        ]

        for fpath in scan_files:
            try:
                if not await self.sandbox.file_exists(fpath):
                    continue
                raw = await self.sandbox.exec_command(f"cat {fpath} 2>/dev/null", timeout=4)
                content = (raw.stdout or "").lower()
                if any(s.lower() in content for s in pg_signals):
                    services.add("postgres")
                if any(s.lower() in content for s in redis_signals):
                    services.add("redis")
                if any(s.lower() in content for s in mongo_signals):
                    services.add("mongodb")
            except Exception:
                pass

        if services:
            logger.info("🗄️  [Services] Detected: %s", ", ".join(sorted(services)))
        return sorted(services)

    async def _start_services(self, service_names: list[str]) -> None:
        """
        V54: Start data services (postgres, redis, mongodb) via sudo service.
        Safe to call if already running — service start is idempotent.
        """
        for svc in service_names:
            try:
                if svc == "postgres":
                    r = await self.sandbox.exec_command(
                        "sudo service postgresql start 2>&1", timeout=20
                    )
                    if r.exit_code == 0:
                        logger.info("🗄️  [Services] PostgreSQL started")
                    else:
                        logger.warning("🗄️  [Services] PostgreSQL start warning: %s", r.stdout[:200])

                elif svc == "redis":
                    # Start Redis in background (no sudo needed, runs as sandbox user)
                    r = await self.sandbox.exec_command(
                        "redis-server --daemonize yes --bind 127.0.0.1 --port 6379 "
                        "--logfile /tmp/redis.log --dir /tmp 2>&1 || "
                        "sudo service redis-server start 2>&1",
                        timeout=10,
                    )
                    logger.info("🗄️  [Services] Redis started")

                elif svc == "mongodb":
                    r = await self.sandbox.exec_command(
                        "sudo mongod --fork --logpath /tmp/mongod.log "
                        "--dbpath /var/lib/mongodb --bind_ip 127.0.0.1 2>&1 || "
                        "sudo service mongod start 2>&1",
                        timeout=15,
                    )
                    logger.info("🗄️  [Services] MongoDB started")

            except Exception as exc:
                logger.warning("🗄️  [Services] Could not start %s: %s", svc, exc)

    async def _ensure_env_file(
        self,
        folder: str,
        framework: str,
        frontend_port: int,
        backend_port: int,
        services: list[str] | None = None,
        index: int = 0,
    ) -> None:
        """
        V54: Scaffold a .env for a backend if none exists.
        Priority order:
          1. If .env/.env.local already exists — add only missing keys, never overwrite.
          2. If .env.example or .env.template exists — copy it and fill in service URLs.
          3. Otherwise — generate a minimal template from framework type.
        """
        env_path  = f"{folder}/.env"       if folder != "." else ".env"
        env_local = f"{folder}/.env.local" if folder != "." else ".env.local"
        example_candidates = [
            f"{folder}/.env.example", f"{folder}/.env.template",
            f"{folder}/.env.sample",  f"{folder}/.env.defaults",
        ] if folder != "." else [".env.example", ".env.template", ".env.sample", ".env.defaults"]

        svc_list  = services or []
        db_url    = f"\nDATABASE_URL=postgresql://sandbox@127.0.0.1:5432/sandbox" if "postgres" in svc_list else ""
        redis_url = "\nREDIS_URL=redis://127.0.0.1:6379"                          if "redis"    in svc_list else ""
        mongo_url = "\nMONGODB_URI=mongodb://127.0.0.1:27017/app"                 if "mongodb"  in svc_list else ""

        # ── Case 1: .env already present — only warn about missing keys ──
        if await self.sandbox.file_exists(env_path) or await self.sandbox.file_exists(env_local):
            await self._warn_missing_env_keys(folder, env_path, env_local, svc_list)
            return

        # ── Case 2: .env.example / .env.template present — copy it ──
        example_src = None
        for cand in example_candidates:
            if await self.sandbox.file_exists(cand):
                example_src = cand
                break

        if example_src:
            try:
                example_content = await self.sandbox.read_file(example_src)
                # Inject service URLs for any placeholder lines
                merged = example_content.rstrip("\n")
                extras = []
                placeholder_subs = {
                    "DATABASE_URL": f"postgresql://sandbox@127.0.0.1:5432/sandbox",
                    "REDIS_URL":    "redis://127.0.0.1:6379",
                    "MONGODB_URI":  "mongodb://127.0.0.1:27017/app",
                }
                for key, val in placeholder_subs.items():
                    if key in merged:
                        # Replace placeholder value (empty, 'your-*', 'change-me', etc.)
                        import re as _re
                        merged = _re.sub(
                            rf"^({key}\s*=\s*).*$",
                            rf"\g<1>{val}",
                            merged, flags=_re.MULTILINE,
                        )
                    elif key in ("DATABASE_URL" if db_url else "",
                                   "REDIS_URL"    if redis_url else "",
                                   "MONGODB_URI"  if mongo_url else ""):
                        extras.append(f"{key}={val}")
                if extras:
                    merged += "\n" + "\n".join(extras)
                await self.sandbox.write_file(env_path, merged + "\n")
                logger.info("📄  [Env] Copied %s → %s", example_src, env_path)
                await self._warn_missing_env_keys(folder, env_path, env_local, svc_list)
                return
            except Exception as _ex:
                logger.debug("📄  [Env] Could not copy %s: %s", example_src, _ex)

        # ── Case 3: Generate minimal template ──
        if framework in ("node", "express", "fastify", "hono"):
            content = (
                f"PORT={backend_port}\nNODE_ENV=development\n"
                f"CORS_ORIGIN=http://localhost:{frontend_port}"
                + db_url + redis_url + mongo_url + "\n"
            )
        elif framework == "django":
            content = (
                f"PORT={backend_port}\nDEBUG=True\n"
                f"SECRET_KEY=dev-secret-key-replace-in-production\n"
                f"ALLOWED_HOSTS=localhost,127.0.0.1\n"
                f"CORS_ALLOWED_ORIGINS=http://localhost:{frontend_port}"
                + db_url + redis_url + mongo_url + "\n"
            )
        elif framework in ("fastapi", "flask", "python"):
            content = (
                f"PORT={backend_port}\nDEBUG=true\n"
                f"CORS_ORIGINS=http://localhost:{frontend_port}"
                + db_url + redis_url + mongo_url + "\n"
            )
        else:
            content = (
                f"PORT={backend_port}\nNODE_ENV=development\n"
                f"CORS_ORIGIN=http://localhost:{frontend_port}"
                + db_url + redis_url + mongo_url + "\n"
            )

        await self.sandbox.write_file(env_path, content)
        logger.info("📄  [Env] Created %s (%s, port %d)%s%s%s",
                    env_path, framework, backend_port,
                    " +postgres" if db_url else "",
                    " +redis"    if redis_url else "",
                    " +mongodb"  if mongo_url else "")

    async def _warn_missing_env_keys(
        self,
        folder: str,
        env_path: str,
        env_local: str,
        services: list[str],
    ) -> None:
        """
        V54: Compare .env.example with .env and log any keys present in the
        example but missing from the actual .env, so the operator knows what
        to fill in before running the project.
        """
        example_candidates = [
            f"{folder}/.env.example", f"{folder}/.env.template",
        ] if folder != "." else [".env.example", ".env.template"]
        example_src = None
        for cand in example_candidates:
            if await self.sandbox.file_exists(cand):
                example_src = cand
                break
        if not example_src:
            return
        try:
            example_raw = await self.sandbox.read_file(example_src)
            actual_src  = env_local if await self.sandbox.file_exists(env_local) else env_path
            actual_raw  = await self.sandbox.read_file(actual_src) if await self.sandbox.file_exists(actual_src) else ""
            import re as _re
            example_keys = set(_re.findall(r'^([A-Z][A-Z0-9_]+)\s*=', example_raw, _re.MULTILINE))
            actual_keys  = set(_re.findall(r'^([A-Z][A-Z0-9_]+)\s*=', actual_raw,  _re.MULTILINE))
            missing = example_keys - actual_keys
            if missing:
                logger.warning(
                    "📄  [Env] %s is missing %d key(s) from %s: %s",
                    actual_src, len(missing), example_src,
                    ", ".join(sorted(missing)[:10]),
                )
        except Exception:
            pass

    async def _ensure_frontend_env(
        self,
        frontend_port: int,
        backends: list[dict],
        frontend_framework: str = "vite",
    ) -> None:
        """
        V54: Merge API URL vars into the frontend .env. Adds lines that are missing;
        does not touch lines that already exist (preserves user values).
        Supports Vite (VITE_*), Next.js (NEXT_PUBLIC_*).
        """
        env_path = ".env"
        env_local = ".env.local"
        target = env_local if await self.sandbox.file_exists(env_local) else env_path

        # Read existing content (or empty)
        existing = ""
        if await self.sandbox.file_exists(target):
            try:
                existing = await self.sandbox.read_file(target)
            except Exception:
                existing = ""

        prefix = "NEXT_PUBLIC" if frontend_framework == "next" else "VITE"
        lines_to_add = []

        for i, bk in enumerate(backends):
            port = bk.get("port", 4000 + i)
            suffix = "" if (i == 0 and len(backends) == 1) else f"_{i+1}"
            var = f"{prefix}_API_URL{suffix}=http://localhost:{port}"
            if var.split("=")[0] not in existing:
                lines_to_add.append(var)

        if lines_to_add:
            new_content = existing.rstrip("\n") + "\n" + "\n".join(lines_to_add) + "\n"
            await self.sandbox.write_file(target, new_content)
            logger.info("📄  [Env] Frontend .env updated with API URL(s): %s",
                        ", ".join(l.split("=")[0] for l in lines_to_add))

    async def _install_backend_deps(self, folder: str, framework: str) -> None:
        """
        V54: Install dependencies for a single backend. Respects the project's
        chosen package manager (pnpm > yarn > npm) and runs Prisma setup if needed.
        """
        if hasattr(self.sandbox, "grant_network"):
            await self.sandbox.grant_network()
        try:
            if framework in ("node", "express", "fastify", "hono"):
                nm = f"{folder}/node_modules"
                has_nm = await self.sandbox.file_exists(nm)
                if not has_nm:
                    # Detect package manager from lockfile in backend folder
                    if await self.sandbox.file_exists(f"{folder}/pnpm-lock.yaml"):
                        pkg_mgr, install_cmd = "pnpm", "pnpm install --frozen-lockfile 2>&1 | tail -10"
                    elif await self.sandbox.file_exists(f"{folder}/yarn.lock"):
                        pkg_mgr, install_cmd = "yarn", "yarn install --frozen-lockfile 2>&1 | tail -10"
                    else:
                        pkg_mgr = "npm"
                        install_cmd = "npm install --legacy-peer-deps --no-audit --no-fund 2>&1 | tail -10"

                    logger.info("📦  [Backend] %s install for %s …", pkg_mgr, folder)
                    r = await self.sandbox.exec_command(
                        f"cd /workspace/{folder} && {install_cmd}",
                        timeout=180,
                    )
                    if r.exit_code != 0:
                        logger.warning("📦  [Backend] %s install warning in %s: %s",
                                       pkg_mgr, folder, (r.stdout or "")[-300:])
                    else:
                        logger.info("📦  [Backend] %s install complete for %s", pkg_mgr, folder)

                # ── Prisma auto-setup ──
                # Run after deps install (or even if node_modules already exist,
                # in case the schema changed since last run).
                await self._setup_prisma(folder)

            elif framework in ("django", "fastapi", "flask", "python"):
                req = f"{folder}/requirements.txt"
                if await self.sandbox.file_exists(req):
                    logger.info("📦  [Backend] pip install for %s …", folder)
                    await self.sandbox.exec_command(
                        f"pip3 install --break-system-packages -r /workspace/{req} "
                        "2>&1 | tail -10",
                        timeout=180,
                    )
                    logger.info("📦  [Backend] pip install complete for %s", folder)
        except Exception as exc:
            logger.warning("📦  [Backend] Dependency install error (%s): %s", folder, exc)
        finally:
            if hasattr(self.sandbox, "revoke_network"):
                await self.sandbox.revoke_network()

    async def _setup_prisma(self, folder: str) -> None:
        """
        V54: If the backend uses Prisma, run `prisma generate` (creates typed client)
        and `prisma db push` (syncs schema to the dev database without requiring
        a migration history). Both are idempotent and safe to re-run.
        """
        schema_path = f"{folder}/prisma/schema.prisma"
        if not await self.sandbox.file_exists(schema_path):
            return

        logger.info("🔷  [Prisma] Schema found in %s — running generate + db push …", folder)
        try:
            # 1. Generate the typed Prisma client
            gen = await self.sandbox.exec_command(
                f"cd /workspace/{folder} && npx prisma generate 2>&1 | tail -5",
                timeout=60,
            )
            if gen.exit_code == 0:
                logger.info("🔷  [Prisma] Client generated for %s", folder)
            else:
                logger.warning("🔷  [Prisma] generate warning: %s", (gen.stdout or "")[-200:])

            # 2. Push schema to the dev database (no migration files needed)
            push = await self.sandbox.exec_command(
                f"cd /workspace/{folder} && npx prisma db push --accept-data-loss 2>&1 | tail -8",
                timeout=30,
            )
            if push.exit_code == 0:
                logger.info("🔷  [Prisma] DB schema synced for %s", folder)
            else:
                logger.warning("🔷  [Prisma] db push warning (DB may not be ready yet): %s",
                               (push.stdout or "")[-200:])
        except Exception as exc:
            logger.warning("🔷  [Prisma] Setup error (non-fatal): %s", exc)

    async def _run_db_migrations(self, folder: str, framework: str) -> None:
        """
        V54: Run database migrations before the backend server starts.
        Each ORM/framework has its own migration CLI — we detect which one applies.
        All commands are non-fatal: a failed migration logs a warning but doesn't
        abort the server start (the dev can fix the schema iteratively).
        """
        _cmd = None

        if framework == "django":
            _cmd = f"cd /workspace/{folder} && python3 manage.py migrate --no-input 2>&1 | tail -15"

        elif framework in ("fastapi", "flask", "python"):
            # Alembic — look for alembic.ini or migrations/ folder
            has_alembic = (
                await self.sandbox.file_exists(f"{folder}/alembic.ini")
                or await self.sandbox.file_exists(f"{folder}/migrations/env.py")
            )
            if has_alembic:
                _cmd = f"cd /workspace/{folder} && alembic upgrade head 2>&1 | tail -10"
            else:
                # Flask-Migrate
                has_flask_migrate = await self.sandbox.file_exists(f"{folder}/migrations/versions")
                if has_flask_migrate:
                    _cmd = f"cd /workspace/{folder} && flask db upgrade 2>&1 | tail -10"

        elif framework in ("node", "express", "fastify", "hono"):
            pkg = {}
            try:
                raw = await self.sandbox.exec_command(
                    f"cat /workspace/{folder}/package.json 2>/dev/null", timeout=4
                )
                pkg = json.loads(raw.stdout or "{}")
            except Exception:
                pass
            scripts = pkg.get("scripts", {})
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            if "typeorm" in deps:
                migrate_script = next(
                    (k for k in scripts if "migration" in k.lower() and "run" in k.lower()),
                    None,
                )
                if migrate_script:
                    _cmd = f"cd /workspace/{folder} && npm run {migrate_script} 2>&1 | tail -10"
                else:
                    _cmd = f"cd /workspace/{folder} && npx typeorm migration:run -d src/data-source.ts 2>&1 | tail -10"

            elif "sequelize" in deps or "sequelize-cli" in deps:
                _cmd = f"cd /workspace/{folder} && npx sequelize-cli db:migrate 2>&1 | tail -10"

            elif "knex" in deps:
                _cmd = f"cd /workspace/{folder} && npx knex migrate:latest 2>&1 | tail -10"

        if not _cmd:
            return

        logger.info("🗃️  [Migrate] Running migrations for %s (%s) …", folder, framework)
        try:
            r = await self.sandbox.exec_command(_cmd, timeout=60)
            if r.exit_code == 0:
                logger.info("🗃️  [Migrate] Migrations complete for %s", folder)
            else:
                logger.warning("🗃️  [Migrate] Migration warning for %s: %s",
                               folder, (r.stdout or "")[-300:])
        except Exception as exc:
            logger.warning("🗃️  [Migrate] Error (non-fatal) for %s: %s", folder, exc)

    async def _start_workers(self, bk: dict) -> None:
        """
        V54: Detect and start background worker processes alongside the backend.
        - BullMQ / Bull: looks for a worker entry point (worker.ts/js, workers/index.ts, etc.)
        - Celery: looks for celery -A ... worker invocation in scripts or a tasks.py
        Workers are started in background with their own log files.
        """
        folder = bk["folder"]
        fw     = bk["framework"]
        port   = bk["port"]

        if fw in ("node", "express", "fastify", "hono"):
            # Detect BullMQ/Bull worker scripts
            pkg = bk.get("pkg", {})
            scripts = pkg.get("scripts", {})
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            has_bull = any(k in deps for k in ("bull", "bullmq", "@bull-board/api"))
            if not has_bull:
                return

            # Look for a worker script in package.json first
            worker_script = next(
                (k for k in scripts if "worker" in k.lower()),
                None,
            )
            if worker_script:
                wlog = f"/tmp/worker-{port}.log"
                cmd = f"cd /workspace/{folder} && nohup npm run {worker_script} > {wlog} 2>&1 &"
                await self.sandbox.exec_command(cmd, timeout=10)
                logger.info("👷  [Worker] BullMQ worker started (%s) → %s", worker_script, wlog)
                return

            # Fallback: probe for common worker file locations
            worker_files = ["worker.js", "worker.ts", "src/worker.ts", "src/worker.js",
                            "workers/index.ts", "workers/index.js"]
            for wf in worker_files:
                if await self.sandbox.file_exists(f"{folder}/{wf}"):
                    wlog = f"/tmp/worker-{port}.log"
                    ext_cmd = "tsx" if wf.endswith(".ts") else "node"
                    cmd = f"cd /workspace/{folder} && nohup {ext_cmd} {wf} > {wlog} 2>&1 &"
                    await self.sandbox.exec_command(cmd, timeout=10)
                    logger.info("👷  [Worker] BullMQ worker started (%s) → %s", wf, wlog)
                    return

        elif fw in ("fastapi", "flask", "python", "django"):
            # Detect Celery — look for celery in requirements.txt or tasks.py
            has_tasks_py = await self.sandbox.file_exists(f"{folder}/tasks.py")
            has_celery_app = await self.sandbox.file_exists(f"{folder}/celery.py")
            if not (has_tasks_py or has_celery_app):
                return

            # Try to find the app name from the folder structure
            app_name = folder.replace("/", ".").lstrip(".")
            wlog = f"/tmp/celery-{port}.log"
            cmd = (
                f"cd /workspace/{folder} && "
                f"nohup celery -A {app_name} worker --loglevel=info > {wlog} 2>&1 &"
            )
            try:
                await self.sandbox.exec_command(cmd, timeout=10)
                logger.info("👷  [Worker] Celery worker started for %s → %s", folder, wlog)
            except Exception as exc:
                logger.warning("👷  [Worker] Celery start error (non-fatal): %s", exc)

    async def _start_backend_server(self, bk: dict) -> None:
        """
        V54: Launch a single backend server in the background. Each backend gets
        its own log file at /tmp/backend-{port}.log.
        Uses a self-restarting bash loop (max 5 restarts, 2s cooldown) so transient
        crashes (DB not ready yet, missing env var on first boot) don't leave the
        backend permanently dead without requiring manual intervention.
        """
        folder  = bk["folder"]
        fw      = bk["framework"]
        port    = bk["port"]
        logfile = f"/tmp/backend-{port}.log"

        # ── TypeScript pre-build ──
        # If backend folder has tsconfig.json + a 'build' script, compile first.
        # This covers Express+TS, Fastify+TS etc. that need tsc output to run.
        if fw in ("node", "express", "fastify", "hono"):
            has_tsconfig = await self.sandbox.file_exists(f"{folder}/tsconfig.json")
            if has_tsconfig:
                pkg = bk.get("pkg", {})
                scripts = pkg.get("scripts", {})
                if "build" in scripts:
                    logger.info("🔨  [Backend:%d] TypeScript build for %s …", port, folder)
                    try:
                        ts_build = await self.sandbox.exec_command(
                            f"cd /workspace/{folder} && npm run build 2>&1 | tail -10",
                            timeout=120,
                        )
                        if ts_build.exit_code != 0:
                            logger.warning("🔨  [Backend:%d] TS build warning: %s",
                                           port, (ts_build.stdout or "")[-300:])
                        else:
                            logger.info("🔨  [Backend:%d] TS build complete", port)
                    except Exception as _ts_exc:
                        logger.warning("🔨  [Backend:%d] TS build error (non-fatal): %s", port, _ts_exc)

        # Kill stale process on this port first
        try:
            await self.sandbox.exec_command(
                f"fuser -k {port}/tcp 2>/dev/null; sleep 0.3", timeout=5
            )
        except Exception:
            pass

        # Build inner start command (the payload that runs in the restart loop)
        # The loop: try up to 5 times with 2s delay between crashes.
        def _restart_loop(inner: str, lf: str, port: int) -> str:
            # Write a small restart-loop script to a tmp file and background it.
            return (
                f"nohup bash -c '"
                f"  for i in 1 2 3 4 5; do"
                f"    {inner};"
                f"    echo \"[backend:{port}] exited (attempt $i/5) — restarting in 2s\" >> {lf};"
                f"    sleep 2;"
                f"  done;"
                f"  echo \"[backend:{port}] gave up after 5 restarts\" >> {lf}"
                f"' >> {logfile} 2>&1 &"
            )

        if fw in ("node", "express", "fastify", "hono"):
            start_script = bk.get("start_script", "start")
            inner = f"PORT={port} NODE_ENV=development npm run {start_script}"
            cmd = f"cd /workspace/{folder} && " + _restart_loop(inner, logfile, port)

        elif fw == "django":
            inner = f"python3 manage.py runserver 0.0.0.0:{port}"
            cmd = f"cd /workspace/{folder} && " + _restart_loop(inner, logfile, port)

        elif fw == "fastapi":
            entry = bk.get("entry", "main")
            inner = f"uvicorn {entry}:app --host 0.0.0.0 --port {port} --reload"
            cmd = f"cd /workspace/{folder} && " + _restart_loop(inner, logfile, port)

        elif fw == "flask":
            entry = bk.get("entry", "app")
            inner = (
                f"FLASK_APP={entry}.py FLASK_DEBUG=1 FLASK_RUN_PORT={port} "
                f"flask run --host 0.0.0.0 --port {port}"
            )
            cmd = f"cd /workspace/{folder} && " + _restart_loop(inner, logfile, port)

        else:
            # Generic Python fallback
            entry = bk.get("entry", "main")
            inner = f"PORT={port} python3 {entry}.py"
            cmd = f"cd /workspace/{folder} && " + _restart_loop(inner, logfile, port)

        logger.info("🖥️  [Backend:%d] Starting %s in %s (auto-restart enabled) …", port, fw, folder)
        try:
            r = await self.sandbox.exec_command(cmd, timeout=12)
            bk["running"] = True
            bk["log"] = logfile
            bk["cmd"] = cmd
            logger.info("🖥️  [Backend:%d] Launched (log: %s)", port, logfile)
        except Exception as exc:
            logger.warning("🖥️  [Backend:%d] Start error: %s", port, exc)
            bk["running"] = False

    # ── Task Execution ───────────────────────────────────────

    async def execute_task(
        self,
        prompt: str,
        timeout: int = 300,
        mandate: str | None = None,
        use_gemini_cli: bool = True,
        task_label: str = "",
        preferred_tier: str = "pro",  # V73: default to Pro for all coding tasks
    ) -> TaskResult:
        """
        Execute a coding task inside the sandbox.

        Strategy:
            1. Write the prompt (with mandate) to a temp file in the sandbox
            2. Write GEMINI.md context file for Gemini CLI
            3. Run Gemini CLI with --yolo flag for autonomous execution
            4. Parse stdout/stderr for results
            5. Detect files changed via git diff

        Args:
            prompt: The coding task to execute.
            timeout: Max seconds for the entire task.
            mandate: Quality mandate to prepend. Default: config.ULTIMATE_MANDATE.
            use_gemini_cli: If True, use Gemini CLI. If False, just run the prompt as a shell command.

        Returns:
            TaskResult with status, output, files changed, errors.
        """
        result = TaskResult(prompt_used=prompt)
        start_time = time.time()

        # V62: Belt-and-suspenders quota guard — if quota is paused,
        # refuse to fire ANY Gemini CLI call. Return immediate failure
        # so the caller can retry after quota resets.
        try:
            from .retry_policy import get_daily_budget as _get_et_budget
            _et_budget = _get_et_budget()
            if _et_budget.quota_paused:
                result.success = False
                result.status = "failed"
                result.errors = ["Quota paused — refusing to fire Gemini CLI call. Will resume after midnight PT reset."]
                result.duration_s = 0.0
                logger.warning("⏸  [Executor] Quota paused — refusing task '%s'", task_label or prompt[:60])
                return result
        except Exception:
            pass

        try:
            if use_gemini_cli:
                # ── Host-side file change detection ──────────────────
                # Gemini CLI runs on the HOST, writing files to the host
                # project dir. The sandbox git_status() can't see those
                # changes (copy-mount mode). So we snapshot host file
                # mtimes before/after and diff them.
                host_project = "."
                if self.sandbox._active:
                    host_project = self.sandbox._active.project_path or "."

                def _snapshot_mtimes(root):
                    """Snapshot file modification times for a directory tree."""
                    mtimes = {}
                    try:
                        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
                            # Skip hidden dirs and common noise
                            dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.git')]
                            for fname in filenames:
                                fpath = os.path.join(dirpath, fname)
                                try:
                                    mtimes[fpath] = os.path.getmtime(fpath)
                                except OSError:
                                    pass
                    except Exception:
                        pass
                    return mtimes

                before_mtimes = _snapshot_mtimes(host_project)

                # V56: TS Regression Guard — capture pre-task error state.
                # After Gemini writes files, we diff against post-task state
                # and attach any regressions to TaskResult for the pool_worker
                # to inject a micro-fix DAG task. No retry here — quota-efficient.
                _ts_pre: dict[str, list[str]] | None = None
                try:
                    from .ts_regression_guard import (
                        capture_ts_errors, load_baseline,
                    )
                    _ts_pre = load_baseline(host_project)
                    if _ts_pre is None:
                        # First run — do a live capture
                        _ts_pre = await capture_ts_errors(host_project)
                except Exception as _tsg_pre_exc:
                    logger.debug("[TSGuard] Pre-capture skipped: %s", _tsg_pre_exc)
                    _ts_pre = None

                cli_dict = await self._execute_via_gemini_cli(
                    prompt, timeout, mandate,
                    task_label=task_label,
                    preferred_tier=preferred_tier,
                )

                # V41: Robust accessor — handles both dict (normal) and TaskResult (edge case)
                def _g(obj, key, default=None):
                    if isinstance(obj, dict):
                        return obj.get(key, default)
                    return getattr(obj, key, default)

                result.exit_code = _g(cli_dict, "exit_code", -1)
                result.output = _g(cli_dict, "stdout", "") or _g(cli_dict, "output", "")
                
                stderr = _g(cli_dict, "stderr", "")
                if stderr:
                    result.errors.append(stderr[:2000])
                
                if _g(cli_dict, "timed_out"):
                    result.status = "timeout"
                    result.errors.append(f"Gemini CLI timed out ({stderr or 'unknown reason'})")
                elif result.exit_code == 0:
                    result.status = "success"
                else:
                    result.status = "error"

                # V40: Track daily budget usage
                try:
                    from .retry_policy import get_daily_budget, get_quota_probe
                    get_daily_budget().record_request()
                    # V62: Also track usage for smart quota estimation
                    get_quota_probe().record_usage()
                except Exception:
                    pass

                after_mtimes = _snapshot_mtimes(host_project)

                # Detect new or modified files
                changed = []
                for fpath, mtime in after_mtimes.items():
                    if fpath not in before_mtimes or mtime > before_mtimes[fpath]:
                        # Make path relative to project root
                        try:
                            rel = os.path.relpath(fpath, host_project)
                        except ValueError:
                            rel = fpath
                        changed.append(rel)
                result.files_changed = changed

                # V57: Zero-change detection — if Gemini claimed success but zero files
                # were modified, flag it. pool_worker can decide to retry or skip.
                if result.status == "success" and not changed:
                    result.zero_change_warning = True
                    logger.warning(
                        "[Verify] Task succeeded but ZERO files changed — possible hallucination"
                    )

                # V57: Config-change detection — flag if config files were touched so
                # pool_worker can trigger a server liveness check.
                _config_patterns = (
                    "vite.config", "next.config", "package.json", "tailwind.config",
                    "postcss.config", "webpack.config", ".env", "tsconfig.json",
                )
                if any(
                    any(pat in f for pat in _config_patterns)
                    for f in changed
                ):
                    result.touched_config = True

                # V57: Self-critique flag — inject a review task ONLY for large,
                # genuinely complex changes. Guards:
                #   1. ≥8 source files changed (not configs/assets/docs)
                #   2. Not already a meta-task (SELF-REVIEW, HEALTH, TSFIX, lint)
                #   3. Not a very short/simple task description (<120 chars)
                # Threshold was previously 3 — far too low, causing a review for
                # almost every task and doubling quota usage unnecessarily.
                _src_exts = {'.ts', '.tsx', '.js', '.jsx', '.py', '.vue', '.svelte'}
                _src_changed = [f for f in changed if any(f.endswith(e) for e in _src_exts)]
                _is_simple_task = len(prompt.strip()) < 120
                # Guard: never inject a review task for meta-tasks (review/health/tsfix/lint/srvchk)
                _meta_markers = (
                    "[self-review]", "[health]", "[tsfix]", "[lint]", "[srvchk]",
                    "-review]", "-health]", "-tsfix]", "-lint]", "-srvchk]",
                    "self-review checklist", "local_action: health_check",
                )
                _is_meta_task = any(m in prompt[:300].lower() for m in _meta_markers)
                # V58: Self-reviews disabled — they were rubber-stamping and burning quota.
                # Re-enable by restoring: result.needs_self_review = True when len(_src_changed) >= 15
                if False:  # noqa: SIM210
                    result.needs_self_review = True
                    # Strip any nested "ORIGINAL TASK" chains to prevent prompt bloat.
                    # Only pass the immediate task prompt — not a review-of-review-of-review.
                    _clean_prompt = prompt
                    _ot_idx = prompt.find("\nORIGINAL TASK")
                    if _ot_idx != -1:
                        _clean_prompt = prompt[:_ot_idx]
                    result.self_review_context = (
                        f"ORIGINAL TASK (first 400 chars):\n{_clean_prompt[:400]}\n\n"
                        f"FILES MODIFIED:\n" +
                        "\n".join(f"  - {f}" for f in changed[:15])
                    )


                # V56: TS Regression Guard — post-capture and diff.
                if _ts_pre is not None and result.status == "success" and result.files_changed:
                    try:
                        from .ts_regression_guard import (
                            capture_ts_errors, check_regressions,
                            update_baseline_after_task,
                        )
                        _ts_post = await capture_ts_errors(host_project)
                        _regs = check_regressions(_ts_pre, _ts_post)
                        if _regs:
                            result.ts_regressions = _regs
                            logger.warning(
                                "[TSGuard] %d regression(s) on task — pool_worker will inject micro-fix: %s",
                                len(_regs), ", ".join(_regs[:3]),
                            )
                        else:
                            # Task is clean — update the baseline
                            update_baseline_after_task(host_project, _ts_pre, _ts_post)
                    except Exception as _tsg_post_exc:
                        logger.debug("[TSGuard] Post-capture skipped: %s", _tsg_post_exc)

            else:
                # Shell tasks run inside the sandbox — git diff works fine
                git_before = await self.tools.git_status()
                result = await self._execute_as_shell(prompt, timeout)
                git_after = await self.tools.git_status()
                result.files_changed = list(set(
                    git_after.modified + git_after.untracked
                ) - set(
                    git_before.modified + git_before.untracked
                ))

            result.duration_s = time.time() - start_time

        except SandboxError as exc:
            result.status = "error"
            result.errors.append(f"Sandbox error: {exc}")
            result.duration_s = time.time() - start_time
        except asyncio.TimeoutError:
            result.status = "timeout"
            result.errors.append(f"Task timed out after {timeout}s")
            result.duration_s = time.time() - start_time
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            result.status = "error"
            result.errors.append(f"Unexpected error: {exc}\n{tb}")
            result.duration_s = time.time() - start_time

        # Record history
        self._last_task_result = result
        self._task_history.append(result)
        if len(self._task_history) > 50:
            self._task_history = self._task_history[-50:]

        # V41: Promote timeout → partial if files were actually changed.
        # Tasks that produce output but don't finish cleanly should not be
        # retried — the work was done, Gemini just didn't exit gracefully.
        if result.status == "timeout" and result.files_changed:
            result.status = "partial"
            result.success = True
            logger.info(
                "Timeout→Partial: Task changed %d files before timeout — treating as partial success",
                len(result.files_changed),
            )
        elif result.status == "timeout" and not result.files_changed:
            # V60: Silent timeout — Gemini produced no output and was killed.
            # Flag for main.py to build a targeted retry prompt.
            result.silent_timeout = True
            logger.warning(
                "Silent timeout: Gemini produced no output and wrote 0 files — flagging for targeted retry."
            )

        # ── V51: Browser verification for frontend tasks ──────
        # After a successful frontend task, trigger a browser-based
        # verification pass via Gemini CLI's MCP browser tools.
        if (use_gemini_cli
                and result.status in ("success", "partial")
                and result.files_changed
                and not getattr(self, '_in_browser_verify', False)):
            try:
                verify_result = await self._browser_verify_if_needed(
                    prompt, result, timeout=min(timeout, 120),
                )
                if verify_result:
                    # Merge any additional files changed during verification
                    result.files_changed = list(set(
                        result.files_changed + verify_result.files_changed
                    ))
            except Exception as bv_exc:
                logger.debug("Browser verify skipped: %s", bv_exc)

        _task_label = task_label or ((prompt[:80] + "…") if len(prompt) > 80 else prompt)
        _task_label = _task_label.replace("\n", " ").strip()

        # ── LOCAL_ACTION: protocol — dispatch infra actions Gemini flagged ────
        # If Gemini's output contains LOCAL_ACTION: markers, execute them now.
        # This lets Gemini signal "npm install needed" / "restart server" without
        # having to run those commands itself (which it can't do in host mode).
        try:
            await self._dispatch_local_actions(result)
        except Exception as _la_exc:
            logger.debug("[LocalAction] Dispatch error (non-fatal): %s", _la_exc)

        # V58: Parse DAG_INJECT: markers — returned to _pool_worker for live planner expansion
        try:
            self._parse_dag_injections(result)
        except Exception as _di_exc:
            logger.debug("[DAGInject] Parse error (non-fatal): %s", _di_exc)

        logger.info(
            "Task completed: status=%s, duration=%.1fs, files_changed=%d, errors=%d — %s",
            result.status, result.duration_s, len(result.files_changed), len(result.errors),
            _task_label,
        )
        return result

    def _parse_dag_injections(self, result: "TaskResult") -> None:
        """V58: Parse DAG_INJECT: markers from Gemini output.

        A task can request live DAG expansion by emitting:
            DAG_INJECT: {"tasks": [{"task_id": "t_new", "description": "...", "dependencies": [...]}]}

        Multiple DAG_INJECT blocks are merged. Results are stored in
        ``result.dag_injections`` for ``_pool_worker`` to consume.
        """
        import re as _re, json as _json
        output = result.output or ""
        if "DAG_INJECT:" not in output:
            return  # Fast path

        pattern = _re.compile(
            r"DAG_INJECT:\s*(\{[^}]+(?:\{[^}]*\}[^}]*)?\}|\{[\s\S]+?\}(?=\s*(?:DAG_INJECT:|$|\n\n)))",
            _re.IGNORECASE,
        )
        # Simpler, more robust: find the JSON after each DAG_INJECT: token
        for match in _re.finditer(r"DAG_INJECT:\s*(\{[\s\S]+?\})(?=\s*(?:DAG_INJECT:|$))", output, _re.IGNORECASE):
            try:
                payload = _json.loads(match.group(1))
                tasks = payload.get("tasks", [])
                if isinstance(tasks, list):
                    result.dag_injections.extend(tasks)
                    logger.info("🔀  [DAGInject] Parsed %d task spec(s) from output.", len(tasks))
            except Exception as _e:
                logger.debug("🔀  [DAGInject] Failed to parse block: %s — %s", match.group(1)[:80], _e)

    async def _dispatch_local_actions(self, result: "TaskResult") -> None:
        """
        LOCAL_ACTION: protocol — parse Gemini output for infra action markers
        and execute them directly in the sandbox.

        Supported markers (case-insensitive, anywhere in result.output):
          LOCAL_ACTION: npm_install           — fresh npm install (wipes node_modules first)
          LOCAL_ACTION: restart_server        — kill+restart dev server
          LOCAL_ACTION: clear_vite_cache      — rm node_modules/.vite + restart server
          LOCAL_ACTION: dep_upgrade           — npm update + tsc verify
          LOCAL_ACTION: reinstall_vite        — full node_modules wipe + reinstall (chunk fix)

        Gemini should emit these when it detects that a local infra change is needed
        (e.g. after modifying package.json, tailwind.config, or identifying chunk corruption).
        Multiple markers in one output are all executed in order.
        """
        output = (result.output or "").lower()
        if "local_action:" not in output:
            return  # Fast path — nothing to do

        import re as _re
        markers = _re.findall(
            r"local_action:\s*([a-z0-9_]+)",
            output,
            flags=_re.IGNORECASE,
        )
        if not markers:
            return

        logger.info("🔧  [LocalAction] Gemini requested %d local action(s): %s", len(markers), markers)

        # V55: Update activity pill during local actions
        _state = getattr(self, 'state', None)
        def _set_local_op(label: str) -> None:
            if _state and hasattr(_state, 'set_current_operation'):
                _state.set_current_operation(label)

        _dispatched: set[str] = set()
        for action in markers:
            action = action.lower().strip()
            if action in _dispatched:
                continue  # deduplicate
            _dispatched.add(action)

            try:
                if action == "npm_install":
                    logger.info("🔧  [LocalAction] Running npm install …")
                    _set_local_op('📦 npm install…')
                    r = await self.sandbox.exec_command(
                        "cd /workspace && rm -rf node_modules/.vite .vite && "
                        "npm install --no-audit --no-fund --no-update-notifier "
                        "--legacy-peer-deps --loglevel=error 2>&1 | tail -10",
                        timeout=300,
                    )
                    _ok = r.exit_code == 0
                    logger.info("🔧  [LocalAction] npm install %s", "✅" if _ok else "⚠️ had issues")

                elif action == "restart_server":
                    logger.info("🔧  [LocalAction] Restarting dev server …")
                    _set_local_op('🔄 Restarting dev server…')
                    await self.sandbox.exec_command(
                        r"pkill -f 'vite\|next dev\|react-scripts\|serve' 2>/dev/null || true"|r"pkill -f 'vite\|next dev\|react-scripts\|serve' 2>/dev/null || true"|r"pkill -f 'vite\|next dev\|react-scripts\|serve' 2>/dev/null || true"|r"pkill -f 'vite\|next dev\|react-scripts\|serve' 2>/dev/null || true",
                        timeout=10,
                    )
                    await asyncio.sleep(2)
                    await self._start_dev_server()

                elif action == "clear_vite_cache":
                    logger.info("🔧  [LocalAction] Clearing Vite cache …")
                    _set_local_op('🧹 Clearing Vite cache…')
                    await self.sandbox.exec_command(
                        "rm -rf /workspace/node_modules/.vite /workspace/.vite 2>/dev/null; true",
                        timeout=10,
                    )
                    # Restart so the server picks up the cleared cache
                    await self.sandbox.exec_command(
                        "pkill -f 'vite' 2>/dev/null || true",
                        timeout=5,
                    )
                    await asyncio.sleep(2)
                    await self._start_dev_server()

                elif action == "dep_upgrade":
                    logger.info("🔧  [LocalAction] Running dep upgrade …")
                    _set_local_op('⬆ Upgrading deps…')
                    await self._try_upgrade_major_deps()

                elif action == "reinstall_vite":
                    logger.info("🔧  [LocalAction] Full node_modules wipe + reinstall (Vite chunk fix) …")
                    _set_local_op('🔧 Reinstalling node_modules…')
                    await self.sandbox.exec_command(
                        "rm -rf /workspace/node_modules 2>/dev/null; true",
                        timeout=30,
                    )
                    await self.sandbox.exec_command(
                        "cd /workspace && npm install --no-audit --no-fund "
                        "--no-update-notifier --legacy-peer-deps --loglevel=error 2>&1 | tail -10",
                        timeout=300,
                    )
                    logger.info("🔧  [LocalAction] reinstall_vite complete")

                else:
                    logger.warning("🔧  [LocalAction] Unknown action '%s' — skipping", action)

            except Exception as exc:
                logger.warning("🔧  [LocalAction] '%s' failed: %s", action, exc)
            finally:
                _set_local_op("")  # clear pill after each action

    async def _browser_verify_if_needed(
        self,
        original_prompt: str,
        task_result: "TaskResult",
        timeout: int = 120,
    ) -> "TaskResult | None":
        """
        V53: Post-task build/lint verification for frontend changes.

        Replaces the old browser-MCP verification (which was a no-op because
        Gemini CLI has no browser_navigate tools). Instead runs:
          1. TypeScript type-check (tsc --noEmit) — catches type errors instantly
          2. ESLint scan on changed files (if .eslintrc* present)
          3. Dev server log scan for new runtime errors
        Errors are surfaced in the result so the DAG can auto-fix them.

        Returns a TaskResult with error details if issues found, else None.
        """
        try:
            from .skills_loader import infer_category
        except ImportError:
            return None

        category = infer_category(original_prompt)
        if category not in ("frontend", "coding", "setup"):
            return None

        # Check if any changed files are frontend-related
        frontend_exts = {'.html', '.css', '.scss', '.jsx', '.tsx', '.vue', '.svelte', '.js', '.ts'}
        has_frontend_files = any(
            any(f.endswith(ext) for ext in frontend_exts)
            for f in task_result.files_changed
        )
        if not has_frontend_files:
            return None

        # Need the sandbox to run checks inside the container
        sandbox = getattr(self, 'sandbox', None)
        if not sandbox or not sandbox.is_running:
            return None


        logger.info("🔍  [Verify] Frontend files changed — running build/lint check")

        errors_found: list[str] = []

        # ── 1: TypeScript type-check ─────────────────────────
        ts_files = [f for f in task_result.files_changed if f.endswith(('.ts', '.tsx'))]
        if ts_files:
            ts_result = await sandbox.exec_command(
                "cd /workspace && npx --no-install tsc --noEmit --pretty false 2>&1 | head -60",
                timeout=min(timeout // 2, 60),
            )
            if ts_result.exit_code != 0 and ts_result.stdout.strip():
                ts_errors = ts_result.stdout.strip()[:1000]
                logger.info("🔍  [Verify] TypeScript errors found:\n%s", ts_errors[:300])
                errors_found.append(f"TypeScript errors:\n{ts_errors}")

        # ── 2: ESLint on changed files ───────────────────────
        eslint_files = [
            f for f in task_result.files_changed
            if f.endswith(('.js', '.jsx', '.ts', '.tsx'))
        ]
        if eslint_files and not errors_found:  # Skip if TS already found issues
            # Check if eslint is available
            has_eslint = await sandbox.exec_command(
                "test -f /workspace/.eslintrc.js || test -f /workspace/.eslintrc.json "
                "|| test -f /workspace/.eslintrc.cjs || test -f /workspace/eslint.config.js "
                "&& echo yes || echo no",
                timeout=5,
            )
            if has_eslint.stdout.strip() == "yes":
                file_args = " ".join(
                    f'"{f}"' for f in eslint_files[:10]  # Limit to avoid huge commands
                )
                lint_result = await sandbox.exec_command(
                    f"cd /workspace && npx --no-install eslint {file_args} "
                    "--max-warnings=0 --format=compact 2>&1 | head -40",
                    timeout=min(timeout // 3, 30),
                )
                if lint_result.exit_code != 0 and lint_result.stdout.strip():
                    lint_errs = lint_result.stdout.strip()[:800]
                    logger.info("🔍  [Verify] ESLint errors found:\n%s", lint_errs[:300])
                    errors_found.append(f"ESLint errors:\n{lint_errs}")

        if not errors_found:
            logger.info("🔍  [Verify] Build/lint check passed ✅")
            return None

        # Build a result with the errors so the DAG audit can pick them up
        tr = TaskResult(prompt_used="build/lint verification")
        tr.status = "error"
        tr.success = False
        tr.errors = errors_found
        tr.output = "\n\n".join(errors_found)
        tr.exit_code = 1
        tr.files_changed = []
        return tr

    async def _execute_via_gemini_cli(
        self,
        prompt: str,
        timeout: int,
        mandate: str | None,
        task_category: str = "",
        task_label: str = "",
        preferred_tier: str = "auto",  # 'flash', 'pro', or 'auto'
    ) -> TaskResult:
        """
        Execute a task via Gemini CLI running on the HOST.

        Architecture: "Host Intelligence, Sandboxed Hands"
            - Gemini CLI runs on the host OS (uses authenticated AI Ultra session)
            - Prompt is constructed on the host and piped to Gemini via stdin
            - Gemini output is parsed on the host
            - File changes and commands are pushed into the sandbox via bridges
            - ZERO credentials enter the container

        Prompt order (optimized for LLM primacy/recency attention):
            1. MANDATE (start — highest attention)
            2. WORKSPACE CONTEXT (middle — reference material)
            3. SKILLS (near end — actionable rules)
            4. TASK (end — highest attention)
        """
        result = TaskResult(prompt_used=prompt)

        # Build sections
        mandate = mandate or getattr(config, "ULTIMATE_MANDATE", "")

        # Gather sandbox context to feed to the host-side Gemini CLI
        # so it has awareness of the workspace state
        logger.info("Context: Gathering sandbox workspace state")
        sandbox_context = await self._gather_sandbox_context_for_prompt()
        if sandbox_context:
            logger.info("Context: Sandbox state collected (%d chars). Injecting into prompt.", len(sandbox_context))
        else:
            logger.info("Context: No sandbox state available -- sending bare prompt.")

        # ── Smart Skills Injection ─────────────────────────────
        # Use passed-in category from Ollama (if available) or infer from keywords.
        skills_text = ""
        try:
            from .skills_loader import select_skills, infer_category
            _category = task_category or infer_category(prompt)
            skills_text = select_skills(category=_category)
            if skills_text:
                logger.info(
                    "Skills: Injected for category '%s' (%d chars)",
                    _category, len(skills_text),
                )
        except Exception as _skills_exc:
            logger.debug("Skills: Injection skipped: %s", _skills_exc)

        # ── Assemble prompt (optimized order) ──────────────────
        # Mandate → Context → Skills → Task
        # LLMs attend most to the start and end of prompts.
        # Mandate anchors behavior (start). Task is the actionable request (end).
        # Skills sit near the task for relevance. Context is reference in the middle.
        sections = []
        if mandate:
            sections.append(mandate)
        if sandbox_context:
            sections.append(f"CURRENT WORKSPACE STATE:\n{sandbox_context}")
        # V56: Inject TS regression contract — shows which files are currently
        # clean and warns Gemini not to introduce new errors into them.
        try:
            from .ts_regression_guard import build_regression_contract, load_baseline
            _ctr_proj = (
                self.sandbox._active.project_path
                if self.sandbox and self.sandbox._active
                else None
            )
            if _ctr_proj:
                _ts_baseline = load_baseline(_ctr_proj)
                if _ts_baseline is not None:  # None = no tsconfig, skip
                    _contract = build_regression_contract(_ts_baseline)
                    if _contract:
                        sections.append(_contract)
        except Exception as _ctr_exc:
            logger.debug("[TSGuard] Contract injection skipped: %s", _ctr_exc)
        if skills_text:
            sections.append(skills_text)

        # V57: Error Pattern Memory — inject known error→fix pairs relevant to this task
        try:
            from .error_memory import build_error_memory_block
            _em_proj = (
                self.sandbox._active.project_path
                if self.sandbox and self.sandbox._active
                else None
            )
            if _em_proj:
                _em_block = build_error_memory_block(_em_proj, prompt)
                if _em_block:
                    sections.append(_em_block)
        except Exception as _em_exc:
            logger.debug("[ErrorMemory] Injection skipped: %s", _em_exc)

        # V57: Acceptance Criteria — if the node provides done_when criteria, append
        _ac = getattr(self, '_current_acceptance_criteria', '')
        if _ac:
            sections.append(
                f"## Acceptance Criteria (Definition of Done)\n\n"
                f"Your task is complete ONLY when ALL of the following are true:\n{_ac}\n\n"
                f"Do not proceed to unrelated work once these criteria are met."
            )

        sections.append(f"TASK:\n{prompt}")

        full_prompt = "\n\n---\n\n".join(sections)

        # ── Prompt Size Guard ──────────────────────────────────
        # Prevents oversized prompts from choking the Gemini CLI.
        # Truncates the middle (sandbox context) while preserving
        # the mandate header and task description.
        prompt_len = len(full_prompt)
        # V44: Gemini has 1M+ token context — raised from 15K/8K to 200K/100K.
        # The old 15K limit was truncating detailed task prompts unnecessarily.
        max_chars = getattr(config, "PROMPT_SIZE_MAX_CHARS", 200_000)
        warn_chars = getattr(config, "PROMPT_SIZE_WARN_CHARS", 100_000)

        if prompt_len > max_chars:
            logger.warning(
                "✂️  [Prompt Guard] Prompt too large (%d chars > %d max). Truncating.",
                prompt_len, max_chars,
            )
            # Keep the first 60% and last 20% of the budget, trim the middle
            head_budget = int(max_chars * 0.60)
            tail_budget = int(max_chars * 0.20)
            truncated_notice = (
                f"\n\n[… {prompt_len - head_budget - tail_budget} chars truncated "
                f"by Prompt Size Guard to fit within {max_chars} char limit …]\n\n"
            )
            full_prompt = (
                full_prompt[:head_budget]
                + truncated_notice
                + full_prompt[-tail_budget:]
            )
            logger.info(
                "✂️  [Prompt Guard] Truncated: %d → %d chars.",
                prompt_len, len(full_prompt),
            )
        elif prompt_len > warn_chars:
            logger.warning(
                "⚠️  [Prompt Guard] Large prompt (%d chars > %d warn threshold). "
                "Consider breaking the task into smaller chunks.",
                prompt_len, warn_chars,
            )

        # Run Gemini CLI on the HOST (not inside the container)
        _used_tier = preferred_tier
        cmd_result = await self._run_gemini_on_host(
            full_prompt, timeout, preferred_tier=preferred_tier
        )

        # ── Flash → Pro auto-upgrade on timeout or hard failure ──────────────
        # If Flash was used and it timed out OR exited non-zero without any
        # API-quota explanation, retry immediately with Pro. Flash has a lower
        # context ceiling and is more likely to stall on large prompts.
        _used_model = cmd_result.get("model", "") if isinstance(cmd_result, dict) else ""
        _flash_model = getattr(config, "GEMINI_DEFAULT_FLASH", "")
        _used_flash = (_used_tier == "flash" or _used_model == _flash_model)
        _was_timed_out = bool(cmd_result.get("timed_out") if isinstance(cmd_result, dict) else False)
        _was_error = (
            (cmd_result.get("exit_code", 0) if isinstance(cmd_result, dict) else 0) not in (0, None)
            and not cmd_result.get("timed_out")
        )
        if _used_flash and (_was_timed_out or _was_error):
            # Record Flash failure timestamp for session-level skip
            self._flash_failed_at = time.time()
            # Report outcome to the complexity router for adaptive learning
            try:
                from .retry_policy import get_complexity_router
                get_complexity_router().record_outcome("flash", success=False)
            except Exception:
                pass
            logger.warning(
                "⏱️  [Gemini] Flash %s — upgrading to Pro and retrying …",
                "timed out" if _was_timed_out else "failed",
            )
            cmd_result = await self._run_gemini_on_host(
                full_prompt, timeout, preferred_tier="pro"
            )
            _used_tier = "pro"
        elif not _was_timed_out and not _was_error:
            # Successful call — report outcome to router for adaptive learning
            try:
                from .retry_policy import get_complexity_router
                _tier_name = "flash" if _used_flash else "pro"
                get_complexity_router().record_outcome(_tier_name, success=True)
            except Exception:
                pass

        # V41: Robust accessor — handles both dict (normal) and TaskResult (edge case)
        def _g(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        result.exit_code = _g(cmd_result, "exit_code", -1)
        result.output = _g(cmd_result, "stdout", "") or _g(cmd_result, "output", "")

        if _g(cmd_result, "timed_out"):
            result.status = "timeout"
            _kill_reason = _g(cmd_result, "kill_reason", "inactivity or outer timeout")
            _elapsed = _g(cmd_result, "elapsed_s", timeout)
            logger.warning(
                "⏱️  [Gemini] TIMEOUT: killed after %.0fs (%s) — task limit was %ds",
                _elapsed, _kill_reason, timeout,
            )
            result.errors.append(f"Gemini CLI timed out after {_elapsed:.0f}s ({_kill_reason})")
        elif result.exit_code != 0:
            result.status = "error"
            result.errors.append(f"Gemini CLI exited with code {result.exit_code}")
            stderr = _g(cmd_result, "stderr", "")
            if stderr:
                result.errors.append(stderr[:1000])

            # V41 FIX: Detect rate-limiting and quota errors and trigger model failover.
            # The previous code only looked for "TerminalQuotaError" and
            # "Error when talking to Gemini API" — but real 429 errors say
            # "rateLimitExceeded", "RESOURCE_EXHAUSTED", or "MODEL_CAPACITY_EXHAUSTED".
            # Also, report_failure() was called with "api_error" instead of the
            # actual model name, so the real model was never cooled down.
            _current_model = _g(cmd_result, "model", "auto")
            if stderr:
                _is_quota_error = "TerminalQuotaError" in stderr
                _is_rate_limit = any(p in stderr for p in [
                    "RESOURCE_EXHAUSTED", "rateLimitExceeded",
                    "MODEL_CAPACITY_EXHAUSTED", "status 429",
                    "No capacity available",
                ])

                if _is_quota_error:
                    try:
                        import re as _re
                        from .retry_policy import get_failover_chain
                        _fc = get_failover_chain()
                        # V62: Prefer retryDelayMs (exact ms) over the text 'reset after Xh Ym'
                        _delay_ms_match = _re.search(r'retryDelayMs:\s*([\d.]+)', stderr)
                        if _delay_ms_match:
                            _cooldown = int(float(_delay_ms_match.group(1)) / 1000)
                        else:
                            _time_match = _re.search(
                                r'reset after\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?',
                                stderr,
                            )
                            if _time_match:
                                _hours = int(_time_match.group(1) or 0)
                                _mins = int(_time_match.group(2) or 0)
                                _secs = int(_time_match.group(3) or 0)
                                _cooldown = _hours * 3600 + _mins * 60 + _secs
                            else:
                                _cooldown = 3600
                        _fc.report_quota_exhausted(_current_model, _cooldown)
                        # V62: Store exact cooldown on result so pool_worker can use it
                        result._quota_cooldown_s = _cooldown
                        # V62: Also update probe snapshots with bucket-spreading.
                        # If gemini-3.1-pro-preview is exhausted, mark its bucket-mates too.
                        try:
                            import time as _time_qe
                            from .retry_policy import get_quota_probe as _get_qp_qe
                            _qp_qe = _get_qp_qe()
                            _now_qe = _time_qe.time()
                            _bucket_map_qe = getattr(config, 'QUOTA_MODEL_TO_BUCKET', {})
                            _buckets_cfg_qe = getattr(config, 'QUOTA_BUCKETS', {})
                            _bkt_qe = _bucket_map_qe.get(_current_model, "")
                            _mark_models_qe = [_current_model]
                            if _bkt_qe and _bkt_qe in _buckets_cfg_qe:
                                _mark_models_qe = _buckets_cfg_qe[_bkt_qe].get("models", [_current_model])
                            for _qm in _mark_models_qe:
                                _qp_qe._snapshots[_qm] = {
                                    "remaining_pct": 0.0,
                                    "resets_in_s": _cooldown,
                                    "resets_at": _now_qe + _cooldown,
                                    "probed_at": _now_qe,
                                    "source": "quota_exhausted",
                                }
                            _qp_qe._last_probe_at = _now_qe
                            _qp_qe._save_state()
                        except Exception:
                            pass
                        logger.warning(
                            "⚡  [Gemini] Quota exhausted on %s — cooldown %ds (resets in %dh%dm). Failing over.",
                            _current_model, _cooldown, _cooldown // 3600, (_cooldown % 3600) // 60,
                        )
                    except Exception:
                        pass
                elif _is_rate_limit:
                    try:
                        from .retry_policy import get_failover_chain
                        _fc = get_failover_chain()
                        _fc.report_failure(_current_model)
                        logger.warning(
                            "⚡  [Gemini] Rate limited on model %s — reported to failover chain.",
                            _current_model,
                        )
                    except Exception:
                        pass
                elif "Error when talking to Gemini API" in stderr:
                    try:
                        from .retry_policy import get_failover_chain
                        _fc = get_failover_chain()
                        _fc.report_failure(_current_model)
                        logger.warning(
                            "⚡  [Gemini] API error on model %s — reported to failover chain.",
                            _current_model,
                        )
                    except Exception:
                        pass

                # Tag result so pool worker can skip auto-fix on rate limits
                if _is_quota_error or _is_rate_limit:
                    result._rate_limited = True
                    # V62: Also try to extract retryDelayMs for rate-limit (non-quota) errors
                    if _is_rate_limit and not hasattr(result, '_quota_cooldown_s'):
                        try:
                            import re as _re_rl
                            _rl_delay = _re_rl.search(r'retryDelayMs:\s*([\d.]+)', stderr)
                            if _rl_delay:
                                result._quota_cooldown_s = int(float(_rl_delay.group(1)) / 1000)
                        except Exception:
                            pass
        else:
            result.status = "success"
            _task_hint = task_label or ((prompt[:80] + "…") if len(prompt) > 80 else prompt)
            _task_hint = _task_hint.replace("\n", " ").strip()
            _n_files = len(result.files_changed) if result.files_changed else 0
            logger.info(
                "✅  [Gemini] Task completed (exit_code=0, files=%d). %s",
                _n_files, _task_hint,
            )

        # Check for error patterns in output — may downgrade success → error
        error_patterns = [
            r"(?i)error:",
            r"(?i)traceback \(most recent call last\)",
            r"(?i)syntaxerror:",
            r"(?i)typeerror:",
            r"(?i)referenceerror:",
        ]
        for pattern in error_patterns:
            if re.search(pattern, result.output):
                if result.status == "success":
                    result.status = "partial"  # Success with warnings
                break

        return result

    # ── System Path Discovery ──────────────────────────────────
    _gemini_cli_path: str | None = None  # Class-level cache

    def _discover_gemini_cli(self) -> str:
        """
        Auto-discover the Gemini CLI binary on this system.
        Probes PATH, common npm global install locations, and npx fallback.
        Caches the result so it only runs once per session.
        """
        if HeadlessExecutor._gemini_cli_path:
            return HeadlessExecutor._gemini_cli_path

        # 1. Probe npm global dirs FIRST (Windows .cmd files need full path)
        logger.info("🔍  [1/4] Probing npm global directories for Gemini CLI …")
        candidates = []
        if os.name == "nt":
            npm_global = os.path.join(os.environ.get("APPDATA", ""), "npm")
            candidates.append(os.path.join(npm_global, "gemini.cmd"))
            local_app = os.environ.get("LOCALAPPDATA", "")
            if local_app:
                candidates.append(os.path.join(local_app, "npm", "gemini.cmd"))
        else:
            home = os.path.expanduser("~")
            candidates.extend([
                os.path.join(home, ".npm-global", "bin", "gemini"),
                "/usr/local/bin/gemini",
                os.path.join(home, ".nvm", "current", "bin", "gemini"),
            ])

        for path in candidates:
            if os.path.isfile(path):
                HeadlessExecutor._gemini_cli_path = path
                logger.info("🔍 Gemini CLI found: %s", path)
                return path

        # 2. Try PATH (returns full resolved path)
        logger.info("🔍  [2/4] Checking system PATH for Gemini CLI …")
        found = shutil.which("gemini")
        if found:
            HeadlessExecutor._gemini_cli_path = found
            logger.info("🔍 Gemini CLI (PATH): %s", found)
            return found

        # 3. (npm dirs already probed in step 1)

        # 4. Fallback: use npx to invoke it
        logger.info("🔍  [3/4] Trying npx fallback …")
        npx = shutil.which("npx")
        if npx:
            HeadlessExecutor._gemini_cli_path = "npx"
            logger.warning("🔍 Gemini CLI not found directly, falling back to npx")
            return "npx"

        # 5. Last resort — return "gemini" and hope for the best
        logger.error("🔍 Gemini CLI could not be discovered on this system")
        return "gemini"

    async def _run_gemini_on_host(
        self,
        prompt: str,
        timeout: int,
        preferred_tier: str = "pro",
    ) -> dict:
        """
        Run Gemini CLI on the HOST OS using the user's authenticated session.

        The prompt is piped via stdin. The Gemini CLI has access to the
        host's credentials (AI Ultra session) but the sandbox does not.

        The --sandbox flag tells Gemini CLI to use its own sandboxing,
        but we override the working directory to point at the bind-mounted
        project path so Gemini can read/write files that are visible
        inside the container.

        Returns:
            dict with keys: exit_code, stdout, stderr, timed_out
        """
        # Model selection: use TaskComplexityRouter to save Flash for lightweight tasks.
        # preferred_tier='flash' → always use Flash (lint, health checks, phase yes/no)
        # preferred_tier='pro'   → always use failover chain (architecture, multi-file)
        # preferred_tier='auto'  → router classifies the prompt and decides
        _default_model = getattr(config, "GEMINI_CLI_MODEL", "auto")
        _pro_only = getattr(config, "PRO_ONLY_CODING", False)
        try:
            from .retry_policy import get_failover_chain, get_complexity_router, get_quota_probe
            _fc = get_failover_chain()
            _router = get_complexity_router()

            if preferred_tier == "flash":
                # V55: Skip Flash if it failed within the last 30 minutes this session.
                # Flash exits with code 1 on quota/auth issues every time in that window,
                # wasting ~35s per call. Go straight to Pro instead.
                _flash_age = time.time() - getattr(self, '_flash_failed_at', 0.0)
                _skip_flash = _flash_age < getattr(self, '_FLASH_SKIP_WINDOW', 1800)
                if _skip_flash:
                    model = _fc.get_active_model() or _default_model
                    logger.debug(
                        "🎯  [Model] Flash recently failed (%.0fs ago) — routing to Pro instead: %s",
                        _flash_age, model,
                    )
                else:
                    model = config.GEMINI_DEFAULT_FLASH
                    logger.debug("🎯  [Model] Routing to Flash (caller-requested): %s", model)
            elif preferred_tier == "pro":
                # V74: Request pro_only when PRO_ONLY_CODING is enabled
                model = _fc.get_active_model(pro_only=_pro_only)
                if model is None and _pro_only:
                    # Pause signal — Pro quota exhausted, refuse to fall back
                    logger.warning(
                        "⏸  [V74] Pro model unavailable. PRO_ONLY_CODING active — "
                        "returning rate-limited failure so caller can wait."
                    )
                    return {
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "Pro model quota exhausted. PRO_ONLY_CODING active — pausing until quota resets.",
                        "timed_out": False,
                        "_rate_limited": True,
                    }
                model = model or _default_model
                logger.debug("🎯  [Model] Routing to Pro (caller-requested): %s", model)
            else:
                # V73/V74: Auto now routes through Pro failover chain (same as pro).
                # Coding tasks should always use Pro. Flash/Lite are only for
                # explicit preferred_tier='flash' callers (lint, health checks).
                model = _fc.get_active_model(pro_only=_pro_only)
                if model is None and _pro_only:
                    logger.warning(
                        "⏸  [V74] Pro model unavailable (auto tier). PRO_ONLY_CODING active — "
                        "returning rate-limited failure so caller can wait."
                    )
                    return {
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "Pro model quota exhausted. PRO_ONLY_CODING active — pausing until quota resets.",
                        "timed_out": False,
                        "_rate_limited": True,
                    }
                model = model or _default_model
                logger.debug("🎯  [Model] Auto routing via Pro chain → %s", model)

            # V62/V73: Quota Probe — use the best model that has quota,
            # but restricted to the same bucket as the preferred tier.
            # This prevents coding tasks (preferred_tier='pro') from falling
            # back to Flash/Lite models when Pro quota is low.
            try:
                _qp = get_quota_probe()
                # Restrict probe candidates to the same bucket
                if preferred_tier in ("pro", "auto"):
                    _probe_candidates = list(config.QUOTA_BUCKETS.get("pro", {}).get("models", []))
                elif preferred_tier == "flash":
                    _probe_candidates = list(config.QUOTA_BUCKETS.get("flash", {}).get("models", []))
                else:
                    _probe_candidates = list(config.GEMINI_MODEL_PROBE_LIST)
                _best = _qp.get_best_model_with_quota(_probe_candidates) if _probe_candidates else None
                if _best and _best != model:
                    if _qp.is_exhausted(model):
                        logger.info(
                            "📊  [QuotaProbe] %s has no quota → using best in %s bucket: %s",
                            model, preferred_tier, _best,
                        )
                        model = _best
                    else:
                        logger.debug(
                            "📊  [QuotaProbe] %s still has quota — keeping it.",
                            model,
                        )
            except Exception as _qp_exc:
                logger.debug("📊  [QuotaProbe] Probe check skipped: %s", _qp_exc)
        except Exception:
            model = _default_model
        gemini_cmd = self._discover_gemini_cli()

        # Resolve the project directory on the host
        # If bind-mounted, the project directory is the host path itself
        project_path = "."
        if self.sandbox._active:
            project_path = self.sandbox._active.project_path or "."

        # V59: Layer 2 (revised) — File-reference hint.
        # We detect files mentioned in the prompt and prepend a short instruction
        # telling Gemini to READ them itself.  We do NOT dump their content into
        # the prompt: Gemini has full filesystem access and doing so was causing
        # silent hangs due to context bloat (52KB+ prepended to every prompt).
        prompt = _inject_current_file_states(prompt, project_path)

        # Build the Gemini CLI command
        if gemini_cmd == "npx":
            cmd_args = [
                "npx", "-y", "@google/gemini-cli",
                "--model", model,
                "--yolo",
            ]
        else:
            cmd_args = [
                gemini_cmd,
                "--model", model,
                "--yolo",  # Auto-approve all file operations
            ]

        # V60 (CONFIRMED): Two-step Plan Mode for complex tasks.
        # Research (March 2026) confirmed: `gemini --approval-mode=plan` works
        # headlessly — accepts prompt via stdin, outputs the generated plan to
        # stdout, and cannot modify files (read-only).  This lets us:
        #   Step 1: Run with --approval-mode=plan → capture the plan from stdout
        #   Step 2: Run with --yolo, injecting that plan as context
        # Falls back to a structured pre-prompt preamble if Step 1 fails.
        _is_complex = (
            config.PLAN_MODE_ENABLED
            and len(prompt) > config.PLAN_MODE_CHAR_THRESHOLD
            and not any(
                kw in prompt[:120].upper()
                for kw in (
                    "MERGE RECOVERY", "[SELF-REVIEW]", "[HEALTH]",
                    "BUILD HEALTH", "[TSFIX]", "[LINT]", "PLAN FIRST",
                )
            )
        )
        if _is_complex:
            _plan_text = None
            # V69/S1: Skip Plan Mode during shutdown — saves ~90s per task.
            # Check if supervisor state has stop_requested set.
            _shutdown_active = False
            try:
                from . import _supervisor_state_ref
                _shutdown_active = getattr(_supervisor_state_ref, 'stop_requested', False)
            except Exception:
                pass

            if _shutdown_active:
                logger.info("📋  [Plan Mode] Skipped — safe stop active. Using preamble fallback.")
            else:
                _plan_args = [a for a in cmd_args]  # copy
                # Replace --yolo with --approval-mode=plan for the planning pass
                _plan_args = [a for a in _plan_args if a != "--yolo"]
                _plan_args += ["--approval-mode=plan"]
                logger.info("📋  [Plan Mode] Step 1: generating plan via --approval-mode=plan (%d chars)", len(prompt))
                try:
                    _plan_proc = await asyncio.create_subprocess_exec(
                        *_plan_args,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=project_path or None,
                        env=dict(
                            os.environ,
                            GEMINI_NONINTERACTIVE="1",
                            GIT_TERMINAL_PROMPT="0",
                            CI="true",
                            NODE_OPTIONS=(
                                os.environ.get("NODE_OPTIONS", "") + " --max-old-space-size=4096"
                            ).strip(),
                        ),
                    )
                    _plan_out, _ = await asyncio.wait_for(
                        _plan_proc.communicate(input=prompt.encode("utf-8")),
                        timeout=45,  # V68: Reduced from 90s — preamble fallback is effective
                    )
                    _plan_text = (_plan_out or b"").decode("utf-8", errors="replace").strip()
                    if _plan_text and len(_plan_text) > 100:
                        logger.info("📋  [Plan Mode] Plan generated (%d chars) → injecting into Step 2: Execute", len(_plan_text))
                    else:
                        logger.warning("📋  [Plan Mode] Plan output too short (%d chars) — using preamble fallback", len(_plan_text or ""))
                        _plan_text = None
                except Exception as _plan_exc:
                    logger.debug("📋  [Plan Mode] Step 1 error (non-fatal) — using preamble fallback: %s", _plan_exc)
                    _plan_text = None

            # Build Step 2 prompt: inject plan or use preamble fallback
            if _plan_text:
                prompt = (
                    f"## IMPLEMENTATION PLAN (generated in planning phase)\n\n"
                    f"{_plan_text}\n\n"
                    f"## EXECUTE THE PLAN ABOVE\n\n"
                    f"Now implement everything described in the plan above. "
                    f"Original task for reference:\n\n{prompt}"
                )
            else:
                # Preamble fallback — same effect, no separate subprocess
                prompt = (
                    "## PLAN FIRST — EXECUTE AFTER\n"
                    "Before writing any code, output a brief plan (5–10 bullet points) covering:\n"
                    "- Which files will be created/modified\n"
                    "- The key logic/API changes\n"
                    "- Any risk areas or edge cases\n"
                    "Then proceed immediately to implement everything in that plan.\n"
                    "Do NOT stop after the plan — complete the full implementation.\n\n"
                ) + prompt
            logger.info("📋  [Plan Mode] Step 2: executing with plan (%d chars)", len(prompt))

        C = getattr(config, "ANSI_CYAN", "")
        R = getattr(config, "ANSI_RESET", "")
        logger.info("🚀  [Gemini] Launching CLI on HOST: model=%s, cwd=%s", model, project_path)
        print(f"  {C}🧠 Host Intelligence: Gemini CLI (model={model}) …{R}")


        # V58: Guard — if project_path doesn't exist on disk, Gemini silently
        # falls back to its own cwd (the supervisor dir) and codes the wrong project.
        if project_path and not os.path.isdir(project_path):
            result.status = "error"
            result.errors.append(
                f"Project directory does not exist on disk: {project_path!r}. "
                "Stop the session, verify the project path, and re-launch."
            )
            result.duration_s = time.time() - start_time
            return result

        try:
            # Windows .cmd files cannot be executed via create_subprocess_exec.
            # Use create_subprocess_shell for .cmd/.bat wrappers.
            use_shell = os.name == "nt" and gemini_cmd.lower().endswith((".cmd", ".bat"))

            import time as _time
            import random as _random

            # V60: Launch jitter — stagger parallel Gemini subprocess starts.
            # V68: Only apply when OTHER workers are already running.
            # First task to start skips the jitter entirely.
            import threading
            _active_count = getattr(threading, '_gemini_active_count', 0)
            if _active_count > 0:
                _jitter_s = _random.uniform(1.5, 4.0)
                logger.debug("🚀  [Gemini] Launch jitter: sleeping %.1fs (%d other workers active) …", _jitter_s, _active_count)
                await asyncio.sleep(_jitter_s)
            threading._gemini_active_count = getattr(threading, '_gemini_active_count', 0) + 1

            _gemini_start = _time.monotonic()
            logger.info("🚀  [Gemini] Spawning subprocess (shell=%s) …", use_shell)

            # V60: Non-interactive environment variables.
            # Gemini CLI v0.32+ 'updated auth handshake' can invoke rl.question()
            # (an interactive readline prompt) when it detects a non-TTY stdin,
            # causing the subprocess to block indefinitely on a user prompt that
            # never arrives (GitHub issue #20854). Setting these env vars forces
            # fully non-interactive behaviour:
            #   GEMINI_NONINTERACTIVE=1 — explicit non-interactive flag for CLI
            #   GIT_TERMINAL_PROMPT=0   — prevents git from issuing auth prompts
            #   CI=true                 — tells node tooling it's running headless
            # V60: NODE_OPTIONS — Gemini CLI is a Node.js process; on large projects
            #   it can OOM with the default 512MB heap.  4GB gives plenty of runway.
            _subproc_env = os.environ.copy()
            _subproc_env["GEMINI_NONINTERACTIVE"] = "1"
            _subproc_env["GIT_TERMINAL_PROMPT"] = "0"
            _subproc_env["CI"] = "true"
            _subproc_env["NODE_OPTIONS"] = (
                os.environ.get("NODE_OPTIONS", "") + " --max-old-space-size=4096"
            ).strip()

            if use_shell:
                # Build a shell command string with proper quoting
                shell_cmd = " ".join(f'"{a}"' if " " in a else a for a in cmd_args)
                proc = await asyncio.create_subprocess_shell(
                    shell_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=project_path,
                    env=_subproc_env,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd_args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=project_path,
                    env=_subproc_env,
                )

            # V41: Activity-based timeout — only kill after sustained silence.
            # Instead of hard-killing after N total seconds, read stdout
            # incrementally and reset the timer on every chunk of output.
            #
            # Gemini CLI goes silent during:
            #   - Reading large codebases (1M token context window)
            #   - Planning multi-file changes between writes
            #   - Processing file operations with --yolo auto-confirm
            #   - API round-trips (internal timeout: 10 min / 600,000ms)
            #
            # V60: Raised inactivity tolerance — real-world profiling showed the CLI
            # takes 60-90s before its first stdout byte on Windows under parallel load.
            # The previous 300s medium limit was too tight when 3 workers competed for
            # Node startup + credential load + large API payloads simultaneously.
            #
            # Scale inactivity tolerance with task complexity:
            #   Simple (timeout < 300):     240s silence before kill  (was 180s)
            #   Medium (timeout 300-599):   480s silence before kill  (was 300s)
            #   Complex (timeout >= 600):   900s silence before kill  (was 600s)
            if timeout >= 600:
                INACTIVITY_TIMEOUT = 900  # Complex: 15 min — covers worst-case parallel contention
            elif timeout >= 300:
                INACTIVITY_TIMEOUT = 480  # Medium: 8 min (was 5 min)
            else:
                INACTIVITY_TIMEOUT = 240  # Simple: 4 min floor (was 3 min)
            MAX_TOTAL_TIMEOUT = 1800   # Absolute ceiling: 30 min (unchanged)

            # Feed prompt to stdin, then close
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            logger.info(
                "🚀  [Gemini] Subprocess started (PID=%s). Reading output "
                "(inactivity=%ds, max=%ds) …",
                proc.pid, INACTIVITY_TIMEOUT, MAX_TOTAL_TIMEOUT,
            )

            # Read stdout incrementally with inactivity timeout
            chunks: list[bytes] = []
            _total_start = _time.monotonic()
            _last_log = _total_start
            _total_bytes = 0
            _timed_out = False
            _timeout_reason = ""

            while True:
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(8192),
                        timeout=INACTIVITY_TIMEOUT,
                    )
                    if not chunk:
                        break  # EOF — Gemini finished naturally
                    chunks.append(chunk)
                    _total_bytes += len(chunk)

                    # Heartbeat log every 30s
                    _now = _time.monotonic()
                    if _now - _last_log >= 30:
                        _elapsed = _now - _total_start
                        logger.info(
                            "🚀  [Gemini] Still working: %d bytes received, %.0fs elapsed",
                            _total_bytes, _elapsed,
                        )
                        _last_log = _now

                except asyncio.TimeoutError:
                    # No output for INACTIVITY_TIMEOUT seconds — Gemini is stuck
                    _timed_out = True
                    _timeout_reason = f"silent for {INACTIVITY_TIMEOUT}s"
                    logger.warning(
                        "⏱️  [Gemini] TIMEOUT: No output for %ds — killing (PID=%s)",
                        INACTIVITY_TIMEOUT, proc.pid,
                    )
                    break

                # Absolute ceiling guard
                if _time.monotonic() - _total_start > MAX_TOTAL_TIMEOUT:
                    _timed_out = True
                    _timeout_reason = f"exceeded {MAX_TOTAL_TIMEOUT}s total"
                    logger.warning(
                        "⏱️  [Gemini] TIMEOUT: Exceeded %ds total — killing (PID=%s)",
                        MAX_TOTAL_TIMEOUT, proc.pid,
                    )
                    break

            if _timed_out:
                try:
                    proc.kill()
                except Exception:
                    pass
                # Collect any partial output
                _partial = b"".join(chunks).decode("utf-8", errors="replace")
                _total_elapsed = _time.monotonic() - _total_start
                return {
                    "exit_code": -1,
                    "stdout": _partial,
                    "stderr": f"Timed out: {_timeout_reason}",
                    "timed_out": True,
                    "kill_reason": _timeout_reason,
                    "elapsed_s": _total_elapsed,
                    "model": model,
                    "total_bytes": _total_bytes,
                }

            # Gemini finished — collect results
            stdout_bytes = b"".join(chunks)
            stderr_bytes = await proc.stderr.read()
            await proc.wait()
            _gemini_elapsed = _time.monotonic() - _gemini_start
            logger.info(
                "🚀  [Gemini] Response received: %.1fs, exit=%s, stdout=%d bytes, stderr=%d bytes",
                _gemini_elapsed, proc.returncode, len(stdout_bytes), len(stderr_bytes),
            )

            _stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            _stderr_str = stderr_bytes.decode("utf-8", errors="replace")

            # V62: Post-execution quota probe — multi-layer approach.
            # Strategy 1 (PRIMARY): Run dedicated /stats probe against the CLI.
            #   Spawns `gemini`, pipes `/stats` + `/quit`, captures REAL API-level
            #   quota data. Zero cost — /stats is a built-in info command.
            #   Throttled: only runs every 5 task completions to avoid excess spawns.
            # Strategy 2: Passive regex scan for /stats-format lines in CLI output.
            # Strategy 3: Rate-limit signal detection from stderr (429, RESOURCE_EXHAUSTED).
            try:
                from .retry_policy import get_quota_probe
                _qp = get_quota_probe()

                # Strategy 0: SUCCESS-BASED RECOVERY.
                # If this task succeeded (exit_code=0) but the model was previously
                # marked as exhausted (0%), clear the stale data. The model clearly
                # has quota since we just used it. Google's rolling windows mean quota
                # recovers gradually — the 429's retryDelayMs is the FULL reset time,
                # not when quota starts becoming available again.
                if proc.returncode == 0:
                    import time as _time_s0
                    _now_s0 = _time_s0.time()
                    _snap_s0 = _qp._snapshots.get(model)
                    if _snap_s0 and _snap_s0.get("remaining_pct", 100) <= 0:
                        # This model was marked exhausted but just worked — recover it
                        # and its bucket-mates.
                        _bucket_map_s0 = getattr(config, 'QUOTA_MODEL_TO_BUCKET', {})
                        _buckets_s0 = getattr(config, 'QUOTA_BUCKETS', {})
                        _bkt_s0 = _bucket_map_s0.get(model, "")
                        _recover_models = [model]
                        if _bkt_s0 and _bkt_s0 in _buckets_s0:
                            _recover_models = _buckets_s0[_bkt_s0].get("models", [model])
                        for _rm in _recover_models:
                            _old_snap = _qp._snapshots.get(_rm, {})
                            _qp._snapshots[_rm] = {
                                "remaining_pct": 50.0,  # Conservative — we know >0% but not exact
                                "resets_in_s": _old_snap.get("resets_in_s", 0),
                                "resets_at": _old_snap.get("resets_at", 0),
                                "probed_at": _now_s0,
                                "source": "success_recovery",
                                "auto_reset": False,
                            }
                        _qp._save_state()
                        logger.info(
                            "📊  [QuotaProbe] Success recovery: %s was at 0%% but just succeeded — "
                            "cleared stale exhaustion for %d model(s) in bucket '%s'.",
                            model, len(_recover_models), _bkt_s0 or "unknown",
                        )

                # Record usage for call-count estimation (250 RPD for Pro)
                _qp.record_usage(model)

                # Strategy 1: Persistent PTY /stats probe — reuses the open
                # CLI session (~2s per call after initial ~25s spawn at startup).
                _sp_parsed = _qp.run_stats_probe()
                if _sp_parsed:
                    logger.info("📊  [QuotaProbe] PTY stats: %d models from CLI /stats.", _sp_parsed)

                # Strategy 2: Passive regex scan for /stats-format lines
                if not _sp_parsed:
                    _parsed = _qp.update_from_cli_output(_stdout_str, _stderr_str)
                    if _parsed:
                        logger.info("📊  [QuotaProbe] CLI output regex: %d models.", _parsed)

                # Strategy 3: Rate-limit error detection — ONLY on failures.
                # The CLI can include rate-limit warnings in stderr even when the
                # task succeeds (e.g., internal retries that were handled).
                # Running this on successful tasks causes false exhaustion.
                if not _sp_parsed and proc.returncode != 0:
                    _combined_err = (_stderr_str + _stdout_str).lower()
                    _is_rate_limited = any(sig in _combined_err for sig in (
                        "429", "resource_exhausted", "quota exceeded",
                        "rate limit", "rate_limit", "too many requests",
                    ))
                    if _is_rate_limited:
                        import re as _re
                        _retry_match = _re.search(
                            r"retry[- ]?after[:\s]*(\d+)",
                            _combined_err,
                        )
                        _retry_s = int(_retry_match.group(1)) if _retry_match else 3600
                        # V62: Also check for retryDelayMs (more precise, in ms)
                        _retry_delay_ms = re.search(r'retryDelayMs:\s*([\d.]+)', _combined_err)
                        if _retry_delay_ms:
                            _retry_s = int(float(_retry_delay_ms.group(1)) / 1000)
                        import time as _time2
                        _now = _time2.time()

                        # V62: Spread 0% across the entire bucket, not just this model.
                        # If gemini-3.1-pro-preview is exhausted, gemini-2.5-pro shares
                        # the same pool and is also exhausted.
                        _bucket_map = getattr(config, 'QUOTA_MODEL_TO_BUCKET', {})
                        _buckets = getattr(config, 'QUOTA_BUCKETS', {})
                        _bucket = _bucket_map.get(model, "")
                        _models_to_mark = [model]
                        if _bucket and _bucket in _buckets:
                            _models_to_mark = _buckets[_bucket].get("models", [model])

                        for _m_to_mark in _models_to_mark:
                            _qp._snapshots[_m_to_mark] = {
                                "remaining_pct": 0.0,
                                "resets_in_s": _retry_s,
                                "resets_at": _now + _retry_s,
                                "probed_at": _now,
                                "source": "rate_limit_detection",
                            }
                        _qp._last_probe_at = _now
                        _qp._save_state()
                        logger.warning(
                            "📊  [QuotaProbe] Rate-limit detected for %s → 0%% (resets in %ds)",
                            model, _retry_s,
                        )
            except Exception as _qp_exc:
                logger.debug("📊  [QuotaProbe] Post-exec probe skipped: %s", _qp_exc)

            return {
                "exit_code": proc.returncode or 0,
                "stdout": _stdout_str,
                "stderr": _stderr_str,
                "timed_out": False,
                "model": model,
            }

        except asyncio.TimeoutError:
            # Legacy catch — should not fire with activity-based timeout,
            # but kept as a safety net for edge cases.
            logger.warning("⏱️  [Gemini] Outer timeout — killing (PID=%s)", proc.pid if 'proc' in dir() else '?')
            try:
                proc.kill()
            except Exception:
                pass
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "Outer timeout safety net triggered",
                "timed_out": True,
                "model": model,
            }
        except FileNotFoundError:
            logger.error("Gemini CLI binary not found: %s", gemini_cmd)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Gemini CLI not found: {gemini_cmd}. Ensure it is installed and in PATH.",
                "timed_out": False,
            }
        except Exception as exc:
            logger.error("Gemini CLI host execution failed: %s", exc)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "timed_out": False,
            }
        finally:
            # V68: Decrement active worker count for jitter tracking
            import threading as _th_fin
            _th_fin._gemini_active_count = max(0, getattr(_th_fin, '_gemini_active_count', 1) - 1)

    # V46: TTL cache for sandbox context — avoids redundant Docker exec
    # calls when multiple workers launch simultaneously.
    _ctx_cache: str = ""
    _ctx_cache_ts: float = 0.0
    _CTX_CACHE_TTL: float = 5.0  # seconds

    async def _gather_sandbox_context_for_prompt(self) -> str:
        """
        Gather lightweight context from the sandbox to include in the
        Gemini prompt so the host-side AI has workspace awareness.

        V46: 5-second TTL cache — when 3 workers call this simultaneously,
        only the first hits Docker. The others reuse cached context.
        """
        import time as _time
        now = _time.monotonic()
        if self._ctx_cache and (now - self._ctx_cache_ts) < self._CTX_CACHE_TTL:
            logger.debug("📡  [Context] Using cached sandbox state (%.1fs old)", now - self._ctx_cache_ts)
            return self._ctx_cache

        parts = []

        try:
            # Git status
            git_result = await self.tools.git_status()
            if git_result.modified or git_result.untracked:
                parts.append(f"Git branch: {git_result.branch}")
                if git_result.modified:
                    parts.append(f"Modified files: {', '.join(git_result.modified[:20])}")
                if git_result.untracked:
                    parts.append(f"Untracked files: {', '.join(git_result.untracked[:20])}")

            # V69: PROJECT_STATE.md is already injected via @PROJECT_STATE.md
            # by _inject_current_file_states(). No need to read from sandbox.

            # File listing (top-level)
            file_list = await self.sandbox.list_files(".", max_depth=2)
            if file_list:
                parts.append(f"Workspace files ({len(file_list)} total): {', '.join(file_list[:30])}")

        except Exception as exc:
            logger.debug("Context bridge gathering failed (non-fatal): %s", exc)

        result = "\n".join(parts) if parts else ""
        # Update cache
        self._ctx_cache = result
        self._ctx_cache_ts = now
        return result

    async def _execute_as_shell(self, command: str, timeout: int) -> TaskResult:
        """Execute a raw shell command (for non-Gemini tasks like npm install)."""
        result = TaskResult(prompt_used=command)

        cmd_result = await self.sandbox.exec_command(command, timeout=timeout)
        result.exit_code = cmd_result.exit_code
        result.output = cmd_result.stdout
        result.status = "success" if cmd_result.success else "error"

        if cmd_result.stderr:
            result.errors.append(cmd_result.stderr[:2000])
        if cmd_result.timed_out:
            result.status = "timeout"
            result.errors.append(f"Command timed out after {timeout}s")

        return result

    # ── Context Gathering ────────────────────────────────────

    async def gather_context(
        self,
        check_diagnostics: bool = True,
        check_dev_server: bool = True,
        max_diagnostic_files: int = 10,
    ) -> ExecutionContext:
        """
        Gather structured context from the sandbox.

        Replaces context_engine.py's gather_context() which used DOM scraping.
        This uses structured API calls that never fail due to CSS changes.

        Pipeline:
            1. Workspace file listing
            2. Git status (recently changed files)
            3. Dev server health check
            4. LSP diagnostics for changed files
            5. PROJECT_STATE.md content
            6. Running processes

        Returns:
            ExecutionContext with complete sandbox state.
        """
        ctx = ExecutionContext(gathered_at=time.time())

        # Run independent operations in parallel
        tasks = {
            "files": self.tools.file_list(".", max_depth=10),
            "git": self.tools.git_status(),
            "processes": self.sandbox.list_processes(),
        }

        if check_dev_server:
            tasks["dev_server"] = self.tools.dev_server_check()

        # Gather results concurrently
        results = {}
        gathered = await asyncio.gather(
            *[tasks[k] for k in tasks],
            return_exceptions=True,
        )
        for key, value in zip(tasks.keys(), gathered):
            if isinstance(value, Exception):
                logger.warning("Context gathering failed for %s: %s", key, value)
                results[key] = None
            else:
                results[key] = value

        # Process file listing
        file_result = results.get("files")
        if file_result and hasattr(file_result, "files"):
            ctx.workspace_files = file_result.files  # No cap — full tree

        # Process git status
        git_result = results.get("git")
        if git_result and isinstance(git_result, GitStatusResult):
            ctx.git_branch = git_result.branch
            ctx.git_modified = git_result.modified
            ctx.git_clean = git_result.clean
            ctx.recently_changed_files = git_result.modified + git_result.untracked

        # Process dev server
        dev_server = results.get("dev_server")
        if dev_server and isinstance(dev_server, DevServerResult):
            ctx.dev_server_running = dev_server.running
            ctx.dev_server_port = dev_server.port
            ctx.dev_server_url = dev_server.url

        # Process running processes
        processes = results.get("processes")
        if processes and isinstance(processes, list):
            ctx.running_processes = processes[:20]  # Cap at 20

        # Run diagnostics on recently changed files
        if check_diagnostics and ctx.recently_changed_files:
            diag_files = ctx.recently_changed_files[:max_diagnostic_files]
            diag_tasks = [self.tools.lsp_diagnostics(f) for f in diag_files]
            diag_results = await asyncio.gather(*diag_tasks, return_exceptions=True)

            for diag_result in diag_results:
                if isinstance(diag_result, Exception):
                    continue
                if hasattr(diag_result, "total_errors"):
                    ctx.diagnostics_errors += diag_result.total_errors
                    ctx.diagnostics_warnings += diag_result.total_warnings
                    if diag_result.errors:
                        ctx.diagnostic_details.extend(diag_result.errors)
                    if diag_result.warnings:
                        ctx.diagnostic_details.extend(diag_result.warnings)

        # V69: PROJECT_STATE.md read removed — already injected via
        # @PROJECT_STATE.md by _inject_current_file_states(). Saves a
        # Docker exec + 50K chars of inline context per task.

        # Determine agent status
        ctx.agent_status = self._classify_agent_status(ctx)

        # Last task output
        if self._last_task_result:
            ctx.last_agent_output = self._last_task_result.output[:20000]  # Cap

        logger.info(
            "Context gathered: files=%d, git_modified=%d, dev_server=%s, errors=%d, warnings=%d",
            len(ctx.workspace_files),
            len(ctx.git_modified),
            ctx.dev_server_running,
            ctx.diagnostics_errors,
            ctx.diagnostics_warnings,
        )
        return ctx

    def _classify_agent_status(self, ctx: ExecutionContext) -> str:
        """
        Classify the agent's current status based on context.

        Replaces context_engine.py's _classify_agent_status() which used
        DOM-based signals (chat messages, activity badges).

        Returns: working, idle, error, or waiting.
        """
        # If there are diagnostic errors, the agent needs to fix them
        if ctx.diagnostics_errors > 0:
            return "error"

        # If files were recently changed, the agent is working
        if ctx.recently_changed_files:
            return "working"

        # If a dev server is running, things are probably fine
        if ctx.dev_server_running:
            return "idle"

        # Default
        return "waiting"

    # ── Context Formatting ───────────────────────────────────

    def format_context_for_prompt(self, ctx: ExecutionContext) -> str:
        """
        Format an ExecutionContext into a string for Gemini prompt inclusion.

        Replaces context_engine.py's format_context_for_prompt().
        """
        parts = []

        parts.append(f"## Sandbox State (gathered at {time.strftime('%H:%M:%S', time.localtime(ctx.gathered_at))})")
        parts.append(f"- Agent status: **{ctx.agent_status}**")

        if ctx.git_branch:
            parts.append(f"- Git branch: `{ctx.git_branch}`")
            parts.append(f"- Git clean: {ctx.git_clean}")

        if ctx.recently_changed_files:
            parts.append(f"- Recently changed files ({len(ctx.recently_changed_files)}):")
            for f in ctx.recently_changed_files[:10]:
                parts.append(f"  - `{f}`")

        if ctx.dev_server_running:
            parts.append(f"- Dev server: ✅ running on `{ctx.dev_server_url}`")
        else:
            parts.append("- Dev server: ❌ not running")

        if ctx.diagnostics_errors > 0:
            parts.append(f"- Diagnostics: ❌ {ctx.diagnostics_errors} errors, {ctx.diagnostics_warnings} warnings")
            for diag in ctx.diagnostic_details[:5]:
                parts.append(f"  - `{diag.get('file', '?')}:{diag.get('line', '?')}` — {diag.get('message', '?')}")
        elif ctx.diagnostics_warnings > 0:
            parts.append(f"- Diagnostics: ⚠️ {ctx.diagnostics_warnings} warnings (no errors)")
        else:
            parts.append("- Diagnostics: ✅ clean")

        # V69: PROJECT_STATE.md excerpt removed — CLI reads it natively
        # via @-file reference from _inject_current_file_states().

        if ctx.last_agent_output:
            parts.append("\n## Last Agent Output (excerpt)")
            parts.append(f"```\n{ctx.last_agent_output[:10000]}\n```")

        return "\n".join(parts)

    # ── Convenience Methods ──────────────────────────────────

    async def install_dependencies(self, timeout: int = 180) -> TaskResult:
        """
        Auto-detect and install project dependencies.

        V51: Comprehensive detection:
          - Detects yarn.lock / pnpm-lock.yaml → uses correct package manager
          - Auto-copies .env.example → .env if missing
          - Detects native deps (canvas, sharp) → installs system packages
          - Nuclear clean retry on failure (any package manager)
        """
        # Check what package managers are needed
        has_package_json = await self.sandbox.file_exists("package.json")
        has_requirements = await self.sandbox.file_exists("requirements.txt")
        has_pyproject = await self.sandbox.file_exists("pyproject.toml")
        has_yarn_lock = await self.sandbox.file_exists("yarn.lock")
        has_pnpm_lock = await self.sandbox.file_exists("pnpm-lock.yaml")

        # V51: Detect correct Node package manager from lock file
        _pkg_mgr = "npm"
        _install_cmd = "npm install --legacy-peer-deps"
        _nuke_lock = "package-lock.json"
        if has_yarn_lock and has_package_json:
            _pkg_mgr = "yarn"
            _install_cmd = "yarn install --frozen-lockfile 2>&1 || yarn install"
            _nuke_lock = "yarn.lock"
        elif has_pnpm_lock and has_package_json:
            _pkg_mgr = "pnpm"
            _install_cmd = "pnpm install --no-frozen-lockfile"
            _nuke_lock = "pnpm-lock.yaml"

        commands = []
        if has_package_json:
            # V61: Fast-path skip — if node_modules exists AND package.json
            # content hasn't changed since the last successful install, skip
            # the cache wipe and npm install entirely. This prevents the 30–120s
            # npm install on every supervisor restart when dependencies are current.
            # Uses a content fingerprint (md5sum of package.json + lockfile) stored
            # in /tmp/.pkg_fingerprint inside the container. Resets on new container.
            _skip_npm = False
            has_node_modules = await self.sandbox.file_exists("node_modules")
            if has_node_modules:
                try:
                    _lockfile = "package-lock.json"
                    if has_yarn_lock:
                        _lockfile = "yarn.lock"
                    elif has_pnpm_lock:
                        _lockfile = "pnpm-lock.yaml"
                    _fp_check = await self.sandbox.exec_command(
                        f"STAMP=/tmp/.pkg_fingerprint; "
                        f"CUR=$(cat package.json {_lockfile} 2>/dev/null | md5sum | cut -d' ' -f1); "
                        f"OLD=$(cat \"$STAMP\" 2>/dev/null || echo ''); "
                        f"if [ \"$CUR\" = \"$OLD\" ] && [ -d node_modules ]; then echo SKIP; else echo \"$CUR\"; fi",
                        timeout=8,
                    )
                    _fp_out = (_fp_check.stdout or "").strip()
                    if _fp_out == "SKIP":
                        _skip_npm = True
                        logger.info(
                            "📦  [Deps] package.json + lockfile unchanged — skipping npm install (node_modules current)"
                        )
                        # Still fix permissions in case container user changed
                        await self.sandbox.exec_command(
                            "mkdir -p /workspace/node_modules/.vite && "
                            "chmod -R 777 /workspace/node_modules/.vite 2>/dev/null; "
                            "chmod -R u+w /workspace/node_modules 2>/dev/null || true",
                            timeout=8,
                        )
                except Exception as _fp_exc:
                    logger.debug("📦  [Deps] Fingerprint check skipped: %s", _fp_exc)

            if not _skip_npm:
                # Clear stale build caches and artifacts from previous sessions
                await self.sandbox.exec_command(
                    "rm -rf node_modules/.vite .vite .next .nuxt dist/.vite "
                    "node_modules/.cache .output .turbo .parcel-cache 2>/dev/null || true",
                    timeout=10,
                )

            if not _skip_npm:
                # V51: Module chunk integrity check — only when actually installing.
                # After version bumps, internal chunks can reference files that
                # no longer exist (Vite's dep-BK3b2jBa.js → dep-D-7KCb9p.js).
                # If corruption found, clear _skip_npm override so install proceeds.
                try:
                    _probe_script = (
                        'node -e "'
                        "const fs=require('fs'),path=require('path');"
                        "const mods=['vite','next','nuxt','webpack','esbuild'];"
                        "let bad=false;"
                        "for(const m of mods){"
                        "const d='node_modules/'+m;"
                        "if(!fs.existsSync(d))continue;"
                        "try{const entry=require.resolve(m);"
                        "const dir=path.dirname(entry);"
                        "const files=fs.readdirSync(dir).filter(f=>f.endsWith('.js')).slice(0,5);"
                        "for(const f of files){"
                        "const content=fs.readFileSync(path.join(dir,f),'utf8').slice(0,5000);"
                        "const refs=[...content.matchAll(/require\\(['\\\"](\\.\\.[^'\\\"]+)['\\\"]\\)/g)];"
                        "for(const r of refs){"
                        "const target=path.resolve(dir,r[1]);"
                        "const targets=[target,target+'.js',target+'.cjs',target+'.mjs'];"
                        "if(!targets.some(t=>fs.existsSync(t))){"
                        "console.error('CORRUPT:'+m+':'+f+' refs missing '+r[1]);"
                        "bad=true}}}}"
                        "catch(e){}}"
                        "if(bad)process.exit(1);"
                        '" 2>&1'
                    )
                    _probe = await self.sandbox.exec_command(_probe_script, timeout=10)
                    _probe_out = (_probe.stdout or "") + (_probe.stderr or "")
                    if _probe.exit_code != 0 and "CORRUPT:" in _probe_out:
                        logger.warning(
                            "📦  [Deps] Corrupt chunk references: %s — nuking node_modules",
                            _probe_out.strip()[:300]
                        )
                        await self.sandbox.exec_command(
                            f"rm -rf node_modules {_nuke_lock} .vite 2>/dev/null || true",
                            timeout=20,
                        )
                except Exception as _probe_exc:
                    logger.debug("📦  [Deps] Module probe skipped: %s", _probe_exc)
                # Ensure correct package manager is installed
                if _pkg_mgr == "pnpm":
                    await self.sandbox.exec_command(
                        "command -v pnpm >/dev/null 2>&1 || npm install -g pnpm 2>&1 | tail -3",
                        timeout=30,
                    )
                elif _pkg_mgr == "yarn":
                    await self.sandbox.exec_command(
                        "command -v yarn >/dev/null 2>&1 || npm install -g yarn 2>&1 | tail -3",
                        timeout=30,
                    )
                # V51: Detect native deps and pre-install system packages
                try:
                    _pkg_raw = await self.sandbox.read_file("package.json")
                    _all_deps = _pkg_raw or ""
                    _sys_pkgs = []
                    if '"canvas"' in _all_deps:
                        _sys_pkgs += ["libcairo2-dev", "libjpeg-dev", "libpango1.0-dev", "libgif-dev"]
                    if '"sharp"' in _all_deps:
                        _sys_pkgs += ["libvips-dev"]
                    if '"bcrypt"' in _all_deps or '"argon2"' in _all_deps:
                        _sys_pkgs += ["build-essential", "python3"]
                    if '"node-gyp"' in _all_deps or '"node-pre-gyp"' in _all_deps:
                        _sys_pkgs += ["build-essential", "python3", "make", "g++"]
                    if '"sqlite3"' in _all_deps or '"better-sqlite3"' in _all_deps:
                        _sys_pkgs += ["build-essential", "python3"]
                    if _sys_pkgs:
                        _uniq = list(dict.fromkeys(_sys_pkgs))
                        logger.info("📦  [Deps] Native deps detected — installing: %s", _uniq[:6])
                        await self.sandbox.exec_command(
                            f"apt-get update -qq && apt-get install -y -qq {' '.join(_uniq)} 2>&1 | tail -5",
                            timeout=60,
                        )
                except Exception as _nat:
                    logger.debug("📦  [Deps] Native dep check skipped: %s", _nat)
                commands.append(_install_cmd)
                logger.info("📦  [Deps] Using %s for Node dependencies", _pkg_mgr)
        if has_requirements:
            commands.append("pip install -r requirements.txt")
        if has_pyproject:
            commands.append("pip install -e .")

        # V51: Auto-copy .env.example → .env if missing
        has_env = await self.sandbox.file_exists(".env")
        if not has_env:
            for _tmpl in (".env.example", ".env.sample", ".env.template", ".env.local.example"):
                if await self.sandbox.file_exists(_tmpl):
                    await self.sandbox.exec_command(f"cp {_tmpl} .env", timeout=5)
                    logger.info("📦  [Deps] Auto-copied %s → .env", _tmpl)
                    break

        if not commands:
            return TaskResult(status="success", output="No dependencies to install")

        combined_cmd = " && ".join(commands)
        
        # V41 EGRESS FAST-GATE: Open the network specifically for package installs
        if hasattr(self.sandbox, 'grant_network'):
            await self.sandbox.grant_network()
            
        try:
            result = await self.execute_task(combined_cmd, timeout=timeout, use_gemini_cli=False)

            # V51: If npm install failed for ANY reason (ERESOLVE, ENOTEMPTY,
            # corrupted chunks, etc.), nuke node_modules and do a clean install.
            # Partial installs leave Vite's internal chunks corrupted, causing
            # "Cannot find module dep-*.js" errors at runtime.
            if has_package_json and result.errors and any(
                kw in " ".join(result.errors)
                for kw in ("ERESOLVE", "ENOTEMPTY", "Cannot find module", "EACCES",
                           "EPERM", "ERR!", "ENOENT", "code 1")
            ):
                logger.warning("📥  [%s] Install failed — nuking node_modules for clean retry …", _pkg_mgr)
                nuke_cmd = (
                    f"rm -rf node_modules {_nuke_lock} 2>/dev/null; "
                    f"{_install_cmd} 2>&1 | tail -20"
                )
                result = await self.execute_task(nuke_cmd, timeout=timeout, use_gemini_cli=False)
                if result.success:
                    logger.info("📥  [%s] Clean reinstall succeeded.", _pkg_mgr)
                else:
                    # V51: Final escalation — --force overrides ALL version conflicts
                    logger.warning("📥  [%s] Retrying with --force as last resort …", _pkg_mgr)
                    force_cmd = (
                        f"rm -rf node_modules {_nuke_lock} 2>/dev/null; "
                        f"npm install --force 2>&1 | tail -20"
                    )
                    result = await self.execute_task(force_cmd, timeout=timeout, use_gemini_cli=False)
                    if result.success:
                        logger.info("📥  [npm] Force install succeeded.")
                    else:
                        logger.warning("📥  [npm] Force install also failed: %s", result.errors[:2])

            # V61: Write fingerprint stamp after successful install so next
            # session can skip the install entirely when package.json is unchanged.
            if result.success and has_package_json:
                try:
                    _lockfile_stamp = "package-lock.json"
                    if has_yarn_lock:
                        _lockfile_stamp = "yarn.lock"
                    elif has_pnpm_lock:
                        _lockfile_stamp = "pnpm-lock.yaml"
                    await self.sandbox.exec_command(
                        f"cat package.json {_lockfile_stamp} 2>/dev/null "
                        f"| md5sum | cut -d' ' -f1 > /tmp/.pkg_fingerprint",
                        timeout=5,
                    )
                    # V61: Signal to the preview layer that a fresh install was
                    # done so it can restart Vite with a clean .vite cache.
                    # Prevents 504 "Outdated Optimize Dep" on mid-session installs.
                    self._fresh_npm_install = True
                except Exception:
                    pass
        finally:
            # Always cut off the network instantly
            if hasattr(self.sandbox, 'revoke_network'):
                await self.sandbox.revoke_network()
                
        return result

    async def run_tests(self, timeout: int = 120) -> TaskResult:
        """
        Auto-detect and run project tests.

        Checks for jest, pytest, mocha, etc.
        """
        has_package_json = await self.sandbox.file_exists("package.json")
        has_pytest = await self.sandbox.file_exists("pytest.ini") or \
                     await self.sandbox.file_exists("pyproject.toml")

        if has_package_json:
            # Check if test script exists in package.json
            pkg_content = await self.sandbox.read_file("package.json")
            try:
                pkg = json.loads(pkg_content)
                if "test" in pkg.get("scripts", {}):
                    return await self.execute_task("npm test", timeout=timeout, use_gemini_cli=False)
            except json.JSONDecodeError:
                pass

        # Try pytest
        return await self.execute_task(
            "python -m pytest -v 2>&1 || echo 'No tests found'",
            timeout=timeout,
            use_gemini_cli=False,
        )

    async def build_health_check(self, timeout: int = 120) -> TaskResult:
        """
        V51: Comprehensive build health validation.

        Checks:
          1. Stale build caches cleared
          2. npm outdated — detects old dependencies
          3. Import/module resolution — verifies no missing modules
          4. Vite/build config validation
          5. Export correctness in package.json

        Results are written to BUILD_ISSUES.md in the project root.
        Returns TaskResult with findings.
        """
        has_package_json = await self.sandbox.file_exists("package.json")
        if not has_package_json:
            return TaskResult(
                status="success",
                output="No package.json — skipping build health check",
            )

        # V51: Comprehensive check script that runs inside the sandbox
        check_script = r'''
#!/bin/bash
set -e
ISSUES_FILE="/workspace/BUILD_ISSUES.md"
TMP_ISSUES=$(mktemp)
FOUND=0

echo "# Build Health Report" > "$TMP_ISSUES"
echo "> Auto-generated by Supervisor Build Health System" >> "$TMP_ISSUES"
echo "> Updated: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$TMP_ISSUES"
echo "" >> "$TMP_ISSUES"

# 1. Clear stale caches
rm -rf node_modules/.vite .vite .next/cache .nuxt dist/.vite node_modules/.cache 2>/dev/null || true
echo "✅ Build caches cleared" >&2

# 1b. Node version check — modern packages need Node 18+
NODE_VER=$(node -v 2>/dev/null | sed 's/v//' | cut -d. -f1)
if [ -n "$NODE_VER" ] && [ "$NODE_VER" -lt 18 ] 2>/dev/null; then
echo "" >> "$TMP_ISSUES"
echo "## ⚠ Node.js Version" >> "$TMP_ISSUES"
echo "- Current: v$(node -v 2>/dev/null). Many modern packages (Vite 5+, deck.gl 9+) require Node 18+." >> "$TMP_ISSUES"
FOUND=$((FOUND+1))
fi

# 1c. Package manager lock file mismatch
if [ -f yarn.lock ] && [ ! -f node_modules/.yarn-integrity ] && [ -f node_modules/.package-lock.json ]; then
echo "" >> "$TMP_ISSUES"
echo "## ⚠ Package Manager Mismatch" >> "$TMP_ISSUES"
echo "- \`yarn.lock\` exists but deps were installed with npm. Run \`yarn install\` instead." >> "$TMP_ISSUES"
FOUND=$((FOUND+1))
fi
if [ -f pnpm-lock.yaml ] && [ ! -f node_modules/.modules.yaml ]; then
echo "" >> "$TMP_ISSUES"
echo "## ⚠ Package Manager Mismatch" >> "$TMP_ISSUES"
echo "- \`pnpm-lock.yaml\` exists but deps were installed with npm. Run \`pnpm install\` instead." >> "$TMP_ISSUES"
FOUND=$((FOUND+1))
fi

# 1d. Missing .env file
if [ -f .env.example ] && [ ! -f .env ]; then
echo "" >> "$TMP_ISSUES"
echo "## ⚠ Missing .env File" >> "$TMP_ISSUES"
echo "- \`.env.example\` exists but \`.env\` is missing. Copy it: \`cp .env.example .env\`" >> "$TMP_ISSUES"
FOUND=$((FOUND+1))
fi

# 2. Check outdated deps — auto-fix non-breaking, report only major bumps
if [ -f package.json ]; then
    OUTDATED=$(npm outdated --json 2>/dev/null || true)
    if [ -n "$OUTDATED" ] && [ "$OUTDATED" != "{}" ]; then
        # Classify: are there any non-breaking (patch/minor) updates?
        HAS_NON_BREAKING=$(echo "$OUTDATED" | node -e "
            const data = JSON.parse(require('fs').readFileSync('/dev/stdin','utf8'));
            const nb = Object.entries(data).filter(([,i]) =>
                (i.current||'0').split('.')[0] === (i.wanted||'0').split('.')[0]);
            process.exit(nb.length > 0 ? 0 : 1);
        " 2>/dev/null && echo "yes" || echo "no")

        # Auto-update non-breaking deps via npm update (respects semver ranges)
        if [ "$HAS_NON_BREAKING" = "yes" ]; then
            echo "🔄 Auto-updating non-breaking dependencies …" >&2
            npm update --save 2>/dev/null || true
        fi

        # Re-check after auto-update — only report what remains
        OUTDATED2=$(npm outdated --json 2>/dev/null || true)
        if [ -n "$OUTDATED2" ] && [ "$OUTDATED2" != "{}" ]; then
            # Filter to only major-version bumps (true breaking changes)
            MAJOR_ONLY=$(echo "$OUTDATED2" | node -e "
                const data = JSON.parse(require('fs').readFileSync('/dev/stdin','utf8'));
                const majors = {};
                for (const [pkg, info] of Object.entries(data)) {
                    const cur = (info.current||'0').split('.')[0];
                    const lat = (info.latest||'0').split('.')[0];
                    if (cur !== lat) majors[pkg] = info;
                }
                if (Object.keys(majors).length > 0) {
                    console.log(JSON.stringify(majors));
                }
            " 2>/dev/null || true)

            if [ -n "$MAJOR_ONLY" ]; then
                echo "" >> "$TMP_ISSUES"
                echo "## ⚠ Outdated Dependencies (Major Version Bumps)" >> "$TMP_ISSUES"
                echo "| Package | Current | Wanted | Latest | Breaking? |" >> "$TMP_ISSUES"
                echo "|---|---|---|---|---|" >> "$TMP_ISSUES"
                echo "$MAJOR_ONLY" | node -e "
                    const data = JSON.parse(require('fs').readFileSync('/dev/stdin','utf8'));
                    for (const [pkg, info] of Object.entries(data)) {
                        const cur = info.current || '?';
                        const want = info.wanted || '?';
                        const latest = info.latest || '?';
                        console.log('| ' + pkg + ' | ' + cur + ' | ' + want + ' | ' + latest + ' | ⚠ Major |');
                    }
                " >> "$TMP_ISSUES" 2>/dev/null || true
                FOUND=$((FOUND+1))
            fi
        fi
    fi
fi

# 3. Check for missing node_modules (incomplete install)
if [ -f package.json ] && [ ! -d node_modules ]; then
    echo "" >> "$TMP_ISSUES"
    echo "## ❌ Missing node_modules" >> "$TMP_ISSUES"
    echo "- node_modules directory does not exist. Run \`npm install\`" >> "$TMP_ISSUES"
    FOUND=$((FOUND+1))
fi

# 4. Vite config validation
if [ -f vite.config.ts ] || [ -f vite.config.js ] || [ -f vite.config.mjs ]; then
    VITE_CHECK=$(npx vite build --mode production 2>&1 | tail -20 || true)
    if echo "$VITE_CHECK" | grep -qi "error\|fail\|cannot find"; then
        echo "" >> "$TMP_ISSUES"
        echo "## ❌ Vite Build Issues" >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        echo "$VITE_CHECK" | tail -10 >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        FOUND=$((FOUND+1))
    fi
fi

# 5. TypeScript check (if tsconfig exists)
if [ -f tsconfig.json ]; then
    TS_CHECK=$(npx tsc --noEmit 2>&1 | head -30 || true)
    if echo "$TS_CHECK" | grep -qi "error TS"; then
        TS_ERRORS=$(echo "$TS_CHECK" | grep -c "error TS" || echo "0")
        echo "" >> "$TMP_ISSUES"
        echo "## ❌ TypeScript Errors ($TS_ERRORS)" >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        echo "$TS_CHECK" >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        FOUND=$((FOUND+1))
    fi
fi

# 6. ESM/CJS module type check
if [ -f package.json ]; then
    HAS_TYPE=$(node -e "const p=JSON.parse(require('fs').readFileSync('package.json','utf8')); console.log(p.type||'none')" 2>/dev/null || echo "none")
    HAS_MJS=$(find /workspace/src -name '*.mjs' 2>/dev/null | head -1)
    HAS_ESM_IMPORT=$(grep -rl 'import .* from' /workspace/src/*.js /workspace/src/*.ts 2>/dev/null | head -1)
    if [ -n "$HAS_ESM_IMPORT" ] && [ "$HAS_TYPE" = "none" ]; then
        echo "" >> "$TMP_ISSUES"
        echo "## ⚠ Module Type" >> "$TMP_ISSUES"
        echo "- ESM imports detected but package.json missing \"type\": \"module\"" >> "$TMP_ISSUES"
        FOUND=$((FOUND+1))
    fi
fi

# 7. Dev-server log — catch runtime errors that only appear after startup
# Covers two classes:
#   A) Vite chunk corruption  → self-heal locally (wipe + reinstall, no AI needed)
#   B) CSS/PostCSS/Tailwind theme() errors → flag for Gemini to fix in code
if [ -f /tmp/dev-server.log ]; then
    # Read last 200 lines (avoids processing megabyte logs from long sessions)
    DS_LOG=$(tail -200 /tmp/dev-server.log 2>/dev/null || true)

    # A) node_modules corruption — any "Cannot find module" that points INSIDE
    #    node_modules is a corrupted/incomplete install. The fix is always the same:
    #    wipe and reinstall. This covers chunks, dist files, peer dep targets, etc.
    #    Note: local source import errors (../foo, ./bar) are code bugs → Gemini.
    NM_ERR=$(echo "$DS_LOG" | \
        grep -iE "Cannot find module ['\"]?.*node_modules|Cannot find module ['\"]?.*\/dist\/|ENOENT.*node_modules|MODULE_NOT_FOUND.*node_modules" | \
        grep -v "Cannot find module ['\"]?\.\." | \
        head -5 || true)
    if [ -n "$NM_ERR" ]; then
        echo "" >> "$TMP_ISSUES"
        echo "## ❌ node_modules Corruption Detected (Auto-healing)" >> "$TMP_ISSUES"
        echo "- 'Cannot find module' pointing inside node_modules — indicates a corrupt or incomplete install." >> "$TMP_ISSUES"
        echo "- This is not a code bug. Wiping node_modules and reinstalling …" >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        echo "$NM_ERR" | head -3 >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        # Self-heal: wipe and reinstall
        rm -rf /workspace/node_modules 2>/dev/null || true
        npm install --no-audit --no-fund --no-update-notifier \
            --legacy-peer-deps --loglevel=error 2>&1 | tail -5 || true
        # Verify with a quick require probe on vite (or node itself if no vite)
        if node -e "require('vite')" 2>/dev/null || \
           node -e "require(JSON.parse(require('fs').readFileSync('/workspace/package.json','utf8')).main||'./index.js')" 2>/dev/null || \
           [ $? -eq 0 ]; then
            echo "- ✅ node_modules repair succeeded." >> "$TMP_ISSUES"
            echo "SELF_HEALED: node_modules_corruption" >> "$TMP_ISSUES"
        else
            echo "- ⚠️  Reinstall done but modules still unresolvable — flagging for Gemini." >> "$TMP_ISSUES"
            FOUND=$((FOUND+1))
        fi
    fi


    # B) CSS/PostCSS/Tailwind runtime errors → Gemini code fix needed
    CSS_ERR=$(echo "$DS_LOG" | grep -iE "\[plugin:vite:css\]|\[postcss\]|tailwindcss:.*Could not resolve|tailwindcss:.*theme\(|css.*error:|sass error" | head -10 || true)
    if [ -n "$CSS_ERR" ]; then
        echo "" >> "$TMP_ISSUES"
        echo "## ❌ CSS / PostCSS / Tailwind Errors" >> "$TMP_ISSUES"
        echo "These errors appeared in the dev-server log and prevent the app from rendering correctly." >> "$TMP_ISSUES"
        echo "" >> "$TMP_ISSUES"
        echo "Common causes:" >> "$TMP_ISSUES"
        echo "- \`theme('colors.X')\` in CSS where X is not defined in tailwind.config — add the color or use a valid token" >> "$TMP_ISSUES"
        echo "- PostCSS plugin misconfiguration or missing postcss.config.js" >> "$TMP_ISSUES"
        echo "" >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        echo "$CSS_ERR" >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        FOUND=$((FOUND+1))
    fi

    # C) Port conflict (EADDRINUSE) → local fix: kill process occupying the port
    PORT_ERR=$(echo "$DS_LOG" | grep -iE "EADDRINUSE|address already in use|port.*in use" | head -3 || true)
    if [ -n "$PORT_ERR" ]; then
        CONFLICT_PORT=$(echo "$PORT_ERR" | grep -oE ':[0-9]+' | head -1 | tr -d ':')
        if [ -n "$CONFLICT_PORT" ]; then
            fuser -k "${CONFLICT_PORT}/tcp" 2>/dev/null || \
            lsof -ti tcp:"$CONFLICT_PORT" | xargs kill -9 2>/dev/null || true
            echo "SELF_HEALED: port_conflict_${CONFLICT_PORT}" >> "$TMP_ISSUES"
        fi
    fi

    # D) Heap out-of-memory → bump NODE_OPTIONS and restart
    OOM_ERR=$(echo "$DS_LOG" | grep -iE "Reached heap limit|JavaScript heap out of memory|FATAL ERROR.*Allocation failed|out of memory" | head -3 || true)
    if [ -n "$OOM_ERR" ]; then
        echo "" >> "$TMP_ISSUES"
        echo "## ⚠ Heap Out-of-Memory (Auto-healing)" >> "$TMP_ISSUES"
        echo "- Bumping NODE_OPTIONS to 6GB and restarting server …" >> "$TMP_ISSUES"
        export NODE_OPTIONS="--max-old-space-size=6144"
        echo 'NODE_OPTIONS=--max-old-space-size=6144' >> /workspace/.env 2>/dev/null || true
        echo "SELF_HEALED: oom_heap" >> "$TMP_ISSUES"
    fi

    # E) ERR_PACKAGE_PATH_NOT_EXPORTED / subpath exports mismatch → node_modules corruption
    SUBPATH_ERR=$(echo "$DS_LOG" | grep -iE "ERR_PACKAGE_PATH_NOT_EXPORTED|Package subpath.*is not defined by exports" | head -3 || true)
    if [ -n "$SUBPATH_ERR" ]; then
        echo "" >> "$TMP_ISSUES"
        echo "## ❌ Package Subpath Export Error (Auto-healing)" >> "$TMP_ISSUES"
        echo "- Package exports config mismatch — wiping node_modules and reinstalling …" >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        echo "$SUBPATH_ERR" | head -2 >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        rm -rf /workspace/node_modules 2>/dev/null || true
        npm install --no-audit --no-fund --no-update-notifier \
            --legacy-peer-deps --loglevel=error 2>&1 | tail -5 || true
        echo "SELF_HEALED: subpath_exports" >> "$TMP_ISSUES"
    fi

    # F) EACCES / permission errors on node_modules → chmod + reinstall
    PERM_ERR=$(echo "$DS_LOG" | grep -iE "EACCES.*node_modules|permission denied.*node_modules" | head -3 || true)
    if [ -n "$PERM_ERR" ]; then
        echo "" >> "$TMP_ISSUES"
        echo "## ❌ node_modules Permission Error (Auto-healing)" >> "$TMP_ISSUES"
        chmod -R 755 /workspace/node_modules 2>/dev/null || true
        rm -rf /workspace/node_modules 2>/dev/null || true
        npm install --no-audit --no-fund --no-update-notifier \
            --legacy-peer-deps --loglevel=error 2>&1 | tail -5 || true
        echo "SELF_HEALED: node_modules_permissions" >> "$TMP_ISSUES"
    fi

fi  # end dev-server.log block

# 8. Python project checks — pip ModuleNotFoundError
if [ -f requirements.txt ] || [ -f pyproject.toml ] || [ -f setup.py ]; then
    # Check recent Python errors from any app log
    PY_LOG=$(cat /tmp/app.log /tmp/server.log /tmp/uvicorn.log 2>/dev/null | tail -100 || true)
    PY_MOD_ERR=$(echo "$PY_LOG" | grep -iE "ModuleNotFoundError|No module named|ImportError" | grep -v "test_" | head -5 || true)
    if [ -n "$PY_MOD_ERR" ]; then
        echo "" >> "$TMP_ISSUES"
        echo "## ❌ Python Module Missing (Auto-healing)" >> "$TMP_ISSUES"
        echo "- Running pip install from requirements file …" >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        echo "$PY_MOD_ERR" | head -3 >> "$TMP_ISSUES"
        echo '```' >> "$TMP_ISSUES"
        if [ -f requirements.txt ]; then
            pip install -r requirements.txt --quiet 2>&1 | tail -5 || true
            echo "SELF_HEALED: pip_install" >> "$TMP_ISSUES"
        elif [ -f pyproject.toml ]; then
            pip install -e . --quiet 2>&1 | tail -5 || true
            echo "SELF_HEALED: pip_install_pyproject" >> "$TMP_ISSUES"
        else
            echo "- ⚠️  No requirements file found — flagging for Gemini to add one." >> "$TMP_ISSUES"
            FOUND=$((FOUND+1))
        fi
    fi
fi

# 9. Dev server not running — restart it if it should be
if [ -f package.json ] && [ -d node_modules ]; then
    SERVER_ALIVE=$(curl -s --max-time 2 http://localhost:3000 -o /dev/null -w "%{http_code}" 2>/dev/null || echo "000")
    if [ "$SERVER_ALIVE" = "000" ]; then
        # Not responding — restart
        pkill -f 'vite|next dev|react-scripts|serve' 2>/dev/null || true
        sleep 1
        nohup npm run dev -- --port 3000 --host 0.0.0.0 > /tmp/dev-server.log 2>&1 &
        echo "SELF_HEALED: dev_server_restarted" >> "$TMP_ISSUES"
    fi
fi

# Write footer
if [ $FOUND -eq 0 ]; then
    echo "" >> "$TMP_ISSUES"
    echo "## ✅ All Clear" >> "$TMP_ISSUES"
    echo "No build issues detected." >> "$TMP_ISSUES"
fi

# Copy to workspace
cp "$TMP_ISSUES" "$ISSUES_FILE"
rm -f "$TMP_ISSUES"

echo "BUILD_HEALTH: $FOUND issues found"
cat "$ISSUES_FILE"
'''
        # Write the script and run it
        await self.sandbox.write_file(
            "_build_health.sh", check_script
        )

        # V51: Grant network for npm outdated (needs registry access)
        if hasattr(self.sandbox, 'grant_network'):
            await self.sandbox.grant_network()

        try:
            result = await self.sandbox.exec_command(
                "chmod +x /workspace/_build_health.sh && "
                "bash /workspace/_build_health.sh 2>&1; "
                "rm -f /workspace/_build_health.sh",
                timeout=timeout,
            )
        finally:
            if hasattr(self.sandbox, 'revoke_network'):
                await self.sandbox.revoke_network()

        # Parse results
        output = result.stdout or ""
        issues_found = 0
        for line in output.splitlines():
            if line.startswith("BUILD_HEALTH:"):
                try:
                    issues_found = int(line.split(":")[1].strip().split()[0])
                except (ValueError, IndexError):
                    pass

        logger.info(
            "🔍  [Build Health] Check complete: %d issue(s) found",
            issues_found,
        )

        # V53: Try to auto-upgrade major version bumps immediately —
        # attempt npm install@latest + tsc verify; if clean, mark resolved.
        try:
            await self._try_upgrade_major_deps()
        except Exception as _upg_exc:
            logger.debug("🔄  [Dep Upgrade] Skipped: %s", _upg_exc)

        return TaskResult(
            status="success" if issues_found == 0 else "warning",
            output=output,
            errors=[f"{issues_found} build issue(s) found"] if issues_found > 0 else [],
            exit_code=result.exit_code,
        )

    async def _try_upgrade_major_deps(self) -> None:
        """
        V53: Try auto-upgrading major version bumps found in BUILD_ISSUES.md.

        Flow:
          1. Parse BUILD_ISSUES.md for the "## ⚠ Outdated Dependencies" table
          2. Extract package names from the | pkg | ... | ⚠ Major | rows
          3. Run: npm install pkg1@latest pkg2@latest --legacy-peer-deps
          4. Verify: tsc --noEmit (30s). If OK → rewrite BUILD_ISSUES.md to
             mark section as ✅ Resolved.
          5. If tsc fails → revert (restore package-lock from backup) and append
             the first 10 TS error lines to BUILD_ISSUES.md so CLI knows context.

        All steps are logged so they appear in the UI WebSocket log stream.
        """
        if not self.sandbox or not self.sandbox.is_running:
            return

        # V54: Only run once per session — build_health_check is called many
        # times (boot, coherence gate, post-restart). Without this guard the
        # same packages are upgraded 3× in a single 20-minute run.
        if self._dep_upgrade_done:
            return

        # ── 1. Read BUILD_ISSUES.md from container ──────────────────────────
        read_r = await self.sandbox.exec_command(
            "cat /workspace/BUILD_ISSUES.md 2>/dev/null", timeout=8
        )
        content = read_r.stdout or ""
        if "## ⚠ Outdated Dependencies" not in content:
            return  # Nothing to try

        # ── 2. Extract package names from the table ──────────────────────────
        import re as _re
        # Rows look like: | i18next | 24.2.3 | 24.2.3 | 25.8.14 | ⚠ Major |
        pkgs = _re.findall(
            r'^\|\s*([^\s|]+)\s*\|\s*\S+\s*\|\s*\S+\s*\|\s*\S+\s*\|\s*⚠ Major',
            content, _re.MULTILINE,
        )
        # Skip header rows / separators
        pkgs = [p for p in pkgs if p not in ("Package", "---")]
        if not pkgs:
            return

        pkg_display = ', '.join(pkgs)
        install_spec = ' '.join(f'{p}@latest' for p in pkgs)

        logger.info("🔄  [Dep Upgrade] Trying auto-upgrade of major deps: %s", pkg_display)

        # ── 3. Back up package-lock for potential revert ─────────────────────
        await self.sandbox.exec_command(
            "cp /workspace/package-lock.json /workspace/package-lock.json.pre-upgrade 2>/dev/null; "
            "cp /workspace/package.json /workspace/package.json.pre-upgrade 2>/dev/null",
            timeout=5,
        )

        # ── 4. Install @latest versions ──────────────────────────────────────
        install_r = await self.sandbox.exec_command(
            f"cd /workspace && npm install --legacy-peer-deps {install_spec} 2>&1",
            timeout=180,
        )
        _log_npm_output(install_r.stdout or "", source="Dep Upgrade")

        if install_r.exit_code != 0:
            logger.warning("🔄  [Dep Upgrade] npm install failed — leaving for CLI")
            await self.sandbox.exec_command(
                "cd /workspace && "
                "cp package-lock.json.pre-upgrade package-lock.json 2>/dev/null; "
                "cp package.json.pre-upgrade package.json 2>/dev/null",
                timeout=10,
            )
            return

        # ── 5. Verify with tsc --noEmit ──────────────────────────────────────
        logger.info("🔄  [Dep Upgrade] Verifying upgrade with tsc --noEmit …")
        tsc_r = await self.sandbox.exec_command(
            "cd /workspace && npx tsc --noEmit 2>&1 | head -20",
            timeout=60,
        )

        if tsc_r.exit_code == 0:
            # ✅ Upgrade succeeded — mark section resolved in BUILD_ISSUES.md
            logger.info("🔄  [Dep Upgrade] ✅ Upgrade verified: %s — marking resolved", pkg_display)
            # V54: Mark done so subsequent build_health_check calls in this
            # session skip the upgrade entirely (prevents 3× upgrade per run).
            self._dep_upgrade_done = True
            # Replace the ⚠ Major section header with ✅ Resolved
            new_content = _re.sub(
                r'## ⚠ Outdated Dependencies.*?(?=\n##|\Z)',
                (
                    f"## ✅ Major Deps Auto-Upgraded\n"
                    f"Packages upgraded to @latest and verified with tsc: {pkg_display}\n"
                ),
                content, flags=_re.DOTALL,
            )
            escaped = new_content.replace("'", "'\\''")
            await self.sandbox.exec_command(
                f"printf '%s' '{escaped}' > /workspace/BUILD_ISSUES.md",
                timeout=8,
            )

            # ── Persist upgrades back to host project ────────────────────────
            # Containers are ephemeral — writing the upgraded package.json back
            # to the HOST means next boot's `npm install` just confirms the
            # already-correct versions. No redundant downloading or upgrading.
            try:
                from pathlib import Path as _Path
                _host_root = None
                if self.sandbox._active and self.sandbox._active.project_path:
                    _host_root = _Path(self.sandbox._active.project_path).resolve()

                if _host_root and _host_root.exists():
                    # Read upgraded package.json from container
                    _pkg_r = await self.sandbox.exec_command(
                        "cat /workspace/package.json 2>/dev/null", timeout=8
                    )
                    if _pkg_r.exit_code == 0 and _pkg_r.stdout.strip():
                        (_host_root / "package.json").write_text(
                            _pkg_r.stdout, encoding="utf-8"
                        )
                        logger.info(
                            "🔄  [Dep Upgrade] ✅ package.json written back to host — upgrades will persist"
                        )

                    # Read upgraded package-lock.json from container
                    _lock_r = await self.sandbox.exec_command(
                        "cat /workspace/package-lock.json 2>/dev/null", timeout=12
                    )
                    if _lock_r.exit_code == 0 and _lock_r.stdout.strip():
                        (_host_root / "package-lock.json").write_text(
                            _lock_r.stdout, encoding="utf-8"
                        )
                        logger.info(
                            "🔄  [Dep Upgrade] ✅ package-lock.json written back to host"
                        )

                    # Clear the BUILD_ISSUES.md flag on the host too so it
                    # doesn't re-trigger the upgrade next session.
                    _issues_host = _host_root / "BUILD_ISSUES.md"
                    if _issues_host.exists():
                        _issues_src = _issues_host.read_text(encoding="utf-8", errors="replace")
                        _issues_new = _re.sub(
                            r'## ⚠ Outdated Dependencies.*?(?=\n##|\Z)',
                            (
                                f"## ✅ Major Deps Auto-Upgraded\n"
                                f"Packages upgraded to @latest and verified with tsc: {pkg_display}\n"
                                f"(Persisted to host workspace — upgrades are permanent)\n"
                            ),
                            _issues_src, flags=_re.DOTALL,
                        )
                        _issues_host.write_text(_issues_new, encoding="utf-8")
                        logger.info("🔄  [Dep Upgrade] ✅ BUILD_ISSUES.md updated on host")
            except Exception as _wb_exc:
                logger.debug("🔄  [Dep Upgrade] Host write-back failed (non-fatal): %s", _wb_exc)

        else:
            # ❌ tsc broke — revert and annotate BUILD_ISSUES.md with error context
            logger.warning(
                "🔄  [Dep Upgrade] tsc errors after upgrade — reverting: %s",
                pkg_display,
            )
            await self.sandbox.exec_command(
                "cd /workspace && "
                "cp package-lock.json.pre-upgrade package-lock.json 2>/dev/null; "
                "cp package.json.pre-upgrade package.json 2>/dev/null && "
                "npm install --legacy-peer-deps --prefer-offline 2>/dev/null",
                timeout=120,
            )
            # Append error context so CLI knows exactly what failed
            ts_errors = (tsc_r.stdout or "").strip()[:600]
            annotation = (
                f"\n\n### ⚠ Auto-Upgrade Attempted — tsc Errors\n"
                f"Upgrade of `{pkg_display}` to `@latest` was reverted because `tsc --noEmit` failed.\n"
                f"Fix the breaking API changes in the source before upgrading.\n\n"
                f"```\n{ts_errors}\n```\n"
            )
            escaped_ann = annotation.replace("'", "'\\''")
            await self.sandbox.exec_command(
                f"printf '%s' '{escaped_ann}' >> /workspace/BUILD_ISSUES.md",
                timeout=8,
            )
            logger.info("🔄  [Dep Upgrade] Reverted — tsc error context appended to BUILD_ISSUES.md")

        # Clean up backup files
        await self.sandbox.exec_command(
            "rm -f /workspace/package-lock.json.pre-upgrade "
            "/workspace/package.json.pre-upgrade",
            timeout=5,
        )

    async def start_dev_server(self, timeout: int = 60) -> TaskResult:
        """
        Start the dev server in the background inside the sandbox.

        Auto-detects the right command (npm run dev, python -m http.server, etc.)
        Uses nohup to survive docker exec session closure.
        Installs npm deps if package.json exists but node_modules doesn't.
        Uses $DEV_SERVER_PORT env var for port binding.

        V44: Auto-scaffolds package.json for static HTML projects so they get
        proper CORS headers and MIME type handling via npx serve.
        V54: Guarded by _dev_server_lock — concurrent calls queue instead of
        racing (prevents double reinstall + double start log lines).
        """
        # Only one start at a time — if already starting, skip and return.
        if self._dev_server_lock.locked():
            logger.debug("🖥️  [Dev Server] Start already in progress — skipping concurrent call")
            return TaskResult(status="skipped", output="dev server start already in progress")

        async with self._dev_server_lock:
            return await self._start_dev_server_impl(timeout)

    async def _start_dev_server_impl(self, timeout: int = 60) -> TaskResult:
        """Internal implementation — always called under _dev_server_lock."""
        # V54: Upgrade npm/vite/tsc to latest once per session before first server start
        await self._upgrade_sandbox_tooling()

        # Resolve the port the container expects
        port = 3000
        if self.sandbox._active:
            port = self.sandbox._active.preview_port or 3000

        # V51: Kill any stale process on the port before starting
        # Prevents "address already in use" from previous crashed dev servers
        try:
            await self.sandbox.exec_command(
                f"fuser -k {port}/tcp 2>/dev/null; "
                "pkill -f 'vite|next|nuxt|webpack|serve|uvicorn|flask' 2>/dev/null; "
                "rm -rf node_modules/.vite .vite .next .nuxt "
                ".output .turbo .parcel-cache 2>/dev/null; "
                "sleep 0.5",
                timeout=8,
            )
        except Exception:
            pass

        has_package_json = await self.sandbox.file_exists("package.json")
        has_node_modules = await self.sandbox.file_exists("node_modules")
        has_index_html = await self.sandbox.file_exists("index.html")
        has_index_php = await self.sandbox.file_exists("index.php")

        # ── V44: Auto-scaffold package.json for static HTML projects ──
        # Without this, static HTML projects fall through to python3 http.server,
        # which doesn't set CORS headers or proper MIME types, breaking the
        # preview iframe and "Open in Browser" functionality.
        if not has_package_json and has_index_html:
            logger.info(
                "🖥️  [Dev Server] Static HTML project detected — "
                "auto-creating package.json with serve script"
            )
            minimal_pkg = json.dumps({
                "name": "static-site",
                "private": True,
                "scripts": {
                    "dev": f"npx -y serve . -l {port} --cors"
                }
            }, indent=2)
            await self.sandbox.write_file("package.json", minimal_pkg)
            has_package_json = True
            logger.info("📦  [Dev Server] Auto-created package.json with CORS-enabled serve")

        # ── Step 1: Install dependencies if needed ──
        _needs_install = has_package_json and not has_node_modules
        
        # V51: Even if node_modules exists, check for corruption.
        # After version bumps, internal chunks can reference files that
        # no longer exist (dep-BK3b2jBa.js → dep-D-7KCb9p.js).
        # Quick probe: try to actually load the dev server framework.
        if has_package_json and has_node_modules and not _needs_install:
            try:
                _quick_probe = await self.sandbox.exec_command(
                    'node -e "'
                    "try{var v=require('vite/package.json');"
                    "var m=require.resolve('vite');process.exit(0)}"
                    "catch(e){"
                    "if(String(e).includes('Cannot find module'))process.exit(1);"
                    "process.exit(0)}"
                    '" 2>&1 || '
                    'node -e "process.exit(0)" 2>&1',
                    timeout=8,
                )
                _probe_stderr = (_quick_probe.stdout or "") + (_quick_probe.stderr or "")
                if "Cannot find module" in _probe_stderr or _quick_probe.exit_code == 1:
                    logger.warning(
                        "📦  [Dev Server] Corrupted node_modules detected — forcing reinstall"
                    )
                    await self.sandbox.exec_command(
                        "rm -rf node_modules package-lock.json .vite 2>/dev/null || true",
                        timeout=20,
                    )
                    _needs_install = True
            except Exception:
                pass

        if _needs_install:
            logger.info("📦  [Dev Server] Installing npm dependencies …")
            # Grant network for install
            if hasattr(self.sandbox, 'grant_network'):
                await self.sandbox.grant_network()
            try:
                install_result = await self.sandbox.exec_command(
                    "npm install --legacy-peer-deps --no-audit --no-fund 2>&1 | tail -10",
                    timeout=180,
                )
                if install_result.exit_code != 0:
                    logger.warning("📦  [Dev Server] npm install failed — trying nuclear clean with --force …")
                    await self.sandbox.exec_command(
                        "rm -rf node_modules package-lock.json 2>/dev/null; "
                        "npm install --force --no-audit --no-fund 2>&1 | tail -10",
                        timeout=180,
                    )
            except Exception as _inst_exc:
                logger.warning("📦  [Dev Server] Install exception: %s", _inst_exc)
            # NOTE: Do NOT revoke network here — npx serve fallback may need it

            # V44-FIX: Ensure Vite's cache dir is writable by sandbox user.
            # Docker copy-in can leave node_modules owned by root, causing
            # EACCES when Vite tries to create .vite/deps_temp_* directories.
            await self.sandbox.exec_command(
                "mkdir -p /workspace/node_modules/.vite && "
                "chmod -R 777 /workspace/node_modules/.vite 2>/dev/null; "
                "chmod -R u+w /workspace/node_modules 2>/dev/null || true",
                timeout=10,
            )
        elif has_package_json and has_node_modules:
            # Existing node_modules — just ensure Vite cache is writable
            await self.sandbox.exec_command(
                "mkdir -p /workspace/node_modules/.vite && "
                "chmod -R 777 /workspace/node_modules/.vite 2>/dev/null; "
                "chmod -R u+w /workspace/node_modules 2>/dev/null || true",
                timeout=10,
            )

        # ── Step 2: Determine the right command ──
        # V42: If index.php exists, check whether package.json scripts actually
        # serve HTTP. Many PHP projects have package.json for build tools only
        # (Tailwind, PostCSS, etc.) — those don't serve the site.
        # V44: Also check if PHP is actually available in the container.
        _use_php = False
        if has_index_php:
            # First check if PHP is even installed in the sandbox
            php_check = await self.sandbox.exec_command("which php 2>/dev/null")
            _php_available = php_check.exit_code == 0 and php_check.stdout.strip()
            if not _php_available:
                logger.info(
                    "🖥️  [Dev Server] index.php found but PHP not installed in sandbox "
                    "— falling through to npm/static serving"
                )
            elif has_package_json:
                try:
                    pkg_content = await self.sandbox.read_file("package.json")
                    pkg = json.loads(pkg_content)
                    scripts = pkg.get("scripts", {})
                    # Check if any script suggests an HTTP server
                    _all_scripts = " ".join(scripts.values()).lower()
                    _serves_http = any(kw in _all_scripts for kw in [
                        "serve", "vite", "next", "nuxt", "webpack-dev-server",
                        "react-scripts", "http-server", "live-server", "parcel",
                    ])
                    _use_php = not _serves_http
                    if _use_php:
                        logger.info(
                            "🖥️  [Dev Server] package.json has no HTTP server scripts "
                            "— using PHP built-in server for index.php"
                        )
                        # Still install npm deps for build tools (tailwind etc.)
                        if not has_node_modules:
                            logger.info("📦  [Dev Server] Installing npm build tools …")
                            await self.sandbox.exec_command(
                                "npm install --no-audit --no-fund 2>&1 | tail -5",
                                timeout=120,
                            )
                        # Run build script in background if it exists
                        if "build" in scripts:
                            await self.sandbox.exec_command(
                                "nohup npm run build > /tmp/npm-build.log 2>&1 &",
                                timeout=15,
                            )
                except Exception:
                    _use_php = True  # If we can't read package.json, assume PHP
            else:
                _use_php = _php_available

        if _use_php:
            cmd = f"nohup php -S 0.0.0.0:{port} -t /workspace > /tmp/dev-server.log 2>&1 &"
        elif has_package_json:
            pkg_content = await self.sandbox.read_file("package.json")
            try:
                pkg = json.loads(pkg_content)
                scripts = pkg.get("scripts", {})

                # V51: Node heap size — auto-set for large projects
                # Node.js defaults to ~1.5GB heap which OOMs on large builds.
                _node_opts = ""
                _all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if len(_all_deps) > 100:
                    _node_opts = "NODE_OPTIONS='--max-old-space-size=4096' "
                    logger.info(
                        "📦  [Dev Server] Large project (%d deps) — setting Node heap to 4GB",
                        len(_all_deps),
                    )

                # V51: Monorepo workspace detection.
                # If root package.json has "workspaces", find the workspace
                # that has a dev script and cd into it before running.
                _ws_prefix = ""
                _workspaces = pkg.get("workspaces", [])
                # Handle yarn-style { packages: [...] } format
                if isinstance(_workspaces, dict):
                    _workspaces = _workspaces.get("packages", [])
                if _workspaces and not scripts.get("dev"):
                    logger.info(
                        "📦  [Dev Server] Monorepo detected — scanning %d workspace patterns",
                        len(_workspaces),
                    )
                    # Find first workspace with a dev script
                    try:
                        _ws_find = await self.sandbox.exec_command(
                            "find . -maxdepth 3 -name package.json -not -path '*/node_modules/*' "
                            "| head -20",
                            timeout=5,
                        )
                        for _ws_pkg_path in (_ws_find.stdout or "").strip().split("\n"):
                            _ws_pkg_path = _ws_pkg_path.strip()
                            if not _ws_pkg_path or _ws_pkg_path == "./package.json":
                                continue
                            try:
                                _ws_content = await self.sandbox.exec_command(
                                    f"cat {_ws_pkg_path} 2>/dev/null", timeout=3,
                                )
                                _ws_pkg = json.loads(_ws_content.stdout or "{}")
                                _ws_scripts = _ws_pkg.get("scripts", {})
                                if "dev" in _ws_scripts:
                                    import os as _os
                                    _ws_dir = _os.path.dirname(_ws_pkg_path)
                                    _ws_prefix = f"cd {_ws_dir} && "
                                    scripts = _ws_scripts  # Use workspace's scripts
                                    logger.info(
                                        "📦  [Dev Server] Using workspace: %s", _ws_dir,
                                    )
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

                if "dev" in scripts:
                    dev_script = scripts["dev"].lower()
                    # V51: Framework-specific host binding.
                    # ALL frameworks default to localhost inside Docker,
                    # which makes the preview invisible to the host.
                    # Each needs its own flag for 0.0.0.0 binding.
                    if "vite" in dev_script or "svelte" in dev_script:
                        # V61: Wipe Vite dep-optimisation cache before every start.
                        # IMPORTANT: do NOT recreate the .vite directory — let Vite
                        # create it from scratch so it generates a fresh hash.
                        # An empty .vite dir confuses Vite and causes the same 504.
                        # --force also tells Vite to re-optimise on start regardless
                        # of its internal cache-staleness check.
                        await self.sandbox.exec_command(
                            "rm -rf node_modules/.vite 2>/dev/null || true",
                            timeout=5,
                        )
                        cmd = (
                            f"{_node_opts}nohup {_ws_prefix}npm run dev -- "
                            f"--port {port} --host 0.0.0.0 --force"
                            f" > /tmp/dev-server.log 2>&1 &"
                        )
                    elif "next" in dev_script:
                        cmd = (
                            f"{_node_opts}nohup {_ws_prefix}npm run dev -- -p {port} -H 0.0.0.0"
                            f" > /tmp/dev-server.log 2>&1 &"
                        )
                    elif "nuxt" in dev_script:
                        cmd = (
                            f"{_node_opts}nohup {_ws_prefix}npm run dev -- --port {port} --host 0.0.0.0"
                            f" > /tmp/dev-server.log 2>&1 &"
                        )
                    elif "astro" in dev_script:
                        cmd = (
                            f"{_node_opts}nohup {_ws_prefix}npm run dev -- --port {port} --host 0.0.0.0"
                            f" > /tmp/dev-server.log 2>&1 &"
                        )
                    elif "webpack" in dev_script:
                        cmd = (
                            f"{_node_opts}nohup {_ws_prefix}npm run dev -- --port {port} --host 0.0.0.0"
                            f" > /tmp/dev-server.log 2>&1 &"
                        )
                    else:
                        # Generic: set PORT + HOST env vars — many frameworks read these
                        cmd = (
                            f"{_node_opts}PORT={port} HOST=0.0.0.0 "
                            f"nohup {_ws_prefix}npm run dev > /tmp/dev-server.log 2>&1 &"
                        )
                elif "build" in scripts and "start" in scripts:
                    # V51: Build-then-serve — project has no dev script
                    # but has build + start (production-only workflow).
                    # Run build first, then start the production server.
                    logger.info("📦  [Dev Server] No dev script — running build + start")
                    await self.sandbox.exec_command(
                        f"{_node_opts}{_ws_prefix}npm run build > /tmp/npm-build.log 2>&1",
                        timeout=120,
                    )
                    start_script = scripts["start"].lower()
                    if "next" in start_script:
                        cmd = (
                            f"{_node_opts}nohup {_ws_prefix}npm start -- -p {port} -H 0.0.0.0"
                            f" > /tmp/dev-server.log 2>&1 &"
                        )
                    else:
                        cmd = (
                            f"{_node_opts}PORT={port} HOST=0.0.0.0 "
                            f"nohup {_ws_prefix}npm start > /tmp/dev-server.log 2>&1 &"
                        )
                elif "start" in scripts:
                    start_script = scripts["start"].lower()
                    if "next" in start_script:
                        cmd = (
                            f"{_node_opts}nohup {_ws_prefix}npm start -- -p {port} -H 0.0.0.0"
                            f" > /tmp/dev-server.log 2>&1 &"
                        )
                    else:
                        cmd = (
                            f"{_node_opts}PORT={port} HOST=0.0.0.0 "
                            f"nohup {_ws_prefix}npm start > /tmp/dev-server.log 2>&1 &"
                        )
                else:
                    cmd = (
                        f"nohup npx -y serve . -l {port} --cors --single"
                        f" > /tmp/dev-server.log 2>&1 &"
                    )
            except json.JSONDecodeError:
                cmd = (
                    f"nohup npx -y serve . -l {port} --cors --single"
                    f" > /tmp/dev-server.log 2>&1 &"
                )
        else:
            # V51: Check for Python web frameworks before falling back
            _py_cmd = None
            has_manage_py = await self.sandbox.file_exists("manage.py")
            has_app_py = await self.sandbox.file_exists("app.py")
            has_main_py = await self.sandbox.file_exists("main.py")
            
            if has_manage_py:
                # Django
                _py_cmd = (
                    f"nohup python3 manage.py runserver 0.0.0.0:{port}"
                    f" > /tmp/dev-server.log 2>&1 &"
                )
            elif has_app_py:
                # Flask / FastAPI — check if it's flask or fastapi
                _app_content = await self.sandbox.exec_command(
                    "head -20 app.py 2>/dev/null", timeout=5,
                )
                _app_head = (_app_content.stdout or "").lower()
                if "fastapi" in _app_head or "uvicorn" in _app_head:
                    _py_cmd = (
                        f"nohup uvicorn app:app --host 0.0.0.0 --port {port}"
                        f" > /tmp/dev-server.log 2>&1 &"
                    )
                elif "flask" in _app_head:
                    _py_cmd = (
                        f"nohup flask run --host 0.0.0.0 --port {port}"
                        f" > /tmp/dev-server.log 2>&1 &"
                    )
            elif has_main_py:
                _main_content = await self.sandbox.exec_command(
                    "head -20 main.py 2>/dev/null", timeout=5,
                )
                _main_head = (_main_content.stdout or "").lower()
                if "fastapi" in _main_head or "uvicorn" in _main_head:
                    _py_cmd = (
                        f"nohup uvicorn main:app --host 0.0.0.0 --port {port}"
                        f" > /tmp/dev-server.log 2>&1 &"
                    )
            
            if _py_cmd:
                # Install Python deps first if requirements.txt exists
                has_requirements = await self.sandbox.file_exists("requirements.txt")
                if has_requirements:
                    await self.sandbox.exec_command(
                        "pip install -r requirements.txt 2>&1 | tail -5",
                        timeout=120,
                    )
                cmd = _py_cmd
            else:
                # Last resort: python http.server (no CORS, but at least serves files)
                cmd = f"nohup python3 -m http.server {port} > /tmp/dev-server.log 2>&1 &"

        # ── Step 3: Pre-flight vite chunk integrity check ──
        # Run BEFORE launching to catch mid-session chunk corruption
        # (partial sync, interrupted install, etc.) that would cause an
        # immediate crash. Wipes + reinstalls if chunks are broken.
        if has_package_json and "npm" in cmd:
            _ws = (self.sandbox._active.workspace_path if self.sandbox._active else None) or "/workspace"
            _pf = await self.sandbox.exec_command(
                f"cd {_ws} && node -e \"require('vite')\" 2>&1 | head -3",
                timeout=12,
            )
            _pf_out = (_pf.stdout or "").lower()
            if "cannot find module" in _pf_out and "chunk" in _pf_out:
                logger.warning(
                    "🖥️  [Dev Server] ⚠️  Vite chunk corruption detected before launch — "
                    "performing clean reinstall …"
                )
                await self.sandbox.exec_command(
                    f"cd {_ws} && rm -rf node_modules .vite 2>/dev/null; "
                    f"npm install --no-audit --no-fund --loglevel=warn 2>&1 | tail -5",
                    timeout=300,
                )
                logger.info("🖥️  [Dev Server] Reinstall complete — proceeding with launch")

        # ── Step 4: Launch in background ──
        logger.info("🖥️  [Dev Server] Starting: %s", cmd)
        self._last_dev_cmd = cmd  # V53: stored for restart after auto-install
        result = TaskResult(prompt_used=cmd)
        start_time = time.time()

        try:
            cmd_result = await self.sandbox.exec_command(cmd, timeout=15)
            result.exit_code = cmd_result.exit_code
            result.output = cmd_result.stdout
            result.status = "success" if cmd_result.exit_code == 0 else "error"
            if cmd_result.stderr:
                result.errors.append(cmd_result.stderr[:500])

            # V61: Patch any service worker to skip non-http(s) requests.
            # Browser extensions (React DevTools etc.) inject chrome-extension://
            # URLs that get intercepted by the SW; Cache API rejects them.
            try:
                _sw_sed = "s/event\\.request\\.url/event.request.url.startsWith('http') \\&\\& event.request.url/g"
                await self.sandbox.exec_command(
                    "find . -maxdepth 6 -type f "
                    r"\( -name 'sw.js' -o -name 'service-worker.js' -o -name 'serviceWorker.js' \) "
                    "-not -path '*/node_modules/*' -not -path '*/.vite/*' "
                    "| head -5 | while read f; do "
                    "  grep -q \"startsWith.*http\" \"$f\" 2>/dev/null || "
                    f"  sed -i '{_sw_sed}' \"$f\" 2>/dev/null || true; "
                    "done",
                    timeout=8,
                )
            except Exception:
                pass
        except Exception as exc:
            result.status = "error"
            result.errors.append(str(exc))

        # ── Step 5: Detect and start data services + backend servers ──
        # Only runs if we successfully launched the frontend (or there is no frontend).
        # Non-fatal: exceptions here don't abort the whole start.
        try:
            # 5a — Data services (Postgres, Redis, MongoDB)
            self._services = await self._detect_services()
            if self._services:
                await self._start_services(self._services)

            # 5b — Backend server processes
            # Only probe if the project is NOT a plain Python/PHP app that IS
            # already the main server (those are handled in Step 2 above).
            _is_full_stack = has_package_json  # frontend is JS — check for backends too
            if _is_full_stack:
                detected_backends = await self._detect_backends()
                self._backends = []
                for i, bk in enumerate(detected_backends):
                    bk["port"] = self._backend_port_base + i
                    # 1. Install backend dependencies (includes Prisma generate + db push)
                    await self._install_backend_deps(bk["folder"], bk["framework"])
                    # 2. Scaffold .env for this backend if none exists
                    await self._ensure_env_file(
                        bk["folder"], bk["framework"],
                        port, bk["port"], self._services, i,
                    )
                    # 3. Run database migrations (Django/Alembic/Sequelize/TypeORM/Knex)
                    await self._run_db_migrations(bk["folder"], bk["framework"])
                    # 4. Launch this backend server (with crash auto-restart loop)
                    await self._start_backend_server(bk)
                    # 5. Start background workers (BullMQ / Celery) if detected
                    await self._start_workers(bk)
                    self._backends.append(bk)

                # Update frontend .env with API URL vars for all backends
                if self._backends:
                    # Detect which frontend framework we're running
                    _fe_dev_cmd = (result.prompt_used or "").lower()
                    _fe_fw = "next" if "next" in _fe_dev_cmd else "vite"
                    await self._ensure_frontend_env(port, self._backends, _fe_fw)
        except Exception as _be_exc:
            logger.warning("🖥️  [Backend] Step 5 error (non-fatal): %s", _be_exc)

        # ── Step 4: Poll for server readiness ──
        server_up = False
        for attempt in range(3):
            await asyncio.sleep(3)
            server = await self.tools.dev_server_check([port])
            if server.running:
                server_up = True
                logger.info(
                    "🖥️  [Dev Server] Running on port %d (attempt %d)",
                    server.port, attempt + 1,
                )
                break
            logger.debug(
                "🖥️  [Dev Server] Not yet ready (attempt %d/3)", attempt + 1
            )

        if not server_up:
            # Check the log for clues
            log_result = await self.sandbox.exec_command(
                "tail -20 /tmp/dev-server.log 2>/dev/null"
            )
            _crash_log = log_result.stdout.strip() if log_result.stdout else ""
            if _crash_log:
                logger.warning(
                    "🖥️  [Dev Server] Primary command failed. Log tail:\n%s",
                    _crash_log[:500],
                )

            # ── V52: Tiered error classification & recovery ──
            # Instead of a narrow allowlist, classify the error and take
            # the appropriate action for each category.
            import re as _re_srv

            # Category 1: Missing binary (vite: not found, tsc: not found)
            # Also covers MODULE_NOT_FOUND when the path is in node_modules —
            # that means the binary/package was nuked or corrupted, not a dep issue.
            _is_missing_binary = (
                ": not found" in _crash_log
                or "command not found" in _crash_log
                or ("MODULE_NOT_FOUND" in _crash_log and "node_modules" in _crash_log)
            )

            # Category 2: Missing dependency (Could not resolve "X",
            #   Cannot find module 'X', Module not found: 'X')
            _missing_dep_patterns = _re_srv.findall(
                r'Could not resolve "([^"]+)"'
                r'|Cannot find module [\'"]([^\'"]+)[\'"]'
                r"|Module not found.*?'([^']+)'"
                r"|ERR_MODULE_NOT_FOUND.*?'([^']+)'"
                r"|Cannot find type definition file for ['\"]([^'\"]+)['\"]",
                _crash_log,
            )
            # Flatten tuples from alternation groups, filter empties
            _missing_deps = list(set(
                dep for group in _missing_dep_patterns
                for dep in group if dep
                # Only keep actual npm package names (not relative paths)
                and not dep.startswith(".")
                and not dep.startswith("/")
            ))

            # Category 3: Port conflict
            _is_port_conflict = "EADDRINUSE" in _crash_log

            # Category 4: Build/config error (syntax, type errors, etc.)
            _is_build_error = bool(_crash_log) and not _is_missing_binary and not _missing_deps and not _is_port_conflict

            # Determine if this project has a bundler (Vite/Next/Webpack etc.)
            # If it does, python3 fallback is USELESS — it serves raw .tsx files
            # Check cmd string, crash log, AND the dev script content.
            # cmd is often just 'npm run dev' which doesn't contain 'vite'.
            _bundler_signals = (cmd or "").lower() + " " + _crash_log.lower()
            _has_bundler = has_package_json and (
                "vite" in _bundler_signals
                or "webpack" in _bundler_signals
                or "next" in _bundler_signals
                or "nuxt" in _bundler_signals
                or "react-scripts" in _bundler_signals
            )

            # ── Recovery: Missing binary → smart binary-first recovery ──
            if _is_missing_binary and has_package_json:
                # V55 Fix #2: Before doing a full nuclear reinstall, check if
                # the binary already exists in node_modules/.bin but is just not
                # on PATH. If so, update the command to use the direct path.
                # This avoids the 25-40s clean-reinstall that fires 3-4x/session.
                try:
                    _bin_check = await self.sandbox.exec_command(
                        "ls /workspace/node_modules/.bin/vite 2>/dev/null && echo 'FOUND' || echo 'MISSING'",
                        timeout=5,
                    )
                    _bin_exists = "FOUND" in (_bin_check.stdout or "")
                except Exception:
                    _bin_exists = False

                if _bin_exists:
                    # Binary exists but PATH doesn't have it — rewrite the cmd to use the direct path
                    _fixed_cmd = cmd.replace("vite ", "./node_modules/.bin/vite ").replace("' vite'", "' ./node_modules/.bin/vite'")
                    if _fixed_cmd == cmd:
                        # Generic case: just prepend PATH fix
                        _fixed_cmd = f"export PATH=/workspace/node_modules/.bin:$PATH && {cmd}"
                    logger.info("🖥️  [Dev Server] Vite binary found at node_modules/.bin — retrying with direct path …")
                    try:
                        await self.sandbox.exec_command(_fixed_cmd, timeout=15)
                        for _ra in range(5):
                            await asyncio.sleep(5)
                            server = await self.tools.dev_server_check([port])
                            if server.running:
                                server_up = True
                                logger.info("🖥️  [Dev Server] Recovery successful (PATH fix) on port %d", server.port)
                                break
                    except Exception:
                        pass

                if not server_up:
                    # Fast path: npm install --prefer-offline reuses local cache (~5s)
                    logger.warning("🖥️  [Dev Server] Missing binary — fast reinstall (prefer-offline) …")
                    try:
                        if hasattr(self.sandbox, 'grant_network'):
                            await self.sandbox.grant_network()

                        _inst = await self.sandbox.exec_command(
                            # V55: prefer-offline reuses npm cache — much faster than clean reinstall
                            # when the packages are already cached from earlier in the session.
                            "npm install --prefer-offline --no-audit --no-fund 2>&1 | tail -10",
                            timeout=120,
                        )
                        if _inst.exit_code != 0:
                            # Fallback: nuclear clean reinstall (last resort)
                            logger.warning("🖥️  [Dev Server] Fast reinstall failed — falling back to nuclear clean …")
                            _inst = await self.sandbox.exec_command(
                                "rm -rf node_modules package-lock.json .vite 2>/dev/null; "
                                "npm install --force --no-audit --no-fund 2>&1 | tail -10",
                                timeout=180,
                            )

                        if _inst.exit_code == 0:
                            logger.info("🖥️  [Dev Server] Reinstall succeeded — retrying server …")
                            await self.sandbox.exec_command(
                                f"fuser -k {port}/tcp 2>/dev/null || true", timeout=5,
                            )
                            await self.sandbox.exec_command(cmd, timeout=15)
                            for _ra in range(5):
                                await asyncio.sleep(5)
                                server = await self.tools.dev_server_check([port])
                                if server.running:
                                    server_up = True
                                    logger.info(
                                        "🖥️  [Dev Server] Recovery successful on port %d", server.port,
                                    )
                                    break
                        else:
                            logger.warning("🖥️  [Dev Server] Reinstall failed (exit %d)", _inst.exit_code)
                    except Exception as _exc:
                        logger.warning("🖥️  [Dev Server] Reinstall recovery failed: %s", _exc)

            # ── Recovery: Missing dependency → targeted install ──
            elif _missing_deps and has_package_json:
                # Only install up to 5 missing deps to avoid runaway installs
                _to_install = _missing_deps[:5]
                _install_str = " ".join(_to_install)
                logger.warning(
                    "🖥️  [Dev Server] Missing %d dep(s): %s — auto-installing …",
                    len(_to_install), _install_str,
                )
                try:
                    if hasattr(self.sandbox, 'grant_network'):
                        await self.sandbox.grant_network()

                    _inst = await self.sandbox.exec_command(
                        f"npm install --force --no-audit --no-fund {_install_str} 2>&1 | tail -10",
                        timeout=120,
                    )
                    if _inst.exit_code == 0:
                        logger.info("🖥️  [Dev Server] Dep install succeeded — retrying server …")
                        await self.sandbox.exec_command(
                            f"fuser -k {port}/tcp 2>/dev/null || true", timeout=5,
                        )
                        await self.sandbox.exec_command(cmd, timeout=15)
                        for _ra in range(5):
                            await asyncio.sleep(5)
                            server = await self.tools.dev_server_check([port])
                            if server.running:
                                server_up = True
                                logger.info(
                                    "🖥️  [Dev Server] Recovery successful on port %d", server.port,
                                )
                                break
                        if not server_up:
                            # Check if there are NEW missing deps after installing first batch
                            _retry_log = await self.sandbox.exec_command(
                                "tail -10 /tmp/dev-server.log 2>/dev/null", timeout=5,
                            )
                            _retry_tail = (_retry_log.stdout or "").strip()
                            if _retry_tail:
                                logger.warning(
                                    "🖥️  [Dev Server] Still failing after dep install:\n%s",
                                    _retry_tail[:300],
                                )
                    else:
                        logger.warning(
                            "🖥️  [Dev Server] Dep install failed: %s",
                            (_inst.stdout or "")[-200:],
                        )
                except Exception as _exc:
                    logger.warning("🖥️  [Dev Server] Dep install recovery failed: %s", _exc)

            # ── Recovery: Port conflict → kill and retry ──
            elif _is_port_conflict:
                logger.warning("🖥️  [Dev Server] Port %d in use — killing and retrying …", port)
                await self.sandbox.exec_command(
                    f"fuser -k {port}/tcp 2>/dev/null || true", timeout=5,
                )
                await asyncio.sleep(1)
                await self.sandbox.exec_command(cmd, timeout=15)
                for _ra in range(3):
                    await asyncio.sleep(3)
                    server = await self.tools.dev_server_check([port])
                    if server.running:
                        server_up = True
                        logger.info("🖥️  [Dev Server] Port kill recovery on port %d", server.port)
                        break

            # ── Build/config error with a bundler → report, DON'T fall to python3 ──
            elif _is_build_error and _has_bundler:
                logger.warning(
                    "🖥️  [Dev Server] Build error detected in bundled project — "
                    "skipping static fallback (python3 can't serve .tsx/.jsx). "
                    "BUILD_ISSUES.md task will fix this."
                )
                # Store the error for UI display
                self._last_dev_server_error = _crash_log[:500]

            # ── V44: Fallback chain for NON-bundled projects ONLY ──
            # npx serve --cors → python3 http.server
            if not server_up and not _has_bundler:
                if "npx" not in cmd and "serve" not in cmd:
                    if hasattr(self.sandbox, 'grant_network'):
                        await self.sandbox.grant_network()
                    await self.sandbox.exec_command(
                        f"fuser -k {port}/tcp 2>/dev/null || true", timeout=5,
                    )
                    fallback_serve = f"nohup npx -y serve . -l {port} --cors --single > /tmp/dev-server.log 2>&1 &"
                    logger.info("🖥️  [Dev Server] Trying fallback: %s", fallback_serve)
                    await self.sandbox.exec_command(fallback_serve, timeout=15)
                    await asyncio.sleep(5)
                    server = await self.tools.dev_server_check([port])
                    if server.running:
                        server_up = True
                        logger.info("🖥️  [Dev Server] npx serve fallback on port %d", server.port)

                if not server_up and "python3" not in cmd:
                    fallback_cmd = f"nohup python3 -m http.server {port} > /tmp/dev-server.log 2>&1 &"
                    logger.info("🖥️  [Dev Server] Trying final fallback: %s", fallback_cmd)
                    await self.sandbox.exec_command(fallback_cmd, timeout=10)
                    await asyncio.sleep(3)
                    server = await self.tools.dev_server_check([port])
                    if server.running:
                        server_up = True
                        logger.info("🖥️  [Dev Server] python3 fallback on port %d", server.port)

        if not server_up:
            logger.warning("🖥️  [Dev Server] All launch attempts failed")
            result.status = "error"
        else:
            # V53: After a successful server start, scan the dev server log
            # for runtime console errors and generate CONSOLE_ISSUES.md.
            # This is separate from BUILD_ISSUES.md (build-time) — these are
            # runtime errors that only appear once the server is actually serving.
            try:
                await self._scan_dev_server_console()
            except Exception as _cse:
                logger.debug("🖥️  [Dev Server] Console scan skipped: %s", _cse)

        result.duration_s = time.time() - start_time
        return result

    async def _scan_dev_server_console(self) -> None:
        """
        V53: Scan /tmp/dev-server.log for runtime errors after a successful
        dev server start.

        Phase 1 — Auto-install missing packages:
          Detects 'Failed to resolve import X' / 'Cannot find module X' lines,
          extracts bare npm package names, runs npm install, then restarts the
          dev server so the fix takes effect immediately.

        Phase 2 — Write CONSOLE_ISSUES.md:
          Records any remaining errors (after auto-install) so the DAG can
          pick them up as a fix task. Writes "All Clear" if the server is clean.

        Phase 3 — Refresh BUILD_ISSUES.md:
          Re-runs build_health_check() so both files reflect the same moment.
        """
        if not self.sandbox or not self.sandbox.is_running:
            return

        # Give the server 3s to emit any startup errors before reading the log
        await asyncio.sleep(3)

        async def _read_log() -> str:
            r = await self.sandbox.exec_command(
                "cat /tmp/dev-server.log 2>/dev/null | tail -300",
                timeout=10,
            )
            return r.stdout or ""

        raw_log = await _read_log()

        # V54: Also scan backend logs for errors
        for _bk in self._backends:
            _bk_log = _bk.get("log", "")
            if not _bk_log:
                continue
            try:
                _bk_r = await self.sandbox.exec_command(
                    f"cat {_bk_log} 2>/dev/null | tail -100", timeout=8
                )
                _bk_content = (_bk_r.stdout or "").strip()
                if _bk_content:
                    _bk_port = _bk.get("port", "?")
                    _bk_fw   = _bk.get("framework", "?")
                    # Log errors/warnings from backend
                    _bk_errors = [
                        l for l in _bk_content.splitlines()
                        if any(kw in l.lower() for kw in ["error", "failed", "exception", "cannot"])
                    ]
                    if _bk_errors:
                        logger.warning(
                            "🖥️  [Backend:%s/%s] %d error line(s) in log:\n%s",
                            _bk_port, _bk_fw, len(_bk_errors),
                            "\n".join(_bk_errors[:5])
                        )
                    else:
                        logger.debug("🖥️  [Backend:%s] Log looks clean", _bk_port)
            except Exception:
                pass

        if not raw_log.strip():
            return

        # ── Phase 1: Detect and auto-install missing packages ─────────────
        import re as _re

        # Match patterns:
        #   Failed to resolve import "react-i18next"
        #   Cannot find module 'lodash'
        #   Module not found: Error: Can't resolve 'axios'
        _missing_re = _re.compile(
            r'(?:failed to resolve import|cannot find module|can\'t resolve|'
            r'could not resolve import)\s+["\']([^"\']+)["\']',
            _re.IGNORECASE,
        )

        def _extract_pkg(specifier: str) -> str | None:
            """Normalise an import specifier to a bare npm package name."""
            s = specifier.strip()
            # Skip: relative paths, virtual:, node:, /, \0
            if s.startswith(('.', '/', 'virtual:', 'node:', '\0')):
                return None
            # Scoped package: @org/name → keep both parts
            if s.startswith('@'):
                parts = s.split('/')
                return '/'.join(parts[:2]) if len(parts) >= 2 else None
            # Un-scoped: take the first path segment (e.g. 'react-dom/client' → 'react-dom')
            return s.split('/')[0] or None

        missing_pkgs: set[str] = set()
        for match in _missing_re.finditer(raw_log):
            pkg = _extract_pkg(match.group(1))
            if pkg:
                missing_pkgs.add(pkg)

        if missing_pkgs:
            pkg_list = ' '.join(sorted(missing_pkgs))
            logger.info("📦  [npm] Installing: %s …", ', '.join(sorted(missing_pkgs)))
            install_result = await self.sandbox.exec_command(
                f"cd /workspace && npm install --legacy-peer-deps {pkg_list} 2>&1",
                timeout=120,
            )
            _log_npm_output(install_result.stdout or "", source="Console Health")
            if install_result.exit_code == 0:
                logger.info("📦  [npm] ✅ Installed — restarting dev server …")
                # Kill the running server and restart it so changes take effect
                try:
                    await self.sandbox.exec_command(
                        "pkill -f 'vite|next dev|nuxt dev|webpack.*serve' 2>/dev/null; "
                        "sleep 1",
                        timeout=8,
                    )
                    # Re-start using the same command stored in the sandbox
                    _ds_cmd = getattr(self.sandbox, '_last_dev_cmd', None)
                    if _ds_cmd:
                        await self.sandbox.exec_command(_ds_cmd, timeout=15)
                        await asyncio.sleep(5)
                except Exception as _restart_exc:
                    logger.debug("📦  [Console Health] Dev server restart skipped: %s", _restart_exc)

                # Re-read log with fresh content after restart
                await asyncio.sleep(3)
                raw_log = await _read_log()
            else:
                logger.warning("📦  [npm] Install failed for: %s", ', '.join(sorted(missing_pkgs)))

        # Patterns that signal real runtime problems
        error_patterns = [
            "error",
            "typeerror",
            "referenceerror",
            "syntaxerror",
            "uncaught",
            "unhandledrejection",
            "failed to load",
            "cannot find module",
            "404",
            "500",
            "module not found",
            "enoent",
            "failed to resolve",
            "could not resolve",
        ]
        # Noisy lines to skip (common false positives)
        skip_patterns = [
            "vite v", "local:", "network:", "ready in",
            "hmr update", "page reload", "[vite]",
            "watching for file changes",
            "➜", "→", "✓", "✅",
        ]

        console_errors: list[str] = []
        for line in raw_log.splitlines():
            low = line.lower()
            if any(sp in low for sp in skip_patterns):
                continue
            if any(ep in low for ep in error_patterns):
                clean = line.strip()[:200]
                if clean:
                    console_errors.append(clean)

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped = []
        for e in console_errors:
            key = e[:80].lower()
            if key not in seen:
                seen.add(key)
                deduped.append(e)
        console_errors = deduped[:30]

        import time as _t
        ts = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())

        if not console_errors:
            report = (
                "# Console Health Report\n"
                "> Auto-generated by Supervisor Console Health System\n"
                f"> Updated: {ts}\n\n"
                "## ✅ All Clear\n\n"
                "No runtime console errors detected after dev server start.\n"
            )
        else:
            lines_md = "\n".join(f"- `{e}`" for e in console_errors)
            report = (
                "# Console Health Report\n"
                "> Auto-generated by Supervisor Console Health System\n"
                f"> Updated: {ts}\n\n"
                f"## ❌ Runtime Console Errors ({len(console_errors)} found)\n\n"
                "These errors appeared in the dev server output after startup.\n"
                "Fix them so the app renders without runtime failures.\n\n"
                f"{lines_md}\n\n"
                "---\n"
                "_Once all errors are resolved, delete this file or ensure the dev "
                "server log shows no errors on next start._\n"
            )

        # Write to container workspace
        escaped = report.replace("'", "'\\''")
        write_result = await self.sandbox.exec_command(
            f"printf '%s' '{escaped}' > /workspace/CONSOLE_ISSUES.md",
            timeout=8,
        )

        if write_result.exit_code == 0:
            issue_count = len(console_errors)
            if issue_count:
                logger.info(
                    "🖥️  [Console Health] %d issue(s) written to CONSOLE_ISSUES.md",
                    issue_count,
                )
            else:
                logger.info("🖥️  [Console Health] Dev server clean — CONSOLE_ISSUES.md marked All Clear")
        else:
            logger.debug("🖥️  [Console Health] Failed to write CONSOLE_ISSUES.md: %s", write_result.stderr[:100])

        # V53: Also re-run the full build health check so BUILD_ISSUES.md is
        # refreshed to reflect the current post-server-start state.
        # Both files are then in sync and cover the same snapshot in time.
        try:
            logger.info("🔍  [Build Health] Re-running post-server build health check …")
            await self.build_health_check()
        except Exception as _bhe:
            logger.debug("🔍  [Build Health] Post-server re-run skipped: %s", _bhe)

    async def resolve_missing_imports(self) -> set[str]:
        """
        V53: Standalone missing-import resolver — called after EVERY task
        completion when new source files may have introduced new import
        specifiers that aren't yet installed.

        Reads the live dev server log, extracts unresolved npm package names
        from Vite/Webpack error lines (Failed to resolve import, Cannot find
        module, etc.), installs them in one npm install call, then restarts
        the dev server.

        Returns the set of package names that were installed (empty set if
        nothing was needed or no server is running).
        """
        import re as _re

        if not self.sandbox or not self.sandbox.is_running:
            return set()

        # Only scan if a dev server is actually running
        server = await self.tools.dev_server_check([3000, 5173, 4173, 8080, 8000])
        if not server.running:
            return set()

        log_r = await self.sandbox.exec_command(
            "cat /tmp/dev-server.log 2>/dev/null | tail -300",
            timeout=10,
        )
        raw_log = log_r.stdout or ""
        if not raw_log.strip():
            return set()

        _missing_re = _re.compile(
            r'(?:failed to resolve import|cannot find module|can\'t resolve|'
            r'could not resolve import)\s+["\']([^"\']+)["\']',
            _re.IGNORECASE,
        )

        def _pkg(specifier: str) -> str | None:
            s = specifier.strip()
            if s.startswith(('.', '/', 'virtual:', 'node:', '\0')):
                return None
            if s.startswith('@'):
                parts = s.split('/')
                return '/'.join(parts[:2]) if len(parts) >= 2 else None
            return s.split('/')[0] or None

        missing: set[str] = set()
        for m in _missing_re.finditer(raw_log):
            p = _pkg(m.group(1))
            if p:
                missing.add(p)

        if not missing:
            return set()

        pkg_list = ' '.join(sorted(missing))
        pkg_display = ', '.join(sorted(missing))
        logger.info("📦  [npm] Installing: %s …", pkg_display)

        install = await self.sandbox.exec_command(
            f"cd /workspace && npm install --legacy-peer-deps {pkg_list} 2>&1",
            timeout=120,
        )

        # Parse and emit meaningful lines from npm output to the log stream
        _log_npm_output(install.stdout or "", source="Import Resolver")

        if install.exit_code != 0:
            logger.warning("📦  [npm] Install failed for: %s", pkg_display)
            return set()

        logger.info("📦  [npm] ✅ Installed: %s — restarting dev server …", pkg_display)
        try:
            await self.sandbox.exec_command(
                "pkill -f 'vite|next dev|nuxt dev|webpack.*serve' 2>/dev/null; sleep 1",
                timeout=8,
            )
            _cmd = getattr(self, '_last_dev_cmd', None)
            if _cmd:
                await self.sandbox.exec_command(_cmd, timeout=15)
                await asyncio.sleep(5)
                logger.info("📦  [npm] Dev server restarted successfully")
        except Exception as _re_exc:
            logger.debug("📦  [Import Resolver] Restart skipped: %s", _re_exc)

        return missing



