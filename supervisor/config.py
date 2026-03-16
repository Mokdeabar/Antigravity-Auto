"""
config.py — Central configuration for the Supervisor AI (V34 Headless Architecture).

All timing constants, thresholds, sandbox settings, and the ULTIMATE_MANDATE
live here so they can be tuned in one place.

V34: Headless architecture — replaced Playwright/CDP/DOM automation with
Docker sandbox + MCP tool interface. All GUI constants removed.
"""

import json
import os
import pathlib

# ─────────────────────────────────────────────────────────────
# Version — single source of truth for all version references
# ─────────────────────────────────────────────────────────────
SUPERVISOR_VERSION = 74                 # Internal version number (bump this on each release)
SUPERVISOR_VERSION_LABEL = f"V{SUPERVISOR_VERSION}"   # Short label: "V74"
# V74: Computed from SUPERVISOR_VERSION to prevent drift
SUPERVISOR_RELEASE_NAME = "Apex"        # Release codename (change per major milestone)
SUPERVISOR_VERSION_FULL = f"{SUPERVISOR_VERSION // 2}.{SUPERVISOR_VERSION % 2}"  # Semantic: "36.1"

# ─────────────────────────────────────────────────────────────
# File paths
# ─────────────────────────────────────────────────────────────
CONFIG_FILE_PATH = str(pathlib.Path(__file__).resolve())

_SUPERVISOR_DIR = pathlib.Path(__file__).resolve().parent

# ─────────────────────────────────────────────────────────────
# Workspace State Isolation
# ─────────────────────────────────────────────────────────────
_ACTIVE_PROJECT_PATH = None

def set_project_path(path: str) -> None:
    """Set the active project path to isolate state files into .ag-supervisor/"""
    global _ACTIVE_PROJECT_PATH
    _ACTIVE_PROJECT_PATH = path

def get_project_path():
    """Return the active project path as a Path, or None if not set."""
    if _ACTIVE_PROJECT_PATH:
        return pathlib.Path(_ACTIVE_PROJECT_PATH)
    return None

def get_state_dir() -> pathlib.Path:
    """Return the .ag-supervisor/ directory for the active project, creating it if needed."""
    if _ACTIVE_PROJECT_PATH:
        state_dir = pathlib.Path(_ACTIVE_PROJECT_PATH) / ".ag-supervisor"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir
    return _SUPERVISOR_DIR

