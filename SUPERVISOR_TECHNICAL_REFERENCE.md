# Supervisor AI — Complete Technical Reference

> **Version**: V35 (Host Intelligence + Command Centre — Docker + Gemini CLI + Ollama + FastAPI)
> **Codebase**: `supervisor/` package — 35+ Python modules, ~18,000 lines
> **Runtime**: Python 3.11+ with Docker, asyncio, aiohttp, SQLite, Ollama
> **Purpose**: Fully autonomous AI pipeline that plans, builds, tests, secures, deploys, monitors, heals, grows, optimizes, listens to users, and polishes until perfect.
>
> **What it does for the user**: Takes a single high-level goal (e.g., "build a landing page" or "fix the login bug") and autonomously drives Gemini CLI on the host to completion — orchestrating tasks, managing Docker sandbox executions, recovering from crashes, switching models on rate limits, and re-executing when the agent stalls. The user can walk away while the supervisor delivers working software.
>
> **What it can do to itself**: When the supervisor encounters a bug in its own code, it reads ALL of its source files, sends them to Gemini with the crash traceback, receives a fix, validates it with `py_compile` and a shadow sandbox, applies it, and reboots — all without human intervention. It has rewritten itself across 35+ versions.

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
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │ Deploy  │  │Telemetry│  │ Growth  │  │ FinOps  │            │
│  │ Engine  │  │Ingester │  │ Engine  │  │ Engine  │            │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │
│                                                                   │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │  User   │  │ Polish  │  │  Local  │  │Sandbox  │            │
│  │Research │  │ Engine  │  │ Ollama  │  │Manager  │            │
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

### Execution Flow (V34 — Host Intelligence, Sandboxed Hands)

1. **Bootstrap**: `main.py` ensures Docker is installed, builds sandbox image
2. **Sandbox Creation**: Creates ephemeral container with workspace mounted (bind or copy mode)
3. **Context Bridge**: Host reads sandbox state via `docker exec` (git status, file listing, project state)
4. **Task Execution**: `HeadlessExecutor` runs Gemini CLI **on the host** via `subprocess`, using the user's authenticated AI Ultra session
5. **Action Bridge**: Gemini output parsed on host → file changes applied via `docker cp`/`write_file`, commands via `docker exec`
6. **Monitoring**: Watches container health, task progress, Gemini output
7. **Verification**: TDD, Visual QA (sandbox headless Chrome), and Compliance gates
8. **Deployment**: Ships to staging/production with health checks
9. **Recovery**: Sandbox restart → mount mode switch → image rebuild → self-evolution

---

## 2. Core Infrastructure

### 2.1 `main.py` — The Orchestrator (~550 lines)

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
1. **RESTART_SANDBOX** — Destroy and recreate the Docker container
2. **SWITCH_MOUNT** — Toggle between bind and copy mount modes
3. **REBUILD_IMAGE** — Force rebuild the Docker sandbox image
4. **EVOLVE** — Self-evolution: rewrite own source code to fix the bug

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

#### Systems Implemented in main.py

| System | Purpose |
|--------|---------|
| Lockfile Memory | Prevents re-injection after restart (anti-amnesia) |
| Session Persistence | Auto-resume from last goal on restart |
| Sandbox Lifecycle | Create, mount, execute, monitor, cleanup Docker containers |
| Headless Monitoring | Structured context gathering via HeadlessExecutor |
| Auto-Recovery | 4-tier sandbox-aware recovery escalation |
| Interactive Goal | CLI goal selection with history |

---

### 2.2 `config.py` — Central Configuration (~340 lines)

Single source of truth for all constants, thresholds, and mandates.

#### Configuration Sections

