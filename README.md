# Supervisor AI — Antigravity IDE Orchestrator

A fully autonomous Python system that takes a goal and delivers working software.
Tell it what to build, and it plans, codes, tests, and deploys — hands-free.

> **Architecture**: "Host Intelligence, Sandboxed Hands" -- Gemini CLI runs on
> the host using your authenticated AI Ultra session. Docker sandbox is a dumb
> terminal with zero credentials. The V74 Command Centre UI is available at
> `http://localhost:8420`.


---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| **Python** | 3.11+ | Auto-installed by `Command Centre.bat` via `winget` if missing |
| **Docker** | Latest | Auto-installed by `Command Centre.bat` via `winget` if missing (requires restart) |
| **Gemini CLI** | latest | Auto-installed by `Command Centre.bat` via `npm install -g` if missing |
| **Ollama** (optional) | latest | **V64: No longer required** — replaced by Gemini Lite Intelligence. Kept for legacy support only |

> **Note**: `Command Centre.bat` automatically sets the PowerShell execution policy to `RemoteSigned` for the current user (no admin needed). This is required for the Gemini CLI to run scripts properly.

---

## 1 — Install dependencies

```bash
cd "c:\Users\mokde\Desktop\Experiments\Antigravity Auto"
pip install -r requirements.txt
```

Docker must be installed before running the supervisor directly via `python -m supervisor`. If not found, you'll get
clear install instructions for your platform. If using `Command Centre.bat`, Docker is auto-installed via `winget`.

---

## 2 — Run the Supervisor

```bash
python -m supervisor --goal "Build a beautiful landing page with animations"
```

### CLI options

| Flag | Description |
|---|---|
| `--goal` / `-g` | **(required)** Your ultimate goal |
| `--project` / `-p` | Path to the project directory |
| `--dry-run` | Skip sandbox creation; print what would happen |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default: `INFO`) |

### Example commands

```bash
# Simple goal
python -m supervisor -g "Create a React dashboard with authentication"

# With project path
python -m supervisor -g "Fix all failing tests" -p ./my-project

# Verbose logging
python -m supervisor -g "Fix all failing tests" --log-level DEBUG

# Dry-run (no Docker needed)
python -m supervisor -g "Test the supervisor" --dry-run
```

### Background mode (immortal daemon)

```bash
# Windows — Command Centre launcher with auto-restart and auto-install
# Auto-installs Python, Node.js, Gemini CLI if missing; starts Docker
"Command Centre.bat"
```

---

## 3 — How it works

```
┌──────────────────────────────────────────────────┐
│                 SUPERVISOR AI                     │
│                                                   │
│   ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│   │ Sandbox  │  │ Headless │  │    Agent     │  │
│   │ Manager  │→ │ Executor │  │   Council    │  │
│   └────┬─────┘  └──────────┘  └──────┬───────┘  │
│        │                              │           │
│        ▼                              ▼           │
│   ┌──────────┐              ┌──────────────┐     │
│   │  Tool    │  ◄──────────  │    Brain     │     │
│   │  Server  │              │ (Gemini CLI) │     │
│   └──────────┘              └──────────────┘     │
└──────────────────────────────────────────────────┘
         │                         ▲
         ▼                         │
    ┌─────────────────────────────────┐
    │       Docker Sandbox            │
    │  (Debian + Python + Node.js)    │
    └─────────────────────────────────┘
```

1. **Sandbox Manager** — Creates Docker containers, mounts workspace, syncs files HOST→sandbox, manages lifecycle
2. **Headless Executor** — Runs Gemini CLI **on the host** (AI Ultra session), pushes changes into sandbox
3. **Tool Server** — File ops, shell, git, LSP, dev server — all via docker exec bridges
4. **Agent Council** — Fast Council (single-pass Diagnostician+Fixer+Auditor) with Swarm Debate escalation on low-confidence diagnoses
5. **Path Translator** — Bidirectional host↔container path mapping
6. **Auto-Fix Engine** — Retries with enriched error context sent directly to Gemini (Ollama removed — context too large for local models)
7. **Auto-Preview** — Detects buildable projects, starts dev server automatically, auto-resyncs on every file change
8. **DAG Planner** — Context-aware decomposition: reads existing project state, creates only tasks for what's missing/broken

### Recovery strategies

If something fails, the supervisor escalates through:

1. **Auto-Fix** — Retry with enriched Gemini context (once, direct — no Ollama hop)
2. **Replan** — DAG replanning: rewrite pending tasks to route around the failure
3. **Restart Sandbox** — Destroy and recreate the container
4. **Switch Mount** — Toggle between `bind` and `copy` mount modes
5. **Rebuild Image** — Force rebuild the Docker image
6. **Self-Evolution** — Rewrite its own code to fix the bug

---

## 4 — Configuration

All tunables live in `supervisor/config.py`. Key environment variables:

| Env Var | Default | Description |
|---|---|---|
| `GEMINI_CLI_MODEL` | `auto` | Model for Gemini CLI — `auto` routes to best Gemini 3+ model |
| `MAX_CONCURRENT_WORKERS` | `3` | Default parallel DAG lanes (user-selectable 1–6 via header toggle) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint (passed to sandbox) |
| `OLLAMA_MODEL` | `llama3.2:3b` | Local LLM for fast task routing (pre-warmed at boot, `keep_alive=5m`) |
| `SANDBOX_MOUNT_MODE` | `copy` | `copy` (isolated, **default**) or `bind` (fast, opt-in) |
| `COMMAND_CENTRE_PORT` | `8420` | Command Centre web UI port |
| `SUPERVISOR_EXPERIMENTS_DIR` | `~/Desktop/Experiments` | Project discovery directory for Command Centre |
| `SUPERVISOR_LOG_LEVEL` | `INFO` | Logging level |

---

## 5 — Glass Brain Log Taxonomy (V36)

The Command Centre Glass Brain shows granular phase-by-phase logs. Each log entry is prefixed with a tag indicating the subsystem:

| Prefix | Subsystem | Example |
|--------|-----------|---------|
| `[Boot]` | Engine startup phases | `🔧 [Boot] Initializing ToolServer + HeadlessExecutor …` |
| `[Gemini]` | Gemini CLI lifecycle | `🚀 [Gemini] Launching CLI on HOST: model=auto, cwd=…` |
| `[Context]` | Sandbox context bridge | `📡 [Context] Sandbox state collected (4520 chars)` |
| `[Sandbox]` | Docker container ops | `📦 [Sandbox] Files copied. Fixing ownership …` |
| `[Ollama]` | Local LLM operations | `🧠 [Ollama] Model pre-warmed into VRAM: llava:latest` |
| `[Task]` | Task classification | `🧠 [Task] Classifying task complexity via Ollama …` |
| `[Auto-Preview]` | Preview detection | `🖥️ [Auto-Preview] Buildable project detected — starting dev server …` |
| `[Dev Server]` | Dev server lifecycle | `🖥️ [Dev Server] Running on port 3000 (attempt 1)` |
| `[Sync]` | File sync | `📦 [Sync] Files synced to sandbox` |
| `📄` | File changes | `📄 Files changed (7): index.html, main.js, ...` |
| `⏰` | Scheduler events | `⏰ Loaded 18 scheduled jobs from disk` |
| `📬` | User instructions | `📬 Instruction queued: '...' (source=launcher)` |
| `✅` | Success confirmations | `✅ [Gemini] Task completed successfully.` |
| `⏱️` | Timeout warnings | `⏱️ [Gemini] TIMEOUT: No response after 300s` |