# ─────────────────────────────────────────────────────────────
# THE ULTIMATE 2026 AWWWARDS MANDATE
# ─────────────────────────────────────────────────────────────
# Prepended to every refinement / enhancement / completion prompt
# sent back into Antigravity to force the agent toward perfection.
ULTIMATE_MANDATE = (
    "CRITICAL DIRECTIVE: You are building a 2026-grade, Awwwards-level product. "
    "Use the latest tools, libraries, and best practices available to you. "
    "Push every aspect -- UI/UX, functionality, performance, accessibility -- "
    "to absolute perfection. Never settle for average. "
    # Windows host compatibility — projects preview and run on Windows
    "WINDOWS HOST: The host operating system is WINDOWS. All dev servers, "
    "build scripts, and package.json scripts MUST use cross-platform commands. "
    "NEVER use Linux-only commands (head, tail, grep, sed, awk, cat, wc, "
    "chmod, chown, ln -s, rm -rf) in package.json scripts, npm run scripts, "
    "or any code that executes at build/dev time. Use cross-platform Node.js "
    "alternatives (rimraf, cross-env, shx) or native JS APIs instead. "
    # Core operational directives (apply to EVERY task)
    "FIRST RUN: On first run, do a comprehensive audit of the entire project. "
    "Review all main flows, identify gaps, bugs, and improvement areas. "
    "Build a full overview of the current state before making changes. "
    "DEV SERVER: Start the dev server at any available port. "
    "TESTING: Run all tests autonomously. If tests fail, fix the code before "
    "proceeding. Check for syntax errors, lint warnings, and type errors. "
    "LIVING STATE: Maintain a PROJECT_STATE.md file in the project root. "
    "After every meaningful change, update it with: what was built, what "
    "needs improvement, known bugs, required features, and design notes. "
    "This file is your persistent memory across sessions. "
    "TOTAL AUTOMATION: NEVER ask the user to manually check features. "
    "Use shell commands and test suites to autonomously verify all "
    "functionality and log results to PROJECT_STATE.md before proceeding. "
    # Dependency quality rule — prevents stale versions from day one
    "DEPENDENCIES: Before importing or installing ANY npm/pip/cargo package "
    "for the first time, verify its latest stable version by running "
    "`npm view <pkg> version` (or `pip index versions <pkg>` for Python) "
    "inside /workspace. Always pin the current latest stable in package.json. "
    "NEVER use a version from memory — versions go stale; always confirm live. "
    "If you are adding a package to package.json, write the exact version "
    "number (e.g. \\\"react\\\": \\\"19.1.0\\\") not a range like \\\"^18.0.0\\\". "
    # Config verification — prevents deprecated patterns from day one
    "CONFIG VERIFICATION: Before writing or modifying ANY build tool config "
    "(vite.config, eslint.config, tailwind.config, tsconfig, webpack.config, "
    "postcss.config, next.config, etc.) or scaffolding a new project, you MUST "
    "first check what version of that tool is installed (`npm ls <tool>`) and "
    "then look up its official docs or migration guide for that specific version. "
    "Your training data may contain outdated patterns — e.g. Vite esbuild config "
    "that was replaced by oxc, old ESLint .eslintrc format replaced by flat config. "
    "Always verify the config schema matches the INSTALLED version, not your memory. "
    # Scaffolding verification — prevents wrong CLI flags
    "SCAFFOLDING: Before running ANY project scaffolding command (npx create-vite, "
    "npx create-next-app, npx create-react-app, npm init, etc.), ALWAYS run it "
    "with `--help` first to see the current valid flags and options. CLI flags "
    "change between versions and using old flags causes silent misconfigurations "
    "or outright failures. Initialize in the current directory with `./`. "
    # Framework API verification — prevents stale usage patterns
    "FRAMEWORK APIS: When using a major framework feature for the first time in "
    "a project (React hooks, Next.js routing, Vue composables, Tailwind utility "
    "classes), check the installed version (`npm ls <framework>`) and verify "
    "you are using the API that matches THAT version. Key examples of breaking "
    "changes: React 19 changed hook rules and added `use()`, Next.js 15 changed "
    "data fetching from getServerSideProps to Server Components, Tailwind v4 "
    "removed tailwind.config.js in favour of CSS-based config, ESLint 9+ uses "
    "flat config (eslint.config.js) not .eslintrc. When in doubt, search the docs. "
    # Note: Visual design, scaffolding, and writing quality rules are now
    # injected dynamically via the Smart Skills Engine (skills_loader.py).
    # See .ag-supervisor/skills/ for the modular skill files.
    # LOCAL_ACTION PROTOCOL: When your task requires a local infrastructure
    # operation that you cannot run yourself (e.g. you've modified package.json
    # and npm install must re-run, or Vite cache must be cleared), emit a marker
    # on its own line in your output: LOCAL_ACTION: <action>
    # The supervisor will execute it automatically after your task completes.
    # Available actions:
    #   LOCAL_ACTION: npm_install      — re-run npm install after package.json changes
    #   LOCAL_ACTION: restart_server   — restart the dev server after config changes
    #   LOCAL_ACTION: clear_vite_cache — clear .vite cache + restart (after tailwind.config changes)
    #   LOCAL_ACTION: dep_upgrade      — upgrade major version deps + verify with tsc
    #   LOCAL_ACTION: reinstall_vite   — full node_modules wipe + reinstall (Vite chunk errors)
    # You MUST emit LOCAL_ACTION: npm_install whenever you modify package.json.
    # You MUST emit LOCAL_ACTION: clear_vite_cache whenever you modify tailwind.config.js/ts
    # or postcss.config.js — this fixes theme() resolution errors immediately.
)

# ─────────────────────────────────────────────────────────────
# Mandate Injection
# ─────────────────────────────────────────────────────────────
MANDATE_FILENAME = "SUPERVISOR_MANDATE.md"
TINY_INJECT_STRING = (
    "Read SUPERVISOR_MANDATE.md and execute all instructions. "
    "Start the dev server and run tests. "
    "Update PROJECT_STATE.md with your current progress. "
    "Test everything autonomously via shell commands."
)