| Section | Key Constants |
|---------|---------------|
| **File Paths** | `CONFIG_FILE_PATH`, `AG_COMMANDS_PATH` |
| **Workspace** | `set_project_path()`, `get_project_path()`, `get_state_dir()` |
| **Mandates** | `ULTIMATE_MANDATE`, `TINY_INJECT_STRING`, `MANDATE_FILENAME` |
| **Lockfile** | `LOCKFILE_NAME = ".supervisor_lock"` |
| **ANSI Colors** | `ANSI_GREEN`, `ANSI_RED`, `ANSI_CYAN`, `ANSI_MAGENTA`, `ANSI_YELLOW`, `ANSI_BOLD`, `ANSI_RESET` |
| **Gemini CLI** | `GEMINI_CLI_CMD`, `GEMINI_TIMEOUT_SECONDS = 180`, `GEMINI_FALLBACK_MODEL`, `GEMINI_MODEL_PROBE_LIST` |
| **Retry Policy** | `GEMINI_RETRY_MAX_ATTEMPTS = 3`, `GEMINI_COOLDOWN_DELAYS` |
| **Context Budget** | `CONTEXT_BUDGET_WARN_CHARS`, `CONTEXT_BUDGET_MAX_CHARS` |
| **Rate Limits** | `RATE_LIMIT_DEFAULT_WAIT_S = 60`, `RATE_LIMIT_MAX_WAIT_S = 300` |
| **Self-Healing** | `SELF_IMPROVEMENT_INTERVAL_S = 3600` |
| **Docker/Sandbox** | `SANDBOX_IMAGE_NAME`, `SANDBOX_MOUNT_MODE`, `SANDBOX_WORKSPACE_DIR` |
| **Ollama** | `OLLAMA_HOST`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT` |
| **Parallel** | `MAX_CONCURRENT_WORKERS = 2` |

---

## 3. Sandbox Execution Layer

**V34** replaced the entire Playwright/CDP/DOM automation layer (15 modules, ~7,500 lines) with a Docker sandbox architecture (4 new modules, ~1,920 lines).

> **Security**: GEMINI_API_KEY and GOOGLE_API_KEY are **NEVER** passed into the container. The container runs as a non-root `sandbox` user. No AI tools are installed inside the container.

### 3.1 `sandbox_manager.py` — Docker Lifecycle Manager (~650 lines)

Manages the entire Docker container lifecycle. If Docker is not installed, raises `DockerNotAvailableError` with actionable install instructions for the user.

#### Docker Verification

`verify_docker()` checks:
1. Docker CLI on PATH — if missing, raises error with install commands (Windows/macOS/Linux)
2. Docker daemon responsive — if not running, tells user to start Docker Desktop manually

No silent installations. No UAC/sudo hangs.

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
| `execute_task(task, sandbox)` | Primary execution pipeline |
| `gather_context(sandbox)` | Context Bridge — reads sandbox state via docker exec |
| `_run_gemini_on_host(prompt, timeout)` | Runs Gemini CLI on HOST via subprocess |
| `_gather_sandbox_context_for_prompt()` | Context Bridge — feeds sandbox state into prompt |
| `_execute_as_shell(cmd, timeout)` | Action Bridge — runs shell commands in sandbox |

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
| `ask_gemini(prompt, timeout, use_cache, max_retries)` | Core async call with retry, failover, and budget tracking |
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

### 4.2 `retry_policy.py` — Production-Grade Retry Systems (~915 lines)

#### `RetryPolicy` — Exponential Backoff with Jitter

#### `ModelFailoverChain` — Cooldown-Based Model Fallback

Model chain: `gemini-3.1-pro-preview → auto → gemini-3-pro-preview → gemini-2.5-pro → gemini-2.5-flash → gemini-2.0-flash`

| Method | Description |
|--------|-------------|
| `get_active_model()` | Returns best available model (respects cooldowns) |
| `report_failure(model)` | Applies escalating cooldown: 1m → 5m → 25m → 1h |
| `report_timeout(model)` | Short 30s cooldown (timeouts are transient) |
| `seconds_until_any_available()` | Returns seconds until soonest model available |

#### `ContextBudget` — Token Consumption Tracking

#### `RateLimitTracker` — Smart Quota Intelligence

Extracts exact cooldown from quota errors (e.g. `"17m50s"` → 1070 seconds).

### 4.3 `local_orchestrator.py` — Local LLM Manager (~448 lines)

Interfaces with Ollama for local inference. `OllamaUnavailable` exception (non-fatal — callers degrade gracefully).

| Method | Description |
|--------|-------------|
| `ask_local_model(system_prompt, user_prompt)` | Local LLM with JSON output, 120s timeout |
| `health_check()` | Pings `/api/ps` to verify Ollama |
| `synthesize_followup(chat_history, system_goal)` | Context-aware follow-up generation |

### 4.4 `external_researcher.py` — Web Research Agent (12,508 bytes)

Performs web searches and summarizes results when external data is needed.

---

## 5. Multi-Agent Council

### 5.1 `agent_council.py` — The Million Dollar Team (~1,330 lines)

Orchestrates 6 specialist Gemini agents. **V34**: `page`/`context` parameters are now optional (headless mode), screenshot capture is guarded with `hasattr` checks.

#### Agent Personas

| Agent | Role | Expertise |
|-------|------|-----------|
| **Diagnostician** | Systems analyst | Debugging Docker, sandbox, async code |
| **Architect** | Systems designer | Robust, extensible solutions |
| **Debugger** | Reverse engineer | Python async, crash analysis |
| **Fixer** | Senior developer | Production-grade async Python |
| **Auditor** | Quality arbiter | PASS/FAIL/NEEDS_WORK verdicts |
| **Synthesizer** | Supreme judge | Resolves conflicting analyses |

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

### 7.1 `temporal_planner.py` — DAG Planner (20,235 bytes)

Converts goals into executable DAGs (Directed Acyclic Graphs).

### 7.2 `workspace_transaction.py` — Atomic Workspace Operations (9,115 bytes)

Transactional workspace changes with git-based rollback.

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

#### Pipeline
1. Install puppeteer-core in sandbox
2. Boot dev server in sandbox
3. Capture screenshot via headless Chrome inside container
4. Compress to 720p
5. Route to Gemini vision model for analysis
6. Parse JSON verdict (PASS|FAIL with critique)
7. Tear down dev server

### 8.3 `merge_arbiter.py` — Conflict Resolution (10,133 bytes)

Resolves conflicting changes from parallel agents.

### 8.4 `reflection_engine.py` — Post-Action Reflection (3,691 bytes)

Reflects on actions and feeds insights into the knowledge base.

---

## 9. Security & Compliance

### 9.1 `compliance_gateway.py` — Compliance Gateway (17,433 bytes)

Security gate: Secret Scanner, Dependency Audit, License Compliance, OWASP Top 10.

### 9.2 `git_manager.py` — Git Operations (8,072 bytes)

Safe git operations with rollback support.

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

## 12. Growth & Optimization

### 12.1 `growth_engine.py` — Growth Engine (21,402 bytes)

A/B experiments with two-proportion Z-test statistical validation.

### 12.2 `finops_engine.py` — FinOps Engine (18,618 bytes)

Cost-aware optimization. Profiles compute costs per transaction.

---

## 13. Scheduler & Cron System

### 13.1 `scheduler.py` — Autonomous Task Scheduler (883 lines, 36,450 bytes)

18 registered background jobs from 10s to 2h intervals. State persisted to `_cron_jobs.json`.

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

### 16.1 `api_server.py` — API Bridge (~280 lines)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ws` | WebSocket | Glass Brain telemetry stream (logs, state, events) |
| `/api/state` | GET | Current status, model, container, uptime, queue |
| `/api/logs` | GET | Recent log entries from ring buffer |
| `/api/instruct` | POST | Push instruction onto queue |
| `/api/preview` | GET | Live preview URL and status |
| `/api/queue` | GET | Instruction queue state + history |
| `/` | GET | Serves the Command Centre dashboard |