### Gemini CLI Execution Phases

During each Gemini CLI run, the Glass Brain shows:

1. `🔍 [1/4] Probing npm global directories …` — discovery phase
2. `🔍 Gemini CLI found: C:\...\gemini.cmd` — resolved binary
3. `📡 [Context] Gathering sandbox workspace state …` — context bridge
4. `📡 [Context] Sandbox state collected (N chars)` — context ready
5. `🚀 [Gemini] Launching CLI on HOST: model=auto …` — subprocess start
6. `🚀 [Gemini] Subprocess started (PID=xxxxx). Waiting …` — in-flight
7. `🚀 [Gemini] Response received: 45.2s, exit=0, stdout=8432 bytes` — result
8. `✅ [Gemini] Task completed successfully.` — done

---

## 6 — Troubleshooting

| Problem | Solution |
|---|---|
| "Docker not found" | Install Docker Desktop manually, restart terminal |
| "Docker daemon not running" | Start Docker Desktop manually |
| Sandbox won't start | Run `docker ps -a` and `docker logs` |
| Gemini CLI not found | Auto-discovery probes npm global dir, PATH, and npx. Check `npm ls -g @google/gemini-cli` |
| Gemini CLI timeout | Increase `GEMINI_TIMEOUT` env var |
| "Ollama unavailable" | Install Ollama or supervisor degrades gracefully |
| Ollama slow on first query | `warm_up()` pre-loads at boot with `keep_alive=5m` — if still slow, check GPU drivers |
| Rate limit errors | Supervisor auto-switches Gemini 3+ models with escalating cooldowns (30s→2m→10m→30m) |
| Daily budget warnings | `DailyBudgetTracker` warns at 80%/90%; quota pause handles exhaustion |
| Stale scheduled jobs | `cleanup_stale_jobs()` runs at boot — pruned automatically |
| EACCES in sandbox | `chown -R sandbox:sandbox /workspace` runs after `docker cp` |
| Tasks tab empty | DAG now preserved when tasks remain pending/failed |
| Dev server won't start | Falls back through: npm run dev → npm start → npx serve → python3 -m http.server |
| supervisor.log empty | FileHandler now attached at `run()` start (was missing in launcher path) |
| DAG always creates 15 tasks | V41: Dynamic — Gemini creates as many as needed (5–50+), no artificial cap |
| Audit finds nothing | V41: Gemini (1M+ context) replaces Ollama for audit, reads all changed files + project state |
| Audit makes blind fixes | V41: Audits ONLY create tasks with detailed descriptions; fixes execute through normal DAG pipeline |
| Preview stale after edits | V41: Auto-redeploy after every file-changing task, not just monitoring ticks |
| DAG history lost on clear | V41: `save_history()` appends to `dag_history.jsonl` + `PROJECT_STATE.md` before every clear |
| Workers queue behind each other | V41: True parallel — removed global `acquire_sandbox()`, workers run Gemini CLI simultaneously |
| Monitoring loop takes 76s | V41: Deterministic heuristics replaced Ollama `decide_action()` — action routing now ~0ms |

---

## 7 — Full Documentation

See [SUPERVISOR_TECHNICAL_REFERENCE.md](SUPERVISOR_TECHNICAL_REFERENCE.md) for the complete technical reference (35+ modules, ~28,000 lines, 74 version iterations).

### V37 Security Hardening

24 findings identified via line-by-line audit. 24/24 addressed:

| Category | Count | Example Fixes |
|----------|-------|---------------|
| **CRITICAL** | 3 | Removed silent installer, self-evolver allowlist, network isolation |
| **HIGH** | 7 | Async sleep, shell injection mitigation, JSON extraction O(n²)→O(n), API localhost-only |
| **MEDIUM** | 7 | Credential blocklist, TOCTOU port race, aiohttp session leak |
| **LOW** | 7 | `__all__`, path validation, `Optional` → PEP 604, public API for HUD |

### V38 Smart Execution

- **DAG decomposition** — Complex tasks auto-decomposed into atomic chunks with parallel execution
- **Smart routing** — Ollama classifies task complexity → routes to optimal model
- **Supervisor state machine** — Structured enum-based state transitions with history tracking
- **Adaptive timeouts** — Per-chunk timeouts based on Ollama classification

### V39 Self-Healing & Auto-Preview

- **Auto-fix on task failure** — Diagnose via Ollama → retry with enriched context (parallel lanes, sequential nodes, monitoring loop)
- **Auto-preview** — Detects buildable projects, starts dev server automatically at 4 trigger points
- **Dev server robustness** — `nohup` background, `npm install` gating, dynamic port, retry polling, python3 fallback
- **Log completeness** — FileHandler in `run()`, actual filenames logged, 2000-entry buffer, no truncation
- **Tasks tab persistence** — DAG nodes preserved when pending/failed tasks remain
- **File sync** — `sync_files_to_sandbox()` for copy mount mode

### V40 Autonomous Efficiency & Quota Intelligence

- **Gemini 3+ models only** — Removed all Gemini 2.x models for better accuracy
- **Continuous worker pool** — Replaced batch `gather()` with semaphore-limited pool; idle workers instantly pick up next unblocked DAG node
- **AI Ultra quota optimization** — `DailyBudgetTracker` tracks requests against 2000/day limit (80% target at 24h continuous); warns at 80%/90%
- **Live DAG progress** — `_update_dag_progress()` is async and broadcasts to UI at 8 lifecycle points; activity + file change recording for all chunk events
- **Post-DAG audit** — `_audit_completed_work()` runs after DAG completion: scans code against original goal, creates deduplicated DAG tasks for all issues found
- **Proactive idle audit** — When idle ~5min, runs full project audit (missing imports, broken paths, dead code); max 10/day
- **Stop button UX** — "Stopping… Please wait" → "✅ Ready to Close" with safe-to-close tooltip
- **Shorter cooldowns** — 30s→2m→10m→30m (was 1m→5m→25m→1h); faster recovery for AI Ultra's generous quotas
- **Safe stop drain** — `stop_requested` now waits for all active workers to finish before exiting (no more orphaned tasks)
- **Council collapse** — Diagnostician+Fixer+Auditor merged into single Fast Council prompt; Swarm Debate (Debugger+Architect) only fires on LOW confidence
- **Visual QA gating** — per-node VQA removed; reserved as final pass before deployment only
- **Growth/FinOps deletion** — `growth_engine.py` (534 lines) and `finops_engine.py` (439 lines) deleted; scheduler wiring removed
- **Timeline milestones** — 8 new `record_activity()` events: decomposition, worker start, auto-fix, replan, safe stop