# ─────────────────────────────────────────────────────────────
# V50: Smart Skills Engine
# ─────────────────────────────────────────────────────────────
# Token budget for dynamically-injected skills per Gemini CLI call.
# Skills are selected based on task category and sorted by priority.
SKILLS_TOKEN_BUDGET = int(os.getenv("SKILLS_TOKEN_BUDGET", "6000"))

# ─────────────────────────────────────────────────────────────
# V6: Lockfile Memory (Anti-Amnesia)
# ─────────────────────────────────────────────────────────────
LOCKFILE_NAME = ".supervisor_lock"

# ─────────────────────────────────────────────────────────────
# V34: Docker Sandbox Configuration
# ─────────────────────────────────────────────────────────────
SANDBOX_IMAGE = os.getenv("SUPERVISOR_SANDBOX_IMAGE", "python:3.11-slim")
SANDBOX_WORKSPACE_PATH = "/workspace"
SANDBOX_TIMEOUT_S = int(os.getenv("SUPERVISOR_SANDBOX_TIMEOUT", "300"))
SANDBOX_MEMORY_LIMIT = os.getenv("SUPERVISOR_SANDBOX_MEMORY", "2g")
MCP_TOOL_TIMEOUT_S = int(os.getenv("SUPERVISOR_MCP_TIMEOUT", "120"))

# ─────────────────────────────────────────────────────────────
# V28: FinOps Engine Configuration
# ─────────────────────────────────────────────────────────────
APPROVED_DEPLOY_PROVIDERS = ["vercel", "railway"]  # Blocks rogue infra migrations
MARGIN_DECAY_THRESHOLD = 0.15  # 15% cost increase triggers refactor
MIN_TRAFFIC_FOR_FINOPS = 1000  # Minimum requests/day before optimizing



# ─────────────────────────────────────────────────────────────
# V6: Glass Brain ANSI Colors
# ─────────────────────────────────────────────────────────────
ANSI_CYAN = "\033[96m"
ANSI_YELLOW = "\033[93m"
ANSI_RED = "\033[91m"
ANSI_GREEN = "\033[92m"
ANSI_MAGENTA = "\033[95m"
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"



# ─────────────────────────────────────────────────────────────
# Gemini CLI
# ─────────────────────────────────────────────────────────────
GEMINI_CLI_CMD = os.getenv("GEMINI_CLI_CMD", "gemini")
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT", "600"))  # 10min: includes Node boot + npx resolve + API response

# Prompt Size Guard — prevents oversized prompts from choking the CLI.
# Prompts above WARN threshold log a warning; above MAX are truncated.
# V45→V73: Gemini has 1M+ token context. Raised from 200K/100K to 1M/500K.
# The old 200K cap was actively trimming audit prompts that use efficient
# @file references — the CLI itself handles context, not the prompt body.
PROMPT_SIZE_WARN_CHARS = int(os.getenv("PROMPT_SIZE_WARN", "500000"))
PROMPT_SIZE_MAX_CHARS = int(os.getenv("PROMPT_SIZE_MAX", "1000000"))

# V60: Plan Mode — for complex tasks, run Gemini CLI with --plan first so it
# generates a plan file before executing.  Catches scope misinterpretation early.
# Tasks with description length > this threshold are considered complex enough.
# Disable by setting GEMINI_PLAN_MODE=false.
# V68: Raised from 600 → 3000.  At 600 chars virtually every task triggered
# the extra Step 1 subprocess (~60-90s overhead). 3000 chars catches only
# genuinely complex multi-file tasks where Gemini planning is worthwhile.
PLAN_MODE_CHAR_THRESHOLD = int(os.getenv("GEMINI_PLAN_MODE_THRESHOLD", "3000"))
PLAN_MODE_ENABLED = os.getenv("GEMINI_PLAN_MODE", "true").lower() != "false"


