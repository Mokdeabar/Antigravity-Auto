# Supervisor AI — Complete Technical Reference

> **Version**: V74 (V37 Security + V38 Smart Execution + V39 Self-Healing + V40 Autonomous Efficiency + V41 Context-Aware Intelligence + V43 Smart Quota + V44 Manual Mode & Persistent State + V45–V54 Dependency Intelligence & UI Polish + V55 Comprehensive Local Self-Healing & Model Refresh + V56 Audit Accuracy & TS Regression Guard + V57 Quality & Efficiency Suite + V58 Five-Layer File Conflict Protection + V59 UI Reliability & Auto-Fix Improvements + V60 Deep Analysis Pipeline, Plan Mode, Audit CWD Fix & Critical Path Scheduling + V61 Command Centre Health/Console/Stats Tabs & Crash Fix + V62 Quota System Overhaul & Smart Estimation + V63 Persistent PTY Probe, Animated Quota UI & Dynamic Model Discovery + V64 Gemini Lite Intelligence & PowerShell Policy Auto-Setup + V65 Graceful Task Requeue on Shutdown + V66–V72 Worker Selector, Quota Pause Modes & Stats Dashboard + V73 Centralized Version Management & Final Completion Audit Gate + V74 Modular Architecture, Pro-Only Coding & Production Hardening)
> **Codebase**: `supervisor/` package — 35+ Python modules, ~28,000 lines
> **Runtime**: Python 3.11+ with Docker, asyncio, aiohttp, SQLite, Gemini Lite Intelligence
> **Purpose**: Fully autonomous AI pipeline that plans, builds, tests, secures, deploys, monitors, heals, listens to users, audits, and polishes until perfect.
>
> **What it does for the user**: Takes a single high-level goal (e.g., "build a landing page" or "fix the login bug") and autonomously drives Gemini CLI on the host to completion — orchestrating tasks, managing Docker sandbox executions, recovering from crashes, switching models on rate limits, and re-executing when the agent stalls. The user can walk away while the supervisor delivers working software.
>
> **What it can do to itself**: When the supervisor encounters a bug in its own code, it reads ALL of its source files, sends them to Gemini with the crash traceback, receives a fix, validates it with `py_compile` and a shadow sandbox, applies it, and reboots -- all without human intervention. It has rewritten itself across 44+ versions.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Core Infrastructure](#2-core-infrastructure)
3. [Sandbox Execution Layer](#3-sandbox-execution-layer)
4. [Intelligence Layer (Gemini Integration)](#4-intelligence-layer)
5. [Multi-Agent Council](#5-multi-agent-council)
6. [Memory Systems](#6-memory-systems)
7. [Planning & Execution Engine](#7-planning--execution-engine)
8. [Verification & Quality Assurance](#8-verification--quality-assurance)
9. [Security & Compliance](#9-security--compliance)
10. [Deployment Pipeline](#10-deployment-pipeline)
11. [Production Monitoring & Self-Healing](#11-production-monitoring--self-healing)
12. [Growth & Optimization](#12-growth--optimization)
13. [Scheduler & Cron System](#13-scheduler--cron-system)
14. [Self-Evolution Engine](#14-self-evolution-engine)
15. [User Research & Polish Engines (V29)](#15-user-research--polish-engines-v29)
16. [Configuration Reference](#16-configuration-reference)
17. [Environment Variables](#17-environment-variables)
18. [File & Directory Structure](#18-file--directory-structure)
19. [Version History](#19-version-history)

---

## 1. Architecture Overview

The Supervisor AI is a Python-based autonomous software engineering system using the **"Host Intelligence, Sandboxed Hands"** architecture. The HOST runs the brain (Gemini CLI using the user's authenticated AI Ultra session + Ollama). The Docker SANDBOX is a dumb terminal — it only executes commands and hosts files. Zero credentials enter the container.

### System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     SUPERVISOR AI (main.py)                      │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │
│  │ Recovery  │  │ Lockfile │  │ Session  │  │   Sandbox        │ │
│  │ Engine    │  │ Memory   │  │ State    │  │   Lifecycle      │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘ │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │           MONITORING LOOP (30s cycle)                         │ │
│  │  Create Sandbox → Execute Task → Gather Context → Monitor    │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │Headless │  │ Gemini  │  │ Agent   │  │ Session │            │
│  │Executor │  │ Advisor │  │ Council │  │ Memory  │            │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │
│                                                                   │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │ Temporal│  │  TDD    │  │ Visual  │  │Complianc│            │
│  │ Planner │  │ Verifier│  │ QA      │  │ Gateway │            │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │
│                                                                   │
│  │ Deploy  │  │Telemetry│  │  User   │  │ Polish  │            │
│  │ Engine  │  │Ingester │  │Research │  │ Engine  │            │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │
│                                                                   │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │  Local  │  │Sandbox  │  │ State   │  │  Loop   │            │
│  │ Ollama  │  │Manager  │  │ Machine │  │Detector │            │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │
└─────────────────────────────────────────────────────────────────┘
              │                              │
              ▼                              ▼
┌──────────────────────┐      ┌──────────────────────┐
│   Docker Sandbox     │      │    Gemini CLI (HOST)  │
│  (dumb terminal —    │      │  Uses authenticated   │
│   no AI, no creds)   │      │  AI Ultra session     │
└──────────────────────┘      └──────────────────────┘
              │
              ▼
┌──────────────────────┐
│   Ollama Local LLM   │
│  (localhost:11434)    │
└──────────────────────┘
```

### Execution Flow (V36 — Host Intelligence, Sandboxed Hands)

1.  **Bootstrap**: `main.py` ensures Docker is installed, builds sandbox image
2.  **Sandbox Creation**: Creates ephemeral container with workspace mounted (bind or copy mode)
3.  **Context Bridge**: Host reads sandbox state via `docker exec` (git status, file listing, project state)
4.  **Task Execution**: `HeadlessExecutor` auto-discovers Gemini CLI (npm global → PATH → npx fallback), then runs it **on the host** via `asyncio.create_subprocess`, using the user's authenticated AI Ultra session. Model defaults to `auto` (routes to Gemini 3+ models only). Timeout: 600s (includes Node.js boot). **Prompt Size Guard**: prompts exceeding `PROMPT_SIZE_MAX_CHARS` (15,000) are auto-truncated. **V38.1 Smart Routing**: complex tasks (Ollama classification or >2000 chars) are auto-decomposed into a DAG of atomic chunks via the `TemporalPlanner`, with multi-layer recursive decomposition up to 3 levels deep, **continuous worker pool** execution with dynamic lanes (`DailyBudgetTracker.get_effective_workers()`), and **adaptive per-chunk timeouts**. **V40**: Workers immediately fill idle slots via `asyncio.wait(FIRST_COMPLETED)` — no batch blocking.
5.  **Action Bridge**: Gemini output parsed on host → file changes applied via `docker cp`/`write_file`, commands via `docker exec`
6.  **Auto-Redeploy**: V41: After every file-changing task, files synced to sandbox and dev server refreshed automatically
7.  **Monitoring**: Watches container health, task progress, Gemini output
8.  **Verification**: TDD and Compliance gates; Visual QA reserved for final deployment pass only
9.  **Deployment**: Ships to staging/production with health checks
10. **Recovery**: Sandbox restart → mount mode switch → image rebuild → self-evolution

---

## 2. Core Infrastructure

### 2.1 `main.py` -- The Orchestrator (~4,700 lines)

The entry point and main event loop. Controls the entire supervisor lifecycle.

#### Key Classes

**`AutoRecoveryEngine`** — Stateful crash recovery that escalates through sandbox-aware strategies.

| Method | Description |
|--------|-------------|
| `__init__()` | Initializes with 4-tier strategy chain |
| `current_strategy` | Returns current recovery strategy name |
| `record_success()` | Resets failure count after successful loop |
| `recover(error_context)` | Escalates through strategies |
| `get_crash_log()` | Returns forensic crash data |

Recovery strategies (in escalation order):
1.  **RESTART_SANDBOX** — Destroy and recreate the Docker container
2.  **SWITCH_MOUNT** — Toggle between bind and copy mount modes
3.  **REBUILD_IMAGE** — Force rebuild the Docker sandbox image
4.  **EVOLVE** — Self-evolution: rewrite own source code to fix the bug

#### Key Functions

| Function | Description |
|----------|-------------|
| `_lockfile_exists(project_path)` | Checks for `.supervisor_lock` anti-amnesia file |
| `_create_lockfile(project_path)` | Creates lockfile after first task dispatch |
| `_remove_lockfile(project_path)` | Removes lockfile on graceful exit |
| `_play_alert()` | Plays audible alert for human escalation |
| `_save_session_state(goal, project_path)` | Persists goal for auto-resume |
| `_load_session_state()` | Loads last goal for resume |
| `_interactive_goal()` | Interactive goal selection UI |
| `_diagnose_and_retry(executor, local_brain, session_mem, planner, task_id, errors, original_prompt)` | V39: Auto-fix — diagnoses failures via Ollama, builds enriched retry prompt, retries once |
| `_compute_chunk_timeout(local_brain, description)` | V41: Instant per-chunk timeout via description-length heuristic (180/300/max seconds). Replaced Ollama classify_task (~22s/call) |
| `_auto_preview_check(sandbox, executor, tools, state, project_path)` | V39: Syncs files to sandbox, detects buildable projects, starts dev server. V41: Also called after every file-changing DAG task for auto-redeploy; saves port to `_preview_port.json` |
| `_save_preview_port(project_path, host_port, container_port)` | V41: Persists host port mapping to `.ag-supervisor/_preview_port.json` for crash recovery |
| `_release_stale_preview(project_path)` | V41: On startup, kills processes bound to previously-saved ports (cross-platform) and deletes stale file |
| `_clear_preview_port(project_path)` | V41: On shutdown, removes the preview port file |
| `_update_dag_progress(planner, depth, running, state)` | V40: Async — broadcasts DAG progress to UI at 8 lifecycle points, records activity + changes. V41: Called immediately after mark_complete/mark_failed in pool_worker |
| `_audit_completed_work(files_changed, executor, local_brain, planner, session_mem, goal, effective_project, state)` | V41: Post-DAG audit — ONLY creates tasks (never fixes directly). Uses AST-sliced context (50K budget) instead of raw source dumps |
| `_git_checkpoint(project_path)` | V41: Commits baseline before DAG start so broken worker output can be reverted |
| `_validate_worker_files(project_path, changed_files)` | V41: Validates changed files for syntax errors (Python via ast, JS via bracket balance) |
| `_revert_worker_files(project_path, files)` | V41: Reverts specific files to last git checkpoint on validation failure |
| `_decompose_user_instructions(instructions, planner, project_path, state)` | V44: Bundles user prompts, sends to Gemini with DAG state + file tree for subtask decomposition. Returns list of `{task_id, description, dependencies}`. Fallback to single task on failure |

#### Systems Implemented in main.py

| System | Purpose |
|--------|---------|
| Lockfile Memory | Prevents re-injection after restart (anti-amnesia) |
| Session Persistence | Auto-resume from last goal on restart |
| Sandbox Lifecycle | Create, mount, execute, monitor, cleanup Docker containers |
| Headless Monitoring | Structured context gathering via HeadlessExecutor |
| Auto-Recovery | 4-tier sandbox-aware recovery escalation |
| Auto-Fix | V39: Diagnose + retry on failure (parallel lanes, sequential nodes, monitoring loop) |
| Auto-Preview | V39: Detects buildable projects, starts dev server at 4 trigger points. V41: Auto-redeploy after every file-changing task |
| DAG Persistence | V39: Tasks tab stays populated when pending/failed tasks remain |
| File Logging | V39: FileHandler attached at `run()` start — captures all logs via launcher path |
| Continuous Worker Pool | V40: Semaphore-limited pool with `asyncio.wait(FIRST_COMPLETED)` — idle workers fill immediately. V41: True parallel execution via Tier 2 per-file locking |
| Deterministic Monitoring | V41: Replaced Ollama `decide_action()` (~76s/call) with instant heuristics using structured context; routes to `fix_errors`/`start_server`/`resume_dag`/`execute_task`/`wait` in ~0ms |
| Deterministic Timeouts | V41: Replaced Ollama `classify_task()` (~22s/call) with description-length heuristic; tiers: 180s/300s/max; floor raised from 120s to 180s |
| Post-DAG Audit | V41: `_audit_completed_work()` ONLY creates tasks (never fixes directly). Uses Gemini CLI with full project context, verification-based (not assumption-based), uncapped task creation |
| Proactive Idle Audit | V40: Full project audit after ~5min idle; max 10/day |
| Live DAG Broadcasting | V40: `_update_dag_progress()` is async, broadcasts at 8 lifecycle points. V41: Also broadcasts immediately after mark_complete/mark_failed |
| DAG History Persistence | V41: `save_history()` appends to `dag_history.jsonl` + `PROJECT_STATE.md` before every `clear_state()` |
| Context-Aware Decomposition | V41: Gemini receives PROJECT_STATE.md, file tree, and dag_history during task planning |
| Expandable Task Nodes | V41: Tasks tab descriptions truncated to 2 lines; click to expand/collapse full details; CSS line-clamp with smooth transition |
| Preview Port Persistence | V41: Host port saved to `_preview_port.json` on preview start; stale ports released on startup; file cleared on shutdown |
| Activity-Based Timeout | V41: Incremental stdout reading; inactivity timeout scaled: 180s simple, 300s medium, 600s complex (matches CLI's 10-min API timeout); 30-min ceiling; heartbeat log; timeout+files→partial promotion; project-level `.gemini/settings.json` bootstrapped; dict→TaskResult conversion fixed |
| AST Context Slicing | V41: `extract_relevant_bodies()` replaces raw 160K source dumps with targeted function/class body extraction; Python via ast, JS via brace-depth counting; 50K budget |
| Worker Isolation | V41: Tier 1 — `_git_checkpoint()` before DAG, `_validate_worker_files()` after each task, `_revert_worker_files()` on failure. Tier 2 — Shadow containers: `create_shadow()`, `validate_in_shadow()`, `destroy_shadow()` for runtime-isolated validation in ephemeral 512MB Docker containers |
| DAG History Tab | V41: `/api/dag/history` serves `dag_history.jsonl`; new History tab with collapsible run cards, task lists, status badges; newest first |
| Goal Persistence | V41: Auto-detects updated `SUPERVISOR_MANDATE.md` between sessions and reloads goal; 'E' key for in-place editing during resume; session state re-saved |
| Interactive Goal | CLI goal selection with history |
| Instruction Decomposition | V44: `_decompose_user_instructions()` -- bundles queued prompts, decomposes via Gemini into atomic subtasks with dependencies. Wired into pool loop, monitoring loop (Path A/B), and manual mode pause loop. Fallback to single task on Gemini failure |
| Manual/Auto Mode | V44: `execution_mode` toggle -- manual mode pauses workers after current tasks complete and waits for user instructions. Auto mode resumes from where it left off |
| Pause-on-Quota | V44: `pause_on_quota` toggle -- stops instead of sleeping when all API rate limits are exceeded. Enters 5s poll loop with instruction draining |
| Session Saves | V44: `_save_session_state()` called after every completed task, not just at shutdown, ensuring nothing is missed |

---

## 2.2 `config.py` — Central Configuration (~261 lines)

Single source of truth for all constants, thresholds, and mandates.

#### Configuration Sections

| Section | Key Constants |
|---------|---------------|
| **File Paths** | `CONFIG_FILE_PATH`, `AG_COMMANDS_PATH` |
| **Workspace** | `set_project_path()`, `get_project_path()`, `get_state_dir()` |
| **Mandates** | `ULTIMATE_MANDATE`, `TINY_INJECT_STRING`, `MANDATE_FILENAME` |
| **Lockfile** | `LOCKFILE_NAME = ".supervisor_lock"` |
| **ANSI Colors** | `ANSI_GREEN`, `ANSI_RED`, `ANSI_CYAN`, `ANSI_MAGENTA`, `ANSI_YELLOW`, `ANSI_BOLD`, `ANSI_RESET` |
| **Gemini CLI** | `GEMINI_CLI_CMD` (default: `gemini`), `GEMINI_CLI_MODEL = "auto"`, `GEMINI_TIMEOUT_SECONDS = 600`, `GEMINI_FALLBACK_MODEL`, `GEMINI_MODEL_PROBE_LIST` (Gemini 3+ only) |
| **Prompt Guard** | `PROMPT_SIZE_WARN_CHARS = 8000`, `PROMPT_SIZE_MAX_CHARS = 15000` |
| **Task Decomposition** | `COMPLEX_TASK_CHAR_THRESHOLD = 2000`, `MAX_CONCURRENT_WORKERS = 3` — dynamic via `DailyBudgetTracker` |
| **Retry Policy** | `GEMINI_RETRY_MAX_ATTEMPTS = 3`, `GEMINI_COOLDOWN_DELAYS = [30, 120, 600, 1800]` |
| **Context Budget** | `CONTEXT_BUDGET_WARN_CHARS`, `CONTEXT_BUDGET_MAX_CHARS` |
| **Rate Limits** | `RATE_LIMIT_DEFAULT_WAIT_S = 30`, `RATE_LIMIT_MAX_WAIT_S = 180` |
| **AI Ultra Budget** | `AI_ULTRA_RPM = 120`, `AI_ULTRA_DAILY = 2000` |
| **Self-Healing** | `SELF_IMPROVEMENT_INTERVAL_S = 3600` |
| **Docker/Sandbox** | `SANDBOX_IMAGE = "python:3.11-slim"` (fallback; uses `supervisor-sandbox:latest` if built), `SANDBOX_WORKSPACE_PATH = "/workspace"`, `SANDBOX_TIMEOUT_S = 300`, `SANDBOX_MEMORY_LIMIT = "2g"`, `MCP_TOOL_TIMEOUT_S = 120` |
| **Ollama** | `OLLAMA_HOST = "http://localhost:11434"` (in `headless_executor.py`), `OLLAMA_MODEL = "llama3.2:3b"` (in `headless_executor.py`), `OLLAMA_VISION_MODEL` (in `local_orchestrator.py`) |

---

## 3. Sandbox Execution Layer

**V34** replaced the entire Playwright/CDP/DOM automation layer (15 modules, ~7,500 lines) with a Docker sandbox architecture (4 new modules, ~1,920 lines).

> **Security**: GEMINI_API_KEY and GOOGLE_API_KEY are **NEVER** passed into the container. The container runs as a non-root `sandbox` user. No AI tools are installed inside the container.

### 3.1 `sandbox_manager.py` — Docker Lifecycle Manager (~650 lines)

Manages the entire Docker container lifecycle.

**Two installation paths:**
- **`Command Centre.bat`** (primary): Auto-installs Docker Desktop via `winget install Docker.DockerDesktop` with 180s timeout. User runs the `.bat`, so consent is implicit.
- **`python -m supervisor`** (direct): If Docker is missing, raises `DockerNotAvailableError` with platform-specific install commands (Windows: `winget install Docker.DockerDesktop`, macOS: `brew install --cask docker`, Linux: `curl -fsSL https://get.docker.com | sh`). Does NOT auto-install.

If the daemon is installed but not running, `Command Centre.bat` starts Docker Desktop automatically and waits 45s. The Python code tells the user to start Docker Desktop manually.

#### Docker Verification

`verify_docker()` checks:
1.  Docker CLI on PATH — if missing, raises error with install commands (Windows/macOS/Linux)
2.  Docker daemon responsive — if not running, tells user to start Docker Desktop manually

#### `SandboxManager` Class

| Method | Description |
|--------|-------------|
| `__init__(project_path, mount_mode)` | Initializes with project path and mount mode (`bind` or `copy`) |
| `ensure_docker()` | Verifies Docker is installed and running (raises error if not) |
| `build_image()` | Builds custom image from `Dockerfile.sandbox` |
| `create_sandbox()` | Creates ephemeral container with workspace |
| `destroy_sandbox()` | Stops and removes the container |
| `exec_command(cmd, timeout)` | Executes a command inside the sandbox |
| `copy_file_in(local, remote)` | Copies file into the container |
| `copy_file_out(remote, local)` | Copies file out from the container |
| `get_health()` | Returns container health status |
| `sync_files_to_sandbox()` | V39: Re-copies HOST files into container via `docker cp` (copy mount mode) |
| `create_shadow(task_id, project_path)` | V41: Spins up ephemeral 512MB shadow container named `shadow-{task_id}-{uuid6}` (unique per worker). No port bindings — headless validation only. Returns container name or None if Docker unavailable |
| `validate_in_shadow(container_name, changed_files)` | V41: Runs syntax/import/test validation inside shadow container. Python: `ast.parse()`, JS: `node --check`, pytest if detected. 15s per-command timeout. Returns `(is_valid, errors)` |
| `destroy_shadow(container_name)` | V41: Force-kills shadow container immediately (`docker rm -f`). Called in `finally` block to prevent container leaks |

#### Workspace Mounting

| Mode | Speed | Isolation | Use Case |
|------|-------|-----------|----------|
| **`copy`** (default) | Slow initial | **High** | **Default. Hallucinated scripts can't corrupt the host.** |
| `bind` | Fast | Low | Development with hot reload (opt-in only) |

#### Ollama Host Passthrough

Containers access the host's Ollama instance via `OLLAMA_HOST=http://host.docker.internal:11434` environment variable, with platform-specific `--add-host` configuration.

### 3.2 `headless_executor.py` — Host Intelligence Executor (~850 lines)

"Host Intelligence, Sandboxed Hands" — Gemini CLI runs on the HOST using the user's authenticated AI Ultra session. The sandbox is a dumb terminal.

#### `OllamaLocalBrain` Class

Fast (~200ms) local analysis via Ollama HTTP API.

| Method | Description |
|--------|-------------|
| `analyze(prompt)` | Quick local LLM analysis |
| `classify_task(task)` | Classifies task complexity |
| `extract_context(output)` | Extracts structured context from output |

Uses `aiohttp` for non-blocking HTTP communication with Ollama's `/api/chat` endpoint.

#### `HeadlessExecutor` Class

Orchestrates task execution with Gemini CLI on the HOST and sandbox for file/command execution.

| Method | Description |
|--------|-------------|
| `execute_task(task, sandbox)` | Primary execution pipeline. V56: Captures tsc error state before Gemini runs (`load_baseline()` or live capture). After file writes, captures post-state, calls `check_regressions()`, and attaches regressions to `TaskResult.ts_regressions` for pool_worker to inject micro-fix task. |
| `gather_context(sandbox)` | Context Bridge — reads sandbox state via docker exec |
| `_run_gemini_on_host(prompt, timeout)` | Runs Gemini CLI on HOST via subprocess |
| `_gather_sandbox_context_for_prompt()` | Context Bridge — feeds sandbox state into prompt |
| `_execute_as_shell(cmd, timeout)` | Action Bridge — runs shell commands in sandbox |
| `_execute_via_gemini_cli(prompt, ...)` | Builds full_prompt with mandate + context + skills + regression contract + task. V56: Injects `TYPESCRIPT REGRESSION CONTRACT` block from `load_baseline()` listing clean files. |
| `start_dev_server(timeout)` | V39: Robust dev server launch with priority chain |

#### V39 Dev Server Launch Chain

`start_dev_server()` uses the following priority chain:

1.  **npm install** — Runs `npm install` if `package.json` exists but `node_modules` doesn't
2.  **nohup background** — All server commands use `nohup cmd > /tmp/dev-server.log 2>&1 &` to survive `docker exec` session closure
3.  **Dynamic port** — Uses `sandbox._active.preview_port` (container-internal), Docker maps to host automatically
4.  **Server selection**: `npm run dev` → `npm start` → `npx -y serve` → `python3 -m http.server` (fallback)
5.  **Retry polling** — Polls port 3 times with 3s sleep; reads `/tmp/dev-server.log` on failure
6.  **python3 fallback** — If primary server fails, automatically retries with `python3 -m http.server` (always available)

#### Host Intelligence Flow

```
Task → Ollama (classify complexity, ~200ms)
         │
         ├── Simple → Ollama handles directly
         │
         └── Complex → Context Bridge (read sandbox state)
                          │
                          → Gemini CLI (on HOST, subprocess)
                          │
                          ├── Code changes → host writes files (bind mount)
                          ├── Commands → Action Bridge (docker exec)
                          └── Analysis → feed back to monitoring loop
```

### 3.3 `tool_server.py` — MCP Tool Interface + Path Translator (~680 lines)

Provides a structured tool interface for the host to interact with the sandbox.
Includes `PathTranslator` for bidirectional host↔container path mapping.

| Tool Category | Operations |
|---------------|------------|
| **File Ops** | Read, write, list, delete, search |
| **Shell** | Execute commands, read output, status |
| **Git** | Status, diff, commit, log |
| **LSP** | Diagnostics, lint, type check |
| **Dev Server** | Start, stop, health check |

### 3.4 `Dockerfile.sandbox` — Custom Dev Image (~80 lines)

Based on Debian Bookworm with:
- Python 3.12 + pip
- Node.js 20 LTS + npm
- Git, curl, ripgrep, jq
- Linters: ruff, eslint, prettier
- Chromium (for Visual QA)

---

## 4. Intelligence Layer (Gemini Integration)

### 4.1 `gemini_advisor.py` — Centralized Gemini CLI Interface (~1,112 lines)

Every module that needs Gemini's intelligence calls through here.

#### Primary Functions

| Function | Description |
|----------|-------------|
| `ask_gemini(prompt, timeout, use_cache, max_retries, all_files, cwd)` | Core async call with retry, failover, and budget tracking. `all_files=True` appends `@./\n\n` to the prompt so the CLI's file-expansion tool loads all project files into context. `cwd` sets the subprocess working directory — **CRITICAL for audit tasks**: must be the project path so `@./` expansion reads the right codebase. V60: `cwd` parameter added. |
| `ask_gemini_json(prompt, timeout, use_cache)` | Calls Gemini and parses JSON from response |
| `ask_gemini_sync(prompt, timeout, max_retries)` | Synchronous variant |
| `call_gemini_with_file(prompt, file_path, timeout)` | Multimodal call with file attachment |
| `call_gemini_with_file_json(prompt, file_path, timeout)` | File call + JSON parsing |

#### Features
- **Session Cache**: 50-entry LRU avoids repeat calls
- **Glass Brain**: Color-coded ANSI console output (Cyan=sent, Yellow=received, Red=error)
- **Smart Routing**: `TaskComplexityRouter` classifies prompt complexity → routes to optimal model
- **Rate Limit Awareness**: Checks `RateLimitTracker` before calls
- **CLI Modernization**: All calls use `--model` flag (Gemini CLI v0.29+)

### 4.2 `retry_policy.py` — Production-Grade Retry & Budget Systems (~1,125 lines)

#### `RetryPolicy` — Exponential Backoff with Jitter

#### `ModelFailoverChain` — Cooldown-Based Model Fallback

Model probe chain (from `config.py:GEMINI_MODEL_PROBE_LIST`): `gemini-3.1-pro-preview` → `auto` → `gemini-3.1-flash-lite-preview` → `gemini-3-flash`. Default Flash model: `gemini-3.1-flash-lite-preview`. **V57: `gemini-3.1-pro-preview-customtools` removed (CLI rejects API suffixes) and `gemini-3-pro-preview` removed (shut down March 9, 2026).** All Gemini 2.x models removed in V40.

| Method | Description |
|--------|-------------|
| `get_active_model()` | Returns best available model (respects cooldowns) |
| `report_failure(model)` | V40: Escalating cooldown: 30s → 2m → 10m → 30m (reads from config) |
| `report_timeout(model)` | Short 30s cooldown (timeouts are transient) |
| `seconds_until_any_available()` | Returns seconds until soonest model available |

#### `ContextBudget` — Token Consumption Tracking

#### `RateLimitTracker` — Smart Quota Intelligence

Extracts exact cooldown from quota errors (e.g. `"17m50s"` → 1070 seconds).

#### `DailyBudgetTracker` — V40 AI Ultra Quota Manager

Tracks daily Gemini CLI invocations against AI Ultra's 2000/day limit.

| Method | Description |
|--------|-------------|
| `record_request()` | Increment daily counter; warns at 80%/90% |
| `set_workers(count)` | Set concurrent worker count (1–6), persists to disk |
| `get_effective_workers()` | Returns user-selected worker count (always respected; no auto-throttle) |
| `get_status()` | Returns dict for UI: used/limit/pct/remaining/worker state |

Persists to `_daily_budget.json` (auto-saves every 20 requests, resets at midnight UTC).

### 4.3 `local_orchestrator.py` — Local LLM Manager (~474 lines)

Interfaces with Ollama for local inference. If Ollama is not installed, raises `OllamaUnavailable` with platform-specific manual install instructions (Windows: `winget install Ollama.Ollama`, macOS: `brew install ollama`, Linux: `curl -fsSL https://ollama.com/install.sh | sh`). **Ollama is never auto-installed** (V37 security mandate). If the required model is missing, pulls it automatically. `OllamaUnavailable` exception raised on failure (non-fatal — callers degrade gracefully).

**V37 Changes**:
- `ensure_ollama_running()` is now `async def` — uses `await asyncio.sleep()` instead of blocking `time.sleep()`
- `LocalManager.initialize()` — new async init method; callers must `await manager.initialize()` after construction

| Method | Description |
|--------|-------------|
| `ask_local_model(system_prompt, user_prompt)` | Local LLM with JSON output, 120s timeout |
| `health_check()` | Pings `/api/ps` to verify Ollama |
| `synthesize_followup(chat_history, system_goal)` | Context-aware follow-up generation |

### 4.4 `external_researcher.py` — Web Research Agent (12,508 bytes)

Performs web searches and summarizes results when external data is needed.

---

## 5. Multi-Agent Council

### 5.1 `agent_council.py` — Fast Council + Swarm Debate (~1,300 lines)

**V40**: Collapsed from 6 separate agent calls to a single Fast Council prompt (Diagnostician + Fixer + Auditor merged). The Swarm Debate (Debugger + Architect + Synthesizer) now only fires when the Fast Council returns `confidence: LOW` — saving 2–4 LLM calls per round.

#### Agent Roles (V40 Fast Council)

| Role | Merged Into | Description |
|------|-------------|-------------|
| **Diagnostician** | Fast Council | Root cause analysis — returns diagnosis, action, confidence |
| **Fixer** | Fast Council | Fix plan + action detail |
| **Auditor** | Fast Council | Quality grade (PASS/NEEDS_IMPROVEMENT) + suggestions |
| **Debugger** | Swarm Debate (LOW confidence only) | Deep reverse engineering |
| **Architect** | Swarm Debate (LOW confidence only) | Structural design |
| **Synthesizer** | Swarm Debate (LOW confidence only) | Resolves conflicting Debugger/Architect analyses |

#### Council Actions (V34)

| Action | Description |
|--------|-------------|
| `REINJECT` | Re-execute task in sandbox |
| `RUN_COMMAND` | Execute shell command |
| `OMNI_BRAIN` | Full long-term memory loop |
| `EPIC` | Multi-file autonomous execution |
| `EVOLVE` | Self-evolution code patch |
| `SCREENSHOT` | Skipped in headless mode (`SKIPPED_HEADLESS`) |
| `RESTART_HOST` | Restart sandbox container |

### 5.2 `council_knowledge.py` — Knowledge Base (10,879 bytes)

Persistent knowledge store. Learns from past resolutions.

---

## 6. Memory Systems

### 6.1 `session_memory.py` — The Hippocampus (594 lines)

Persistent session memory. Tracks everything during a supervisor session.

### 6.2 `episodic_memory.py` — Long-Term Memory (8,369 bytes)

Cross-session memories using ChromaDB for vector similarity search.

### 6.3 `memory_consolidation.py` — Memory Consolidation (8,805 bytes)

Consolidates short-term memories into long-term storage.

### 6.4 `brain.py` — Central Brain Module (7,654 bytes)

Unified interface for memory retrieval and storage.

---

## 7. Planning & Execution Engine

### 7.1 `temporal_planner.py` — DAG Planner (~36,000 bytes)

Converts goals into executable DAGs (Directed Acyclic Graphs).

#### V41 Key Features

- **Dynamic task count** — No hardcoded cap. `DECOMPOSITION_PROMPT` instructs: "Create AS MANY tasks as needed (5–50+)". Layered decomposition: core/engine → features → UI/polish.
- **Context-aware decomposition** — For complex goals (>2000 chars), skips Ollama and goes straight to Gemini CLI with full project context: `PROJECT_STATE.md`, file tree (up to 80 files), `dag_history.jsonl` (last 3 runs). Gemini is told: "Create tasks ONLY for what is MISSING, BROKEN, or needs IMPROVEMENT."
- **DAG history persistence** — `save_history()` appends all DAG nodes (status, description, dependencies, errors) to `dag_history.jsonl` and `PROJECT_STATE.md` before every `clear_state()`. No task history is ever lost.
- **Detailed descriptions** — Prompt requires: "include which files to create/modify, what functions to implement, what data structures to use."
- **Epic text: 8000 chars** (was 3000) for Gemini; 3000 for Ollama (simple goals only).

#### Key Methods

| Method | Description |
|--------|-------------|
| `decompose_epic(epic_text)` | Two-tier decomposition: Ollama (simple) or Gemini CLI (complex). V41: complex goals skip Ollama |
| `save_history()` | V41: Appends DAG nodes to `dag_history.jsonl` + `PROJECT_STATE.md` before clear |
| `clear_state()` | Wipes state files and nodes. V41: auto-calls `save_history()` first |
| `inject_task(task_id, description, dependencies, priority)` | Inserts a new task into the DAG mid-execution |
| `get_parallel_batch()` | Returns all unblocked nodes for concurrent execution |
| `mark_complete(task_id)` / `mark_failed(task_id)` | Status transitions |
| `is_epic_complete()` | Returns `True` when all nodes are complete or failed |
| `replan(failed_task_id, lesson)` | Rewrites pending tasks to route around failures |
| `record_prompt(prompt)` | V44: Records a user prompt for persistent DAG context |
| `get_user_prompts()` | V44: Returns all recorded user prompts |
| `get_task_offset()` | V44: Returns current task ID offset for continuous numbering |

#### V44 Key Features

- **Continuous task IDs** -- `_task_offset` counter persists in `epic_state.json`. LLM-generated IDs (t1, t2...) are remapped in `_parse_dag()` to continue the sequence (t30, t31...) across DAG phases. Dependencies remapped accordingly.
- **Persistent state across sessions** -- `clear_state()` no longer deletes `epic_state.json`. Instead preserves completed/failed/skipped nodes and only removes pending/running. `_parse_dag()` merges new tasks into existing archived nodes.
- **User prompt persistence** -- `_user_prompts` list saved in `epic_state.json` (last 50 prompts). Recorded at goal decomposition and instruction injection.
- **Failed/running nodes resume as pending** -- `load_state()` resets running/failed nodes to pending with fresh retries, while completed/skipped stay as-is.

### 7.2 `workspace_transaction.py` — Atomic Workspace Operations + Concurrency (~310 lines)

Transactional workspace changes with git-based rollback. Also provides `WorkspaceFileLock` — a two-tier concurrency manager for parallel DAG workers.

#### Workspace Concurrency Model

| Tier | Method | Granularity | Status |
|------|--------|-------------|--------|
| **Tier 1** | `acquire_sandbox()` | Global mutex — serializes ALL sandbox access | **Reserved** for bulk sync (`sync_files_to_sandbox`) only |
| **Tier 2** | `acquire_files(paths)` | Per-file mutex — only blocks same-file writes | **V41: ACTIVE** — all workers use this for post-execution state mutation |

**V41 behavior**: Workers run Gemini CLI truly in parallel. Docker handles concurrent `docker cp`/`docker exec` to different destination paths natively. After execution, workers acquire per-file locks on `chunk_result.files_changed` before mutating shared state (`all_files_changed`, `planner.mark_complete()`). Two workers modifying different files run with zero contention.

**Deadlock prevention**: `acquire_files()` sorts file paths before acquiring locks, ensuring consistent acquisition order across all workers.

```
Worker A: execute_task(engine.js) → acquire_files([engine.js]) → update state → release
Worker B: execute_task(ui.css)    → acquire_files([ui.css])    → update state → release
Worker C: execute_task(main.js)   → acquire_files([main.js])   → update state → release
           ────── ALL THREE RUN SIMULTANEOUSLY ──────
```

### 7.3 `cli_worker.py` — CLI Command Executor (3,622 bytes)

Executes shell commands with timeout management and error capture.

### 7.4 `workspace_indexer.py` — Workspace Index (11,421 bytes)

Structured index of all workspace files with fast search.

---

## 8. Verification & Quality Assurance

### 8.1 `autonomous_verifier.py` — TDD Verifier (13,224 bytes)

Enforces Test-Driven Development on every code change.

### 8.2 `visual_qa_engine.py` — V24 Visual QA (~290 lines)

**V34**: Rewritten for sandbox architecture. Screenshots captured inside the Docker container using `puppeteer-core` + headless Chromium. Images copied out for Gemini vision analysis.

**V40**: Per-node Visual QA removed from EPIC path. Now reserved as a **final pass before deployment only** — runs once before `DeploymentEngine.deploy_epic()`, not per-node.

#### Pipeline
1.  Install puppeteer-core in sandbox
2.  Boot dev server in sandbox
3.  Capture screenshot via headless Chrome inside container
4.  Compress to 720p
5.  Route to Gemini vision model for analysis
6.  Parse JSON verdict (PASS/FAIL with critique)
7.  Tear down dev server

### 8.3 `merge_arbiter.py` — Conflict Resolution (10,133 bytes)

Resolves conflicting changes from parallel agents.

### 8.4 `reflection_engine.py` — Post-Action Reflection (3,691 bytes)

Reflects on actions and feeds insights into the knowledge base.

---

## 9. Security & Compliance

### 9.1 `compliance_gateway.py` — Compliance Gateway (17,433 bytes)

Security gate: Secret Scanner, Dependency Audit, License Compliance, OWASP Top 10.

### 9.2 `git_manager.py` — Git Operations (8,072 bytes)

Safe git operations with rollback support. Uses `shell=config.IS_WINDOWS` for Windows PATH resolution of `git.exe`/`gh.exe` (safe: args are a list, not user-provided strings).

### 9.3 V37 Security Hardening

24 findings from a line-by-line security audit, all addressed:

| ID | Severity | Fix | File |
|----|----------|-----|------|
| C-1 | CRITICAL | Removed zero-touch Ollama installer | `local_orchestrator.py` |
| C-2 | CRITICAL | Self-evolver allowlist + path guard | `self_evolver.py` |
| C-3 | CRITICAL | `--network host` → explicit forwarding | `sandbox_manager.py` |
| H-1 | HIGH | `time.sleep` → `asyncio.sleep` | `local_orchestrator.py` |
| H-2 | HIGH | Temp file leak fix | `gemini_advisor.py` |
| H-3 | HIGH | O(n²) → O(n) JSON extraction | `gemini_advisor.py` |
| H-5 | HIGH | `shell=True` → `shlex.split` | `workspace_transaction.py` |
| H-7 | HIGH | API bind `0.0.0.0` → `127.0.0.1` | `api_server.py` |
| M-1 | MEDIUM | TOCTOU port → Docker auto-alloc | `sandbox_manager.py` |
| M-2 | MEDIUM | Credential env var blocklist | `sandbox_manager.py` |
| M-5 | MEDIUM | aiohttp session `close()` | `headless_executor.py` |
| M-6 | MEDIUM | Hardcoded path → env var | `api_server.py` |
| L-2 | LOW | `Optional` → PEP 604 (`X \| None`) | 26 files |
| L-3 | LOW | Added `__all__` to `__init__.py` | `__init__.py` |
| L-6 | LOW | Test path traversal guard | `autonomous_verifier.py` |
| L-7 | LOW | HUD uses public API | `telemetry_hud.py` |

---

## 10. Deployment Pipeline

### 10.1 `deployment_engine.py` — Autonomous Deployment Engine (23,722 bytes)

Full release pipeline from code to production.

```
Code Complete → Secret Scan → Migration Check → Stage
                                                  │
                                         Health Check (3 retries)
                                                  │
                              10-second confirmation countdown
                                                  │
                                    ┌──────Yes──────┴──────No──────┐
                                    │                               │
                              Production Deploy                 Local Only
```

---

## 11. Production Monitoring & Self-Healing

### 11.1 `telemetry_ingester.py` — Ouroboros Loop (20,045 bytes)

Self-healing production monitor. Rate-limited: 2 hotfix attempts per error per 24h.

### 11.2 `telemetry_hud.py` — Live Dashboard (4,399 bytes)

ANSI-based real-time HUD.

---

## 12. Growth & Optimization (Removed)

**V40**: `growth_engine.py` (534 lines, A/B experiments) and `finops_engine.py` (439 lines, cost profiling) were deleted. Their 4 scheduler jobs (`experiment_evaluator`, `experiment_watcher`, `finops_monitor`, `refactor_watcher`) and handler functions were also removed from `scheduler.py`. The monitoring loop now focuses exclusively on execution and recovery.

---

## 13. Scheduler & Cron System

### 13.1 `scheduler.py` — Autonomous Task Scheduler (~840 lines)

14 registered background jobs from 10s to 2h intervals. State persisted to `_cron_jobs.json`. **V40**: 4 Growth/FinOps jobs removed.

#### V36: Stale Job Cleanup

`cleanup_stale_jobs()` runs automatically at every startup via `create_default_scheduler()`. Prunes:

| Category | Description |
|----------|-------------|
| **Completed one-shots** | Ran at least once and disabled |
| **Orphaned actions** | Action key has no registered handler |
| **Duplicates** | Same action key under different names (keeps highest `run_count`) |

Log output: `⏰ Cleaned N stale job(s) (M remaining)`

---

## 14. Self-Evolution Engine

### 14.1 `self_evolver.py` — Omniscient God Loop (630 lines)

When the supervisor crashes, reads ALL source files, asks Gemini to fix, validates with `py_compile` and shadow sandbox, applies fix, reboots via exit code 42.

#### Safety Mechanisms
- Backup to `_evolution_backups/` before modification
- Pre-write AND post-write syntax checks
- Size check (rejects patches <30% of original)
- Shadow sandbox integration tests
- Auto-rollback on any validation failure
- Max 5 backup retention
- **V37**: Evolution allowlist restricts writable files to supervisor modules only

### 14.2 `loop_detector.py` — Anti-Loop Protection (5,790 bytes)

Detects repetitive cycles via action entropy analysis.

---

## 15. User Research & Polish Engines (V29)

### 15.1 `user_research_engine.py` — Qualitative Synthesis Engine (749 lines)

Ingests customer feedback, strips PII, clusters semantically, validates against product vision, auto-generates feature EPICs.

### 15.2 `polish_engine.py` — Infinite Polish Engine (519 lines)

Socratic pre-flight clarification, live preview loops, DAG injection, user-confirmed termination.

---

## 16. V35 Command Centre

Thin-client web UI served at `http://localhost:8420`. The engine runs independently — closing the browser tab has **ZERO impact** on execution.

### Architecture

```
Engine (async loop) → SupervisorState singleton ← API Bridge (FastAPI, same loop)
                                                    └→ WebSocket /ws (Glass Brain)
                                                    └→ REST /api/* (state queries)
                                                    └→ Static / (UI files)
```

### 16.1 `api_server.py` — API Bridge (~2,550 lines)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ws` | WebSocket | Glass Brain telemetry stream (logs, state, events) |
| `/api/state` | GET | Current status, model, container, uptime, queue, files_changed (no truncation) |
| `/api/logs` | GET | Recent log entries from ring buffer (2,000-entry capacity) |
| `/api/dag` | GET | V38.1: Live DAG decomposition progress (nodes, status, dependencies) |
| `/api/instruct` | POST | Push instruction onto queue (full text, never truncated) |
| `/api/preview` | GET | Live preview URL and status |
| `/api/queue` | GET | Instruction queue state + history |
| `/api/stop` | POST | V38.1: Graceful stop — saves checkpoint after current task |
| `/api/mode` | POST | V44: Toggle execution mode (`auto` or `manual`). Manual pauses workers; auto resumes |
| `/api/quota-pause` | POST | V44: Toggle pause-on-quota behavior (off/pro/all modes) |
| `/api/ports` | GET | V44: List all localhost listening ports with project preview flagged |
| `/api/ports/kill` | POST | V44: Kill a process by port number |
| `/api/dag/history` | GET | V41: DAG run history from `dag_history.jsonl` |
| `/api/health` | GET | V44: System health metrics (CPU, memory, Docker, Ollama) |
| `/api/issues` | GET | V61: Build & console issue files parsed into structured JSON with severity detection |
| `/api/model-status` | GET | V52: Current model availability and cooldown status per model |
| `/api/budget` | GET | V43: Daily budget and worker count status |
| `/api/quota-probe` | GET | V62: Per-model quota probe data (remaining_pct, resets_in_s, stale flag, alert_level) |
| `/api/lite/ask` | POST | V64: Stream response from Gemini Lite Intelligence (replaces Ollama) |
| `/api/lite/stats` | GET | V64: Gemini Lite usage stats (calls_today, fallback_count) |
| `/api/workers` | POST | V66: Set concurrent worker count (1–6) |
| `/api/projects` | GET | V62: List available projects in Experiments directory |
| `/api/projects/launch` | POST | V62: Launch supervisor on a project with goal |
| `/` | GET | Serves the Command Centre dashboard |

`SupervisorState` singleton shared between engine and API. V44 state additions: `execution_mode` (auto/manual), `pause_on_quota` (bool), `quota_pause_mode` (off/pro/all). `WebSocketLogHandler` pipes Python logging to the Glass Brain. Log ring buffer: 2,000 entries (V39, previously 500). V73: Periodic `/stats` probe runs every 60s as background asyncio task.

### 16.2 `instruction_queue.py` — Instruction Queue (~100 lines)

Async queue for user commands. UI pushes via API, engine drains at top of monitoring loop.

### 16.3 `ui/index.html` — Dashboard (~9,470 lines)

Single HTML file (no build step). Dark glassmorphism design with 12 tabs:
- **Glass Brain (Logs)** — scrolling terminal, WebSocket-fed
- **Status Bar** — goal, model, container, uptime, Ollama
- **Instruction Input** — text box → `POST /api/instruct`
- **Live Preview** -- iframe, auto-refreshing on file changes, **Open in New Tab** button
- **Tasks (Graph) Tab** -- DAG task list with progress bar, status icons, color-coded rows
- **Changes Tab** -- Files changed with commit-style diff display
- **Phases Tab** -- Phase plan progress with expand/collapse per phase
- **Timeline Tab** -- Activity event stream
- **DAG History Tab** -- Collapsible run cards from `dag_history.jsonl`
- **Ports Tab** -- All localhost listening ports; flags project preview port; kill button
- **Files Tab** -- File explorer with sort + filter
- **Health Tab** -- V61: Aggregates BUILD_ISSUES.md + CONSOLE_ISSUES.md + Vite errors; system resource bar; pulsing red badge when issues exist
- **Console Tab** -- V61: Dedicated browser console relay with level filter; auto-scroll
- **Stats Tab** -- V61: DAG progress, timing, model status, system resources
- **Universal scroll preservation** -- V61: All tabs save/restore `scrollTop` on innerHTML refresh
- **Mode Toggle** -- Auto/Manual mode pill button in header
- **Quota Pause Toggle** -- Pause-on-quota pill button in header
- **Agent Council** — Fast Council status indicator
- **Connection** — WebSocket reconnect with exponential backoff
- **📋 Copy All** — copies all Glass Brain log entries to clipboard with toast feedback
- **⏳ In-Log Spinner** — animated hourglass during long operations (`executing`, `initializing`)
- **🌐 Preview New Tab** — opens live preview in a full browser tab (appears when preview is available)

### V36: Granular Phase Logging

Every subsystem now emits tagged log messages that flow through to the Glass Brain WebSocket terminal. Tags identify the subsystem and phase:

| Tag | Source File | Phase |
|-----|------------|-------|
| `[Boot]` | `main.py` | Engine init: Docker verify, sandbox create, tool init, Ollama check, session load, scheduler |
| `[Gemini]` | `headless_executor.py` | CLI launch, subprocess PID, response timing (seconds + bytes), exit code, success/failure |
| `[Context]` | `headless_executor.py` | Sandbox state gathering and injection into prompt (reports char count) |
| `[Sandbox]` | `sandbox_manager.py` | Container create, workspace copy-in, `chown` ownership fix, workspace ready |
| `[Ollama]` | `headless_executor.py` | Availability check, model auto-select, VRAM pre-warm, `keep_alive` status |
| `[Task]` | `main.py` | Task complexity classification via Ollama (simple/medium/complex) |

#### Gemini CLI Execution Timeline

```
🔍  [1/4] Probing npm global directories for Gemini CLI …
🔍  Gemini CLI found: C:\Users\...\npm\gemini.cmd
📡  [Context] Gathering sandbox workspace state …
📡  [Context] Sandbox state collected (4520 chars). Injecting into prompt.
🚀  [Gemini] Launching CLI on HOST: model=auto, cwd=C:\...\project
🚀  [Gemini] Spawning subprocess (shell=True) …
🚀  [Gemini] Subprocess started (PID=12345). Waiting for response (timeout=600s) …
🚀  [Gemini] Response received: 45.2s, exit=0, stdout=8432 bytes, stderr=0 bytes
✅  [Gemini] Task completed successfully.
📄  Files changed (5): index.html, styles.css, main.js, config.json, README.md
```

#### V39: Auto-Preview Timeline

```
📦  [Sync] Files synced to sandbox (supervisor-s)
🖥️  [Auto-Preview] Buildable project detected — starting dev server …
🖥️  [Dev Server] Starting: PORT=3000 nohup npm run dev > /tmp/dev-server.log 2>&1 &
🖥️  [Dev Server] Running on port 3000 (attempt 1)
```

#### V39: Auto-Fix Timeline

```
❌  Task failed: status=error, errors=1
🔧  [Auto-Fix] Diagnosing failure via Ollama …
🔧  [Auto-Fix] Root cause: missing import in main.js
🔧  [Auto-Fix] Retrying with enriched context …
✅  [Auto-Fix] Retry succeeded.
```

---

## 17. Configuration Reference

### `_best_model_cache.json`

Cached best Gemini model from auto-discovery:

```json
{
  "model": "gemini-3.1-pro-preview",
  "probed_at": "2026-02-20T10:00:00Z"
}
```

---

## 17. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_CLI_MODEL` | `auto` | Gemini CLI model — `auto` routes to best available (3.1 Pro, 3 Pro, Flash) |
| `GEMINI_TIMEOUT` | `600` | Gemini CLI timeout in seconds (10min: includes Node boot + npx resolve + API response) |
| `PROMPT_SIZE_WARN` | `8000` | Prompt char count that triggers a warning log |
| `PROMPT_SIZE_MAX` | `15000` | Prompt char count above which the prompt is auto-truncated |
| `COMPLEX_TASK_THRESHOLD` | `2000` | Goal char count above which auto-DAG decomposition triggers |
| `MAX_CONCURRENT_WORKERS` | `3` | Default parallel DAG lanes (user-selectable 1–6 via header toggle) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint (passed to sandbox) |
| `OLLAMA_MODEL` | `llama3.2:3b` | Ollama model for local analysis (pre-warmed at boot, `keep_alive=5m`) |
| `SANDBOX_MOUNT_MODE` | `copy` | Workspace mount mode (**`copy`** default, `bind` opt-in) |
| `SUPERVISOR_SANDBOX_IMAGE` | `python:3.11-slim` | Fallback Docker image (overridden by `supervisor-sandbox:latest` if built) |
| `COMMAND_CENTRE_PORT` | `8420` | V36 Command Centre web UI port |
| `DEPLOY_PROVIDER` | `vercel` | Deployment provider (vercel/railway) |
| `DEPLOY_TOKEN` | — | Deployment authentication token |
| `PROJECT_CWD` | `.` | Project working directory |
| `TEST_COMMAND` | `python -m tests.mock_repo_tests` | Test command for verification |
| `DEV_SERVER_CMD` | `npm run dev` | Dev server command |
| `DEV_SERVER_PORT` | `3000` | Dev server port |
| `SUPERVISOR_LOG_LEVEL` | `INFO` | Logging level |
| `SUPERVISOR_EXPERIMENTS_DIR` | `~/Desktop/Experiments` | Project discovery directory for Command Centre |

---

## 18. File & Directory Structure

```
supervisor/
├── __init__.py              # Package init
├── __main__.py              # python -m supervisor entry
├── main.py                  # Orchestrator (~550 lines)
├── config.py                # Central configuration (~340 lines)
│
├── # Sandbox Execution Layer (V34)
├── sandbox_manager.py       # Docker lifecycle manager (~650 lines)
├── headless_executor.py     # Dual-brain executor (~760 lines)
├── tool_server.py           # MCP tool interface (~430 lines)
├── Dockerfile.sandbox       # Custom dev image (~80 lines)
│
├── # Intelligence
├── gemini_advisor.py        # Centralized Gemini CLI (~1,265 lines); all_files param V56
├── retry_policy.py          # Retry + failover + budget (~915 lines)
├── local_orchestrator.py    # Local Ollama LLM manager (~448 lines)
├── external_researcher.py   # Web research agent
├── ts_regression_guard.py   # V56: TypeScript regression guard — tsc capture, diff, contract, micro-fix
│
├── # Multi-Agent Council
├── agent_council.py         # Fast Council + Swarm Debate (~1,300 lines)
├── council_knowledge.py     # Persistent knowledge base
│
├── # Memory
├── session_memory.py        # The Hippocampus (594 lines)
├── episodic_memory.py       # Long-term memory
├── memory_consolidation.py  # Memory consolidation
├── brain.py                 # Central brain module
│
├── # Planning & Execution
├── temporal_planner.py      # DAG planner
├── workspace_transaction.py # Atomic workspace ops
├── cli_worker.py            # CLI command executor
├── workspace_indexer.py     # Workspace file index
│
├── # Verification & QA
├── autonomous_verifier.py   # TDD verifier
├── visual_qa_engine.py      # V24 Visual QA (final deployment pass only)
├── merge_arbiter.py         # Conflict resolution
├── reflection_engine.py     # Post-action reflection
│
├── # Security
├── compliance_gateway.py    # Compliance gateway
├── git_manager.py           # Git operations
│
├── # Deployment
├── deployment_engine.py     # Deployment pipeline
│
├── # Monitoring
├── telemetry_ingester.py    # Ouroboros loop
├── telemetry_hud.py         # Live HUD dashboard
│
├── # Growth & Optimization (V40: REMOVED)
├── # growth_engine.py       # DELETED in V40 — A/B experiments
├── # finops_engine.py       # DELETED in V40 — FinOps cost profiling
│
├── # Self-Evolution
├── self_evolver.py          # Self-modification engine
├── loop_detector.py         # Anti-loop protection
│
├── # Scheduling
├── scheduler.py             # Cron scheduler (~840 lines, 14 jobs)
│
├── # User Research & Polish
├── user_research_engine.py  # Qualitative Synthesis (749 lines)
├── polish_engine.py         # Infinite Polish (519 lines)
│
├── # Support
├── bootstrap.py             # Startup bootstrapper
├── skills_loader.py         # Skills file loader
├── supervisor_state.py      # State machine
│
├── # State Files
├── _best_model_cache.json   # Cached Gemini model
├── _cron_jobs.json          # Scheduler state
├── _daily_budget.json       # V40: Daily quota tracker state
├── _failover_state.json     # Model failover state
├── _session_state.json      # Session memory
├── workspace_map.json       # Workspace index
├── supervisor.log           # Runtime log file
│
├── # Per-Project State (in .ag-supervisor/)
├── epic_state.json                # DAG state for crash recovery
├── dag_history.jsonl              # V41: Persistent DAG run history (append-only)
├── audit_done_fingerprints.json   # V56: MD5 fingerprints of completed audit tasks (dedup)
├── ts_error_baseline.json         # V56: Persisted tsc error baseline for regression guard
│
├── # Assets
├── help.txt                 # Gemini CLI help text
└── _evolution_backups/      # Self-evolution backups
```

---

## 19. Version History

| Version | Codename | Key Feature |
|---------|----------|-------------|
| V1–V5 | — | Playwright, DOM probing, self-healing, loop detection |
| V6 | Unbreakable | 9 core systems: lockfile, approval sniper, mandate firewall |
| V7 | Background Mode | Non-interactive operation, auto-restart |
| V8–V11 | — | Gemini file attachments, retry policy, model failover, rate limits |
| V12 | — | Time-travel engine (state snapshots) |
| V13 | — | Agent Council (Fast Council — originally 6 agents, collapsed in V40) |
| V14 | — | Self-evolution engine with shadow sandbox |
| V15–V18 | — | Hard reset rollback, deep context, workspace indexer, external researcher |
| V19 | Temporal Planner | DAG-based task planning and execution |
| V20–V21 | — | Workspace transactions, proactive engine, reflection |
| V22 | TDD Verifier | Autonomous test-driven development |
| V23 | Visual QA | Screenshot-based visual regression testing |
| V24 | Compliance Gateway | Security scanning, OWASP, license audit |
| V25 | Autonomous Deploy | Full release pipeline with health checks |
| V26 | Ouroboros Loop | Production telemetry, self-healing hotfixes |
| V27 | Growth Engine | A/B experiments, conversion optimization |
| V28 | FinOps Engine | Compute cost profiling, margin decay detection |
| V29a/b | User Research + Polish | Feedback ingestion, PII redaction, Socratic pre-flight |
| V30–V30.6 | Total Automation | Route guard, mandate generator, boot resilience, quota intelligence |
| V31 | Forensic Hardening | 9 fixes: graceful shutdown, outer timeout, model probe order |
| V32 | Major Leap Forward | 13 fixes: smart cooldown, CLI modernization, multi-port scan |
| V33–V33.1 | Boot Overhaul | 7-strategy progressive boot, direct injection, browser preview fix |
| **V34** | **Headless Sandbox** | **Complete architecture migration**: Playwright/CDP/DOM (15 modules, ~7,500 lines) replaced with Docker sandbox + Gemini CLI + Ollama dual-brain (4 modules, ~1,920 lines). 550-line headless orchestrator. **Docker required** (raises error with install instructions if missing — never auto-installed). Smart workspace mounting (bind/copy). Sandbox recovery strategies. Visual QA via container headless Chrome. |
| **V35** | **Command Centre** | V35 Command Centre web UI — Glass Brain terminal, live preview, instruction queue, WebSocket telemetry. FastAPI backend with REST + WS endpoints. |
| **V36** | **Smart Discovery** | **Gemini CLI auto-discovery** — probes npm global dir, PATH, npx fallback; handles Windows `.cmd` wrappers via `create_subprocess_shell`. **Model `auto`** — routes to best available model (3.1 Pro, 3 Pro, Flash). **Ollama pre-warm + VRAM mgmt** — `warm_up()` loads model into GPU/RAM at boot; `keep_alive=5m` auto-unloads after 5min idle. **Scheduler cleanup** — `cleanup_stale_jobs()` prunes completed one-shots, orphaned actions, duplicates. **UI improvements** — Open in New Tab for preview, Copy All toast feedback, in-log spinner for long ops. **Timeout 600s** for Node boot + npx resolve. **Zero truncation** — goals and user instructions logged in full everywhere. **Sandbox EACCES fix** — single batched `chown -R` on full `/workspace` after `docker cp` (not per-file). |
| **V37** | **Security Hardening** | **24-finding security audit** — line-by-line review of all 43 modules (~20,000 lines). 3 CRITICAL fixes (removed silent installer, self-evolver allowlist, network isolation). 7 HIGH fixes (async sleep, shell injection, JSON O(n), API localhost binding). 7 MEDIUM fixes (credential blocklist, TOCTOU port, aiohttp leak). 7 LOW fixes (PEP 604 modernization across 26 files, `__all__`, path validation, public HUD API). Architecture health: 8.2 → 9.1/10. |
| **V38** | **Smart Execution** | **DAG decomposition** — complex tasks auto-decomposed into atomic chunks with parallel execution (up to `MAX_CONCURRENT_WORKERS` lanes). **Smart routing** — Ollama classifies task complexity → routes to optimal model. **Supervisor state machine** — structured enum-based state transitions with history tracking. **Adaptive timeouts** — per-chunk timeouts based on Ollama classification (estimated_duration × 3, clamped 60s–600s). **DAG API** — `/api/dag` endpoint, `/api/stop` graceful shutdown, Tasks tab in UI. |
| **V39** | **Self-Healing & Auto-Preview** | **Auto-fix on task failure** — `_diagnose_and_retry()` diagnoses failures via Ollama, builds enriched retry prompt, retries once; wired into parallel lane failures, sequential node failures (before replan), and monitoring loop errors. **Auto-preview** — `_auto_preview_check()` detects buildable projects and starts dev server at 4 trigger points (boot, post-goal, post-instruction, every 30s). **Dev server robustness** — `nohup` background with output redirect, `npm install` gating, dynamic `preview_port`, retry polling (3×3s), python3 http.server fallback. **File sync** — `sync_files_to_sandbox()` re-copies HOST files into container for copy mount mode. **Log completeness** — FileHandler attached at `run()` start (was missing in launcher path), actual filenames logged per task, log buffer 500→2000 entries, no truncation on files_changed. **Tasks tab persistence** — DAG nodes preserved when pending/failed tasks remain. **State updates** — monitoring loop tasks now update `state.files_changed`, `state.last_task_status`, `state.tasks_completed`. |
| **V40** | **Autonomous Efficiency & Quota Intelligence** | **Gemini 3+ only** — removed all 2.x models from probe list, pro models, flash models, and default for better accuracy. **Continuous worker pool** — replaced batch `asyncio.gather()` with semaphore-limited pool + `asyncio.wait(FIRST_COMPLETED)`: idle workers instantly pick up next unblocked DAG node. **DailyBudgetTracker** — tracks requests against AI Ultra 2000/day limit; warns at 80%/90%, auto-throttles workers at 90%/95%; persists to `_daily_budget.json`. **Boost mode** — `activate_boost()` gives 4 workers for 2h, once per 24h cycle. **Live DAG broadcasting** — `_update_dag_progress()` converted to async with `state=` param, broadcasts at 8 lifecycle points, `record_activity()`/`record_change()` for all chunk events. **Post-DAG audit** — `_audit_completed_work()` runs after DAG completion: scans code against original goal and creates deduplicated DAG tasks for issues found. **Proactive idle audit** — when supervisor is idle ~5min (30 ticks), runs full project audit; max 10/day to conserve budget. **Stop button UX** — "Stopping… Please wait" → "✅ Ready to Close" with safe-to-close tooltip. **Shorter cooldowns** — 30s→2m→10m→30m (was 1m→5m→25m→1h). **Rate limit tuning** — default wait 60s→30s, max wait 300s→180s, history 20→30 events. **Active model display** — `state.active_model` set from failover chain at boot and updated every 60s. |
| **V41** | **Context-Aware Intelligence & True Parallelism** | **Dynamic task count** — Gemini creates 5–50+ tasks with layered decomposition. **Context-aware decomposition** — receives PROJECT_STATE.md, file tree, dag_history. **AST context slicing** — `extract_relevant_bodies()` replaces 160K dumps with 50K targeted extraction. **Activity-based timeout** — kills only after 120s silence, 30-min ceiling. **Worker isolation** — Tier 1: git checkpoint + host-side syntax check; Tier 2: shadow container validation (512MB ephemeral Docker, 15s timeouts, auto-fallback). **Shadow containers** — `create_shadow()`, `validate_in_shadow()`, `destroy_shadow()` in SandboxManager; per-worker runtime isolation. **Goal persistence** — auto-detects updated mandate files, 'E' key for in-place editing on resume, immediate UI `/api/projects/launch` persistence. **True parallel execution** — per-file locking, 3 simultaneous workers. **DAG History tab** — collapsible run cards from dag_history.jsonl. **Deterministic monitoring/timeouts** — replaced Ollama calls with instant heuristics. **TaskResult robust accessor** — crash-proof `_g()` helper in executor. **Docker auto-recovery prompt** — intercepts timeouts on Windows to exterminate hung WSL/Docker processes. **Docker WSL init** — `sc.exe config wslservice start= demand` pre-flight command. |
| **V43** | **Smart Quota Management** | **429 rate limit detection** -- pauses all task execution when quota exhausted; `pause_for_quota()` / `resume_from_quota()` in `DailyBudgetTracker`. **Global quota pause** -- workers sleep until midnight Pacific Time reset. **Quota stats in UI** -- top bar pill shows daily usage, remaining budget, pause state. **Failover chain cooldowns** -- escalating cooldowns per model (30s, 2m, 10m, 30m) with `all_models_on_cooldown()` check. |
| **V44** | **Manual Mode, Instruction Decomposition & Persistent State** | **Manual/Auto mode** -- dashboard toggle; manual pauses after current tasks, auto resumes; `/api/mode` endpoint. **Pause-on-quota toggle** -- `/api/quota-pause`. **Smart instruction decomposition** -- `_decompose_user_instructions()`. **Persistent DAG state** -- completed/failed/skipped nodes preserved. **Continuous task IDs** -- `_task_offset` persists. **User prompt persistence** -- `_user_prompts` in `epic_state.json`. **Session saves**. **Ports tab**. **Sidebar animations**. **2026 Visual Design Mandate** -- HSL colors, premium typography, micro-interactions, glassmorphism. |
| **V45–V54** | **Dependency Intelligence, Health Reporting & UI Polish** | **Session Complete report** -- tasks, files, duration, DAG stats, errors, cycles, project path, log tail. **Telemetry sidebar** -- live stats fixed (were stuck at 0). **`CONSOLE_ISSUES.md`** -- 3-phase: auto-install missing imports → record errors or “All Clear” → refresh `BUILD_ISSUES.md`; both sent as one combined CLI fix-task. **Missing import auto-install (startup)** -- `_scan_dev_server_console()` installs unresolved imports, restarts dev server. **Missing import auto-install (post-task)** -- `resolve_missing_imports()` standalone method called after every file sync. **`_log_npm_output()`** -- module-level helper streams per-package install progress, add/update counts, `⚠️` warnings, `❌` errors to Glass Brain. **`_try_upgrade_major_deps()`** -- runs after every build_health_check(); tries `npm install@latest` + `tsc --noEmit`; success marks BUILD_ISSUES resolved; failure reverts from backup and appends TS error context. **Stop button redesign** -- red pill, `::before` square icon, amber spinner + pulse on `.stopping`, green dot on complete. **Volume cleanup log** -- now distinguishes “X cleaned — Y still mounted by running containers”; nothing-to-clean at DEBUG level. **Duplicate state-save fix** -- removed redundant `_save_state()` after `mark_complete()`; no more double log entries. |
| **V55** | **Comprehensive Local Self-Healing & Model Refresh** | **Gemini 3.x model roster** -- `gemini-3.1-pro-preview` (Pro primary) → `gemini-3-pro-preview` (Pro fallback) → `auto` → `gemini-3.1-flash-lite` (Flash primary) → `gemini-3-flash` (Flash fallback). **dev-server.log multi-pattern self-healer** -- 4 patterns with targeted fix. **Browser console local self-healer** -- intercepts `.vite` 504, `ERR_CONNECTION_REFUSED`, and Vite chunk errors before Gemini sees them. **Sync exclusion hardening** -- all three sync layers updated. |
| **V56** | **Audit Accuracy & TypeScript Regression Guard** | **`--all_files` audit mode** -- `ask_gemini()` gains `all_files: bool` param; audit call passes `all_files=True` + `use_cache=False`; Gemini CLI loads all non-gitignored files natively, eliminating 3s pattern-match responses. **Persistent audit dedup** -- `.ag-supervisor/audit_done_fingerprints.json` stores MD5 hashes of completed audit task descriptions across cycles; `_desc_fp()` creates stable normalized fingerprints. **TypeScript Regression Guard** (`ts_regression_guard.py`) -- `capture_ts_errors()` runs tsc via asyncio subprocess; `check_regressions(pre, post)` diffs fingerprint sets; `build_regression_contract()` builds prompt block listing clean files; `build_microfix_description()` formats targeted DAG task. `headless_executor.execute_task()` captures pre/post tsc state, attaches regressions to `TaskResult.ts_regressions`. `_execute_via_gemini_cli()` injects regression contract into every task prompt. `pool_worker` in `main.py` reads `ts_regressions` after `mark_complete()` and injects `{task_id}-tsfix` micro-fix DAG task — no quota-wasting retries. Baseline persisted to `.ag-supervisor/ts_error_baseline.json`. |
| **V57** | **Quality & Efficiency Suite** | **Self-review cascade prevention** -- `needs_self_review` never set for `[SELF-REVIEW]`, `[HEALTH]`, `[TSFIX]`, lint tasks (prompt guard in `headless_executor`). Second-line `pool_worker` guard checks `node.task_id` for meta suffixes (`-review`, `-tsfix`, `-srvchk`, `-health`, `-lint`). **Prompt de-nesting** -- `self_review_context` strips nested `ORIGINAL TASK` chains; only root task's first 400 chars forwarded. **Model roster cleanup** -- removed `gemini-3.1-pro-preview-customtools` (CLI rejects API variant suffixes) and `gemini-3-pro-preview` (shut down March 9, 2026); corrected flash-lite name to `gemini-3.1-flash-lite-preview`; `GEMINI_FALLBACK_MODEL` updated; stale cache cleared. **Queued task state** -- `_update_dag_progress()` accepts `queued_ids`; pool loop passes active-but-pending task IDs immediately after worker launch; UI shows blue-tinted Queued row between Running and Pending. **Preview dot colour fix** -- dot was toggling class `live`; CSS only defines `.online`/.offline`; fixed. **Phase expand/collapse memory** -- `_userPhasePref` Map prevents poll refreshes from overriding user's manual expand/collapse choices. |
| **V58** | **Five-Layer File Conflict Protection** | **Layer 1** -- File-claim registry in `temporal_planner.py`: `register_file_writes()` + `inject_file_conflict_deps()` auto-serialize parallel workers writing to the same file. **Layer 2** -- Current-file injection in `headless_executor.py`: `_inject_current_file_states()` prepends freshest on-disk content before every Gemini call; capped at 5 files × 60KB. **Layer 3** -- Regression guard in `main.py`: `node._pre_task_line_snap` before task, line-count diff after; >40% shrinkage on >30-line source files → auto-inject `{id}-merge` recovery task. **Layer 4** -- Git commit per task: `git add -A && git commit` after every successful task; `bootstrap.py` runs `git init` idempotently. **Layer 5** -- SEARCH/REPLACE patch protocol: `GEMINI.md` instructs surgical diff blocks instead of full rewrites. Self-reviews disabled (V57 decision). |
| **V59** | **UI Reliability & Auto-Fix Improvements** | **Supervisor UI HTML rendering fixed** -- All dynamically-injected HTML strings used broken `< tag attr = "val" >` syntax causing plain-text rendering. Fixed across `addLogLine`, `updatePreview`, `ftBuildNode`, `ftRender`, `_tlToggleDetail`, file-tree error states. **Ollama removed from auto-fix path** -- Local models can't handle task context size; Gemini retries with raw error text directly — no dead latency. **Merge recovery excludes generated files** -- `BUILD_ISSUES.md`, `CONSOLE_ISSUES.md`, `PROJECT_STATE.md`, `PROGRESS.md`, etc. excluded by filename; `.md` removed from regression-tracked extensions (source-only: `.ts/.tsx/.js/.jsx/.py/.css/.scss/.html`). **Session complete screen** -- `sr-card` compacted (reduced padding/font) + `max-height: 90vh` + `overflow-y: auto`. **Quota pill fixed** -- Was reading non-existent `s.quota_used`; now reads `s.quota.daily_used/daily_limit/daily_pct`. **`toggleQuotaPause` + `sendInstruction` URL fixed** -- Spaces in template literals broke fetch URLs. **Dev server reinstall crash fixed** -- `start_dev_server(port=, sandbox=)` kwargs removed. |
| **V60** | **Deep Analysis Pipeline, Plan Mode, Audit CWD Fix & Critical Path Scheduling** | **Deep Analysis** — `_run_deep_analysis()` writes `DEEP_ANALYSIS.md` then immediately calls `bootstrap_workspace()` to re-bake findings into `GEMINI.md`; resume guard skips re-analysis if >5 nodes already complete. **Two-step Plan Mode** — for complex tasks (`PLAN_MODE_ENABLED` + prompt > `PLAN_MODE_CHAR_THRESHOLD`), Step 1 spawns a read-only `--approval-mode=plan` subprocess (90s timeout, stdin/stdout, no file writes) and captures the plan; Step 2 injects that plan into the `--yolo` execution prompt; graceful preamble fallback if Step 1 fails. **Audit CWD fix** — `ask_gemini()` and `_call_gemini_async()` gain a `cwd` parameter; both audit call sites in `main.py` pass `cwd=effective_project` so `@./` file expansion reads the target project, not the supervisor directory. **Critical path scheduling** — `_compute_critical_path()` calculates descendant counts; `get_parallel_batch()` sorts tasks by critical-path depth to run bottleneck tasks first. **Dynamic replan count** — `_max_replan_count = max(3, len(tasks) // 10)` computed in `_parse_dag()`; cache invalidated on `inject_task()` and `inject_nodes()`. **OOM prevention** — `NODE_OPTIONS=--max-old-space-size=4096` injected into all Gemini CLI subprocess environments. **Silent timeout detection** — `TaskResult.silent_timeout` flag; `_diagnose_and_retry` branches on timeout-with-zero-output vs normal errors. **Batch `.gemini/settings.json`** — `checkpointing: true`, `ui.inlineThinkingMode: full`, `timeout: 600` always overwritten at bootstrap. **SyntaxWarning fix** in `headless_executor.py` (raw string for shell pkill pattern). **Ollama check** in `Command Centre.bat` fixed: unescaped `)` caused both branches to print; now uses `where ollama` as primary check with escaped parens. |
| **V61** | **Command Centre Health/Console/Stats Tabs & Crash Fix** | **Health tab** — aggregates `BUILD_ISSUES.md` + `CONSOLE_ISSUES.md` + Vite compile errors; system resource bar (CPU, Memory, Docker, Uptime); pulsing red badge when issues exist; header pill `✓ CLEAN` / `✕ N ISSUES`; auto-refreshes when tab is active. **Console tab** — dedicated browser console relay view; reuses existing `_consoleLogs` array (zero overhead); level filter; smart auto-scroll; error badge. **Stats tab** — DAG progress (Complete/Pending/Running/Failed) with live progress bar; timing (session uptime, last/avg task durations); model availability with cooldown countdowns; system CPU/Memory/Disk. **`/api/issues` endpoint** — parses `BUILD_ISSUES.md` and `CONSOLE_ISSUES.md` into structured JSON with severity detection (`error`/`warning`/`info`). **Universal scroll preservation** — `saveScrolls()`/`restoreScrolls()` helper pair applied to all 10+ tab panels; Console tab uses smart bottom-detect instead of always resetting. **`temporal_planner.py` crash fix** — `NameError: name 're' is not defined` in `get_parallel_batch()` was crashing the supervisor at startup (premature session-complete screen); fixed by adding `import re` at module scope. |
| **V62** | **Quota System Overhaul & Smart Estimation** | **Bulletproof quota pause** — 4 new leak-gap guards: audit cycle sleeps until reset, vision refresh skipped, proactive audit skipped, headless executor returns immediate failure. Defense-in-depth ensures zero API calls when quota is exhausted. **All-model quota display** — `get_quota_snapshot()` seeds ALL models from `GEMINI_MODEL_PROBE_LIST` at 100% defaults; actual probe data overlays. UI always shows every model the supervisor uses. **Pooled quota buckets** — `QUOTA_BUCKETS` in `config.py`: Pro bucket (3.1-pro-preview + 2.5-pro, ~500 RPD), Flash bucket (3-flash-preview + 2.5-flash, ~1500 RPD), Lite bucket (2.5-flash-lite, ~5000 RPD). `QUOTA_MODEL_TO_BUCKET` reverse lookup. **Smart per-bucket estimation** — `record_usage()` counts local CLI calls between probes; `get_quota_snapshot()` applies bucket-specific offset (Pro: 0.2%/call, Flash: 0.067%/call, Lite: 0.02%/call). Resets on successful probe, day rollover, persists to disk. **Launch-time probe** — Background daemon thread runs `/stats` probe at supervisor startup (non-blocking). **Direct node invocation** — `run_stats_probe()` bypasses `.CMD` wrapper (hangs on Windows piped stdin) by running `node index.js` directly (~18s). **429 recovery** — When quota is fully exhausted and `/stats` fails, parses `retryDelayMs` and `QUOTA_EXHAUSTED` from CLI stderr; marks the entire bucket at 0% with correct reset timer. **Exact 429 reset countdown** — `retryDelayMs` (exact ms) preferred over coarse `Xh Ym` text; stored on `TaskResult._quota_cooldown_s`; pool worker passes exact cooldown to `pause_for_quota()` with 3-layer fallback (retryDelayMs → probe snapshot `resets_at` → midnight PT); `get_status()` exposes `quota_resets_at_exact` epoch for UI timers. **Live-tested: zero quota in success** — `gemini-2.5-flash-lite` returns only model text (stdout) and `Loaded cached credentials` (stderr); no quota metadata in successful responses. **Alert thresholds** — each model includes `alert_level` (ok >25%, low 10-25%, critical 1-10%, exhausted 0%) and `bucket` name for UI color coding. **Full task history for audits** — `dag_history.jsonl` fingerprints loaded at audit time; 200-description cap removed. **🔒 Locked icon** — blocked tasks (pending with unmet deps) show locked padlock icon in DAG strip, tasks tab, and graph history. |
| **V63** | **Persistent PTY Probe, Animated Quota UI & Dynamic Model Discovery** | **Persistent PTY session** — Gemini CLI spawned once in `pywinpty` pseudo-terminal at startup (~25s); subsequent `/stats` calls reuse the open session (~2s each); `atexit` handler sends `/quit` for graceful shutdown. **Accurate PTY buffer slicing** — `_pty_wait_for()` takes `from_pos` parameter, searches only NEW buffer data, preventing stale cached output from returning false positives. **Animated quota UI** — Launcher quota panel updates every 15s with in-place DOM updates: smooth CSS bar transitions, `requestAnimationFrame` number counting (800ms ease-out cubic), green/amber cell pulse on change, ▲/▼ delta indicators that fade after 3s. **Dynamic model auto-discovery** — `classify_model(name)` auto-classifies new models by name pattern; `update_models_from_probe(discovered)` adds new models and removes deprecated ones (2-probe anti-flap delay); co-bucketing auto-detected from matching `remaining_pct` and `resets_in_s`; called after every successful PTY probe. **Preemptive failover disabled** — `QUOTA_PROBE_AVOID_THRESHOLD` set to 0%; models only switch on actual 429 errors. **Pending task SVG icon** — distinct SVG clock icon replaces dot character in DAG strip and tasks tab. **WebSocket zombie timeout** — increased from 30s to 90s to eliminate false "Connection stale" warnings. |
| **V64** | **Gemini Lite Intelligence & PowerShell Policy Auto-Setup** | **Gemini Lite replaces Ollama** — New `POST /api/lite/ask` endpoint in `api_server.py` calls `_call_gemini_async()` with `GEMINI_DEFAULT_LITE = "gemini-2.5-flash-lite"` then falls back to `GEMINI_DEFAULT_FLASH = "gemini-3-flash-preview"` only on actual errors (never pre-emptively). **Usage tracking** — Calls logged to `_lite_usage.json` (calls_today, fallback_count, day); auto-reset at midnight. `/api/lite/stats` endpoint. **SupervisorState** — `ollama_online` → `lite_intelligence_online = True`; compatibility aliases in `to_dict()`. **UI rebrand** — Panel title "Ollama Local Intelligence" → "Gemini Lite Intelligence"; FAB icon → official Gemini star SVG; buttons always enabled; health dot "Local LLM" → "Lite AI" (always green); fetch URLs → `/api/lite/ask`. **`local_orchestrator.py` deprecated** — Marked as deprecated, no active callers. **PowerShell execution policy** — `Command Centre.bat` step 0 runs `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force` (no admin, idempotent). Required for Gemini CLI to spawn `.ps1` scripts on Windows. |
| **V65** | **Graceful Task Requeue on Shutdown + Gemini Lite Platform Context** | **Stop-requested failure guard** — V65 guard in `_pool_worker` (`main.py`) before `_diagnose_and_retry`: resets `node.status = "pending"`, clears `started_at`, calls `planner._save_state()` and returns — no retry, no quota burn. **Rate-limit retry skip** — 30s same-model-retry wait now checks `stop_requested` first; if set, requeues as pending and returns. **Audit loop early-exit** — Post-DAG audit `while` loop checks `stop_requested` at the top of every iteration; breaks immediately if set. **Audit cooldown sleeps** — Two 30s sleeps inside the audit loop now check `stop_requested` and `break` instead of sleeping. **Gemini Lite platform context injection** — `/api/lite/ask` in `api_server.py` now prepends a comprehensive ~4K-char `_PLATFORM_KNOWLEDGE` block to every user question: platform purpose, architecture, all UI tabs (Logs/Graph/Timeline/History/Health/Console/Stats/Files/Ports/Gemini Lite), key concepts (DAG, workers, auto-fix, audit loop, self-healing, quota, conflict protection, session persistence), all API endpoints, and live session state (goal, active model name, per-model quota %). Users can now ask Gemini Lite questions about the platform itself, not just project code. |
| **V66–V72** | **Worker Selector, Quota Pause Modes & Stats Dashboard** | **Concurrent worker selector (1–6)** — `W [1] [2] [3] [4] [5] [6]` toggle in main header bar; `set_workers(count)` in `DailyBudgetTracker` clamps 1–6, persists to disk; `get_effective_workers()` always returns user-selected count (no auto-throttle). Replaces old boost mode. **Dynamic semaphore resizing** — When worker count changes mid-DAG, semaphore `_value` adjusted by delta: +N releases extra slots immediately, -N clamps value so in-progress tasks complete naturally; logged as `🔧 [Pool] Worker count changed: N → M`. **Quota pause modes** — Three-state toggle (off/pro/all): `off` = auto-wait; `pro` = pause on pro-bucket exhaustion only; `all` = pause when all failover models exhausted. UI label "QP" → "Quota Pause". **Probe-based resume timers** — All 4 quota pause sleep gates (worker, scheduler, audit, `pause_for_quota`) use `_quota_resume_at` from live 429/probe data instead of midnight PT; sleep in 30s stop-aware chunks. **Fallback model completeness** — All models in the failover chain included in quota pause mode checks. **Stats dashboard redesign** — `loadStats()` rewritten as CSS grid widget dashboard with 6 named areas: Overview KPIs (full width), DAG Breakdown + System (side by side), Models & Quota + Budget & Workers (side by side), Phase Progress (full width). Widget cards with subtle shadows, 14px rounded corners, hover lift effects, responsive 720px breakpoint. **Unlimited audit loop** — Removed `_MAX_AUDIT_CYCLES = 3` hard cap; audit loop now runs `while True` until it naturally terminates (audit finds 0 issues, user stops, all tasks in a cycle fail, or max scan failures). Large projects get as many audit passes as needed. |
| **V73** | **Centralized Version, Verified Quota Resume & Post-Call Probes** | **Centralized version** — `config.py` defines `SUPERVISOR_VERSION`, `SUPERVISOR_VERSION_LABEL`, `SUPERVISOR_VERSION_FULL` as single source of truth; all UI/logs auto-update. **Verified quota resume** — `verified_resume_from_quota()` runs `/stats` probe before resuming; re-sleeps if still exhausted. **Post-call quota probe** — `_post_call_probe()` runs `/stats` in background after every successful Gemini call. **Periodic `/stats` probe** — 60s background asyncio task. **Total scheduling silence** — Zero log noise when `quota_paused`. **Default pro mode** — `quota_pause_mode` default `pro`. **Final completion audit gate** — Multi-step verification with false-completion prevention. **Boot stop gates** — 7 stop-awareness checkpoints; <1s exit. **Stop-cancelled calls skip cooldowns**. **Exclusive `/stats`-only quota** — removed speculation. **Early PTY probe reuse**. **Image model exclusion**. **Pro model enforcement for audits** — 7 call sites. **Activity-aware timeouts** — deadline resets on data. **Fresh audit preserves completed tasks**. **Replan budget increase & soft-stop**. |
| **V74** | **Modular Architecture, Pro-Only Coding & Production Hardening** | **Modular refactor** — 5 new dedicated modules (`a2a_protocol.py`, `dev_server_manager.py`, `health_diagnostics.py`, `task_intelligence.py`, `environment_setup.py`) extracted from monolithic files. **Pro-only coding enforcement** — `PRO_ONLY_CODING = True`: all coding/planning tasks exclusively use Pro models; system pauses on Pro quota exhaustion instead of falling back. **Hot-reload config** — `config.reload()` reads `.ag-supervisor/config_overrides.json` with 12 whitelisted keys; runtime tuning without restart. **Per-IP rate limiting** — 60 req/min per client IP on all API endpoints; HTTP 429 on exceed. **Session log persistence** — log entries auto-persisted to `session_log.jsonl` with 5MB rotation. **UI preferences persistence** — execution mode, quota pause, worker count saved to `ui_prefs.json`; survive restarts. **Adaptive WS debounce** — 200ms during active execution, standard during idle. **Cross-session council memory** — Swarm Debate on LOW confidence OR 3+ consecutive failures; success/failure recorded to persistent memory; Reviewer agent validates Fixer output. **Removed "null" CORS origin** — blocked `file://` page security risk. **Dead code cleanup** — 9 unused imports removed across 5 modules, verified via AST analysis with zero regressions (215 tests). **serve_index encoding fix** — `read_text(encoding="utf-8", errors="replace")` prevents `UnicodeDecodeError` crash when `index.html` has non-UTF-8 bytes. **Quota exhaustion requeue** — pool worker returns tasks to `pending` instead of sleeping+proceeding when all models exhaust; prevents cascading retry failures. **Smart PTY probe suppression** — periodic `/stats` probe sleeps until 5min before known quota reset during long cooldowns instead of probing every 60s. **Event-driven quota probing** — `/stats` fires on every CLI call (`record_usage`), failure (`report_failure`), and quota exhaustion (`report_quota_exhausted`), gated by `_pty_ready`; dashboard data always fresh. **Test-log compression** — `_compress_errors_for_retry()` extracts error name, file:line refs, and 5 lines of stack context per error (1500 char cap); focuses retry prompts on exact errors. **Architectural drift detection** — coherence gate scans for duplicate exports across files; injects priority-85 consolidation task. **Shared-file impact check** — immediate `tsc --noEmit` after worker modifies shared dirs (`lib/`, `utils/`, `types/`, etc.); injects priority-95 repair task on failure. |

---

> **Total**: 35+ Python modules, ~28,000 lines of code, 14 scheduled jobs, Fast Council (single-pass), 74 version iterations.
>
> The Supervisor AI plans, builds, tests, secures, deploys, monitors, heals, listens to users, and polishes until perfect — autonomously.
>
> **External API Dependencies**:
> - **Gemini CLI** (v0.29.0): Invoked via `--model` flag + stdin piping. Probe list (Gemini 3+ only): `gemini-3.1-pro-preview` → `auto` → `gemini-3.1-flash-lite-preview` → `gemini-3-flash`. AI Ultra: 120 RPM, 2000/day. Worker count user-selectable 1–6 via UI header toggle.
> - **Docker**: Sandbox containers built from `Dockerfile.sandbox` (Debian Bookworm + Python 3.12 + Node 20). Auto-installed by `Command Centre.bat` via `winget`; Python code raises `DockerNotAvailableError` with install commands if missing.
> - **Gemini Lite Intelligence** (`api_server.py`): V64 replacement for Ollama. `POST /api/lite/ask` calls Gemini CLI with `gemini-2.5-flash-lite` (lite bucket, ~5000 RPD) → fallback to `gemini-3-flash-preview` on 429. **V65 context injection**: every request prepends a ~4K-char `_PLATFORM_KNOWLEDGE` block covering platform architecture, all UI tabs, key concepts, API endpoints, and live session state (goal, model, quota snapshot). Users can ask about the platform or their project code. Usage tracked in `_lite_usage.json`. `GET /api/lite/stats` for monitoring. **No local install required** — uses existing authenticated Gemini CLI session.
>
> **Observability**:
> - **Glass Brain**: Color-coded ANSI console output (Cyan=prompt, Yellow=response, Red=error, Magenta=council, Green=success)
> - **Structured Logging**: `supervisor.log` with millisecond timestamps, module source, level — FileHandler guaranteed active for both launcher and direct entry paths
> - **Container Health**: Docker health checks and exec status monitoring
> - **Tasks Tab**: Live DAG progress with node status, dependencies, and blocked indicators
> - **Files Changed**: Per-task actual filenames logged and displayed in UI (no truncation)
> - **npm Install Log**: `_log_npm_output()` streams per-package install progress, add/update counts, warnings, and errors to the Glass Brain in real time
