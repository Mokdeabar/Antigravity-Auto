# Supervisor AI — Antigravity IDE Orchestrator

A fully autonomous Python script that babysits the Antigravity IDE agent.  
Tell it your goal, and it monitors, approves, unblocks, and refines — hands-free.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| **Python** | 3.11+ | Uses `asyncio`, type hints, `match` |
| **Playwright** | ≥ 1.40 | Installed via pip, browser binaries needed |
| **Gemini CLI** | latest | Must be callable from PATH as `gemini` |
| **Antigravity IDE** | — | Electron app, launched with debug port |

---

## 1 — Install dependencies

```bash
cd "c:\Users\mokde\Desktop\Experiments\Antigravity Auto"
pip install -r requirements.txt
playwright install chromium
```

> **Note:** `playwright install chromium` downloads the Chromium binary that
> Playwright needs for CDP connections. You only need to do this once.

---

## 2 — Launch Antigravity with the debug port

### Option A: Shortcut / Command line

Find where Antigravity is installed (e.g. `C:\Users\mokde\AppData\Local\Programs\antigravity\Antigravity.exe`) and launch it with:

```bash
"C:\path\to\Antigravity.exe" --remote-debugging-port=9222
```

### Option B: Create a Windows shortcut

1. Right-click your Antigravity shortcut → **Properties**
2. In the **Target** field, append ` --remote-debugging-port=9222` after the `.exe"` path
3. Click **OK**

### Option C: PowerShell one-liner

```powershell
Start-Process "C:\path\to\Antigravity.exe" -ArgumentList "--remote-debugging-port=9222"
```

> **Important:** The `--remote-debugging-port=9222` flag must be present or the
> Supervisor cannot connect. You can verify it's working by opening
> `http://localhost:9222/json/version` in a browser — you should see a JSON blob.

---

## 3 — Run the Supervisor

```bash
python -m supervisor --goal "Build a beautiful landing page with animations"
```

### CLI options

| Flag | Description |
|---|---|
| `--goal` / `-g` | **(required)** Your ultimate goal |
| `--dry-run` | Skip CDP connection; print what would happen |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default: `INFO`) |

### Example commands

```bash
# Simple goal
python -m supervisor -g "Create a React dashboard with authentication"

# Verbose logging
python -m supervisor -g "Fix all failing tests" --log-level DEBUG

# Dry-run (no Antigravity needed)
python -m supervisor -g "Test the supervisor" --dry-run
```

---

## 4 — How it works

```
┌──────────────────────────────────────────────────┐
│                 SUPERVISOR AI                     │
│                                                   │
│   ┌─────────┐  ┌──────────┐  ┌──────────────┐   │
│   │  Eyes   │  │  Hands   │  │ Loop Detector│   │
│   │ monitor │→ │ approver │  │              │   │
│   └────┬────┘  └──────────┘  └──────┬───────┘   │
│        │                            │             │
│        ▼                            ▼             │
│   ┌─────────┐              ┌──────────────┐      │
│   │ Injector│  ◄──────────  │    Brain     │      │
│   │         │              │ (Gemini CLI) │      │
│   └─────────┘              └──────────────┘      │
└──────────────────────────────────────────────────┘
         │                         ▲
         ▼                         │
    ┌─────────────────────────────────┐
    │        ANTIGRAVITY IDE          │
    │    (CDP on localhost:9222)       │
    └─────────────────────────────────┘
```

1. **Eyes** (`monitor.py`) — Connects via CDP, scrapes chat messages, finds buttons
2. **Hands** (`approver.py`) — Auto-clicks Approve / Allow / Run buttons
3. **Loop Detector** (`loop_detector.py`) — Tracks last 5 messages, catches repeating errors
4. **Brain** (`brain.py`) — Calls local `gemini` CLI (zero API cost) to generate fix prompts
5. **Injector** (`injector.py`) — Types responses into the chat and sends them

### Loop detection rules

- Same message repeated **2×** → loop
- Same error substring **3×** in window → loop
- Same tool failure **2× in a row** → immediate pivot (no retry)
- **5 interventions** on the same error → 🚨 human escalation (audible alert + pause)

---

## 5 — Configuration

All tunables live in `supervisor/config.py`. You can also override key values
via environment variables:

| Env Var | Default | Description |
|---|---|---|
| `SUPERVISOR_CDP_URL` | `http://localhost:9222` | CDP endpoint |
| `GEMINI_CLI_CMD` | `gemini` | Path/name of the Gemini CLI binary |
| `GEMINI_TIMEOUT` | `120` | Seconds to wait for Gemini response |
| `SUPERVISOR_POLL_INTERVAL` | `3.0` | Seconds between poll cycles |
| `SUPERVISOR_LOG_LEVEL` | `INFO` | Logging verbosity |

### Tuning DOM selectors

If the Supervisor can't find buttons or the chat input, you'll need to update
the CSS selectors in `supervisor/config.py`. Open Antigravity's DevTools
(`Ctrl+Shift+I`) and inspect the chat UI to find the correct selectors.

---

## 6 — Troubleshooting

| Problem | Solution |
|---|---|
| "No browser contexts found" | Antigravity isn't running or not launched with `--remote-debugging-port=9222` |
| "Could not find chat input" | Update `CHAT_INPUT_SELECTOR` in `config.py` |
| Buttons not being clicked | Update `APPROVAL_BUTTON_TEXTS` in `config.py` |
| Gemini CLI timeout | Increase `GEMINI_TIMEOUT` env var |
| "Failed to connect" | Check `http://localhost:9222/json/version` in browser |