### V41 Context-Aware Intelligence & True Parallelism

- **Dynamic task count** — Removed hardcoded `Maximum 15 tasks per epic`; Gemini now creates as many tasks as the project needs (5–50+), with layered decomposition (core/engine → features → UI/polish)
- **Context-aware decomposition** — Gemini receives `PROJECT_STATE.md`, full file tree, and `dag_history.jsonl` during task planning; for existing projects, creates tasks only for what's MISSING, BROKEN, or needs IMPROVEMENT
- **Gemini-first for complex goals** — Goals >2000 chars skip Ollama entirely, go straight to Gemini CLI (1M+ context vs Ollama's 8K); `needs_gemini` forced `True` for complex tasks
- **Enriched audit prompt** — Post-DAG audit now includes: original goal, `PROJECT_STATE.md`, file tree, completed DAG tasks, DAG run history, and full source code of up to 20 changed files (8000 chars each)
- **Tasks-only audit** — Audits NEVER fix code directly; they ONLY create detailed DAG tasks (with exact files, functions, reasoning, and expected behavior) that execute through the normal worker pipeline
- **Uncapped audit tasks** — Removed `Maximum 5 tasks` limit and `scan_result[:5]` truncation; audit produces ALL needed follow-on tasks
- **DAG history persistence** — `save_history()` method appends all DAG nodes to `dag_history.jsonl` and `PROJECT_STATE.md` before every `clear_state()`; no task history is ever lost
- **Auto-redeploy on file changes** — `_auto_preview_check()` called after every successful task with file changes, not just at monitoring ticks; preview always reflects latest code
- **Boot planner poisoning fix** — Boot planner's `goal-init` node (marked complete) was poisoning `_execute_dag_recursive`, causing `is_epic_complete()=True` immediately and skipping all decomposition; now `state.planner=None` and `_boot_planner.clear_state()` before DAG entry
- **Full task descriptions in UI** — Removed 100-char truncation on Graph tab task descriptions; added `word-break: break-word` CSS
- **Live Graph tab updates** — `_update_dag_progress()` called immediately after `mark_complete` and `mark_failed` in `_pool_worker`; audit tasks also broadcast instantly
- **True parallel execution** — Removed global `acquire_sandbox()` mutex that serialized all workers into a queue; switched to Tier 2 per-file `acquire_files()` locking on post-execution state mutation only; Docker handles concurrent `docker cp`/`docker exec` to different paths natively; 3 workers now run Gemini CLI simultaneously
- **Deterministic monitoring loop** — Replaced Ollama `decide_action()` call (~76s per cycle) with instant deterministic heuristics; structured context (`ctx.diagnostics_errors`, `ctx.dev_server_running`, `planner.has_active_dag()`) routes to `fix_errors`/`start_server`/`resume_dag`/`execute_task`/`wait` in ~0ms
- **Deterministic chunk timeouts** — Replaced Ollama per-node `classify_task()` (~22s/call, always returned 3600s) with instant description-length heuristic; floor raised to 180s (Gemini needs time to read project context); tiers: <150 chars → 180s, <400 chars → 300s, else → full timeout
- **Expandable Graph task nodes** — Task descriptions in Tasks tab truncated to 2 lines by default; click any task row to expand/collapse full details; "▸ click to expand" hint for long descriptions
- **Preview port persistence** — Host port mapping saved to `.ag-supervisor/_preview_port.json` on every preview start; stale ports released on startup (cross-platform: netstat/taskkill on Windows, lsof/kill on Unix); port file cleared on shutdown
- **Activity-based Gemini timeout** — Replaced fixed `proc.communicate(timeout)` with incremental stdout reading; inactivity timeout scaled by complexity: 180s simple, 300s medium, 600s complex (matches CLI's internal 10-min API timeout); tasks that actively produce output can run up to 30 minutes; heartbeat log every 30s; timeout-with-file-changes promoted to partial success (skip retry); project-level `.gemini/settings.json` bootstrapped with `timeout: 600000`; dict→TaskResult conversion fixed for accurate error messages
- **AST context slicing** — Replaced raw 160K source dump (20 files × 8000 chars) in audit prompts with targeted function/class body extraction via `WorkspaceMap.extract_relevant_bodies()`; Python uses `ast.get_source_segment()`, JS uses brace-depth counting; 50K char budget with graceful fallback
- **Worker isolation via git checkpoints** — `_git_checkpoint()` commits baseline before DAG start; after each worker, `_validate_worker_files()` checks for syntax errors (Python via ast, JS via bracket balance); broken files reverted to checkpoint and task re-routed to `_diagnose_and_retry()` with validation errors as context
- **DAG History tab** — New "History" tab in UI renders all previous DAG runs from `dag_history.jsonl`; each run is a collapsible card showing timestamp, task count, and full task list with statuses; newest runs first; `/api/dag/history` endpoint
- **Shadow container isolation** — Tier 2 runtime-isolated validation: each worker's changed files validated inside an ephemeral 512MB Docker container (syntax + import + test checks) before merging to primary workspace; 15s per-command timeout prevents infinite loops; automatic fallback to Tier 1 host-side validation when Docker is unavailable
- **Goal persistence on directive update** — Auto-detects when `SUPERVISOR_MANDATE.md` is manually edited between sessions and reloads the updated goal; added 'E' key to edit goal in-place during session resume; session state re-saved with updated goal; **immediate persistence** from Launcher UI `/api/projects/launch` before engine startup
- **Docker auto-recovery prompt** — Intercepts `DockerNotAvailableError` connection timeouts on Windows and offers an interactive prompt to cleanly exterminate hung background processes (`wsl.exe`, `com.docker.*`) via `taskkill`, then automatically retries Docker initialization.
- **Docker WSL initialization** — added Windows-specific `sc.exe config wslservice start= demand` pre-flight command to ensure WSL service readiness before checking Docker daemon
- **TaskResult accessor robustness** -- added `_g()` dictionary/object accessor helper to `headless_executor.py` to seamlessly handle both dicts and `TaskResult` returns from Gemini CLI, eliminating `'TaskResult' object has no attribute 'get'` crashes in the auto-fix retry path; fixed misleading 'Task completed successfully' log on exit code false positives

### V43 Smart Quota Management

- **429 rate limit detection** -- Detects HTTP 429 rate limit errors and pauses all task execution until quota resets
- **Global quota pause** -- `pause_for_quota()` / `resume_from_quota()` in `DailyBudgetTracker`; workers sleep until midnight Pacific Time reset
- **Quota stats in UI** -- Top bar pill shows daily usage, remaining budget, and pause state
- **Failover chain cooldowns** -- Escalating cooldowns per model (30s, 2m, 10m, 30m) with `all_models_on_cooldown()` check before task launch

### V44 Manual Mode, Instruction Decomposition & Persistent State

- **Manual/Auto mode toggle** -- Dashboard toggle switches between autonomous execution and manual-prompt-only mode. Manual mode pauses after current tasks finish; auto resumes from where it left off. `/api/mode` endpoint
- **Pause-on-quota toggle** -- Dashboard toggle to pause (stop) instead of sleeping when all API rate limits are exhausted. Enters 5s poll loop with instruction draining. `/api/quota-pause` endpoint
- **Smart instruction decomposition** -- User prompts are sent to Gemini with current DAG state and project file tree; Gemini decides whether to break them into 2-8 atomic subtasks with inter-dependencies. Fallback to single task if Gemini fails. Wired into all 4 instruction handling points (pool loop, monitoring loop Path A/B, manual mode pause loop)
- **Bundled prompt decomposition** -- Multiple queued user instructions are bundled together before Gemini decomposition for more accurate subtask generation
- **Persistent DAG state** -- All completed/failed/skipped nodes preserved across DAG phases. `clear_state()` no longer deletes `epic_state.json`; archives pending/running nodes only. Graph tab shows full project history across sessions
- **Continuous task IDs** -- `_task_offset` counter persists in `epic_state.json`. LLM-generated IDs (t1, t2...) are remapped to continue the sequence (t30, t31...) across DAG phases. Dependencies remapped accordingly
- **User prompt persistence** -- All user prompts (goal + instructions) saved to `_user_prompts` list in `epic_state.json` (last 50). `record_prompt()` / `get_user_prompts()` API on planner
- **Session state saves after every task** -- `_save_session_state()` called after each completed task, not just at shutdown
- **Ports tab** -- New tab showing all localhost listening ports, which one is the project preview, and a button to close other ports. `/api/ports` and `/api/ports/kill` endpoints
- **Sidebar task animations** -- Running tasks in the sidebar pulse with a glow animation. Graph tab rows color-coded by status (green=complete, red=failed, yellow=running, gray=pending)
- **Preview auto-update on drain** -- Preview now updates even when draining (previously skipped). Preview URL dynamically reflects the current project port
- **User instructions as next task** -- In auto mode, submitted prompts are queued as the next task (highest priority=100), not appended to end of DAG
- **Instruction visibility in UI** -- User instructions immediately appear as DAG nodes in the Graph tab when submitted
- **Task pill click-to-navigate** -- Clicking a task pill in the sidebar switches to the Graph tab, smooth-scrolls to that task row, and flashes it with a golden highlight animation
- **2026 Visual Design Mandate** -- Comprehensive UI/UX quality mandate injected into every prompt via `ULTIMATE_MANDATE` and `DECOMPOSITION_PROMPT`. Covers HSL color systems, premium typography, 4px spacing grids, micro-interactions, glassmorphism, responsive mobile-first design, domain-appropriate aesthetics, icon consistency, and beautiful empty/error states. The acid test: if the user doesn't say 'wow', the design has failed

### V45–V54 Dependency Intelligence, Health Reporting & UI Polish

- **Comprehensive Session Complete report** — End-of-session screen: tasks, files, duration, errors, project path, log tail.
- **Telemetry sidebar live stats** — Changes / DAG Nodes / Errors / Cycles correctly read from `SupervisorState` on every WebSocket tick.
- **`CONSOLE_ISSUES.md`** — Written after every dev server start: auto-installs missing imports, records remaining errors, refreshes `BUILD_ISSUES.md`. Sent to CLI as a combined fix-task.
- **Missing import auto-install** — `_scan_dev_server_console()` detects unresolvable imports, runs `npm install --legacy-peer-deps`, restarts. Also runs post-task via `resolve_missing_imports()`.
- **npm install progress in log** — `_log_npm_output()` parses npm stdout to the Glass Brain live: `+ pkg@version`, `added N packages`, `⚠️` deprecation warnings. Replaces silent `| tail -20` truncation.
- **Major dep try-and-verify upgrade** — `_try_upgrade_major_deps()` runs after `build_health_check()`. Upgrades outdated deps, verifies with `tsc --noEmit`, reverts on failure.
- **Stop button redesign** — Red pill → `Shutting Down…` (amber) → `Session Saved` (green).
- **Stale volume cleanup log** — Logs X cleaned / Y still mounted; DEBUG-only when nothing removed.
- **Duplicate state-save fix** — Removed redundant `planner._save_state()` after `mark_complete()`.
- **Model Cooldown UI** — Cooldown timers shown in Launcher and Dashboard. Stale model cache invalidated at boot.
- **Activity Pill** — Animated header pill shows current supervisor operation; disappears when idle.

### V55 Comprehensive Local Self-Healing & Model Refresh (2026-03-06)

- **Gemini 3.x model roster** — All Gemini 3.x models in `config.py` with correct priority chain: `gemini-3.1-pro-preview` (Pro primary) → `gemini-3-pro-preview` (Pro fallback) → `auto` | `gemini-3.1-flash-lite` (Flash primary, Mar 2026) → `gemini-3-flash` (Flash fallback). `GEMINI_DEFAULT_FLASH = "gemini-3.1-flash-lite"`.
- **dev-server.log multi-pattern self-healer** — Runs every 60s while server is running. Four patterns, each with targeted fix + activity pill:
  - `Cannot find module` + `node_modules` → clean reinstall queued
  - `EADDRINUSE` / port in use → kill stale process on 3000/5173, restart
  - `new dependencies optimized` / `deps changed` / `504` → clear `.vite` cache + restart
  - Tailwind `theme()` / PostCSS `index.css` error → clear `.vite` cache + restart
- **Browser console local self-healer** — After every `_capture_console_errors()`, intercepts infrastructure errors before Gemini sees them:
  - `.vite/deps` 504 / `Outdated Optimize Dep` → clear `.vite` cache + restart
  - `Failed to fetch dynamically imported module` from `.vite` URL → same
  - `ERR_CONNECTION_REFUSED` / `ERR_EMPTY_RESPONSE` on localhost → restart server
  - Locally-healed errors stripped so Gemini never sees them
- **Sync exclusion hardening** — All three sync layers updated (`sync_files_to_sandbox`, `sync_changed_files`, `_SYNC_EXCLUDED`):
  - Added: `.vite`, `coverage`, `storybook-static`, `.expo`, `.svelte-kit`, `.parcel-cache`, `out`
  - Windows docker cp contamination window closed: all volatile dirs removed immediately post-copy

### V56 Audit Accuracy, Dedup & TypeScript Regression Guard (2026-03-07)

- **`--all_files` audit mode** — The post-DAG audit now launches Gemini CLI with `--all_files` (`-a`), which causes the CLI to load all non-gitignored project files into its context window natively via its own `read_many_files` tool — the same mechanism used during regular task execution. Previously the audit would answer in ~3s by pattern-matching on file paths; now it reads and reasons over actual code. `ask_gemini()` gains an `all_files: bool` parameter threaded through `_call_gemini_async()`. The audit call is also forced `use_cache=False` so results are never served from session cache.
- **Persistent audit fingerprint deduplication** — Completed audit task descriptions are stored as MD5 fingerprints in `.ag-supervisor/audit_done_fingerprints.json`. The fingerprint store is loaded at the start of each audit cycle. This prevents Gemini from re-injecting the same 14 tasks across every DAG cycle. A normalized MD5 hash (`_desc_fp`) is used so minor rephrasing doesn't bypass the check. Tasks with `failed` status are also included in the dedup set.
- **TypeScript Regression Guard** — New module `supervisor/ts_regression_guard.py`. Captures `npx tsc --noEmit` error state before and after every task via asyncio subprocess. Computes a diff (`file:line:TScode` fingerprints) to identify regressions (new errors not present in the pre-task state). Two layers:
  1. **Preventive** (free): A `TYPESCRIPT REGRESSION CONTRACT` block is injected into every Gemini task prompt listing which files are currently clean. Gemini is warned not to introduce errors.
  2. **Reactive** (quota-efficient): If regressions are detected after a task, the task is **accepted anyway** (no retry waste). Instead, a tiny targeted `{task_id}-tsfix` micro-fix DAG task is injected into the planner with the exact regression details. Typical micro-fix runtime: 30–90s vs 5+ min for a full retry.
- **Persistent TS baseline** — Successful post-task tsc state is persisted to `.ag-supervisor/ts_error_baseline.json`. The next task reads from this file instead of running a fresh tsc process, making the pre-capture essentially free after the first task.

### V57 Quality & Efficiency Suite (2026-03-07)

- **Self-review cascade prevention** — `needs_self_review` is never set if the task prompt is already a `[SELF-REVIEW]`, `[HEALTH]`, `[TSFIX]`, or lint task. A second guard in `pool_worker` also skips injection if the node's `task_id` has a meta suffix (`-review`, `-tsfix`, `-srvchk`, `-health`, `-lint`). Prevents the infinite cascade where a self-review touching 3+ files spawned a further self-review of itself.
- **Prompt de-nesting** — `self_review_context` strips nested `ORIGINAL TASK` chains before building the review prompt. Only the root task's first 400 chars are passed — not a recursive chain that bloated prompts and wasted tokens.
- **Gemini model roster cleanup** — Removed `gemini-3.1-pro-preview-customtools` (CLI rejects API variant suffixes) and `gemini-3-pro-preview` (shut down March 9, 2026) from probe lists. Corrected flash-lite name to `gemini-3.1-flash-lite-preview`. Updated `GEMINI_FALLBACK_MODEL` to `gemini-3.1-pro-preview`. Stale model cache auto-cleared on deploy. Active model chain: `gemini-3.1-pro-preview` → `auto` → `gemini-3.1-flash-lite-preview` → `gemini-3-flash`.
- **Queued task state** — Tasks submitted to the asyncio pool but waiting at the semaphore now show as **Queued** (blue row, ⏳ icon) instead of Pending in the right panel. `_update_dag_progress()` accepts a `queued_ids` set; the pool loop passes active-but-still-pending task IDs immediately after launching each batch.
- **Preview dot colour fix** — The dot next to the Reload button was toggling class `live`; the stylesheet only defines `.status-dot.online` (green) and `.status-dot.offline` (red). Fixed to use the correct class names.
- **Phase tab expand/collapse memory** — Phase expand/collapse state tracked per-phase in a `_userPhasePref` Map. Poll refreshes no longer override user's manual choices.


### V58 Five-Layer File Conflict Protection (2026-03-08)

Eliminated the race condition where parallel Gemini workers could overwrite each other's file changes (e.g. `architectures.ts` being clobbered from 30+ templates back to 15). Five independent, cumulative layers protect every write:

- **Layer 1 — File-claim registry** (`temporal_planner.py`): After a task writes file F, `register_file_writes()` records it in `_file_last_writer`. `inject_file_conflict_deps()` scans all _pending_ nodes whose descriptions mention F and injects the completed task as a dependency — automatically serialising them without any manual intervention.
- **Layer 2 — Current-file injection** (`headless_executor.py`): Before Gemini is invoked, `_inject_current_file_states()` scans the prompt for source file mentions (regex over `.ts/.tsx/.js/.py/.md/...`), reads the current on-disk content of each, and prepends it to the prompt with a `[V58 FILE GUARD] MERGE — never overwrite` header. Gemini always works from the freshest state. Capped at 5 files × 60 KB.
- **Layer 3 — Regression guard** (`main.py`): Before a task runs, snapshots line counts of all source files into `node._pre_task_line_snap`. After completion, compares. If any file shrank >40% and had >30 lines, a `{id}-merge` recovery task is auto-injected with a detailed restoration prompt.
- **Layer 4 — Git commit per task** (`main.py` + `bootstrap.py`): After every successful task: `git add -A && git commit "[supervisor] {task_id} complete"`. `bootstrap.py` now runs `git init` idempotently on every project to guarantee a repo exists. Recovery tasks can `git show HEAD:{file}` to diff what was lost.
- **Layer 5 — SEARCH/REPLACE patch protocol** (`bootstrap.py` → `GEMINI.md`): `GEMINI.md` now instructs Gemini to output surgical `<<<SEARCH/=====REPLACE>>>` diff blocks instead of full file rewrites. Each block is self-describing (SEARCH = "what's there", REPLACE = "what to put there"), making intent explicit and additions non-destructive.

Self-reviews also remain disabled (V57 decision) — they were rubber-stamping and burning quota without measurable value.

### V59 UI Reliability & Auto-Fix Improvements (2026-03-08)

- **Supervisor UI HTML rendering fixed** — All dynamically-injected HTML strings in `index.html` were using broken `< tag attr = "val" >` syntax (space after `<`, spaces around `=`) causing them to render as plain text. Fixed across `addLogLine`, `updatePreview`, `ftBuildNode`, `ftRender` stats, `_tlToggleDetail`, and file-tree error states.
- **Ollama removed from auto-fix path** — Local models can't handle the task context size and Gemini diagnoses its own errors more effectively on a direct retry. Auto-fix now builds the retry prompt immediately with raw error text, eliminating dead latency on every task failure.
- **Merge recovery excludes generated files** — The regression guard (`main.py` Layer 3) was treating `BUILD_ISSUES.md`, `CONSOLE_ISSUES.md`, `PROJECT_STATE.md`, and similar files shrinking as "content loss" and injecting recovery tasks. These files legitimately shrink when issues are resolved. Now excluded by filename. `.md` removed from the tracked extension list entirely — regression detection is for source code only (`.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.css`, `.scss`, `.html`).
- **Session complete screen** — Compacted padding/font sizes across header, KPI grid, and body columns. `sr-card` now has `max-height: 90vh` + `overflow-y: auto` so it scrolls rather than overflows on small screens.
- **Quota pill fixed** — Was reading non-existent `s.quota_used` key; now correctly reads `s.quota.daily_used / daily_limit / daily_pct` from the quota object.
- **`toggleQuotaPause` URL fixed** — Fetch URL had literal spaces: `` `${API_BASE} /api/quota - pause` `` → fixed to `` `${API_BASE}/api/quota-pause` ``.
- **`sendInstruction` URL fixed** — Same broken URL pattern: `` `${API_BASE} /api/instruct` `` → fixed to `` `${API_BASE}/api/instruct` ``.
- **Dev server reinstall crash fixed** — `executor.start_dev_server(port=_preview_port, sandbox=sandbox)` was passing kwargs the function doesn't accept. Fixed to `executor.start_dev_server()`.

### V61 Command Centre Enhancements (2026-03-08)

- **Health tab** — New dedicated `Health` tab aggregates `BUILD_ISSUES.md` + `CONSOLE_ISSUES.md` + Vite compile errors into a single pane with color-coded severity cards (red=error, yellow=warning, blue=info). System resource bar (CPU, Memory, Docker status, Uptime) shows at the top. Header pill shows **✓ CLEAN** or **✕ N ISSUES**. Badge on tab pulses red when issues exist. Auto-refreshes whenever the tab is active.
- **Console tab** — Dedicated browser console relay tab. Reuses the existing `_consoleLogs` array populated by the preview iframe's postMessage relay — zero additional overhead. Level filter dropdown (All / Errors / Warnings / Log / Info). Smart scroll: auto-follows bottom if user is at bottom; holds position if user scrolled up to read. Error count badge on tab. Clear button.
- **Stats tab** — Session statistics dashboard. Shows: DAG progress (Complete/Pending/Running/Failed) with live progress bar; Timing (session uptime, last task duration, average task time); Model status (per-model green/red availability dot + cooldown countdown); System resources (CPU/Memory/Disk). Auto-refreshes whenever the tab is active.
- **`/api/issues` endpoint** — New `GET /api/issues` in `api_server.py` reads `BUILD_ISSUES.md` and `CONSOLE_ISSUES.md` from the active project's `.ag-supervisor/` directory (falls back to project root), parses markdown section headers into structured issue objects with severity detection, and returns JSON with `build_issues`, `console_issues`, `vite_errors`, `total`.
- **Universal scroll preservation** — All tab panels (Logs, Changes, Graph, Timeline, History, Ports, Files, Health, Console, Stats) now save and restore their `scrollTop` before/after `innerHTML` wipes on UI refresh. Smart auto-scroll in Console: only snaps to bottom if user was already at bottom.
- **`temporal_planner.py` crash fix** — `NameError: name 're' is not defined` in `get_parallel_batch()` was crashing the supervisor at startup, causing the premature session-complete screen. Fixed by adding `import re` at module scope. Guards simplified to use `.startswith()` where regex was unnecessary.

### V62 Quota System Overhaul & Smart Estimation (2026-03-10)

- **Bulletproof quota pause** — 4 new leak-gap guards sealed: audit cycle sleeps until reset; vision refresh skipped; proactive audit skipped; headless executor returns immediate failure. Defense-in-depth ensures zero API calls when quota is exhausted.
- **All-model quota display** — `get_quota_snapshot()` seeds ALL models from `GEMINI_MODEL_PROBE_LIST` at 100% defaults. UI always shows every model the supervisor uses, even unprobed ones.
- **Pooled quota buckets** — `QUOTA_BUCKETS` in `config.py`: Pro (3.1-pro + 2.5-pro, ~500 RPD), Flash (3-flash + 2.5-flash, ~1500 RPD), Lite (2.5-flash-lite, ~5000 RPD). Shared pool means using one model drains its bucket-mates.
- **Smart per-bucket estimation** — Counts local CLI calls between probes; applies bucket-specific offset (Pro: 0.2%/call, Flash: 0.067%/call, Lite: 0.02%/call). Persists to disk; resets on successful probe or day rollover.
- **Launch-time quota probe** — Background daemon thread runs quota probe at supervisor startup (non-blocking ~18s).
- **Direct node invocation** — Bypasses `.CMD` wrapper (hangs on Windows piped stdin) by running `node index.js` directly.
- **429 QUOTA_EXHAUSTED recovery** — Parses `retryDelayMs` from CLI error output; marks entire bucket at 0% with correct reset timer.
- **Exact 429 reset countdown** — `retryDelayMs` (exact ms) stored on `TaskResult._quota_cooldown_s`; pool worker passes it to `pause_for_quota(cooldown_seconds=N)` for precise countdown instead of guessing midnight PT. Three-layer fallback: (1) `retryDelayMs` from 429 error, (2) `resets_at` from probe snapshot, (3) midnight PT. `get_status()` exposes `quota_resets_at_exact` (epoch) for UI timers.
- **Confirmed: no quota data in successful calls** — Live-tested `gemini-2.5-flash-lite`; successful API responses return ZERO quota metadata (only model text in stdout, `Loaded cached credentials` in stderr). Smart estimation is the only source for non-exhausted models.
- **Alert thresholds** — Each model includes `alert_level` (ok/low/critical/exhausted) and `bucket` name for UI color coding.
- **Full task history for audits** — `dag_history.jsonl` fingerprints loaded at audit time; 200-description cap removed.
- **🔒 Locked icon** — Blocked tasks (pending with unmet dependencies) show locked padlock icon in DAG strip, tasks tab sidebar, and graph history.

### V65 Graceful Task Requeue on Shutdown (2026-03-10)

- **Stop-requested failure guard** — Added stop-requested check in `_pool_worker` immediately before the auto-fix retry path. If `state.stop_requested` is `True` when a task fails mid-run, the node is reset to `pending` (with `started_at = None`) and saved to disk. No quota wasted; task fully resumable next session.
- **Rate-limit retry skip** — The 30s wait before a same-model rate-limit retry now checks `stop_requested` first. If shutdown is in progress, the task is requeued as `pending` and the worker exits instead of waiting.
- **Audit loop early-exit** — Top of the post-DAG audit `while` loop now breaks immediately on `stop_requested`. Audit tasks already injected into the DAG are persisted and will resume as `pending` next session.
- **Audit cooldown sleeps** — Two 30s cooldown sleeps inside the audit loop (all-models-cooldown wait, scan-failure retry cooldown) now check `stop_requested` first and `break` instead of sleeping.
- **Gemini Lite platform context injection** — `/api/lite/ask` now prepends a comprehensive ~4K-char Supervisor AI knowledge block to every question. Covers: platform purpose and architecture, all UI tabs (Logs, Graph, Timeline, History, Health, Console, Stats, Files, Ports, Gemini Lite panel), key concepts (DAG, worker pool, auto-fix, audit loop, self-healing, quota buckets, 5-layer file conflict protection, session persistence), all API endpoints, and live session state (goal, active model, quota snapshot). Users can now ask Gemini Lite questions about the platform itself — not just their project code.
- **Previously covered** — V40 pre-run guard (tasks waiting at semaphore), V62 quota-paused sleep (chunked 60s loops with stop-check). V65 closes the remaining gaps.

### V64 Gemini Lite Intelligence & PowerShell Policy Auto-Setup (2026-03-10)

- **Gemini Lite replaces Ollama** — New `POST /api/lite/ask` endpoint in `api_server.py` handles all Q&A using `gemini-2.5-flash-lite` (`GEMINI_DEFAULT_LITE`) with automatic fallback to `gemini-3-flash-preview` (`GEMINI_DEFAULT_FLASH`) on quota errors (429 only). No more dependency on a local Ollama install.
- **Usage monitoring** — Every Lite call is tracked (calls_today, fallback_count) in `supervisor/_lite_usage.json` with daily auto-reset. `/api/lite/stats` endpoint exposes live counts.
- **SupervisorState updated** — `ollama_online` renamed to `lite_intelligence_online = True` (always available). Compatibility aliases kept in `to_dict()` for smooth WS clients.
- **UI fully rebranded** — "Ollama Local Intelligence" panel → "Gemini Lite Intelligence". FAB icon updated to the official Gemini star SVG. Buttons always enabled (no offline state). Health dot "Local LLM" → "Lite AI" (always green). All fetch URLs changed from `/api/ollama/ask` → `/api/lite/ask`.
- **`local_orchestrator.py` deprecated** — Marked as deprecated in V64 with no active callers. Kept to avoid breaking residual imports.
- **PowerShell execution policy auto-set** — `Command Centre.bat` step 0 now runs `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force` (no admin required) before all dependency checks. Gemini CLI needs this to spawn scripts on Windows. Idempotent — skips if already set.

### V63 Persistent PTY Probe, Animated Quota UI & Dynamic Model Discovery (2026-03-10)

- **Persistent PTY session** — Gemini CLI is spawned once in a `pywinpty` pseudo-terminal at supervisor startup (~25s). Subsequent `/stats` calls reuse the open session (~2s each). `atexit` handler sends `/quit` for graceful shutdown.
- **Accurate PTY buffer slicing** — `_pty_wait_for()` now searches only NEW buffer data (via `from_pos` parameter), preventing stale cached output from returning false positives.
- **Animated quota UI** — Launcher quota panel updates every 15s with in-place DOM updates: smooth CSS bar transitions, `requestAnimationFrame` number counting (800ms ease-out cubic), green/amber cell pulse on change, and ▲/▼ delta indicators that fade after 3s.
- **Dynamic model auto-discovery** — `classify_model()` auto-classifies new models by name pattern (`*-pro*` → pro, `*-flash*` → flash, `*-flash-lite*` → lite). `update_models_from_probe()` adds new models and removes deprecated ones (2-probe anti-flap). Co-bucketing auto-detected from matching `remaining_pct` and `resets_in_s`. Called automatically after every successful PTY probe.
- **Preemptive failover disabled** — `QUOTA_PROBE_AVOID_THRESHOLD` set to 0%. Models now ONLY switch on actual 429 Resource Exhausted errors, never based on estimated quota percentage.
- **Pending task SVG icon** — Pending tasks now show a distinct SVG clock icon instead of a dot character, in both the DAG strip and tasks tab.
- **WebSocket zombie timeout** — Increased from 30s to 90s to eliminate false "Connection stale" warnings during idle periods.
- **`resets_at` integration** — Reset timestamps propagated throughout: stored in snapshots, used for auto-reset logic, exposed in `get_quota_summary()` for UI display, used for exact cooldown calculation.

### V66–V72 Worker Selector, Quota Pause Modes & Stats Dashboard (2026-03-11)

- **Concurrent worker selector (1–6)** — New `W [1] [2] [3] [4] [5] [6]` toggle in the main header bar allows users to choose how many tasks run simultaneously. Replaces the old boost mode. Setting is persisted to disk and survives restarts.
- **Dynamic semaphore resizing** — When the worker count changes mid-DAG, the asyncio semaphore is dynamically resized: increasing workers immediately releases extra slots for queued tasks, decreasing workers clamps the semaphore so in-progress tasks complete naturally.
- **Quota pause modes** — Three-state quota pause toggle (off/pro/all): `off` = auto-wait for reset; `pro` = pause only when pro-bucket model exhausted; `all` = pause when all models in the failover chain are exhausted. UI label renamed from "QP" to "Quota Pause".
- **Probe-based resume timers** — All 4 quota pause sleep gates (worker, scheduler, audit, `pause_for_quota`) now use probe-derived `_quota_resume_at` instead of hardcoded midnight PT. Sleep occurs in 30-second stop-aware chunks.
- **Fallback model completeness** — All models used for tasks across the failover chain are now included in the quota pause mode checks, not a hardcoded subset.
- **Budget throttle removed** — The auto-throttle that silently reduced workers at 90%/95% budget usage has been removed. Worker count always respects the user's selection; quota exhaustion is handled by the quota pause system.
- **Stats dashboard redesign** — Stats tab rewritten as a CSS grid widget dashboard with 6 named areas: Overview KPIs (full width), DAG Breakdown + System Resources (side by side), Models & Quota + Budget & Workers (side by side), Phase Progress (full width). Each widget is a card with subtle shadows, rounded corners, and hover lift effects. Responsive single-column breakpoint at 720px.
- **Unlimited audit loop** — Removed the `_MAX_AUDIT_CYCLES = 3` hard cap that caused the supervisor to shut down after exactly 3 post-DAG audit cycles, even if the last cycle still found and executed new tasks. The audit loop now runs indefinitely (`while True`) until it naturally terminates: audit finds 0 issues (project complete), user presses Stop, all tasks in a cycle fail, or max scan failures reached. This ensures large projects get as many audit passes as needed.

### V73 — Centralized Version Management, Final Completion Audit Gate & Gemini Stats Tracking (2026-03-12)

- **Centralized version constant** — `config.py` now defines `SUPERVISOR_VERSION`, `SUPERVISOR_VERSION_LABEL`, and `SUPERVISOR_VERSION_FULL`. All version references across `main.py`, `api_server.py`, `index.html`, and patch files read from this single source of truth.
- **Version in WebSocket state** — `supervisor_version` field added to the state broadcast dict.
- **Robust final completion audit gate** — Multi-step verification: DAG sync → phase completion check → comprehensive Gemini hand-off audit with false-completion prevention.
- **Verified quota resume** — `verified_resume_from_quota()` runs `/stats` probe before resuming; re-sleeps if still exhausted.
- **Post-call quota probe** — `_post_call_probe()` runs `/stats` in background thread after every successful Gemini call.
- **Total scheduling silence during pause** — Zero log noise when `quota_paused`.
- **Default quota pause mode** — Changed from `off` to `pro`.
- **Boot stop gates** — 7 stop-awareness checkpoints; safe stop during boot exits in <1s.
- **Stop-cancelled calls skip cooldowns** — Failover chain no longer marks killed-by-stop models as failed.
- **Exclusive `/stats`-only quota** — Removed all speculative quota estimation.
- **Early PTY probe reuse** — Reuses existing probe from `launcher.py`.
- **Image model exclusion** — Image models filtered from failover chain and probe list.
- **Pro model enforcement for audits** — `ask_gemini()` accepts optional `model` parameter; 7 audit call sites explicitly use Pro.
- **Activity-aware timeouts** — Deadline resets on every received data chunk; stall detection for mid-stream timeout.
- **Fresh audit preserves completed task history** — Selectively preserves `status == "complete"` nodes.
- **Replan budget increase & soft-stop** — `max(5, nodes // 5)` formula; exhausted budget skips failed task instead of aborting.
- **Periodic `/stats` probe** — Background asyncio task every 60s.
- **Local reset time in UI** — Computed local time displayed alongside quota countdowns.

### V74 — Modular Architecture, Pro-Only Coding Enforcement & Production Hardening (2026-03-14)

- **Modular architecture refactor** — 5 new dedicated modules extracted from `main.py` and `api_server.py` to improve maintainability and separation of concerns:
  - `a2a_protocol.py` — Agent-to-Agent (A2A) communication protocol for inter-agent typed messaging.
  - `dev_server_manager.py` — Dev server lifecycle management (start, stop, health check, console scanning).
  - `health_diagnostics.py` — Static health scans (TypeScript errors, ESLint, dangerous code patterns) and `HealthReport` generation.
  - `task_intelligence.py` — Task classification, complexity routing, and performance tracking with `record_result()` / `get_stats()`.
  - `environment_setup.py` — `.env` file provisioning and safe development defaults.
- **Pro-only coding enforcement** — `PRO_ONLY_CODING = True` in `config.py`: ALL coding and planning tasks exclusively use Pro models. If Pro quota is exhausted, the system PAUSES instead of falling back to Flash/Lite. Non-coding paths (LiteBrain chat, `classify_task`, `analyze_errors`) remain unaffected.
- **Hot-reload configuration** — `config.reload()` reads `.ag-supervisor/config_overrides.json` at runtime. Whitelisted keys (12 settings including `MAX_CONCURRENT_WORKERS`, `GEMINI_TIMEOUT_SECONDS`, `SKILLS_TOKEN_BUDGET`) can be changed without restarting the supervisor.
- **Per-IP rate limiting** — `api_server.py` enforces 60 requests per minute per client IP on all API endpoints. Returns HTTP 429 on exceed.
- **Session log persistence** — All log entries auto-persisted to `.ag-supervisor/session_log.jsonl` with 5MB rotation. Full session history survives restarts.
- **UI preferences persistence** — Execution mode, quota pause mode, and worker count saved to `.ag-supervisor/ui_prefs.json`. Settings survive restarts.
- **Adaptive WebSocket debounce** — Broadcast interval dynamically adjusts: faster (200ms) during active task execution, standard during idle. Reduces UI latency during active work.
- **Cross-session council memory** — `agent_council.py` V74 upgrades: Swarm Debate triggers on LOW confidence OR 3+ consecutive failures. Success/failure results recorded to cross-session memory. Reviewer agent validates Fixer output before commit.
- **Removed "null" CORS origin** — Blocked `file://` page CORS requests that posed a security risk.
- **Dead code cleanup** — 9 unused imports removed across `a2a_protocol.py`, `environment_setup.py`, `dev_server_manager.py`, `health_diagnostics.py`, and `task_intelligence.py`. Verified via AST analysis and grep with zero regressions across 215 tests.
- **serve_index encoding fix** — `read_text(encoding="utf-8", errors="replace")` in `api_server.py` prevents `UnicodeDecodeError` crash when `index.html` contains non-UTF-8 bytes (previously crashed the Command Centre on load).
- **Quota exhaustion requeue** — Pool worker returns tasks to `pending` instead of sleeping-then-proceeding when all models are exhausted. Prevents cascading retry failures; the scheduling loop's quota pause gate handles the wait cleanly.
- **Smart PTY probe suppression** — Periodic `/stats` probe suppresses during known long cooldowns, sleeping until ~5min before the soonest quota reset. Eliminates recurring PTY timeout noise during multi-hour cooldowns.
- **Event-driven quota probing** — `/stats` probe fires on every CLI call, every failure, and every quota exhaustion event (gated by `_pty_ready`). Dashboard quota data is always fresh with zero staggering overhead.
- **Test-log compression for auto-fix** — `_compress_errors_for_retry()` extracts only error name, file:line references, and 5 lines of stack context per error (capped at 1500 chars each). Focuses CLI retry prompts on exact errors instead of flooding them with passing test output, while the CLI still has full `@.` file access for deeper investigation.
- **Architectural drift detection** — Coherence gate now scans for duplicate exported names across different files after every 5 completed nodes. When duplicates are found, injects a priority-85 consolidation task into the DAG to merge redundant exports.
- **Shared-file impact check** — When a worker modifies files in shared directories (`lib/`, `utils/`, `shared/`, `types/`, `hooks/`, `common/`, `core/`), immediately runs `tsc --noEmit` to catch type breakage. Injects a priority-95 repair task on failure before other workers commit stale code.

---

## 7 — Full Documentation

See [SUPERVISOR_TECHNICAL_REFERENCE.md](SUPERVISOR_TECHNICAL_REFERENCE.md) for the complete technical reference (35+ modules, ~28,000 lines, 74 version iterations).