# Task Decomposition — goals longer than this are auto-decomposed into DAG chunks.
# Also triggers when Ollama classifies the task as "complex".
# V54: Raised from 2000 → 30000. Gemini has 1M+ token context — a 2000 char
# threshold was a legacy artefact from smaller model days and caused every
# detailed goal brief (e.g. 4368 chars) to trigger _execute_single_chunk's
# sub-decomposition path, which crashed with 'name sandbox is not defined'.
# DAG node descriptions are typically 200-800 chars; 30k is a safe ceiling
# that only triggers for genuinely extreme single-task descriptions.
COMPLEX_TASK_CHAR_THRESHOLD = int(os.getenv("COMPLEX_TASK_THRESHOLD", "30000"))
# V40: AI Ultra Throughput — 24h Continuous Operation
#
#   AI Ultra quotas: 120 RPM (burst), 2000/day (budget)
#   RPM is never the bottleneck (peak burst ~3 RPM = 2.5% of 120)
#   Daily 2000 limit is THE constraint for 24h runs
#
#   Rates: DAG execution = (W workers + 1 overhead) req/min
#          Monitoring idle = ~0.15 req/min
#   Assumption: ~25% DAG-active over 24h (360 min active, 1080 idle)
#
#   W=2: 360×3.0 + 1080×0.15 = 1242/day = 62% (under-utilized)
#   W=3: 360×4.0 + 1080×0.15 = 1602/day = 80% ← SWEET SPOT
#   W=4: 360×5.0 + 1080×0.15 = 1962/day = 98% (too close)
#
MAX_CONCURRENT_WORKERS = int(os.getenv("MAX_CONCURRENT_WORKERS", "3"))  # 80% daily @ 24h continuous

# Model auto-discovery: check once per day, cache result to disk.
GEMINI_MODEL_CACHE_TTL_HOURS = 24
GEMINI_MODEL_CACHE_PATH = _SUPERVISOR_DIR / "_best_model_cache.json"
GEMINI_FALLBACK_MODEL = "gemini-3.1-pro-preview"

# V73: Only Gemini 3+ models. Legacy 2.5 models removed — user mandate:
#   "I don't want any model that isn't 3+ and non-image to be used,
#    apart from the lite in the chat, as they're not as critical."
#
# Model audit (March 13, 2026):
#   - gemini-3.1-pro-preview:        Primary — best reasoning + agentic coding
#   - gemini-3-flash-preview:        Flash — SWE-bench 78%, 3x faster than 2.5 Pro
#   - gemini-3.1-flash-lite-preview: Lite — fastest/cheapest Gemini 3
#   - gemini-2.5-flash-lite:         Lite chat only (kept for low-priority chat panel)
GEMINI_MODEL_PROBE_LIST = [
    "gemini-3.1-pro-preview",         # Primary: best reasoning + agentic tool use
    "gemini-3-flash-preview",         # Flash: #1 SWE-bench coding (78%)
    "gemini-3.1-flash-lite-preview",  # Lite: fastest/cheapest Gemini 3
]

# V73: Only Gemini 3+ in each pool. No legacy 2.5 models.
GEMINI_PRO_MODELS = ["gemini-3.1-pro-preview"]
GEMINI_FLASH_MODELS = ["gemini-3-flash-preview"]
GEMINI_DEFAULT_FLASH = "gemini-3-flash-preview"          # Flash primary
GEMINI_DEFAULT_LITE = "gemini-2.5-flash-lite"            # Lite chat only (non-critical)
GEMINI_DEFAULT_IMAGE = "gemini-3.1-flash-image-preview"  # Nano Banana 2 — image gen/editing (Feb 26 2026)

# V74: Pro-Only Coding Enforcement
# When True, ALL coding and planning tasks exclusively use Pro models.
# If Pro quota is exhausted, the system PAUSES until quota resets instead
# of falling over to Flash/Lite models. Non-coding paths (LiteBrain chat
# panel, classify_task, analyze_errors) remain unaffected.
PRO_ONLY_CODING = True

# Model alias routing (CLI --model shortcuts → canonical names)
GEMINI_MODEL_ALIASES = {
    "auto":  "auto",                    # Gemini 3 auto-selector (routes between pro/flash)
    "pro":   "gemini-3.1-pro-preview",  # Complex tasks + coding
    "flash": "gemini-3-flash-preview",  # Speed-optimised general purpose
    "lite":  "gemini-2.5-flash-lite",   # Cost-efficient Q&A (Gemini Lite panel)
    "image": "gemini-3.1-flash-image-preview",  # Image generation (Nano Banana 2)
}

