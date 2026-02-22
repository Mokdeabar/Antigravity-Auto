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
    "Push every aspect — UI/UX, functionality, performance, accessibility — "
    "to absolute perfection. Never settle for average. "
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
    "functionality and log results to PROJECT_STATE.md before proceeding."
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
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT", "180"))

# Model auto-discovery: check once per day, cache result to disk.
GEMINI_MODEL_CACHE_TTL_HOURS = 24
GEMINI_MODEL_CACHE_PATH = _SUPERVISOR_DIR / "_best_model_cache.json"
GEMINI_FALLBACK_MODEL = "gemini-3-pro-preview"

# Prioritized list of models to probe during auto-discovery.
# The first model that works becomes the cached best model.
# Flash models are included as last-resort fallbacks (OpenClaw pattern).
# V31: Moved 'auto' after first pro model — it wastes 30s timing out on cold boot.
GEMINI_MODEL_PROBE_LIST = [
    "gemini-3.1-pro-preview",
    "auto",
    "gemini-3-pro-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",       # Last-resort Flash fallbacks
    "gemini-2.0-flash",
]

# Smart Model Routing: tier-based model lists
GEMINI_PRO_MODELS = ["auto", "gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-2.5-pro"]
GEMINI_FLASH_MODELS = ["gemini-3-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
GEMINI_DEFAULT_FLASH = "gemini-2.5-flash"  # Best balance of speed + quality for light tasks

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
# Escalating cooldown delays in seconds: 1m → 5m → 25m → 1h (cap)
GEMINI_COOLDOWN_DELAYS = [60, 300, 1500, 3600]

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
RATE_LIMIT_DEFAULT_WAIT_S = 60          # Default wait when no retry-after header
RATE_LIMIT_MAX_WAIT_S = 300             # Max wait before giving up or downgrading model
RATE_LIMIT_HISTORY_SIZE = 20            # Track last N rate limit events

# ─────────────────────────────────────────────────────────────
# Self-Healing / Self-Improvement
# ─────────────────────────────────────────────────────────────
SELF_IMPROVEMENT_INTERVAL_S = 3600      # Run self-improvement check every 60 minutes

# ─────────────────────────────────────────────────────────────
# V20: Parallel Execution Matrix
# ─────────────────────────────────────────────────────────────
MAX_CONCURRENT_WORKERS = 2              # Max parallel Gemini CLI workers (API rate limit guard)

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
