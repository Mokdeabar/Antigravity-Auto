# Supervisor AI — Antigravity IDE Orchestrator

A fully autonomous Python system that takes a goal and delivers working software.
Tell it what to build, and it plans, codes, tests, and deploys — hands-free.

> **Architecture**: "Host Intelligence, Sandboxed Hands" — Gemini CLI runs on
> the host using your authenticated AI Ultra session. Docker sandbox is a dumb
> terminal with zero credentials. The V35 Command Centre UI is available at
> `http://localhost:8420`.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| **Python** | 3.11+ | Uses `asyncio`, type hints |
| **Docker** | Latest | Auto-installed if missing |
| **Gemini CLI** | latest | Must be callable from PATH as `gemini` |
| **Ollama** (optional) | latest | Local LLM for fast analysis |

---

## 1 — Install dependencies

```bash
cd "c:\Users\mokde\Desktop\Experiments\Antigravity Auto"
pip install -r requirements.txt
```

Docker must be installed before running the supervisor. If not found, you'll get
clear install instructions for your platform.

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
# Windows — auto-restarts on crash with exponential backoff
"Launch Supervisor.bat"
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

1. **Sandbox Manager** — Creates Docker containers, mounts workspace, manages lifecycle
2. **Headless Executor** — Runs Gemini CLI **on the host** (AI Ultra session), pushes changes into sandbox
3. **Tool Server** — File ops, shell, git, LSP, dev server — all via docker exec bridges
4. **Agent Council** — 6 specialist AI agents that diagnose, fix, test, and audit
5. **Path Translator** — Bidirectional host↔container path mapping

### Recovery strategies

If something fails, the supervisor escalates through:

1. **Restart Sandbox** — Destroy and recreate the container
2. **Switch Mount** — Toggle between `bind` and `copy` mount modes
3. **Rebuild Image** — Force rebuild the Docker image
4. **Self-Evolution** — Rewrite its own code to fix the bug

---

## 4 — Configuration

All tunables live in `supervisor/config.py`. Key environment variables:

| Env Var | Default | Description |
|---|---|---|
| `GEMINI_CLI_CMD` | `gemini` | Gemini CLI binary (**host only**) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint (passed to sandbox) |
| `OLLAMA_MODEL` | `llama3` | Local LLM model |
| `SANDBOX_MOUNT_MODE` | `copy` | `copy` (isolated, **default**) or `bind` (fast, opt-in) |
| `COMMAND_CENTRE_PORT` | `8420` | V35 Command Centre web UI port |
| `SUPERVISOR_LOG_LEVEL` | `INFO` | Logging level |

---

## 5 — Troubleshooting

| Problem | Solution |
|---|---|
| "Docker not found" | Install Docker Desktop manually, restart terminal |
| "Docker daemon not running" | Start Docker Desktop manually |
| Sandbox won't start | Run `docker ps -a` and `docker logs` |
| Gemini CLI timeout | Increase `GEMINI_TIMEOUT` env var |
| "Ollama unavailable" | Install Ollama or supervisor degrades gracefully |
| Rate limit errors | Supervisor auto-switches models; wait for cooldown |

---

## 6 — Full Documentation

See [SUPERVISOR_TECHNICAL_REFERENCE.md](SUPERVISOR_TECHNICAL_REFERENCE.md) for the complete technical reference (35+ modules, ~18,000 lines, 34 version iterations).