# V62/V73: Quota Buckets — models that share a pooled quota.
# 2.5 models removed from pro/flash buckets. Lite keeps 2.5-flash-lite for chat.
QUOTA_BUCKETS = {
    "pro": {
        "models": ["gemini-3.1-pro-preview"],
        "estimated_rpd": 250,   # ~250 requests per rolling 24h window
        "label": "Pro Bucket",
    },
    "flash": {
        "models": ["gemini-3-flash-preview"],
        "estimated_rpd": 1500,  # ~1000-1500 requests per rolling 24h window
        "label": "Flash Bucket",
    },
    "lite": {
        "models": ["gemini-3.1-flash-lite-preview", "gemini-2.5-flash-lite"],
        "estimated_rpd": 5000,  # Very high; largely token-limited rather than RPD
        "label": "Lite Bucket",
    },
    "image": {
        "models": ["gemini-3.1-flash-image-preview"],  # Nano Banana 2 — image-gen only
        "estimated_rpd": 0,  # Not tracked via /stats — separate image API quota
        "label": "Image Bucket",
    },
}

# Build reverse lookup: model → bucket name
QUOTA_MODEL_TO_BUCKET: dict[str, str] = {}
for _bname, _bdata in QUOTA_BUCKETS.items():
    for _m in _bdata["models"]:
        QUOTA_MODEL_TO_BUCKET[_m] = _bname

# ─────────────────────────────────────────────────────────────
# Dynamic Model Auto-Discovery
# ─────────────────────────────────────────────────────────────
# Models discovered by the PTY probe are automatically classified
# and registered into the tier system based on name patterns.

import re as _re_model
import logging as _logging_cfg
_cfg_logger = _logging_cfg.getLogger("supervisor.config")

# Track consecutive absences before removing a model (anti-flap)
_model_absence_count: dict[str, int] = {}


def classify_model(name: str) -> str:
    """
    Classify a Gemini model name into a tier bucket.

    Rules (checked in order):
      - *-flash-image* or *-image* → "image"  (Nano Banana / image-gen models)
      - *-flash-lite*              → "lite"
      - *-pro*, *-ultra*           → "pro"
      - *-flash*                   → "flash"
      - everything else            → "flash"  (safe default)
    """
    n = name.lower()
    if "flash-image" in n or ("-image" in n and "flash" in n):
        return "image"  # Nano Banana 2 / gemini-3.1-flash-image-preview
    if "flash-lite" in n or "flash_lite" in n:
        return "lite"
    if "-pro" in n or "-ultra" in n:
        return "pro"
    if "-flash" in n:
        return "flash"
    return "flash"  # Safe default — never wastes limited pro quota


def _model_sort_key(name: str) -> tuple:
    """
    Extract (version, tier_rank) for priority sorting.
    Higher version = higher priority. Pro > Flash > Lite within same version.
    """
    # Extract version number: "gemini-3.1-pro-preview" → 3.1
    import re as _re
    ver_match = _re.search(r'(\d+(?:\.\d+)?)', name)
    version = float(ver_match.group(1)) if ver_match else 0.0

    # Tier rank (higher = better)
    n = name.lower()
    if "ultra" in n:
        tier = 4
    elif "-pro" in n:
        tier = 3
    elif "flash-lite" in n or "flash_lite" in n:
        tier = 0
    elif "flash" in n:
        if "preview" in n:
            tier = 2  # flash-preview models often top coding benchmarks
        else:
            tier = 1
    else:
        tier = 1

    return (version, tier)


