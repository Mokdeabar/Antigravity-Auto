# Supervisor AI — Complete Technical Reference

> **Version**: V33 (Major Leap Forward — Boot Sequence Overhaul + Full Stack Hardening)
> **Codebase**: `supervisor/` package — 45+ Python modules, ~22,000 lines
> **Runtime**: Python 3.11+ with Playwright, asyncio, SQLite, ChromaDB, Ollama
> **Purpose**: Fully autonomous AI pipeline that plans, builds, tests, secures, deploys, monitors, heals, grows, optimizes, listens to users, and polishes until perfect.
>
> **What it does for the user**: The Supervisor AI takes a single high-level goal (e.g., "build a landing page" or "fix the login bug") and autonomously drives an AI IDE agent to completion — injecting prompts, clicking approvals, managing browser previews, recovering from crashes, monitoring build servers, switching models on rate limits, and re-injecting when the agent stalls. The user can walk away while the supervisor delivers working software.
>
> **What it can do to itself**: When the supervisor encounters a bug in its own code, it reads ALL of its source files, sends them to Gemini with the crash traceback, receives a fix, validates it with `py_compile` and a shadow sandbox, applies it, and reboots — all without human intervention. It has rewritten itself across 30+ versions.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Core Infrastructure](#2-core-infrastructure)
3. [IDE Automation Layer](#3-ide-automation-layer)
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

The Supervisor AI is a Python-based autonomous software engineering system that wraps around the Antigravity IDE (a VS Code fork). It controls the IDE via Playwright browser automation over CDP (Chrome DevTools Protocol), uses Google's Gemini LLM for intelligence, and orchestrates a full software delivery lifecycle from abstract thought to live deployed infrastructure.

### System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     SUPERVISOR AI (main.py)                      │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │
│  │ Recovery  │  │ Lockfile │  │ Workspace│  │   Page           │ │
│  │ Engine    │  │ Memory   │  │ Locking  │  │   Resolution     │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘ │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │              MONITORING LOOP (30s cycle)                      │ │
│  │  Context Gather → Analyze → Decide → Act → Record            │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │ Context │  │ Gemini  │  │ Agent   │  │ Session │            │
│  │ Engine  │  │ Advisor │  │ Council │  │ Memory  │            │
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
│  │  User   │  │ Polish  │  │  Local  │  │Presence │            │
│  │Research │  │ Engine  │  │ Ollama  │  │Tracker  │            │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │
└─────────────────────────────────────────────────────────────────┘
              │                                    │
              ▼                                    ▼
┌──────────────────────┐            ┌──────────────────────┐
│   Antigravity IDE    │            │    Gemini CLI         │
│   (Playwright/CDP)   │            │(gemini --model M "P") │
└──────────────────────┘            └──────────────────────┘
```

### Execution Flow

1. **Bootstrap**: `main.py` launches Antigravity IDE, connects via CDP
2. **Monitoring Loop**: Every 30 seconds, gathers IDE context (DOM scraping)
3. **Analysis**: Gemini analyzes the context and recommends actions
4. **Execution**: Actions dispatched through Agent Council or direct injection
5. **Verification**: TDD, Visual QA, and Compliance gates validate changes
6. **Deployment**: Ships to staging/production with health checks
7. **Monitoring**: Telemetry ingester watches for production crashes
8. **Growth**: Analyzes conversion analytics, runs A/B experiments
9. **FinOps**: Profiles compute costs, optimizes for profitability

---

## 2. Core Infrastructure

### 2.1 `main.py` — The Orchestrator (~3,057 lines)

The entry point and main event loop. Controls the entire supervisor lifecycle.

#### Key Classes

**`AutoRecoveryEngine`** — Stateful crash recovery that gets smarter with each failure.

| Method | Description |
|--------|-------------|
| `__init__()` | Initializes with 6-tier strategy chain |
| `current_strategy` | Returns current recovery strategy name |
| `record_success()` | Resets failure count after successful loop |
| `recover(error_context)` | Escalates through strategies: reconnect → relaunch → evolve |
| `get_crash_log()` | Returns forensic crash data for debugging |

Recovery strategies (in escalation order):
1. **reconnect_cdp** — Reconnect to the IDE via CDP
2. **force_relaunch** — Kill and restart Antigravity
3. **clear_state** — Wipe stale state files
4. **gemini_diagnose** — Ask Gemini to analyze the crash
5. **self_evolve** — Rewrite own source code to fix the bug
6. **human_escalation** — Play alert sound, halt

#### Key Functions

| Function | Description |
|----------|-------------|
| `_find_antigravity_exe()` | Locates the Antigravity binary across fallback paths |
| `_launch_antigravity(project_path)` | Starts IDE with `--new-window`, `--disable-gpu`, `--no-sandbox`, `--disable-dev-shm-usage` (V33: removed unrecognized `--disable-software-rasterizer`) |
| `_lockdown_workspace(context, project_path)` | Closes pages not matching the project |
| `_get_best_page(context)` | Resolves the best IDE page (prioritizes jetski-agent, then workbench URL) |
| `_connect_cdp()` | **V33: 7-Strategy Boot Detection Pipeline**. Connects via CDP, enumerates ALL pages with URLs/titles/frame counts, then progressively detects UI rendering via: (1) `div[id*="workbench"]`, (2) `contenteditable`, (3) `.monaco-grid-view`, (4) any input, (5) JS DOM check `div.length > 50`, (6) frame-walk all pages, (7) F1 probe all non-extension pages. Removed destructive `page.reload()` recovery |
| `_lockdown_workspace(context, project_path)` | Closes pages not matching the project |
| `_get_best_page(context)` | Resolves the best IDE page (prioritizes jetski-agent) |
| `_lockfile_exists(project_path)` | Checks for `.supervisor_lock` anti-amnesia file |
| `_create_lockfile(project_path)` | Creates lockfile after first injection |
| `_remove_lockfile(project_path)` | Removes lockfile on graceful exit |
| `_play_alert()` | Plays audible alert for human escalation |

#### Systems Implemented in main.py

| System | Purpose |
|--------|---------|
| Lockfile Memory | Prevents re-injection after restart (anti-amnesia) |
| Auto-Reconnect | CDP reconnection with exponential backoff |
| Approval Sniper | Auto-clicks "Run", "Allow", "Always Allow", "Trust", "Run All" buttons within 1000ms |
| Notification Toast Sniper | Auto-clicks VS Code notification toasts (network permissions, extension prompts) via `.notification-toast` and `.notifications-toasts` selectors. **V31**: Filters out `chrome-extension://` and `devtools://` pages before iterating selectors (V30.4+V31) |
| Mandate Firewall | Blocks IDE navigation to localhost (V30 route guard) |
| Workspace Locking | Closes rogue pages/tabs |
| Dynamic Page Resolution | Finds chat webview across Electron frames |
| Ghost Hotkey Injection | Ctrl+L → type → Enter fallback for DOM failures |
| **V33 Direct Injection** | Tries `contenteditable` chat input via frame-walk BEFORE Command Palette (avoids toggling chat closed). Falls back to F1+Command Palette only if direct fails |
| Vision God Loop | Periodic DOM/ARIA-based state detection (V30.1: screenshots severed) |
| **V33 Boot Detection** | 7-strategy progressive pipeline replacing V30's single `.monaco-grid-view` selector (which never matched Antigravity's DOM). Each strategy has 8s timeout. Includes page enumeration logging for diagnostics |
| Boot Retry Loop | On `IDEBootError`, kills zombies, **V31: closes old Playwright instances**, and relaunches Antigravity before exiting (V30.5+V31) |
| UNKNOWN Coma Detector | 6× consecutive UNKNOWN states → page reload → resuscitation (V30.3) |
| Log Preservation | Appends to log on auto-restart (log age < 120s); only wipes on fresh user session (V30.5) |
| Electron Stderr Capture | **V31**: Background daemon thread reads Electron stderr → `logger.warning()` (was black-holed) |
| Graceful Shutdown | **V31**: `_graceful_shutdown()` flushes logs, removes lockfile, exits cleanly. Replaced `os._exit(1)` which skipped ALL cleanup |
| Smart Cooldown Sleep | **V31**: `ALL_MODELS_EXHAUSTED` handler queries `seconds_until_any_available()` from failover chain instead of hardcoded 60s wait |
| LocalManager Singleton | **V32**: WAITING handler lazy-initializes `LocalManager` once and reuses, instead of re-creating every 30s tick (was 2-5s overhead per cycle). Catches `OllamaUnavailable` and degrades gracefully |

---

### 2.2 `config.py` — Central Configuration (~400 lines)

Single source of truth for all constants, selectors, thresholds, and mandates.

#### Configuration Sections

| Section | Key Constants |
|---------|---------------|
| **File Paths** | `CONFIG_FILE_PATH`, `SELECTORS_JSON_PATH`, `AG_COMMANDS_PATH`, `COMMAND_PALETTE_CACHE_PATH` |
| **Workspace** | `set_project_path()`, `get_project_path()`, `get_state_dir()` |
| **Mandates** | `ULTIMATE_MANDATE`, `TINY_INJECT_STRING`, `MANDATE_FIREWALL_RULE`, `MANDATE_FILENAME` |
| **Lockfile** | `LOCKFILE_NAME = ".supervisor_lock"` |
| **Approval** | `APPROVAL_SNIPER_TIMEOUT_MS = 1000`, `APPROVAL_BUTTON_TEXTS`, `APPROVAL_BUTTON_XPATH` |
| **Vision** | `VISION_POLL_INTERVAL_SECONDS = 30.0`, `SCREENSHOT_PATH`, `MAX_RESUSCITATE_FAILURES = 2` |
| **Ollama Vision** | `_DEPRECATED_OLLAMA_VISION_MODEL = "llava"`, `_DEPRECATED_OLLAMA_VISION_TIMEOUT = 30` **(V31: marked deprecated — dead code path, getattr fallback only)**, `OLLAMA_VISION_CONFIDENCE_THRESHOLD = 0.7` |
| **ANSI Colors** | `ANSI_GREEN`, `ANSI_RED`, `ANSI_CYAN`, `ANSI_MAGENTA`, `ANSI_YELLOW`, `ANSI_BOLD`, `ANSI_RESET` |
| **CDP** | `CDP_URL = "http://localhost:9222"` |
| **Gemini CLI** | `GEMINI_CLI_CMD`, `GEMINI_TIMEOUT_SECONDS = 180`, `GEMINI_FALLBACK_MODEL`, `GEMINI_MODEL_PROBE_LIST` **(V31: `auto` moved to position 2 — was wasting 30s timeout on cold boots)**, `GEMINI_PRO_MODELS` **(V31: `auto` removed)**, `GEMINI_FLASH_MODELS`, `GEMINI_DEFAULT_FLASH` |
| **Retry Policy** | `GEMINI_RETRY_MAX_ATTEMPTS = 3`, `GEMINI_COOLDOWN_DELAYS` |
| **Context Budget** | `CONTEXT_BUDGET_WARN_CHARS`, `CONTEXT_BUDGET_MAX_CHARS` |
| **Rate Limits** | `RATE_LIMIT_DEFAULT_WAIT_S = 60`, `RATE_LIMIT_MAX_WAIT_S = 300`, `RATE_LIMIT_HISTORY_SIZE = 20` |
| **Self-Healing** | `SELF_IMPROVEMENT_INTERVAL_S = 3600` |
| **Presence** | `PRESENCE_IDLE_THRESHOLD_S = 300` |
| **Pacing** | `PACING_MIN_MS = 800`, `PACING_MAX_MS = 2500` |
| **Timing** | `POLL_INTERVAL_SECONDS = 10.0`, `ACTION_DELAY_MS = 300`, `PAGE_LOAD_TIMEOUT_MS`, `SELECTOR_TIMEOUT_MS` |
| **Loop Thresholds** | `LOOP_HISTORY_SIZE = 5`, `DUPLICATE_THRESHOLD = 2`, `ERROR_SUBSTRING_THRESHOLD = 3`, `CONSECUTIVE_FAIL_THRESHOLD = 2`, `MAX_SAME_ERROR_INTERVENTIONS = 5` |
| **WAITING Escalation** | `WAITING_REINJECT_THRESHOLD = 2`, `WAITING_GHOST_THRESHOLD = 4`, `WAITING_DEFIB_THRESHOLD = 6`, `WORKING_STALE_THRESHOLD = 3` |
| **Context Engine** | `CONTEXT_CONFIDENCE_THRESHOLD = 0.7`, `SCREENSHOT_DIFF_THRESHOLD`, `MIN_SCREENSHOT_INTERVAL`, `FORCE_SCREENSHOT_INTERVAL`, `DEV_SERVER_CHECK_PORT = 3000` |
| **Proactive Mode** | `PROACTIVE_MODE = true`, `HEARTBEAT_INTERVAL_SECONDS = 60.0`, `SIMPLE_BROWSER_AUTO_OPEN = true` |
| **V28 FinOps** | `APPROVED_DEPLOY_PROVIDERS`, `MARGIN_DECAY_THRESHOLD = 0.15`, `MIN_TRAFFIC_FOR_FINOPS = 1000` |
| **Page Filtering** | `IGNORED_PAGE_URL_PREFIXES`, `DESIRED_PAGE_URL_PREFIXES`, `DESIRED_PAGE_TITLE_KEYWORDS`, `CHAT_PAGE_URL_HINTS` |
| **Parallel** | `MAX_CONCURRENT_WORKERS = 2` |

---

## 3. IDE Automation Layer

The Supervisor controls the Antigravity IDE (a VS Code / Electron fork) via Playwright browser automation over CDP. This layer handles DOM discovery, text injection, frame walking, and visual monitoring.

### 3.1 `dom_prober.py` — Dynamic DOM Discovery (552 lines)

Instead of static CSS selectors, this module **probes the live DOM** to find elements dynamically. Uses a cascading first-match-wins strategy with 20+ selector candidates.

#### Core Functions

| Function | Description |
|----------|-------------|
| `probe_element(page, candidates, selector_key)` | Tries each candidate selector, returns first visible match, persists to `selectors.json` |
| `probe_chat_input(page)` | Specialized probe for the chat input element (textarea or contenteditable) |
| `probe_chat_messages(page)` | Probe for agent response message elements |
| `find_best_page(pages, ...)` | Scores and ranks Playwright pages to find the main IDE window |
| `_live_dom_scan_for_input(page)` | Last-resort: enumerates ALL textareas, scores by context clues |
| `_minify_html_for_gemini(raw_html)` | Strips HTML for Gemini analysis when all probes fail |

#### Selector Candidate Categories
- **Golden Selectors** — Confirmed from live UI (`div[contenteditable='true']`)
- **Agent Panel** — Jetski agent-specific selectors
- **Broad VS Code** — Generic Monaco editor patterns
- **ARIA Fallbacks** — Accessibility attribute matching

### 3.2 `injector.py` — Text Injection Engine (13,680 bytes)

Handles the actual typing of prompts into the IDE's chat input.

#### Injection Methods (in priority order)
1. **Direct DOM Injection** — Set `textContent` + dispatch `input` event
2. **Playwright Type** — Character-by-character typing via `page.type()`
3. **Ghost Hotkey** — Ctrl+L → type → Enter (fallback when DOM is unreachable)
4. **Clipboard Injection** — Copy to clipboard → Ctrl+V paste

### 3.3 `context_engine.py` — Deep Context Awareness (1,067 lines)

Replaces shallow screenshot analysis with structured DOM-based context gathering. Scrapes chat, terminal, server state, approval buttons, activity indicators, and user presence.

#### Data Classes

| Class | Fields | Purpose |
|-------|--------|---------|
| `MessageInfo` | `role`, `content`, `message_type`, `has_diff`, `has_error`, `timestamp` | Single chat message with metadata |
| `DiffReport` | `filename`, `additions`, `deletions`, `summary` | Parsed file change from agent output |
| `ServerInfo` | `running`, `port`, `url`, `last_check` | Dev server health |
| `ProgressInfo` | `files_mentioned`, `commands_run`, `errors_seen`, `percent_complete` | Agent progress tracking |
| `ContextSnapshot` | All above + `terminal_output`, `simple_browser_open`, `has_pending_approval`, `confidence`, `gathered_at`, `context_chars_sent`, `context_budget_pct`, `user_idle_seconds` | Complete IDE state snapshot |

#### Core Function

**`gather_context(context, page, goal)`** — The main pipeline:
1. Deep chat scrape (separates user vs agent messages)
2. Terminal output reader (xterm row extraction)
3. Dev server health check — **V32: multi-port scan** (see below)
4. Simple Browser detection
5. Approval button scan
6. Progress estimation
7. Confidence scoring

> **V32 Dynamic Port Wiring**: The dev server check now scans **7 common ports** (3000, 3001, 4200, 5000, 5173, 8000, 8080) concurrently instead of just the hardcoded `DEV_SERVER_CHECK_PORT`. Session memory recorded ports get priority. Discovered ports are persisted via `session_memory.record_port()` for future runs.

#### Internal Functions

| Function | Description |
|----------|-------------|
| `_deep_chat_scrape(context)` | DOM class analysis to separate message roles |
| `_classify_message(text, role)` | Detects diffs, errors, questions, completions |
| `_parse_diff_blocks(messages)` | Extracts structured diff data from code blocks |
| `_read_terminal_output(context)` | Reads xterm rows across all frames |
| `_check_dev_server(port)` | Async HTTP health check on localhost |
| `_check_dev_server_multi(ports)` | **V32**: Scans multiple ports concurrently via `asyncio.gather`, returns first UP server, records to session memory |
| `_check_simple_browser(context)` | Detects Antigravity Browser Extension panel |
| `_check_activity_indicators(context)` | Detects Thinking/Running activity badges |
| `_check_approval_buttons(context)` | Scans for pending approval buttons |
| `_build_progress(messages, terminal)` | Builds progress info from messages and terminal |
| `_classify_agent_status(...)` | Returns WORKING, WAITING, ASKING, or IDLE |
| `_compute_confidence(snapshot)` | Confidence score 0.0–1.0 for screenshot need |
| `needs_screenshot(snapshot)` | V30.1: **Always returns False** — vision severed, state detection is 100% DOM/ARIA-driven. Screenshots are never taken in the main polling loop. |
| `format_context_for_prompt(snapshot)` | Formats snapshot for Gemini prompt inclusion |

#### `PresenceTracker` Class (OpenClaw-Inspired)

Tracks user activity in the IDE via DOM signals.

| Method | Description |
|--------|-------------|
| `record_activity()` | Records that user activity was detected |
| `check_chat_change(chat_messages)` | Checks if chat messages changed (indicates activity) |
| `get_idle_seconds()` | Returns seconds since last detected activity |
| `is_user_idle(threshold)` | Returns True if user appears idle |
| `get_status()` | Returns full presence status dict |

### 3.4 `frame_walker.py` — Cross-Frame Navigation (384 lines, 15,292 bytes)

Walks the Electron frame tree to find elements buried in nested iframes. VS Code / Antigravity uses deeply nested webview frames for panels, terminals, and chat.

> **V32**: `find_element_in_all_frames()` now uses Playwright's `frame.wait_for_selector()` with 1.5s auto-wait instead of raw `query_selector_all()`. This catches elements still rendering after navigation, eliminating a class of "element not found" false negatives.

| Function | Description |
|----------|-------------|
| `_collect_all_frames(page)` | Recursively collects ALL frames including deep Webview iframes |
| `find_element_in_all_frames(context, selector)` | Search all pages/frames for first visible match — **V32: uses auto-wait** |
| `find_all_elements_in_all_frames(context, selector)` | Find ALL matches across all pages/frames |
| `find_all_elements_in_all_frames(context, selector)` | Find ALL matches across all pages/frames |
| `find_chat_frame(context)` | Finds the frame containing the AI Agent Chat input |
| `extract_all_html(context, minify, max_chars)` | Scrapes and concatenates HTML from all pages/frames |
| `_minify_html(raw_html)` | Aggressive DOM minification via BeautifulSoup |
| `ghost_hotkey_inject(page, message)` | Keyboard-driven injection fallback via Command Palette |

### 3.5 `monitor.py` — Visual Monitoring (14,722 bytes)

Periodic screenshot capture and analysis. Takes IDE screenshots, detects visual anomalies, and triggers corrective actions.

### 3.6 `terminal.py` — Terminal Control (6,681 bytes)

Reads and writes to the IDE's integrated terminal. Parses command output, detects errors, and monitors running processes.

### 3.7 `command_resolver.py` — VS Code Command Engine (12,586 bytes)

Resolves and executes VS Code commands via the command palette. Maintains a cache of valid command IDs from the `ag-commands.txt` dump.

### 3.8 `vision_optimizer.py` — Screenshot Analysis (7,424 bytes)

Optimizes screenshots before sending to Gemini: resizes, compresses, and extracts relevant regions. Reduces token consumption for vision calls.

---

## 4. Intelligence Layer (Gemini Integration)

### 4.1 `gemini_advisor.py` — Centralized Gemini CLI Interface (~1,112 lines)

Every module that needs Gemini's intelligence calls through here. Provides async, sync, and JSON-parsing variants.

#### Primary Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `ask_gemini` | `(prompt, timeout, use_cache, max_retries)` | Core async call with retry, failover, and budget tracking |
| `ask_gemini_json` | `(prompt, timeout, use_cache)` | Calls Gemini and parses JSON from response (balanced-brace extraction) |
| `ask_gemini_sync` | `(prompt, timeout, max_retries)` | Synchronous variant for exception handlers where event loop unavailable |
| `ask_gemini_sync_json` | `(prompt, timeout)` | Synchronous call + JSON parsing |
| `call_gemini_with_file` | `(prompt, file_path, timeout)` | Multimodal call with file attachment — V14 full retry/failover integration |

> **V32 CLI Modernization**: All Gemini CLI invocations now use `--model` flag instead of `-m` (Gemini CLI v0.29+ standard, Feb 2026). Prompts are piped via stdin (the correct automation pattern). The probe function uses `-p` for non-interactive prompt testing.
>
> **V31 Outer Timeout**: `ask_gemini()` now wraps the entire retry loop in an outer safety timeout of `timeout * (attempts + 1)`. If the retry loop itself hangs (e.g., subprocess deadlock), this prevents the monitoring loop from stalling forever.
| `call_gemini_with_file_json` | `(prompt, file_path, timeout)` | File call + JSON parsing with two-stage extraction |

#### Internal Functions

| Function | Description |
|----------|-------------|
| `_get_best_model(prompt)` | Smart model routing: TaskComplexityRouter → rate limit check → failover chain |
| `_probe_and_cache_model(preferred)` | Startup model probing with fallback chain |
| `_cache_key(prompt)` | Creates cache key from first 200 chars |
| `_strip_markdown_fences(text)` | Removes markdown code fences from responses |
| `_extract_json_object(text)` | V10: String-aware balanced brace JSON extraction |
| `_glass_brain_send(prompt)` | ANSI cyan console output for sent prompts |
| `_glass_brain_receive(response)` | ANSI yellow console output for responses |
| `_glass_brain_error(error)` | ANSI red console output for errors |
| `_call_gemini_async(prompt, timeout, model)` | Low-level async subprocess wrapper — **V32**: uses `--model` flag |
| `_call_gemini_sync(prompt, timeout)` | Low-level synchronous subprocess wrapper |
| `_diagnose_gemini_error(error, prompt)` | Feeds CLI errors back through Gemini for diagnosis |
| `clear_cache()` | Clears the session cache |
| `cache_stats()` | Returns cache hit/miss statistics |
| `self_diagnose(error, context)` | Flash-powered self-diagnosis of CLI errors |
| `request_self_improvement(issue_summary)` | Asks Gemini to review errors and suggest improvements |

#### Features
- **Session Cache**: Avoids asking identical questions (50 entry LRU)
- **Glass Brain**: Color-coded ANSI console output for debugging
- **Smart Routing**: Uses `TaskComplexityRouter` to classify prompt complexity → select optimal model
- **Rate Limit Awareness**: Checks `RateLimitTracker` before calls

### 4.2 `retry_policy.py` — Production-Grade Retry Systems (~915 lines)

Three core systems inspired by OpenClaw's architecture, plus V30.5 quota intelligence.

#### `RetryPolicy` — Exponential Backoff with Jitter

```python
RetryPolicy(max_attempts=3, base_delay_s=2.0, max_delay_s=30.0, jitter_pct=0.10)
```

| Method | Description |
|--------|-------------|
| `delay_for(attempt)` | `min(base * 2^attempt, max_delay) ± jitter` |
| `should_retry(attempt)` | Returns True if `attempt < max_attempts` |

#### `ModelFailoverChain` — Cooldown-Based Model Fallback

Manages an ordered list of Gemini models with per-model cooldowns.

| Method | Description |
|--------|-------------|
| `get_active_model()` | Returns best available model (respects cooldowns, sticky preference) |
| `report_failure(model)` | Applies escalating cooldown: 1m → 5m → 25m → 1h |
| `report_timeout(model)` | **V30.6**: Short 30s cooldown WITHOUT failure escalation — timeouts are transient and should not trigger 1h cooldowns like API errors |
| `report_success(model)` | Resets failure count, sets as sticky model |
| `seconds_until_any_available()` | **V32**: Returns seconds until the soonest model comes off cooldown. Used by WAITING handler for precise sleep instead of hardcoded 60s. Returns 0.0 if any model is already available, 600.0 cap if no cooldown data |
| `get_status()` | Returns chain status dict with cooldown timers |
| `_load_state()` | V30.5: Caps stale failure counts at 3 per model to prevent zombie 3600s cooldowns from old sessions |

Model chain (from `config.GEMINI_MODEL_PROBE_LIST`):
```
gemini-3.1-pro-preview → auto → gemini-3-pro-preview → gemini-2.5-pro → gemini-2.5-flash → gemini-2.0-flash
```

> **V31**: `auto` was moved from position 1 to position 2 because it wasted 30s timing out on every cold boot before falling through. Also removed from `PRO_MODELS` to prevent re-probing.

State persisted to `_failover_state.json`.

#### `ContextBudget` — Token Consumption Tracking

```python
ContextBudget(warn_chars=500_000, max_chars=2_000_000, history_size=50)
```

| Method | Description |
|--------|-------------|
| `record(prompt_chars, response_chars, model)` | Records a Gemini call's usage |
| `budget_pct` | Percentage of budget consumed |
| `get_report()` | Human-readable usage report |
| `should_prune()` | True if context pruning is needed |

#### Additional Classes

| Class | Description |
|-------|-------------|
| `TaskComplexityRouter` | Classifies prompts as simple/medium/complex → routes to Flash/Pro models |
| `RateLimitTracker` | Tracks rate limit events per model, calculates cooldown windows |

#### `RateLimitTracker` — V30.5 Smart Quota Intelligence

Rate limit patterns detected (in `_RATE_LIMIT_PATTERNS`):
- `429`, `quota exceeded`, `RESOURCE_EXHAUSTED`, `rate limit`, `too many requests`
- `retry-after: <N>`, `TerminalQuotaError`, `exhausted your capacity`

| Feature | Description |
|---------|-------------|
| `_parse_quota_reset_seconds(error_text)` | V30.5: Extracts exact cooldown from quota errors (e.g. `"17m50s"` → 1070 seconds) |
| `record_rate_limit(model, error_msg)` | Records rate limit event; uses exact parsed time + 10s buffer if available, else adaptive estimation |
| `suggest_alternative_model(model)` | Suggests next available model in the failover chain |
| `is_rate_limit_error(error_text)` | Static method: checks if error text matches any rate limit pattern |

### 4.3 `local_orchestrator.py` — Local LLM Manager (~448 lines)

Interfaces with a local Ollama model for local inference. Provides the `LocalManager` class for hypothesis generation, vision-first screenshot analysis, and context-aware follow-up synthesis.

#### `OllamaUnavailable` Exception (V32)

Raised when Ollama fails to boot or respond. **Non-fatal** — callers catch this and degrade gracefully instead of killing the supervisor. Previously, `ensure_ollama_running()` called `sys.exit(1)` which killed the entire supervisor over an optional dependency.

#### `ensure_ollama_running(model_name, host)` — Autonomous LLM bootstrapper

1. Pings `GET {host}/` to check if Ollama is alive
2. If not running: locates `ollama` binary → starts `ollama serve` as detached process
3. Polls health endpoint for up to 30 seconds
4. **V32**: Raises `OllamaUnavailable` on failure instead of `sys.exit(1)`
5. Checks model availability via `GET {host}/api/tags` → auto-pulls missing models

#### `LocalManager` Class

| Method | Description |
|--------|-------------|
| `__init__(model_name, host)` | Initializes with model name (default: `llama3`) and Ollama host URL. Calls `ensure_ollama_running()` |
| `health_check()` | **V32**: Pings `/api/ps` to verify Ollama is responsive. 5s timeout. Sets internal `_healthy` flag. Called before each API request when unhealthy |
| `ask_local_model(system_prompt, user_prompt, temperature)` | Universal local LLM interface; forces strict JSON output via `format: "json"`, uses `stream: false`, `num_predict: 300`, **V32: 120s timeout** (was 300s) |
| `_sync_http_post(url, data, timeout)` | Raw HTTP POST via `urllib.request.urlopen` (no aiohttp dependency). **V32**: Default timeout reduced to 120s. Called from `asyncio.run_in_executor()` to avoid blocking event loop |
| `analyze_screenshot(image_path, prompt, temperature)` | Vision analysis via local model (e.g. llava) with base64-encoded images. **V30**: Downscales to 512×512 grayscale WebP for fast inference. Uses deprecated config getattr fallback |
| `synthesize_followup(chat_history, system_goal)` | Dynamically generates context-aware follow-up instructions to unblock a stalled agent |

> **V32 Degradation**: Both `main.py` call sites now catch `OllamaUnavailable` — vision analysis and WAITING handler log a warning and continue without the local LLM. The WAITING handler uses a lazy singleton pattern (initialized once, reused across ticks).

### 4.4 `external_researcher.py` — Web Research Agent (12,508 bytes)

When the Local Manager needs external data (npm packages, API docs, Stack Overflow), this module performs web searches and summarizes results.

---

## 5. Multi-Agent Council

### 5.1 `agent_council.py` — The Million Dollar Team (1,324 lines)

Orchestrates 6 specialist Gemini agents that collaborate to diagnose, fix, test, and evolve the supervisor autonomously.

#### Agent Personas

| Agent | Role | Expertise |
|-------|------|-----------|
| **Diagnostician** | Systems analyst | 30 years debugging Electron apps, VS Code extensions, browser automation |
| **Architect** | Systems designer | Designs robust, extensible solutions; thinks in systems, not patches |
| **Debugger** | Reverse engineer | Python async, Playwright, crash analysis; finds the root, not the symptom |
| **Fixer** | Senior developer | Writes production-grade async Python; generates complete, deployable code |
| **Reviewer** | Quality arbiter | PASS/FAIL/NEEDS_WORK verdicts; last line of defense |
| **Synthesizer** | Supreme judge | Resolves conflicting analyses; outputs single definitive action plan |

#### Data Classes

**`Issue`** — What the council needs to resolve:
- `issue_type` — Category (crash, stall, error, improvement)
- `trigger` — What caused it
- `screenshot_path` — Visual evidence
- `logs` — Recent log lines
- `source_context` — Relevant source code
- `goal` — What the system is trying to achieve
- `consecutive_count` — How many times this issue has recurred

**`Resolution`** — Council outcome:
- `resolved: bool` — Whether the issue was fixed
- `diagnosis: str` — Root cause analysis
- `action: str` — Final action taken
- `actions_log: list[dict]` — Full audit trail
- `rounds_used: int` — How many debate rounds
- `code_patch: dict` — `{file, code}` if code was generated

#### Core Functions

| Function | Description |
|----------|-------------|
| `AgentCouncil.convene(issue)` | Main entry — convenes the council for an issue |
| `_swarm_debate(issue)` | Architect + Debugger analyze in parallel, Synthesizer resolves |
| `_generate_fix(diagnosis)` | Fixer generates code patch based on diagnosis |
| `_review_fix(patch)` | Reviewer validates with PASS/FAIL/NEEDS_WORK |
| `_read_recent_logs(n)` | Reads last N lines of supervisor.log |
| `_read_module_source(filename)` | Reads a specific module's source |
| `_read_all_module_sources()` | Reads all modules (truncated to 30K chars) |

#### EPIC Handler

The council handles **EPIC** requests — multi-file, multi-step feature implementations:
1. Parses EPIC markdown into tasks
2. Builds a dependency DAG
3. Executes nodes in topological order
4. Injects each task into the IDE agent
5. Monitors completion per node
6. Triggers V25 Deployment Engine on completion

### 5.2 `council_knowledge.py` — Knowledge Base (10,879 bytes)

Persistent knowledge store for the council. Learns from past resolutions and applies them to similar future issues. Stored in `_council_knowledge.json`.

---

## 6. Memory Systems

### 6.1 `session_memory.py` — The Hippocampus (594 lines)

Persistent session memory. Survives restarts via JSON file. Tracks everything that happens during a supervisor session.

#### `SessionMemory` Class

| Method | Description |
|--------|-------------|
| `set_goal(goal)` | Sets the session goal |
| `record_event(event_type, detail)` | Records semantic events (injection, approval, error, recovery, etc.) |
| `record_context_gather()` | Lightweight counter for context gathers |
| `record_files(filenames)` | Tracks files the agent has modified |
| `record_port(port)` | Records detected dev server ports |
| `update_status(status)` | Updates agent status (WORKING, IDLE, STALLED) |
| `get_session_summary()` | Generates narrative summary for Gemini context |
| `snapshot_state(context)` | V12: Time-travel engine — serializes state before actions |
| `get_latest_snapshot()` | Loads most recent pre-action snapshot |
| `compact_history()` | OpenClaw-style: summarizes old events, keeps recent ones detailed |
| `prune_gemini_context(messages)` | Soft-trim old tool results (head 500 + tail 500 chars) |
| `flush()` | Forces save to disk |
| `_save()` | V30.3: Atomic save with WinError 5 retry (3 attempts, 0.5s delay) for Windows file lock resilience |

#### Event Types
`goal_injected`, `approval_clicked`, `error_detected`, `error_resolved`, `question_answered`, `refinement_triggered`, `screenshot_taken`, `screenshot_skipped`, `simple_browser_opened`, `server_detected`, `loop_detected`, `recovery_attempted`, `agent_status_change`, `compaction_performed`, `pruning_performed`

State stored in `.ag-supervisor/_session_state.json`.

### 6.2 `episodic_memory.py` — Long-Term Memory (8,369 bytes)

Stores cross-session episodic memories. Remembers patterns, solutions, and mistakes from previous runs. Uses ChromaDB for vector similarity search.

### 6.3 `memory_consolidation.py` — Memory Consolidation (8,805 bytes)

Runs periodically to consolidate short-term session memories into long-term episodic storage. Extracts learnings and patterns from past events.

### 6.4 `brain.py` — Central Brain Module (7,654 bytes)

Coordinates between memory systems, providing a unified interface for memory retrieval and storage.

---

## 7. Planning & Execution Engine

### 7.1 `temporal_planner.py` — V19 Temporal Planner (20,235 bytes)

Converts abstract goals into executable DAGs (Directed Acyclic Graphs).

#### Pipeline
1. **Parse Goal** — Breaks goal into discrete tasks
2. **Build DAG** — Identifies dependencies between tasks
3. **Topological Sort** — Orders tasks for execution
4. **Execute Nodes** — Processes each node, injecting into IDE
5. **Monitor Progress** — Tracks completion per node
6. **Handle Failures** — Retries or re-plans on failure

#### State Management
- State persisted to `.ag-memory/temporal_state.json`
- Survives supervisor restarts
- Supports resume from last completed node

### 7.2 `workspace_transaction.py` — Atomic Workspace Operations (9,115 bytes)

Provides transactional guarantees for workspace changes:
- **Begin**: Snapshot current state (git stash or file backup)
- **Commit**: Accept changes
- **Rollback**: Restore to pre-change state

### 7.3 `cli_worker.py` — CLI Command Executor (3,622 bytes)

Executes shell commands as part of the planning pipeline. Wraps subprocess execution with timeout management, error capture, and output parsing.

### 7.4 `workspace_indexer.py` — Workspace Index (11,421 bytes)

Maintains a structured index of all files in the workspace. Provides fast file search, type detection, and dependency mapping. Updated periodically by the scheduler.

---

## 8. Verification & Quality Assurance

### 8.1 `autonomous_verifier.py` — V22 TDD Verifier (13,224 bytes)

Enforces Test-Driven Development on every code change.

#### Verification Pipeline
1. **Test Discovery** — Finds existing test files matching modified code
2. **Test Generation** — If no tests exist, generates them via Gemini
3. **Test Execution** — Runs pytest/jest/mocha depending on project type
4. **Coverage Check** — Verifies minimum coverage thresholds
5. **Regression Guard** — Ensures existing tests still pass

### 8.2 `visual_qa_engine.py` — V23 Visual QA (14,631 bytes)

Screenshot-based visual regression testing.

#### Pipeline
1. **Capture** — Takes screenshots of UI components
2. **Gemini Analysis** — Sends screenshots to Gemini for visual evaluation
3. **Comparison** — Compares against baseline screenshots
4. **Verdict** — PASS/FAIL with specific UI issues identified
5. **Auto-Fix** — Generates CSS/layout fixes for visual regressions

### 8.3 `merge_arbiter.py` — Conflict Resolution (10,133 bytes)

When multiple agents propose conflicting changes, the Merge Arbiter:
- Detects overlapping file modifications
- Uses Gemini to evaluate competing changes
- Selects the superior implementation
- Merges non-conflicting changes automatically

### 8.4 `reflection_engine.py` — Post-Action Reflection (3,691 bytes)

After each significant action, reflects on what happened:
- Was the action successful?
- What could have been done better?
- Should the approach be changed?
- Feeds insights back into the council knowledge base

---

## 9. Security & Compliance

### 9.1 `compliance_gateway.py` — V24 Compliance Gateway (17,433 bytes)

Security gate that every code change must pass through before deployment.

#### Audit Checks

| Check | Description | Severity |
|-------|-------------|----------|
| **Secret Scanner** | Regex-based detection of API keys, tokens, passwords in code | CRITICAL |
| **Dependency Audit** | Checks `package.json`/`requirements.txt` for known CVEs | HIGH |
| **License Compliance** | Validates dependency licenses against approved list | MEDIUM |
| **OWASP Top 10** | Scans for SQL injection, XSS, CSRF patterns | HIGH |
| **Hardcoded Credentials** | Detects `.env` files or hardcoded secrets in tracked files | CRITICAL |

#### Pipeline
1. **Pre-Commit Scan** — Runs before any git commit
2. **LLM Review** — Gemini analyzes code for security anti-patterns
3. **Verdict** — PASS (deploy), WARN (deploy with notes), FAIL (block)
4. **Remediation** — Generates fix suggestions for failures

### 9.2 `git_manager.py` — Git Operations (8,072 bytes)

Safe git operations with rollback support.

| Function | Description |
|----------|-------------|
| `commit(message)` | Atomic commit with pre-commit hooks |
| `stash()` / `stash_pop()` | Save/restore dirty working state |
| `checkout(sha)` | Checkout specific commit (for hotfix targeting) |
| `rollback(sha)` | Hard reset to previous state |
| `get_current_sha()` | Returns HEAD commit hash |

---

## 10. Deployment Pipeline

### 10.1 `deployment_engine.py` — V25 Autonomous Deployment Engine (23,727 bytes)

Full release pipeline from code to production.

#### `DeploymentEngine` Class

| Method | Description |
|--------|-------------|
| `deploy_epic()` | Complete deployment pipeline |
| `request_deploy_confirmation()` | 10-second interactive countdown for human confirmation |
| `_scan_secrets()` | Regex + .env tracking guard before deploy |
| `_check_migrations()` | Detects database migrations, requires human approval |
| `_provision_staging(provider)` | Deploys to staging (Vercel/Railway/generic CLI) |
| `_run_health_check(url)` | HTTP health check with 3 retries |
| `_promote_to_production()` | Staging → production promotion |
| `_rollback()` | Instant rollback to previous stable release |

#### Deployment Flow

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
                                    │
                              Health Check
                                    │
                              ┌──Pass──┴──Fail──┐
                              │                  │
                           SUCCESS          ROLLBACK
```

#### Safety Caps
- `MAX_ENVIRONMENTS = 3` per epic
- `DEPLOY_TOKEN` required for production
- Migration approval gate (explicit human input)
- Approved providers only: `APPROVED_DEPLOY_PROVIDERS`

---

## 11. Production Monitoring & Self-Healing

### 11.1 `telemetry_ingester.py` — V26 Ouroboros Loop (20,045 bytes)

Self-healing production monitor. Catches crashes, writes its own hotfix, and re-enters the build pipeline.

#### `TelemetryIngester` Class

| Method | Description |
|--------|-------------|
| `ingest_webhook(payload)` | Receives error payload, returns (should_fix, signature, message) |
| `poll_vercel_logs()` | Polls Vercel deployment logs for fatal errors |
| `process_error(payload)` | Full pipeline: filter → dedup → RCA → epic generation |
| `is_noise(error_text)` | Returns True for 404s, favicon, ECONNRESET, deprecation warnings |
| `is_fatal(error_text)` | Returns True for TypeError, 500, Traceback, SIGKILL |
| `compute_signature(error_text)` | Stable hash after stripping timestamps/PIDs/addresses |
| `check_rate_limit(signature)` | Returns (allowed, attempt_count) — max 2 per 24h |
| `record_attempt(signature)` | Records a hotfix attempt for rate limiting |

#### Noise Filter Patterns
`404`, `favicon.ico`, `robots.txt`, `DeprecationWarning`, `ECONNRESET`, `ECONNABORTED`, `client timeout`, `ETIMEDOUT`

#### Fatal Detection Patterns
`TypeError`, `ReferenceError`, `SyntaxError`, `500`, `502`, `503`, `Traceback`, `SIGKILL`, `SIGABRT`, `OOMKilled`, `heap out of memory`, `FATAL ERROR`

#### Rate Limiting
- 2 autonomous hotfix attempts per unique error signature per 24 hours
- 3rd attempt: HALT and page the operator
- Error signatures are hashed after stripping volatile data (timestamps, PIDs, memory addresses)

### 11.2 `telemetry_hud.py` — Live Dashboard (4,399 bytes)

Real-time ANSI-based HUD showing system health metrics, updated every 10 seconds by the scheduler.

---

## 12. Growth & Optimization

### 12.1 `growth_engine.py` — V27 Autonomous Growth Engine (21,402 bytes)

Proactive growth mechanism. Ingests analytics, deploys A/B experiments, evaluates with statistical rigor.

#### `GrowthEngine` Class

| Method | Description |
|--------|-------------|
| `ingest_report(report)` | Parses daily conversion report, flags underperformers (< 50% of mean) |
| `generate_hypothesis(underperformers)` | LLM generates growth hypothesis |
| `generate_experiment_epic(hypothesis)` | Writes EXPERIMENT_EPIC.md with feature flag |
| `evaluate_experiment(flag, results)` | Two-proportion Z-test with min sample + significance |
| `cleanup_experiment(flag, winner, file)` | Removes flag, keeps winner code, deletes loser |
| `get_pending_evaluations()` | Returns experiments ready for evaluation (48h+ elapsed) |
| `is_cacheable(route, context)` | Checks route against no-cache patterns |

#### Scope Bounding
- **Allowed**: `src/components/`, `pages/`, `views/`, `layouts/`, `frontend/`
- **Blocked**: `api/`, `payments/`, `migrations/`, `database/`, `server/`

#### Statistical Engine
- `MIN_SAMPLE_SIZE = 100` per variant
- `SIGNIFICANCE_THRESHOLD = 0.05` (p < 0.05)
- Two-proportion Z-test with Abramowitz & Stegun CDF approximation
- `EXPERIMENT_DURATION_H = 48` hours before evaluation

### 12.2 `finops_engine.py` — V28 Autonomous FinOps Engine (18,618 bytes)

Cost-aware optimization. Profiles compute costs per transaction.

#### `FinOpsEngine` Class

| Method | Description |
|--------|-------------|
| `ingest_apm_report(report)` | Parses APM metrics, flags routes with margin decay > 15% |
| `calculate_unit_cost(route_data)` | CPU + memory + DB queries + bandwidth × provider pricing |
| `generate_refactor_epic(route_data)` | Writes REFACTOR_EPIC.md for expensive routes |
| `get_optimization_strategy(route_data)` | LLM generates optimization plan (raw SQL, caching, pagination) |
| `is_cacheable(route, context)` | Blocks caching of checkout, payment, balance, real-time data |
| `validate_provider(provider)` | Ensures provider is in approved list |
| `reset_baseline(route, new_cost)` | Updates baseline after successful optimization |
| `get_cost_summary(report)` | Generates cost summary for all routes |

#### Cloud Pricing Tiers
| Provider | Compute/GB·h | Memory/GB·h | Bandwidth/GB | DB Query/1000 |
|----------|-------------|-------------|--------------|---------------|
| Vercel | $0.18 | $0.09 | $0.15 | $0.004 |
| Railway | $0.20 | $0.10 | $0.10 | $0.005 |
| AWS | $0.085 | $0.047 | $0.09 | $0.003 |

#### Safety Guards
- **Traffic threshold**: `MIN_TRAFFIC_FOR_FINOPS = 1000` req/day
- **Caching bounds**: Blocks `checkout`, `payment`, `balance`, `real_time`, `exchange_rate`
- **Provider lockdown**: `APPROVED_DEPLOY_PROVIDERS = ["vercel", "railway"]`
- **Billing purity**: Excludes `interest`, `late_fee`, `penalty`, `credit_scheme`

---

## 13. Scheduler & Cron System

### 13.1 `scheduler.py` — Autonomous Task Scheduler (883 lines, 36,450 bytes)

Background job system that runs periodic tasks without human intervention.

#### `CronScheduler` Class

| Method | Description |
|--------|-------------|
| `add_job(name, action, interval_seconds)` | Registers a recurring job |
| `register_action(name, handler)` | Registers a handler function for an action |
| `tick()` | Main loop — checks due jobs, runs handlers, logs results |
| `remove_job(name)` | Removes a job from the schedule |

#### Registered Jobs

| Job | Action | Interval | Purpose |
|-----|--------|----------|---------|
| `telemetry_hud_update` | `_action_telemetry_hud_update` | 10s | Live system health dashboard |
| `experiment_watcher` | `_action_experiment_watcher` | 30s | Detects EXPERIMENT_EPIC.md |
| `hotfix_watcher` | `_action_hotfix_watcher` | 30s | Detects HOTFIX_EPIC.md |
| `refactor_watcher` | `_action_refactor_watcher` | 30s | Detects REFACTOR_EPIC.md |
| `workspace_index` | `_action_workspace_index` | 5m | Indexes workspace files |
| `telemetry_poll` | `_action_telemetry_poll` | 5m | Polls Vercel logs for errors |
| `budget_report` | `_action_budget_report` | 15m | Logs Gemini context budget |
| `rate_limit_report` | `_action_rate_limit_report` | 20m | Logs rate limit statistics |
| `context_compact` | `_action_context_compact` | 30m | Compacts session history |
| `failover_check` | `_action_failover_check` | 10m | Logs model failover status |
| `self_improvement` | `_action_self_improvement` | 60m | Gemini improvement suggestions |
| `metacognitive_review` | `_action_metacognitive_review` | 60m | Self-reflection and optimization |
| `experiment_evaluator` | `_action_experiment_evaluator` | 60m | Evaluates matured A/B experiments |
| `finops_monitor` | `_action_finops_monitor` | 60m | Profiles compute costs |
| `memory_consolidation` | `_action_memory_consolidation` | 2h | Consolidates episodic memory |
| `feature_request_watcher` | `_action_feature_request_watcher` | 30s | Detects FEATURE_EPIC.md |
| `feature_pipeline` | `_action_feature_pipeline` | 60m | Runs full qualitative synthesis pipeline |
| `user_injection_monitor` | `_action_user_injection_monitor` | 10s | Monitors for live user feedback injections during polish |

State persisted to `_cron_jobs.json`.

---

## 14. Self-Evolution Engine

### 14.1 `self_evolver.py` — V4 Omniscient God Loop (630 lines)

When the supervisor crashes catastrophically, this module reads its own source code, asks Gemini to diagnose and fix the bug, and rewrites itself.

#### Evolution Pipeline
1. **Read ALL Modules** — Reads source of every .py file in the package
2. **Read Crash Logs** — Last 50 lines of supervisor.log
3. **Include Screenshot** — Optional visual context of the crash state
4. **Council Pipeline** — 4 specialist agents analyze and fix:
   - DEBUGGER: Analyzes traceback + source for root cause
   - FIXER: Generates complete fixed source code
   - REVIEWER: Validates the fix
   - TESTER: Shadow sandbox integration test

#### Safety Mechanisms (Defense in Depth)

| Mechanism | Description |
|-----------|-------------|
| **Backup** | All files backed up to `_evolution_backups/` with timestamp before modification |
| **Pre-Write Syntax Check** | `compile(patched_code, target_file, "exec")` validates Python syntax BEFORE writing (line 530) |
| **Post-Write Syntax Check** | Reads written file back, re-compiles to verify disk write integrity (line 584) |
| **Size Check** | Rejects patches where output is <30% of original size or <50 chars (prevents Gemini returning truncated code) |
| **Balance-Brace JSON** | String-aware balanced-brace JSON extraction — handles nested `{` in Python code blocks that naive regex would truncate |
| **Shadow Sandbox** | Runs `mock_repo_tests.py` integration tests after write but before reboot (line 588) |
| **Auto-Rollback** | If ANY validation fails, restores from backup immediately via `_restore_from_backup()` |
| **Backup Pruning** | Keeps max 5 recent backups, prunes older ones to prevent disk bloat |
| **Exit Code 42** | Triggers `.bat` reboot after successful evolution — distinct from error exits |
| **Council Pipeline** | 4 specialist agents (Debugger → Fixer → Tester → Auditor) with REJECT gates at each stage. Falls back to single-call Gemini if council fails |

### 14.2 `self_healer.py` — Self-Healing Engine (15,527 bytes)

Lower-level self-healing for runtime errors:
- DOM failures → retries with different selectors
- CDP disconnections → reconnects
- Gemini timeouts → model failover
- Memory pressure → context pruning

### 14.3 `loop_detector.py` — Anti-Loop Protection (5,790 bytes)

Detects when the supervisor is stuck in a repetitive cycle:
- Tracks action history sliding window
- Computes action entropy
- If entropy drops below threshold → breaks loop with alternative strategy

### 14.4 `proactive_engine.py` — Proactive Intelligence (14,566 bytes)

Instead of waiting for failures, proactively identifies potential improvements:
- Scans code for anti-patterns
- Suggests optimizations
- Identifies missing tests
- Reports potential security issues
- **V30.3**: Browser preview opening via `subprocess.Popen` calling `open_browser.ps1` externally (no longer uses Command Palette keystrokes via Playwright, which caused DOM corruption)
- **V32**: `open_browser.ps1` updated: `Simple Browser: Show` → `Antigravity Browser Extension` (command name was changed), `-Url` parameter now mandatory (no more silent port 3000 default)

---

## 15. User Research & Polish Engines (V29)

### 15.1 `user_research_engine.py` — V29a Qualitative Synthesis Engine (749 lines, 32,098 bytes)

Listens to the voice of the user. Ingests raw customer feedback from support tickets, churn surveys, and in-app feedback widgets. Strips PII, clusters semantically, checks thresholds, validates against product vision, and auto-generates feature EPICs.

#### `UserResearchEngine` Class

| Method | Description |
|--------|-------------|
| `__init__(workspace_path)` | Initializes with workspace path, loads state and product vision |
| `_load_state()` / `_save_state()` | Persist feedback store and clusters to disk |
| `_load_vision()` | Loads PRODUCT_VISION.md if it exists |
| `redact_pii(text)` | Strips all PII: emails, phones, IPs, credit cards, SSNs, names |
| `ingest_webhook(payload)` | Ingests a single support ticket with PII redaction |
| `ingest_batch(tickets)` | Batch ingestion with summary stats |
| `cluster_feedback()` | LLM-powered semantic clustering of unclustered tickets |
| `check_thresholds()` | Identifies clusters exceeding FEATURE_THRESHOLD within rolling window |
| `check_vision_alignment(feature, desc)` | LLM gate: does this feature align with PRODUCT_VISION.md? |
| `generate_feature_epic(cluster_data)` | Writes FEATURE_EPIC.md for approved features |
| `feature_pipeline()` | Full pipeline: cluster → threshold → vision check → epic generation |

#### Safety Systems

| System | Description |
|--------|-------------|
| **PII Redaction** | Regex patterns for email, phone, IP, credit card, SSN, proper names |
| **Compliance Blocklist** | Blocks features related to lending, credit scoring, debt collection, binary options, prohibited integrations |
| **Feature Threshold** | Minimum unique users (default: 50) within rolling window (default: 90 days) |
| **Vision Gate** | LLM validates alignment with PRODUCT_VISION.md before epic generation |

### 15.2 `polish_engine.py` — V29b Infinite Polish Engine (519 lines, 22,169 bytes)

Transforms the Temporal Planner from fire-and-forget into a Socratic, user-confirmed iterative builder. The system refuses to commit until the user explicitly confirms satisfaction.

#### `PolishEngine` Class

| Method | Description |
|--------|-------------|
| `__init__(workspace_path)` | Initializes with workspace path, loads state |
| `_load_state()` / `_save_state()` | Persist polish session state to disk |
| `detect_ambiguity(prompt)` | Analyzes prompt for missing technical constraints; returns ambiguity score |
| `generate_clarification_mcq(prompt, dims)` | Generates multiple-choice questions to clarify user intent |
| `generate_preview_prompt(component, file)` | Generates live preview feedback request for chat injection |
| `is_termination(user_message)` | Detects satisfaction phrases: "perfect", "approved", "ship it", "lgtm", etc. |
| `is_change_request(user_message)` | Detects if user is requesting changes vs confirming |
| `inject_dag_node(dag_state, task, after)` | Dynamically injects new DAG nodes without breaking test locks |
| `compress_polish_context(chat_history)` | Aggressively compresses chat during polish loops |
| `check_user_injection()` | Monitors `.ag-memory/user_injection.txt` for mid-execution feedback |
| `start_polish_session(prompt, epic_id)` | Starts a new polish session |
| `record_iteration(feedback, changes)` | Records a polish iteration |
| `end_polish_session()` | Ends session and returns summary |
| `should_request_feedback(node_type, is_architectural)` | Decides when to solicit feedback (architectural moments, not every CSS change) |

#### Ambiguity Dimensions Checked
`color_palette`, `typography`, `layout_structure`, `responsive_behavior`, `animation_style`, `data_requirements`, `accessibility_targets`, `performance_budget`, `existing_patterns`

#### Termination Phrases
`perfect`, `approved`, `looks great`, `ship it`, `lgtm`, `all good`, `done`, `that's exactly right`, `this is perfect`, `no more changes`, `finalize`, `merge it`

#### Constants
- `SOFT_LIMIT_ITERATIONS = 20` — Maximum polish iterations before suggesting finalization
- `POLISH_CONTEXT_WINDOW = 5` — Messages retained during polish compression

---

## 16. Configuration Reference

### `selectors.json`

Cached DOM selectors discovered by the DOM prober:

```json
{
  "chat_input": "div[contenteditable='true'][role='textbox']",
  "chat_messages": ".interactive-result-editor",
  "approval_button": "a.monaco-button[role='button']"
}
```

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
| `SUPERVISOR_CDP_URL` | `http://localhost:9222` | Chrome DevTools Protocol endpoint |
| `GEMINI_CLI_CMD` | `gemini` | Gemini CLI binary name |
| `GEMINI_TIMEOUT` | `180` | Gemini CLI timeout in seconds |
| `GEMINI_RETRY_ATTEMPTS` | `3` | Max retry attempts per Gemini call |
| `GEMINI_RETRY_BASE_DELAY` | `2.0` | Base retry delay in seconds |
| `GEMINI_RETRY_MAX_DELAY` | `30.0` | Max retry delay in seconds |
| `DEPLOY_PROVIDER` | `vercel` | Deployment provider (vercel/railway) |
| `DEPLOY_TOKEN` | — | Deployment authentication token |
| `PROJECT_CWD` | `.` | Project working directory |
| `SUPERVISOR_VISION_POLL` | `30.0` | Vision polling interval seconds |
| `SUPERVISOR_POLL_INTERVAL` | `10.0` | Main loop polling interval seconds |
| `OLLAMA_VISION_MODEL` | `llava` | Local Ollama vision model name |
| `OLLAMA_VISION_TIMEOUT` | `30` | Ollama vision call timeout seconds |
| `PRESENCE_IDLE_THRESHOLD` | `300` | User idle threshold seconds |
| `PACING_MIN_MS` | `800` | Minimum pacing between actions |
| `PACING_MAX_MS` | `2500` | Maximum pacing between actions |
| `CONTEXT_BUDGET_WARN` | `500000` | Context budget warning threshold chars |
| `CONTEXT_BUDGET_MAX` | `2000000` | Context budget maximum chars |
| `SUPERVISOR_CTX_THRESHOLD` | `0.7` | Context confidence threshold |
| `SUPERVISOR_MIN_SS_INTERVAL` | `15.0` | Min seconds between screenshots |
| `SUPERVISOR_FORCE_SS_INTERVAL` | `120.0` | **V30.1: DEAD** — `needs_screenshot()` always returns False; interval is never checked |
| `SUPERVISOR_DEV_PORT` | `3000` | Dev server port to monitor |
| `SUPERVISOR_PROACTIVE` | `true` | Enable proactive mode |
| `SUPERVISOR_HEARTBEAT` | `60.0` | Heartbeat interval seconds |
| `SUPERVISOR_AUTO_BROWSER` | `true` | Auto-open Antigravity Browser Extension |
| `SUPERVISOR_LOG_LEVEL` | `INFO` | Logging level |

---

## 18. File & Directory Structure

```
supervisor/
├── __init__.py              # Package init
├── __main__.py              # python -m supervisor entry
├── main.py                  # Orchestrator (~2,985 lines)
├── config.py                # Central configuration (389 lines)
│
├── # IDE Automation
├── dom_prober.py            # Dynamic DOM discovery (552 lines)
├── injector.py              # Text injection engine
├── context_engine.py        # Deep context awareness (1,067 lines)
├── frame_walker.py          # Cross-frame navigation (384 lines)
├── monitor.py               # Visual monitoring
├── terminal.py              # Terminal control
├── command_resolver.py      # VS Code command engine
├── vision_optimizer.py      # Screenshot optimization
│
├── # Intelligence
├── gemini_advisor.py        # Centralized Gemini CLI (~1,112 lines)
├── retry_policy.py          # Retry + failover + budget (~915 lines)
├── local_orchestrator.py    # Local Ollama LLM manager (~448 lines)
├── external_researcher.py   # Web research agent
│
├── # Multi-Agent Council
├── agent_council.py         # The Million Dollar Team (1,324 lines)
├── council_knowledge.py     # Persistent knowledge base
│
├── # Memory
├── session_memory.py        # The Hippocampus (594 lines)
├── episodic_memory.py       # Long-term memory
├── memory_consolidation.py  # Memory consolidation
├── brain.py                 # Central brain module
│
├── # Planning & Execution
├── temporal_planner.py      # V19 DAG planner
├── workspace_transaction.py # Atomic workspace ops
├── cli_worker.py            # CLI command executor
├── workspace_indexer.py     # Workspace file index
│
├── # Verification & QA
├── autonomous_verifier.py   # V22 TDD verifier
├── visual_qa_engine.py      # V23 Visual QA
├── merge_arbiter.py         # Conflict resolution
├── reflection_engine.py     # Post-action reflection
│
├── # Security
├── compliance_gateway.py    # V24 compliance gateway
├── git_manager.py           # Git operations
│
├── # Deployment
├── deployment_engine.py     # V25 deployment pipeline
│
├── # Monitoring
├── telemetry_ingester.py    # V26 Ouroboros loop
├── telemetry_hud.py         # Live HUD dashboard
│
├── # Growth & Optimization
├── growth_engine.py         # V27 growth engine
├── finops_engine.py         # V28 FinOps engine
│
├── # Self-Evolution
├── self_evolver.py          # Self-modification engine
├── self_healer.py           # Runtime self-healing
├── loop_detector.py         # Anti-loop protection
├── proactive_engine.py      # Proactive intelligence
│
├── # Scheduling
├── scheduler.py             # Cron scheduler (883 lines, 18 jobs)
│
├── # V29: User Research & Polish
├── user_research_engine.py  # V29a Qualitative Synthesis (749 lines)
├── polish_engine.py         # V29b Infinite Polish (519 lines)
│
├── # Support
├── bootstrap.py             # Startup bootstrapper (generates SUPERVISOR_MANDATE.md + open_browser.ps1 with Antigravity Browser Extension command)
├── approver.py              # Approval button handler
├── chat_handler.py          # Chat message handler
├── agent_manager.py         # Agent lifecycle manager (V30.3: subprocess browser opening)
├── extensions.py            # Extension manager
├── skills_loader.py         # Skills file loader
│
├── # State Files
├── selectors.json           # Cached DOM selectors
├── _best_model_cache.json   # Cached Gemini model
├── _council_knowledge.json  # Council knowledge base
├── _cron_jobs.json          # Scheduler state
├── _failover_state.json     # Model failover state
├── _session_state.json      # Session memory
├── workspace_map.json       # Workspace index
├── supervisor.log           # Runtime log file
│
├── # Assets
├── ide_state.png            # Last IDE screenshot
├── command_palette_cache.json
├── help.txt                 # CLI help text
├── _palette_verify.png      # Command palette verification screenshot
└── _evolution_backups/      # Self-evolution backups
```

---

## 19. Version History

| Version | Codename | Key Feature |
|---------|----------|-------------|
| V1 | — | Basic Playwright-based IDE automation |
| V2 | — | DOM probing, chat injection |
| V3 | — | Workspace locking, auto-reconnect |
| V4 | — | Gemini CLI integration |
| V5 | — | Self-healing, loop detection |
| V6 | Unbreakable | 9 core systems: lockfile, approval sniper, mandate firewall |
| V7 | Background Mode | Non-interactive operation, auto-restart |
| V8 | — | File attachment support for Gemini |
| V9 | OpenClaw-Enhanced | Retry policy, model failover, context budget |
| V10 | — | Task complexity router, smart model routing |
| V11 | — | Rate limit tracker, probe-and-cache model |
| V12 | — | Time-travel engine (state snapshots) |
| V13 | — | Agent Council (6 specialist agents) |
| V14 | — | Self-evolution engine with shadow sandbox |
| V15 | — | Hard reset rollback mechanics |
| V16 | — | Deep context awareness engine |
| V17 | — | Workspace indexer |
| V18 | — | External researcher |
| V19 | Temporal Planner | DAG-based task planning and execution |
| V20 | — | Workspace transactions |
| V21 | — | Proactive engine, reflection engine |
| V22 | TDD Verifier | Autonomous test-driven development |
| V23 | Visual QA | Screenshot-based visual regression testing |
| V24 | Compliance Gateway | Security scanning, OWASP, license audit |
| V25 | Autonomous Deploy | Full release pipeline with health checks |
| V26 | Ouroboros Loop | Production telemetry, self-healing hotfixes |
| V27 | Growth Engine | A/B experiments, conversion optimization |
| V28 | FinOps Engine | Compute cost profiling, margin decay detection |
| V29a | Qualitative Synthesis | User feedback ingestion, PII redaction, semantic clustering, compliance blocklist, vision gate |
| V29b | Infinite Polish | Socratic pre-flight, live preview loops, DAG injection, user-confirmed termination |
| V30 | Total Automation | Route guard firewall, SUPERVISOR_MANDATE.md generator, open_browser.ps1 golden template, preview vs testing workflow separation |
| V30.1 | Vision Severance | `needs_screenshot()` always returns False, state detection 100% DOM/ARIA-driven, zero pixels processed for state checking |
| V30.2 | Autonomous Continuation | Local LLM auto-prompting with project state + mandate context, Total Automation mandate in config.py |
| V30.3 | Crash Recovery | Browser hijack kill (subprocess open_browser.ps1), boot hardening (reload+IDEBootError), UNKNOWN coma detector, WinError 5 retry |
| V30.4 | GPU + Quota + Toast | `--disable-gpu --disable-gpu-compositing` launch flags, `TerminalQuotaError` in rate limit patterns, Run All/Trust/Yes I trust toast selectors |
| V30.5 | Boot Resilience | `--no-sandbox --disable-dev-shm-usage` flags, smart quota time parser (`17m50s` → 1080s), stale failure cap, boot retry loop (no sys.exit on first failure), log append mode on auto-restart |
| V30.6 | Quota Intelligence | `report_timeout()` with 30s short cooldown (timeout ≠ failure), `TerminalQuotaError` parsing with exact cooldown extraction |
| V31 | Forensic Hardening | **9 fixes**: (1) Electron stderr capture via daemon thread, (2) `os._exit(1)` → graceful `_graceful_shutdown()` with log flush, (3) Outer safety timeout on `ask_gemini` retry loop, (4) Playwright cleanup before boot retry, (5) Model probe order fix (`auto` → position 2), (6) `ALL_MODELS_EXHAUSTED` smart cooldown from failover chain, (7) `--disable-gpu-compositing` → `--disable-software-rasterizer`, (8) Dead vision config deprecated, (9) Notification sniper page filtering (`chrome-extension://`, `devtools://`) |
| **V32** | **Major Leap Forward** | **13 fixes from forensic analysis of all 16 modules**: (1) `seconds_until_any_available()` implemented — was MISSING, (2) Gemini CLI `--model` flag, (3) open_browser.ps1 → Antigravity Browser Extension + mandatory -Url, (4) Self-evolver safety verified, (5) LocalManager lazy singleton + OllamaUnavailable, (6) Ollama `sys.exit` → exception, (7) Ollama `/api/ps` health check + timeout 300→120s, (8) Frame walker `wait_for_selector` auto-wait, (9) Dynamic multi-port dev server scan (7 ports) with session memory persistence, (10) Council `OllamaUnavailable` → cloud fallback, (11) Council already had asyncio.gather for Debugger+Architect, (12) Systematic print→logger pass (8 critical paths), (13) All `OllamaUnavailable` call sites handle graceful degradation |
| **V33** | **Boot Sequence Overhaul** | **11 fixes from forensic log analysis of 3 consecutive boot failures**: (1) 7-strategy progressive boot detection replacing single `.monaco-grid-view` selector that never matched Antigravity's DOM, (2) Page enumeration logging at boot (URL + title + frame count), (3) Removed destructive `page.reload()` recovery, (4) F1 probe scans ALL pages (not just selected), (5) `--disable-software-rasterizer` removed (unrecognized by Antigravity's Electron), (6) 12s blind `time.sleep` → 16s progressive poll, (7) Gemini CLI `-p` → positional prompt (v0.29+ canonical form), (8) Direct `contenteditable` injection as primary method (avoids F1 toggle), (9) `BOOT_DETECTION_STRATEGIES` + `BOOT_STRATEGY_TIMEOUT_MS` in config.py, (10) Fixed leftover `sys.exit(1)` in Ollama installer, (11) JS DOM size check + frame-walk as fallback boot strategies |
| **V33.1** | **Browser Preview Fix** | **5 fixes from first successful V33 boot run**: (1) Workspace lockdown page re-resolution — closing Launchpad page invalidated CDP page reference mid-injection, now re-resolves page after lockdown, (2) open_browser.ps1 `SendKeys("Antigravity Browser Extension")` → `SendKeys("Simple Browser: Show")` (the actual VS Code palette command), (3) All mandate text updated with explicit `open_browser.ps1 -Url` instructions so agents know HOW to open the preview, (4) Stale references cleaned across 5 files (main.py, config.py, bootstrap.py, proactive_engine.py, agent_manager.py), (5) Hijack reprimand updated |

---

> **Total**: 45+ Python modules, ~22,000 lines of code, 18 scheduled jobs, 6 specialist agents, 33+ version iterations.
>
> The Supervisor AI plans, builds, tests, secures, deploys, monitors, heals, grows, optimizes for profit, listens to users, and polishes until perfect — autonomously.
>
> **What it does for the user**: Takes a single high-level goal ("build a landing page", "fix the login bug", "make a game") and autonomously drives an AI IDE agent to completion. Injects prompts into the chat, clicks approvals, manages browser previews, recovers from IDE crashes, monitors dev servers across 7 ports, switches models on rate limits, and re-injects when the agent stalls. The user can walk away while the supervisor delivers working software.
>
> **What it can do to itself**: When the supervisor encounters a bug in its own code, it reads ALL of its source files, sends them to Gemini with the crash traceback, receives a fix, validates it with `py_compile` and a shadow sandbox, applies it, and reboots — all without human intervention. It has rewritten itself across 33+ versions.
>
> **External API Dependencies**:
> - **Gemini CLI** (v0.29.0, Feb 17 2026): Latest stable. Invoked via `--model` flag + positional prompt (`gemini --model M "prompt"`) for probing, stdin piping for long prompts. **V33: `-p` flag deprecated, positional args canonical.** Supports `--yolo` (auto-approve), `--output-format json`, GEMINI.md context files, Plan Mode, streaming tool calls, `/prompt-suggest`. Current probe list: `gemini-3.1-pro-preview` → `auto` → `gemini-3-pro-preview` → `gemini-2.5-pro` → `gemini-2.5-flash` → `gemini-2.0-flash`.
> - **Playwright** (2025/2026): Controls Antigravity IDE via CDP on `localhost:9222`. **V33: 7-strategy progressive boot detection** (div[id*=workbench] → contenteditable → monaco-grid-view → any input → JS DOM check → frame-walk → F1 probe ALL pages). Uses recursive frame walking for nested Electron webviews, `wait_for_selector` auto-wait (1.5s), DOM-based state detection (no screenshots). **V33: Direct contenteditable injection** as primary chat input method.
> - **Ollama HTTP API**: Local LLM on `localhost:11434`. Uses `/api/chat` with `format: "json"`, `stream: false`, 120s timeout. `/api/ps` health check before each request. Supports `/api/tags` for model discovery, auto-pulls missing models. Vision via `/api/chat` with base64 images.
>
> **Boot Sequence** (V33):
> 1. Kill stale Electron/Node processes (Zombie Hunter)
> 2. Launch Antigravity with `--new-window --remote-debugging-port=9222 --disable-gpu --no-sandbox --disable-dev-shm-usage`
> 3. Progressive 16s startup poll (8 × 2s) instead of blind 12s sleep
> 4. Connect via CDP → enumerate ALL pages (urls, titles, frame counts → log)
> 5. `_get_best_page()` selects primary page (jetski-agent > workbench > file:// > any)
> 6. 7-strategy progressive boot detection (8s per strategy, any match = OK)
> 7. 3s extension host binding wait
> 8. Direct contenteditable injection → Command Palette fallback → DOM fallback
>
> **Observability**:
> - **Glass Brain**: Color-coded ANSI console output (Cyan=prompt, Yellow=response, Red=error, Magenta=council, Green=success)
> - **Structured Logging**: `supervisor.log` with millisecond timestamps, module source, level. V32/V33 print→logger pass ensures all critical events are captured.
> - **Page Enumeration**: V33 logs every page's URL, title, and frame count at boot for diagnostics.