`SupervisorState` singleton shared between engine and API. `WebSocketLogHandler` pipes Python logging to the Glass Brain.

### 16.2 `instruction_queue.py` — Instruction Queue (~100 lines)

Async queue for user commands. UI pushes via API, engine drains at top of monitoring loop.

### 16.3 `ui/index.html` — Dashboard (~530 lines)

Single HTML file (no build step). Dark glassmorphism design with:
- **Glass Brain** — scrolling terminal, WebSocket-fed
- **Status Bar** — goal, model, container, uptime, Ollama
- **Instruction Input** — text box → `POST /api/instruct`
- **Live Preview** — iframe, auto-refreshing on file changes
- **Agent Council** — 6 agent status indicators
- **Connection** — WebSocket reconnect with exponential backoff

---

## 17. Configuration Reference

### `_best_model_cache.json`

Cached best Gemini model from auto-discovery:

```json
{
  "model": "gemini-2.5-pro",
  "probed_at": "2026-02-20T10:00:00Z"
}
```

---

## 17. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_CLI_CMD` | `gemini` | Gemini CLI binary name (**runs on host only**) |
| `GEMINI_TIMEOUT` | `180` | Gemini CLI timeout in seconds |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint (passed to sandbox) |
| `OLLAMA_MODEL` | `llama3` | Ollama model for local analysis |
| `SANDBOX_MOUNT_MODE` | `copy` | Workspace mount mode (**`copy`** default, `bind` opt-in) |
| `SANDBOX_IMAGE_NAME` | `supervisor-sandbox` | Docker image name |
| `COMMAND_CENTRE_PORT` | `8420` | V35 Command Centre web UI port |
| `DEPLOY_PROVIDER` | `vercel` | Deployment provider (vercel/railway) |
| `DEPLOY_TOKEN` | — | Deployment authentication token |
| `PROJECT_CWD` | `.` | Project working directory |
| `TEST_COMMAND` | `python -m tests.mock_repo_tests` | Test command for verification |
| `DEV_SERVER_CMD` | `npm run dev` | Dev server command |
| `DEV_SERVER_PORT` | `3000` | Dev server port |
| `SUPERVISOR_LOG_LEVEL` | `INFO` | Logging level |

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
├── gemini_advisor.py        # Centralized Gemini CLI (~1,112 lines)
├── retry_policy.py          # Retry + failover + budget (~915 lines)
├── local_orchestrator.py    # Local Ollama LLM manager (~448 lines)
├── external_researcher.py   # Web research agent
│
├── # Multi-Agent Council
├── agent_council.py         # The Million Dollar Team (~1,330 lines)
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
├── visual_qa_engine.py      # V24 Visual QA (sandbox headless Chrome)
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
├── # Growth & Optimization
├── growth_engine.py         # Growth engine
├── finops_engine.py         # FinOps engine
│
├── # Self-Evolution
├── self_evolver.py          # Self-modification engine
├── loop_detector.py         # Anti-loop protection
│
├── # Scheduling
├── scheduler.py             # Cron scheduler (883 lines, 18 jobs)
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
├── _failover_state.json     # Model failover state
├── _session_state.json      # Session memory
├── workspace_map.json       # Workspace index
├── supervisor.log           # Runtime log file
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
| V13 | — | Agent Council (6 specialist agents) |
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
| **V34** | **Headless Sandbox** | **Complete architecture migration**: Playwright/CDP/DOM (15 modules, ~7,500 lines) replaced with Docker sandbox + Gemini CLI + Ollama dual-brain (4 modules, ~1,920 lines). 550-line headless orchestrator. Auto-install Docker. Smart workspace mounting (bind/copy). Sandbox recovery strategies. Visual QA via container headless Chrome. |

---

> **Total**: 35+ Python modules, ~18,000 lines of code, 18 scheduled jobs, 6 specialist agents, 34+ version iterations.
>
> The Supervisor AI plans, builds, tests, secures, deploys, monitors, heals, grows, optimizes for profit, listens to users, and polishes until perfect — autonomously.
>
> **External API Dependencies**:
> - **Gemini CLI** (v0.29.0): Invoked via `--model` flag + stdin piping. Probe list: `gemini-3.1-pro-preview` → `auto` → `gemini-3-pro-preview` → `gemini-2.5-pro` → `gemini-2.5-flash` → `gemini-2.0-flash`.
> - **Docker**: Sandbox containers built from `Dockerfile.sandbox` (Debian Bookworm + Python 3.12 + Node 20). Auto-installed on first run.
> - **Ollama HTTP API**: Local LLM on `localhost:11434`. Uses `/api/chat` with JSON output, 120s timeout. Containers access via `host.docker.internal`.
>
> **Observability**:
> - **Glass Brain**: Color-coded ANSI console output (Cyan=prompt, Yellow=response, Red=error, Magenta=council, Green=success)
> - **Structured Logging**: `supervisor.log` with millisecond timestamps, module source, level
> - **Container Health**: Docker health checks and exec status monitoring