def update_models_from_probe(discovered: dict[str, dict]) -> dict:
    """
    Update all model config lists from PTY probe discovered models.

    Args:
        discovered: dict of {model_name: snapshot_dict} from the probe.
                    Each snapshot has remaining_pct, resets_in_s, etc.

    Returns:
        {"added": [...], "removed": [...], "buckets": current QUOTA_BUCKETS}
    """
    global GEMINI_MODEL_PROBE_LIST, GEMINI_PRO_MODELS, GEMINI_FLASH_MODELS
    global GEMINI_DEFAULT_FLASH, GEMINI_FALLBACK_MODEL

    if not discovered:
        return {"added": [], "removed": [], "buckets": QUOTA_BUCKETS}

    discovered_names = set(discovered.keys())
    current_names = set(QUOTA_MODEL_TO_BUCKET.keys())

    added = []
    removed = []

    # ── 1. Detect and add NEW models ──
    for name in sorted(discovered_names - current_names):
        bucket = classify_model(name)

        # Auto-detect co-bucketing: if this model shares identical
        # remaining_pct AND resets_in_s with an existing model, they're
        # in the same pool.
        new_snap = discovered[name]
        new_pct = new_snap.get("remaining_pct", -1)
        new_reset = new_snap.get("resets_in_s", -1)
        for existing_name, existing_snap in discovered.items():
            if existing_name == name or existing_name not in QUOTA_MODEL_TO_BUCKET:
                continue
            if (abs(existing_snap.get("remaining_pct", -2) - new_pct) < 0.5
                    and abs(existing_snap.get("resets_in_s", -2) - new_reset) < 120):
                # Same quota pool — put in same bucket
                bucket = QUOTA_MODEL_TO_BUCKET[existing_name]
                break

        # Add to bucket
        if bucket in QUOTA_BUCKETS:
            QUOTA_BUCKETS[bucket]["models"].append(name)
        else:
            # Create new bucket (unlikely but safe)
            QUOTA_BUCKETS[bucket] = {
                "models": [name],
                "estimated_rpd": 500,
                "label": f"{bucket.title()} Bucket",
            }

        QUOTA_MODEL_TO_BUCKET[name] = bucket
        _model_absence_count.pop(name, None)
        added.append((name, bucket))
        _cfg_logger.info("📊  [AutoDiscovery] New model: %s → %s bucket", name, bucket)

    # ── 2. Detect and remove DEPRECATED models ──
    # Only remove after 2+ consecutive absences (anti-flap)
    for name in sorted(current_names - discovered_names):
        _model_absence_count[name] = _model_absence_count.get(name, 0) + 1
        if _model_absence_count[name] >= 2:
            bucket = QUOTA_MODEL_TO_BUCKET.pop(name, None)
            if bucket and bucket in QUOTA_BUCKETS:
                models = QUOTA_BUCKETS[bucket]["models"]
                if name in models:
                    models.remove(name)
            _model_absence_count.pop(name, None)
            removed.append((name, bucket))
            _cfg_logger.info(
                "📊  [AutoDiscovery] Removed deprecated model: %s (was %s)", name, bucket
            )

    # Reset absence counter for models that ARE present
    for name in discovered_names & current_names:
        _model_absence_count.pop(name, None)

    # ── 3. Rebuild derived lists ──
    all_models = sorted(QUOTA_MODEL_TO_BUCKET.keys(),
                        key=_model_sort_key, reverse=True)

    GEMINI_MODEL_PROBE_LIST = list(all_models)
    GEMINI_PRO_MODELS = [m for m in all_models if classify_model(m) == "pro"]
    # V65: image models are never used for text/code tasks — exclude from flash list
    GEMINI_FLASH_MODELS = [m for m in all_models if classify_model(m) in ("flash", "lite")]

    # Best flash = highest-priority flash model
    if GEMINI_FLASH_MODELS:
        GEMINI_DEFAULT_FLASH = GEMINI_FLASH_MODELS[0]

    # Fallback = highest-priority model overall
    if all_models:
        GEMINI_FALLBACK_MODEL = all_models[0]

    if added or removed:
        _cfg_logger.info(
            "📊  [AutoDiscovery] Models updated: %d total, %d added, %d removed. "
            "Priority: %s",
            len(all_models), len(added), len(removed),
            " → ".join(all_models),
        )

    return {"added": added, "removed": removed, "buckets": QUOTA_BUCKETS}


# Gemini CLI model: "auto" routes to best available model automatically
# (3.1 Pro for complex, Flash for simple). Override with env var if needed.
GEMINI_CLI_MODEL = os.getenv("GEMINI_CLI_MODEL", "auto")

# ─────────────────────────────────────────────────────────────
# OpenClaw-Inspired: Retry Policy
# ─────────────────────────────────────────────────────────────
GEMINI_RETRY_MAX_ATTEMPTS = int(os.getenv("GEMINI_RETRY_ATTEMPTS", "3"))
GEMINI_RETRY_BASE_DELAY_S = float(os.getenv("GEMINI_RETRY_BASE_DELAY", "2.0"))
GEMINI_RETRY_MAX_DELAY_S = float(os.getenv("GEMINI_RETRY_MAX_DELAY", "30.0"))
GEMINI_RETRY_JITTER_PCT = 0.10  # 10% jitter to prevent thundering herd

# ─────────────────────────────────────────────────────────────
# OpenClaw-Inspired: Model Failover Cooldowns
# ─────────────────────────────────────────────────────────────
# V40: Optimized for AI Ultra (120 RPM, 2000/day).
# Shorter cooldowns since Ultra has generous quotas — get back to work faster.
# Escalating: 30s → 2m → 10m → 30m (cap)
GEMINI_COOLDOWN_DELAYS = [30, 120, 600, 1800]

# ─────────────────────────────────────────────────────────────
# OpenClaw-Inspired: Context Budget
# ─────────────────────────────────────────────────────────────
CONTEXT_BUDGET_WARN_CHARS = int(os.getenv("CONTEXT_BUDGET_WARN", "500000"))
CONTEXT_BUDGET_MAX_CHARS = int(os.getenv("CONTEXT_BUDGET_MAX", "2000000"))

# ─────────────────────────────────────────────────────────────
# OpenClaw-Inspired: Cron Scheduler
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# Smart Model Routing: Rate Limit Tracking
# ─────────────────────────────────────────────────────────────
# V40: Optimized for AI Ultra — shorter default wait since quota is high
RATE_LIMIT_DEFAULT_WAIT_S = 30           # Default wait when no retry-after header
RATE_LIMIT_MAX_WAIT_S = 180              # Max wait before downgrading model
RATE_LIMIT_HISTORY_SIZE = 30             # Track last N rate limit events

# ─────────────────────────────────────────────────────────────
# V62: Gemini CLI Quota Probe — live quota tracking from CLI output
# ─────────────────────────────────────────────────────────────
QUOTA_PROBE_ENABLED = os.getenv("QUOTA_PROBE_ENABLED", "true").lower() != "false"
QUOTA_PROBE_AVOID_THRESHOLD = float(os.getenv("QUOTA_PROBE_AVOID", "0.0"))   # DISABLED — only switch on actual 429 errors
QUOTA_PROBE_PREFER_THRESHOLD = float(os.getenv("QUOTA_PROBE_PREFER", "50.0")) # Prefer models above 50% remaining

# AI Ultra plan reference limits (for documentation / budget tracking)
AI_ULTRA_RPM = 120                       # Requests per minute
AI_ULTRA_DAILY = 2000                    # Requests per day

# ─────────────────────────────────────────────────────────────
# Self-Healing / Self-Improvement
# ─────────────────────────────────────────────────────────────
SELF_IMPROVEMENT_INTERVAL_S = 3600      # Run self-improvement check every 60 minutes



# ─────────────────────────────────────────────────────────────
# OpenClaw-Inspired: Presence Tracking
# ─────────────────────────────────────────────────────────────
PRESENCE_IDLE_THRESHOLD_S = int(os.getenv("PRESENCE_IDLE_THRESHOLD", "300"))  # 5min

# ─────────────────────────────────────────────────────────────
# OpenClaw-Inspired: Human-Like Pacing
# ─────────────────────────────────────────────────────────────
PACING_MIN_MS = int(os.getenv("PACING_MIN_MS", "800"))
PACING_MAX_MS = int(os.getenv("PACING_MAX_MS", "2500"))

# Windows uses .cmd wrappers for npm-installed CLI tools.
IS_WINDOWS = os.name == "nt"


def get_gemini_cli_cmd() -> str:
    """Return the correct Gemini CLI executable name for this OS."""
    cmd = GEMINI_CLI_CMD
    if IS_WINDOWS and not cmd.endswith(".cmd") and not cmd.endswith(".exe"):
        cmd = cmd + ".cmd"
    return cmd

# ─────────────────────────────────────────────────────────────
# Timing
# ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = float(os.getenv("SUPERVISOR_POLL_INTERVAL", "10.0"))
ACTION_DELAY_MS = 300          # small delay between mechanical actions

# V14 Semantic RAG constraints
MAX_GRAPH_DEPTH = 2

# ─────────────────────────────────────────────────────────────
# Loop / Escalation Thresholds
# ─────────────────────────────────────────────────────────────
LOOP_HISTORY_SIZE = 5           # rolling window of recent messages
DUPLICATE_THRESHOLD = 2         # same message repeated this many times → loop
ERROR_SUBSTRING_THRESHOLD = 3   # same error substring this many times → loop
CONSECUTIVE_FAIL_THRESHOLD = 2  # same tool fail 2× in a row → immediate pivot
MAX_SAME_ERROR_INTERVENTIONS = 5  # escalate to human after N interventions

# ─────────────────────────────────────────────────────────────
# WAITING State Escalation
# ─────────────────────────────────────────────────────────────
WAITING_REINJECT_THRESHOLD = 2    # consecutive WAITINGs before re-injection
WAITING_ESCALATE_THRESHOLD = 4   # consecutive WAITINGs before escalation
WORKING_STALE_THRESHOLD = 3      # consecutive WORKINGs before skeptical re-check

# ─────────────────────────────────────────────────────────────
# Alert Sound
# ─────────────────────────────────────────────────────────────
ALERT_REPEAT = 5

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("SUPERVISOR_LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s │ %(levelname)-7s │ %(name)-18s │ %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

# ─────────────────────────────────────────────────────────────
# Dev Server Ports (scanned concurrently)
# ─────────────────────────────────────────────────────────────
DEV_SERVER_PORTS = [3000, 3001, 4200, 5000, 5173, 8000, 8080]

# ─────────────────────────────────────────────────────────────
# Proactive Mode & Heartbeat Scheduler
# ─────────────────────────────────────────────────────────────
PROACTIVE_MODE = os.getenv("SUPERVISOR_PROACTIVE", "true").lower() == "true"
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("SUPERVISOR_HEARTBEAT", "60.0"))

# ─────────────────────────────────────────────────────────────
# V74: Hot-Reload Configuration (§4.8)
# ─────────────────────────────────────────────────────────────
# Whitelisted keys that can be overridden via .ag-supervisor/config_overrides.json
_RELOADABLE_KEYS = {
    "MAX_CONCURRENT_WORKERS",
    "GEMINI_TIMEOUT_SECONDS",
    "SKILLS_TOKEN_BUDGET",
    "PLAN_MODE_CHAR_THRESHOLD",
    "PLAN_MODE_ENABLED",
    "COMPLEX_TASK_CHAR_THRESHOLD",
    "PROMPT_SIZE_WARN_CHARS",
    "PROMPT_SIZE_MAX_CHARS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "PROACTIVE_MODE",
    "SANDBOX_TIMEOUT_S",
    "ALERT_REPEAT",
    "LOG_LEVEL",
}


def reload() -> dict:
    """
    V74: Hot-reload configuration overrides from .ag-supervisor/config_overrides.json.

    Only whitelisted keys can be overridden for safety. Returns a dict of
    {key: new_value} for all successfully applied overrides.

    Usage: Create .ag-supervisor/config_overrides.json with:
      {"MAX_CONCURRENT_WORKERS": 4, "GEMINI_TIMEOUT_SECONDS": 900}
    Then call config.reload() to apply without restarting.
    """
    import json as _json
    import logging as _logging
    _logger = _logging.getLogger("supervisor.config")

    applied = {}
    _pp = get_project_path()
    if not _pp:
        return applied

    overrides_path = _pp / ".ag-supervisor" / "config_overrides.json"
    if not overrides_path.exists():
        return applied

    try:
        overrides = _json.loads(overrides_path.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            _logger.warning("⚙️  config_overrides.json must be a JSON object")
            return applied

        _module = __import__(__name__)
        for key, value in overrides.items():
            if key not in _RELOADABLE_KEYS:
                _logger.warning("⚙️  Config key '%s' is not reloadable — skipped", key)
                continue

            old_value = getattr(_module, key, None)
            # Type-coerce to match original type
            if isinstance(old_value, int) and not isinstance(value, bool):
                value = int(value)
            elif isinstance(old_value, float):
                value = float(value)
            elif isinstance(old_value, bool):
                value = bool(value)

            setattr(_module, key, value)
            applied[key] = value
            _logger.info("⚙️  Config reload: %s = %s (was %s)", key, value, old_value)

        if applied:
            _logger.info("⚙️  Reloaded %d config override(s)", len(applied))
    except Exception as exc:
        _logger.warning("⚙️  Failed to reload config: %s", exc)

    return applied
