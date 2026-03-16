"""
api_server.py — V65 Command Centre API Bridge.

FastAPI server running as a background asyncio.Task inside the same
event loop as the supervisor engine. This is a THIN COMMUNICATION LAYER —
it does not drive execution, it reflects engine state.

Architecture:
    ┌──────────────────────────────────┐
    │  Supervisor Engine (async loop)   │ ← runs forever
    │  shares SupervisorState singleton │
    ├──────────────────────────────────┤
    │  API Bridge (FastAPI, same loop)  │ ← reflects state
    │  WebSocket /ws                    │ ← Glass Brain stream
    │  REST /api/*                      │ ← state queries
    │  Static /                         │ ← serves UI files
    └──────────────────────────────────┘

    Closing the browser tab has ZERO impact on the engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .instruction_queue import InstructionQueue
from . import config

logger = logging.getLogger("supervisor.api_server")

# UI static files directory
_UI_DIR = Path(__file__).parent / "ui"


# ─────────────────────────────────────────────────────────────
# Supervisor State — Shared singleton between engine + API
# ─────────────────────────────────────────────────────────────

# V53: Warmup psutil CPU sampler at import time so interval=None returns real
# values from the very first to_dict() call (not 0.0 cold-start).
try:
    import psutil as _psutil_seed
    _psutil_seed.cpu_percent(interval=0.05)
    del _psutil_seed
except Exception:
    pass

class SupervisorState:

    """
    Thread-safe shared state between the supervisor engine and the API.

    The ENGINE writes to this object.
    The API reads from it and exposes via REST/WebSocket.
    """

    def __init__(self, goal: str = "", project_path: str = ""):
        # Core state
        self.goal = goal
        self.project_path = project_path
        self.status = "initializing"  # initializing, executing, monitoring, error, stopped
        self.active_model = "unknown"
        self.container_id = ""
        self.container_health = "unknown"
        self.mount_mode = "copy"
        self.uptime_start = time.time()
        self.loop_count = 0
        self.last_action = ""
        self.last_action_time = 0.0

        # Preview
        self.preview_port = 0
        self.preview_running = False

        # Instruction queue
        self.queue = InstructionQueue()

        # Log ring buffer (last 500 entries for the Glass Brain)
        self._log_buffer: deque[dict] = deque(maxlen=2000)

        # WebSocket clients
        self._ws_clients: list[WebSocket] = []
        # V46: Serialize all WS writes — concurrent send_text() calls
        # crash the websockets library's drain mechanism.
        self._ws_write_lock = asyncio.Lock()

        # V64: Gemini Lite Intelligence (replaces Ollama)
        self.lite_intelligence_online = True  # Always available — uses Gemini CLI

        # Task result
        self.last_task_status = ""
        self.last_task_duration = 0.0
        self.files_changed: list[str] = []
        self.tasks_completed: int = 0
        self.error_count: int = 0

        # V44: Change tracking & activity timeline
        self.change_history: list[dict] = []   # [{path, action, ts, chunk_id}]
        self.activity_timeline: list[dict] = []  # [{ts, type, desc, detail}]

        # Engine launch state (for project launcher)
        self.engine_running = False
        self._run_callback = None  # set by main.py to start the engine

        # V38.1: Safe stop flag — set via /api/stop, checked by main loop
        self.stop_requested = False
        # V46: Force stop — cancel workers immediately instead of draining
        self.force_stop = False
        self._active_worker_count = 0

        # V42: Continuous audit loop — when True, audits re-run after each cycle
        self.audit_loop_enabled = True
        self.audit_cycle = 0

        # V51: Fresh Audit — when set, discards old DAG and re-audits codebase
        self.fresh_audit = False

        # V54: Reset Phases — when set, deletes phase_state.json so Gemini re-plans from scratch
        self.reset_phases = False

        # V40: Active planner reference — set by DAG execution, used by instruction injection
        self.planner = None

        # V37 FIX (M-6): Use env var instead of hardcoded user-specific path.
        exp_dir = os.environ.get("SUPERVISOR_EXPERIMENTS_DIR", "").strip().strip('"').strip("'")
        self.experiments_dir = Path(
            exp_dir or Path.home() / "Desktop" / "Experiments"
        )

        # V43: Quota status (populated from DailyBudgetTracker each broadcast)
        self.quota_status: dict = {}

        # V44: Execution mode — "auto" (fully autonomous) or "manual" (waits for user prompts)
        self.execution_mode: str = "auto"

        # V44: Pause-on-quota — if True, stop when rate limits hit instead of waiting
        self.pause_on_quota: bool = True
        # V70/V73: Quota pause mode — 'off', 'pro', or 'all' (default: pro)
        self.quota_pause_mode: str = "pro"

        # V74: Load persisted UI preferences
        self._load_ui_prefs()

        # V51: Cooldown countdown — seconds remaining until next model available
        self.cooldown_remaining: float = 0

        # V52: Dev server restart request and error tracking
        self.restart_dev_server_requested: str = ""  # "restart" or "reinstall"
        self.dev_server_error: str = ""  # Last dev server error for UI display

        # V52: Model status for UI display at boot and during execution
        self.model_status: dict = {}  # {"models": {...}, "active_model": "...", "cooldown_remaining": 0}

        # V46: Broadcast debounce — coalesce rapid-fire calls into one per 2s
        self._last_broadcast_ts: float = 0.0
        self._broadcast_pending: bool = False

        # V55: Current operation — displayed as animated pill in the dashboard header.
        # Set to a short human-readable string while any long-running operation is active.
        # Empty string = idle (pill hidden).
        self.current_operation: str = ""

        # V55: Session complete — True when the DAG finishes naturally (audit clean pass)
        # WITHOUT the user pressing Stop. Keeps the dashboard live so they can inspect
        # the preview. The shutdown screen only appears on explicit Stop.
        self.session_complete: bool = False

        # V73: Feature flags for UI components
        self.flags: dict[str, bool] = {
            "enable-dashboard-csv-export": True,
        }

    @property
    def uptime_s(self) -> float:
        return time.time() - self.uptime_start

    def record_change(self, path: str, action: str = "modified", chunk_id: str = ""):
        """V44: Log a file change event for the Changes tab."""
        self.change_history.append({
            "path": path, "action": action,
            "ts": time.time(), "chunk_id": chunk_id,
        })
        # Keep last 500
        if len(self.change_history) > 500:
            self.change_history = self.change_history[-500:]
        # V53: Also update files_changed list so the telemetry counters in the
        # sidebar reflect real activity (deduplicated, capped to last 1000).
        if path not in self.files_changed:
            self.files_changed.append(path)
            if len(self.files_changed) > 1000:
                self.files_changed = self.files_changed[-1000:]

    def record_task_complete(self) -> None:
        """V53: Increment tasks_completed counter (call from worker on success)."""
        self.tasks_completed += 1

    def record_task_error(self) -> None:
        """V53: Increment error_count counter (call from worker on failure)."""
        self.error_count += 1

    def set_current_operation(self, op: str) -> None:
        """V55: Set (or clear) the current long-running operation label shown in the UI header.

        Call with a short human-readable string when starting a slow operation
        (e.g. '🔍 Build health check', '📦 npm install', '⬆ Upgrading deps').
        Call with empty string (or None) when the operation completes.
        """
        self.current_operation = op or ""
        # Immediate broadcast so the UI updates without waiting for the normal 2s debounce
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast_state())
        except RuntimeError:
            pass

    def record_activity(self, event_type: str, desc: str, detail: str = ""):
        """V44: Log a timeline event. V46: Also triggers debounced broadcast."""
        self.activity_timeline.append({
            "ts": time.time(), "type": event_type,
            "desc": desc, "detail": detail,
        })
        if len(self.activity_timeline) > 500:
            self.activity_timeline = self.activity_timeline[-500:]
        # V46: Trigger debounced broadcast so UI sees activity in real-time
        self._schedule_broadcast()

    def log(self, level: str, message: str, **extra) -> None:
        """Add a log entry and broadcast to all WebSocket clients."""
        entry = {
            "type": "log",  # V46: Explicit type for client-side handling
            "ts": time.time(),
            "level": level,
            "msg": message,
            **extra,
        }
        self._log_buffer.append(entry)

        # V74: Auto-persist to session_log.jsonl
        self._persist_log(entry)

        # V45: Robust async broadcast — handle missing/closed event loops
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._broadcast(entry))
        except RuntimeError:
            # No running loop (called from sync context or background thread)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(
                        lambda e=entry: asyncio.ensure_future(self._broadcast(e))
                    )
            except Exception:
                pass  # Buffer it — will be sent on next WS connect via init payload

    def _persist_log(self, entry: dict) -> None:
        """V74: Persist log entry to session_log.jsonl with 5MB rotation."""
        if not self.project_path:
            return
        try:
            log_dir = Path(self.project_path) / ".ag-supervisor"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "session_log.jsonl"

            # Rotate if > 5MB
            if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
                rotated = log_path.with_suffix(f".{int(time.time())}.jsonl")
                log_path.rename(rotated)

            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass  # Never block on logging

    def _load_ui_prefs(self) -> None:
        """V74: Load persisted UI preferences from .ag-supervisor/ui_prefs.json."""
        if not self.project_path:
            return
        try:
            prefs_path = Path(self.project_path) / ".ag-supervisor" / "ui_prefs.json"
            if prefs_path.exists():
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                if "execution_mode" in prefs and prefs["execution_mode"] in ("auto", "manual"):
                    self.execution_mode = prefs["execution_mode"]
                if "quota_pause_mode" in prefs and prefs["quota_pause_mode"] in ("off", "pro", "all"):
                    self.quota_pause_mode = prefs["quota_pause_mode"]
                    self.pause_on_quota = self.quota_pause_mode != "off"
        except Exception:
            pass

    def _save_ui_prefs(self) -> None:
        """V74: Persist UI preferences to .ag-supervisor/ui_prefs.json."""
        if not self.project_path:
            return
        try:
            prefs_dir = Path(self.project_path) / ".ag-supervisor"
            prefs_dir.mkdir(parents=True, exist_ok=True)
            prefs = {
                "execution_mode": self.execution_mode,
                "quota_pause_mode": self.quota_pause_mode,
                "saved_at": time.time(),
            }
            (prefs_dir / "ui_prefs.json").write_text(
                json.dumps(prefs, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _schedule_broadcast(self) -> None:
        """V46: Schedule a debounced broadcast. V74: Adaptive interval."""
        if self._broadcast_pending:
            return  # Already scheduled
        now = time.time()
        # V74: Adaptive debounce — faster during active execution
        if self.status == "executing":
            interval = 0.5
        elif self.status == "initializing":
            interval = 1.0
        else:
            interval = 2.0
        delay = max(0, interval - (now - self._last_broadcast_ts))
        self._broadcast_pending = True
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(delay, lambda: loop.create_task(self._do_debounced_broadcast()))
        except RuntimeError:
            self._broadcast_pending = False

    async def _do_debounced_broadcast(self) -> None:
        """V46: Execute the debounced broadcast."""
        self._broadcast_pending = False
        self._last_broadcast_ts = time.time()
        await self.broadcast_state()

    async def _broadcast(self, data: dict) -> None:
        """Push data to all connected WebSocket clients.
        
        V46: Serialized with asyncio.Lock — concurrent send_text() calls
        on the same WS connection crash the websockets drain mechanism.
        """
        if not self._ws_clients:
            return
        payload = json.dumps(data)
        async with self._ws_write_lock:
            dead = []
            for ws in self._ws_clients:
                try:
                    await ws.send_text(payload)
                except Exception as _ws_err:
                    logger.debug("🔌 WS send failed (removing client): %s", type(_ws_err).__name__)
                    dead.append(ws)
            for ws in dead:
                try:
                    self._ws_clients.remove(ws)
                except ValueError:
                    pass

    async def broadcast_state(self) -> None:
        """Push full state snapshot to all WebSocket clients."""
        await self._broadcast({
            "type": "state",
            **self.to_dict(),
        })

    @property
    def dag_progress(self) -> dict:
        """V38.1: Read live DAG progress from the execution engine."""
        try:
            from .main import _dag_progress
            return _dag_progress
        except Exception:
            return {"active": False}

    def to_dict(self) -> dict:
        # V43: Refresh quota status from DailyBudgetTracker
        try:
            from .retry_policy import get_daily_budget
            self.quota_status = get_daily_budget().get_status()
        except Exception:
            pass

        # V53: Refresh model status from failover chain on every broadcast
        # so the UI always has live availability data (not stale boot-time data)
        try:
            from .retry_policy import get_failover_chain
            _fc = get_failover_chain()
            self.model_status = _fc.get_status()
            # V62: Only use failover chain as FALLBACK — don't overwrite
            # active_model if the engine/executor already set it to the
            # model currently in use. Overwriting every 2s was causing the
            # UI pill to show the wrong model.
            _fc_model = _fc.get_active_model()
            if _fc_model and self.active_model in ("unknown", ""):
                self.active_model = _fc_model
            _cd = _fc.seconds_until_any_available()
            if _cd > 0:
                self.cooldown_remaining = _cd
            elif self.status == "cooldown":
                self.cooldown_remaining = 0
        except Exception:
            pass

        # V62: Refresh quota probe snapshot from CLI output parser
        _quota_probe_snapshot = {}
        try:
            from .retry_policy import get_quota_probe
            _quota_probe_snapshot = get_quota_probe().get_quota_snapshot()
        except Exception:
            pass

        # V53: Refresh system health (CPU, memory, disk) — cached to avoid
        # blocking the broadcast coroutine with psutil's interval-based sampler.
        # The cache is populated by _refresh_system_health() called every 5s.
        try:
            import psutil as _ps
            # Use non-blocking percent (interval=None uses last cached reading)
            _cpu = _ps.cpu_percent(interval=None)
            _mem = _ps.virtual_memory()
            _disk = _ps.disk_usage('/')
            self._system_health = {
                "cpu_percent": round(_cpu, 1),
                "memory_percent": round(_mem.percent, 1),
                "memory_used_mb": round(_mem.used / 1024 / 1024),
                "memory_total_mb": round(_mem.total / 1024 / 1024),
                "disk_percent": round(_disk.percent, 1),
                "disk_used_gb": round(_disk.used / 1024 / 1024 / 1024, 1),
                "disk_total_gb": round(_disk.total / 1024 / 1024 / 1024, 1),
            }
        except Exception:
            if not hasattr(self, '_system_health'):
                self._system_health = {"cpu_percent": 0, "memory_percent": 0,
                                       "memory_used_mb": 0, "memory_total_mb": 0,
                                       "disk_percent": 0, "disk_used_gb": 0, "disk_total_gb": 0}

        return {
            "goal": self.goal,
            "project_path": self.project_path,
            "status": self.status,
            "engine_running": self.engine_running,
            "active_model": self.active_model,
            "container_id": self.container_id,
            "container_health": self.container_health,
            "mount_mode": self.mount_mode,
            "uptime_s": round(self.uptime_s, 1),
            "loop_count": self.loop_count,
            "last_action": self.last_action,
            "preview_port": self.preview_port,
            "preview_running": self.preview_running,
            "ollama_online": self.lite_intelligence_online,  # V64: compatibility alias
            "ollama_model": "gemini-lite",  # V64: compatibility alias
            "lite_intelligence_online": self.lite_intelligence_online,
            "last_task_status": self.last_task_status,
            "last_task_duration": round(self.last_task_duration, 1),
            "files_changed": self.files_changed,
            "files_changed_count": len(self.files_changed),
            "tasks_completed": self.tasks_completed,
            "error_count": self.error_count,
            "queue_depth": self.queue.size,
            "queue_size": self.queue.size,
            "dag": self.dag_progress,
            "stop_requested": self.stop_requested,
            "audit_loop_enabled": self.audit_loop_enabled,
            "audit_cycle": self.audit_cycle,
            "changes_count": len(self.change_history),
            "timeline_count": len(self.activity_timeline),
            "change_history": self.change_history[-100:],
            "activity_timeline": self.activity_timeline[-100:],
            # V43: Quota management stats
            "quota": self.quota_status,
            # V44: Execution mode
            "execution_mode": self.execution_mode,
            "pause_on_quota": self.pause_on_quota,
            "quota_pause_mode": self.quota_pause_mode,
            # V51: Cooldown countdown for UI timer
            "cooldown_remaining": round(self.cooldown_remaining, 0),
            # V52: Dev server error for UI
            "dev_server_error": self.dev_server_error,
            # V52: Model status for launcher/dashboard
            "model_status": self.model_status,
            # V53: Live system health in every broadcast
            "system_health": getattr(self, '_system_health', {}),
            # V55: Current operation label for header activity pill
            "current_operation": self.current_operation,
            # V55: Session naturally complete — keeps dashboard live until user presses Stop
            "session_complete": self.session_complete,
            # V62: Live per-model quota probe data from CLI output
            "quota_probe": _quota_probe_snapshot,
            # V73: Centralized version — UI reads this instead of hardcoding
            "supervisor_version": config.SUPERVISOR_VERSION_LABEL,
            # V73: UI Feature Flags
            "flags": self.flags,
        }


# ─────────────────────────────────────────────────────────────
# WebSocket Log Handler — pipes Python logging to Glass Brain
# ─────────────────────────────────────────────────────────────

class WebSocketLogHandler(logging.Handler):
    """Logging handler that feeds log records into the SupervisorState ring buffer.
    
    V45: Attaches to the root logger so ALL Python logs (supervisor.*,
    uvicorn, etc.) are captured and sent to the Glass Brain Logs tab.
    """

    def __init__(self, state: SupervisorState):
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord):
        # Skip noisy internal loggers that would flood the UI
        if record.name.startswith(("uvicorn", "websockets", "asyncio")):
            return
        try:
            self.state.log(
                level=record.levelname,
                message=self.format(record),
                logger=record.name,
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# FastAPI Application
# ─────────────────────────────────────────────────────────────

def create_app(state: SupervisorState) -> FastAPI:
    """Create the FastAPI application with the shared state."""

    app = FastAPI(
        title=f"Supervisor AI — {config.SUPERVISOR_VERSION_LABEL} Command Centre",
        version=config.SUPERVISOR_VERSION_FULL,
        docs_url=None,  # Disable Swagger — this is a UI server
        redoc_url=None,
    )

    # V37 SECURITY FIX (H-7): Restrict CORS to localhost only.
    # Previously allow_origins=["*"] let any origin on the LAN inject instructions.
    # V74: Removed "null" origin — it's sent by file:// pages and poses a
    # security risk. The Command Centre is served by the API itself (localhost).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:8420",
            "http://127.0.0.1:8420",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # V74: Simple per-IP rate limiting (§10) — 60 req/min, 429 on exceed
    _rate_limit_store: dict[str, list[float]] = {}
    _RATE_LIMIT_MAX = 60
    _RATE_LIMIT_WINDOW = 60.0  # seconds

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        """V74: Basic rate limiting — 60 requests per minute per client IP."""
        # Skip rate limiting for WebSocket upgrades and static files
        if request.url.path.startswith("/ws") or request.url.path.startswith("/ui/"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Clean up old entries
        if client_ip in _rate_limit_store:
            _rate_limit_store[client_ip] = [
                t for t in _rate_limit_store[client_ip]
                if now - t < _RATE_LIMIT_WINDOW
            ]
        else:
            _rate_limit_store[client_ip] = []

        if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT_MAX:
            return JSONResponse(
                {"error": "Rate limit exceeded. Max 60 requests per minute."},
                status_code=429,
            )

        _rate_limit_store[client_ip].append(now)
        return await call_next(request)

    # ── Global Exception Handler ──────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        import traceback
        tb = traceback.format_exc()
        logger.error("Unhandled API error on %s %s: %s\n%s", request.method, request.url.path, exc, tb)
        # V42/V74 DEBUG: Write to state dir (not inside package source)
        try:
            err_log = config.get_state_dir() / "api_errors.log"
            with open(err_log, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"[{time.strftime('%H:%M:%S')}] {request.method} {request.url.path}\n")
                f.write(f"Error: {exc}\n")
                f.write(tb)
        except Exception:
            pass
        return JSONResponse({"error": str(exc)}, status_code=500)

    # ── REST Endpoints ──────────────────────────────────────

    @app.get("/api/ping")
    async def server_ping():
        """V42: Simple ping — always succeeds if server is up."""
        return JSONResponse({
            "ok": True,
            "experiments_dir": str(state.experiments_dir),
            "experiments_dir_exists": state.experiments_dir.is_dir(),
        })

    @app.get("/api/state")
    async def get_state():
        """Return current supervisor state."""
        return JSONResponse(state.to_dict())

    @app.get("/api/dag")
    async def get_dag_progress():
        """V38.1: Return live DAG decomposition progress."""
        return JSONResponse(state.dag_progress)

    @app.post("/api/dag/retry")
    async def retry_dag_task(body: dict):
        """V40: Manually retry a failed DAG task from the UI."""
        task_id = body.get("task_id", "").strip()
        if not task_id:
            return JSONResponse({"error": "No task_id provided"}, status_code=400)
            
        if not state.planner:
            return JSONResponse({"error": "No active DAG planner"}, status_code=400)
            
        # Use force=True to reset the retry counter back to 0
        success = state.planner.mark_retry(task_id, force=True)
        if success:
            logger.info("🛠️  [API] User manually requeued failed task: %s", task_id)
            state.record_activity("user", f"Manually retried failed task: {task_id}")
            return JSONResponse({"status": "requeued", "task_id": task_id})
        else:
            return JSONResponse({"error": f"Could not requeue task {task_id} (not failed or not found)"}, status_code=400)

    @app.post("/api/dag/cancel")
    async def cancel_dag_task(body: dict):
        """V70: Cancel a running DAG task from the UI."""
        task_id = body.get("task_id", "").strip()
        if not task_id:
            return JSONResponse({"error": "No task_id provided"}, status_code=400)

        if not state.planner:
            return JSONResponse({"error": "No active DAG planner"}, status_code=400)

        # Check that the task actually exists and is cancellable
        node = state.planner._nodes.get(task_id)
        if not node:
            return JSONResponse({"error": f"Task {task_id} not found"}, status_code=404)
        if node.status not in ("running", "queued", "pending"):
            return JSONResponse({"error": f"Task {task_id} is {node.status}, cannot cancel"}, status_code=400)

        if node.status == "pending":
            # Pending tasks have no asyncio worker — mark cancelled directly
            node.status = "cancelled"
            node.started_at = None
            try:
                state.planner._save_state()
            except Exception:
                pass
            logger.info("⛔  [API] User cancelled pending task: %s", task_id)
            state.record_activity("user", f"Cancelled pending task: {task_id}")
            return JSONResponse({"status": "cancelled", "task_id": task_id})

        # Running/queued — signal the scheduling loop to cancel the asyncio task
        if not hasattr(state, '_cancel_task_ids'):
            state._cancel_task_ids = set()
        state._cancel_task_ids.add(task_id)
        logger.info("⛔  [API] User requested cancel for task: %s", task_id)
        state.record_activity("user", f"Cancel requested: {task_id}")
        return JSONResponse({"status": "cancelling", "task_id": task_id})

    @app.get("/api/dag/history")
    async def get_dag_history():
        """V41: Return full DAG run history from dag_history.jsonl."""
        import json as _json
        entries = []
        try:
            project_path = state.project_path or os.getcwd()
            hist_path = os.path.join(project_path, "dag_history.jsonl")
            if not os.path.exists(hist_path):
                # Try .ag-supervisor subdirectory
                hist_path = os.path.join(project_path, ".ag-supervisor", "dag_history.jsonl")
            if os.path.exists(hist_path):
                with open(hist_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entries.append(_json.loads(line))
                            except Exception:
                                continue
        except Exception as exc:
            logger.debug("DAG history read failed: %s", exc)
        return JSONResponse({"runs": entries})

    @app.get("/api/phases")
    async def get_phases():
        """Return phased project plan from phase_state.json for the Phases tab."""
        import json as _json
        try:
            project_path = state.project_path or os.getcwd()
            plan_path = os.path.join(project_path, ".ag-supervisor", "phase_state.json")
            if not os.path.exists(plan_path):
                return JSONResponse({"available": False, "phases": [], "current_phase": 1})
            raw = open(plan_path, "r", encoding="utf-8").read()
            plan = _json.loads(raw)
            # Enrich each phase with computed completion stats
            for ph in plan.get("phases", []):
                tasks = ph.get("tasks", [])
                total  = len(tasks)
                done   = sum(1 for t in tasks if t.get("status") == "done")
                review = sum(1 for t in tasks if t.get("status") == "needs_review")
                ph["_stats"] = {
                    "total":   total,
                    "done":    done,
                    "review":  review,
                    "pending": total - done - review,
                    "pct":     round(done / total * 100) if total else 0,
                }
            return JSONResponse({"available": True, **plan})
        except Exception as exc:
            logger.debug("Phase plan read failed: %s", exc)
            return JSONResponse({"available": False, "phases": [], "current_phase": 1})

    @app.post("/api/stop")
    async def request_stop():
        """V38.1: Gracefully request the supervisor to stop after current task.
        V46: Also disables audit loop and sends worker count for UI prompt."""
        state.stop_requested = True
        state.audit_loop_enabled = False  # V46: Audit must not run during shutdown
        # V55: Cancel any in-flight Gemini subprocess immediately
        try:
            from .gemini_advisor import set_gemini_stop as _gs
            _gs(True)
        except Exception:
            pass
        logger.info("🛑  [API] Safe stop requested via Command Centre.")

        # V46: Count active workers so UI can prompt force vs graceful
        _n_workers = getattr(state, '_active_worker_count', 0)
        await state._broadcast({
            "type": "stop_confirm",
            "workers": _n_workers,
            "msg": f"Shutdown requested. {_n_workers} worker(s) still active." if _n_workers > 0
                   else "Shutting down — no active workers."
        })
        return JSONResponse({
            "status": "stop_requested",
            "workers": _n_workers,
            "msg": f"Stop requested. {_n_workers} active worker(s).",
        })

    @app.post("/api/stop/force")
    async def request_force_stop():
        """V46: Immediately cancel all workers and shut down."""
        state.stop_requested = True
        state.force_stop = True
        state.audit_loop_enabled = False
        # V55: Cancel any in-flight Gemini subprocess immediately
        try:
            from .gemini_advisor import set_gemini_stop as _gs
            _gs(True)
        except Exception:
            pass
        logger.info("🛑  [API] FORCE stop requested — cancelling all workers immediately.")
        state.record_activity("system", "Force stop: cancelling all active workers NOW")
        await state._broadcast({"type": "force_stop"})
        await state.broadcast_state()
        return JSONResponse({"status": "force_stopped", "msg": "All workers being cancelled."})

    @app.post("/api/audit/stop")
    async def stop_audit_loop():
        """V42: Stop the continuous audit loop after the current cycle."""
        state.audit_loop_enabled = False
        logger.info("⏸  [API] Audit loop stop requested via Command Centre.")
        state.record_activity("system", "Audit loop will stop after current cycle completes")
        await state.broadcast_state()
        return JSONResponse({"status": "audit_loop_stopping", "msg": "Audits will stop after the current cycle."})

    @app.post("/api/restart-dev-server")
    async def restart_dev_server(request: Request):
        """V52: Request dev server restart. Main loop picks this up."""
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        mode = body.get("mode", "restart")  # "restart" or "reinstall"
        state.restart_dev_server_requested = mode
        state.dev_server_error = ""  # Clear error on manual restart
        logger.info("🖥️  [API] Dev server %s requested via Command Centre.", mode)
        state.record_activity("system", f"Dev server {mode} requested")
        await state.broadcast_state()
        return JSONResponse({"status": "requested", "mode": mode, "msg": f"Dev server {mode} queued."})

    @app.get("/api/model-status")
    async def model_status():
        """V52: Return current model availability and cooldown status."""
        try:
            from .retry_policy import get_failover_chain
            chain = get_failover_chain()
            status = chain.get_status()
            # Add overall cooldown_remaining
            status["cooldown_remaining"] = round(chain.seconds_until_any_available(), 0)
            return JSONResponse(status)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/audit/resume")
    async def resume_audit_loop():
        """V42: Resume the continuous audit loop."""
        state.audit_loop_enabled = True
        logger.info("▶  [API] Audit loop resumed via Command Centre.")
        state.record_activity("system", "Audit loop resumed")
        await state.broadcast_state()
        return JSONResponse({"status": "audit_loop_resumed", "msg": "Audit loop resumed."})

    @app.post("/api/mode")
    async def set_execution_mode(body: dict):
        """V44: Toggle between auto and manual execution mode."""
        mode = body.get("mode", "").strip().lower()
        if mode not in ("auto", "manual"):
            return JSONResponse({"error": "mode must be 'auto' or 'manual'"}, status_code=400)

        old_mode = state.execution_mode
        state.execution_mode = mode
        state._save_ui_prefs()  # V74: Persist across restarts
        logger.info("🔀  [API] Execution mode changed: %s → %s", old_mode, mode)
        state.record_activity("system", f"Execution mode changed: {old_mode} → {mode}")
        # V46: Resume from paused when switching back to auto
        if mode == "auto" and state.status == "paused":
            state.status = "executing"
        elif mode == "manual" and state.status == "executing":
            state.status = "paused"
        await state.broadcast_state()
        return JSONResponse({"status": "mode_changed", "mode": mode, "previous": old_mode})

    @app.post("/api/quota-pause")
    async def toggle_quota_pause(body: dict):
        """V44/V70: Cycle quota pause mode: off → pro → all → off.

        Modes:
          'off'  - Never pause, wait for quota reset automatically.
          'pro'  - Pause when pro/paid model quota is exhausted.
          'all'  - Pause when ALL available model quotas are exhausted.
        """
        # V70: Support explicit mode setting or cycle
        explicit_mode = body.get("mode")
        if explicit_mode in ("off", "pro", "all"):
            state.quota_pause_mode = explicit_mode
        else:
            # Cycle: off → pro → all → off
            current = getattr(state, 'quota_pause_mode', 'off')
            # Backwards compat: boolean True → 'all'
            if current is True:
                current = 'all'
            elif current is False or current == 'off':
                current = 'off'
            _cycle = {'off': 'pro', 'pro': 'all', 'all': 'off'}
            state.quota_pause_mode = _cycle.get(current, 'pro')
        # V70: Keep legacy flag in sync for any code still checking it
        state.pause_on_quota = state.quota_pause_mode != 'off'
        logger.info("⏸️  [API] Quota pause mode: %s", state.quota_pause_mode)
        state._save_ui_prefs()  # V74: Persist across restarts
        state.record_activity("system", f"Quota pause mode: {state.quota_pause_mode}")
        await state.broadcast_state()
        return JSONResponse({"status": "ok", "quota_pause_mode": state.quota_pause_mode, "pause_on_quota": state.pause_on_quota})

    @app.post("/api/set-workers")
    async def set_worker_count(request: Request):
        """Set the number of concurrent workers (1-6)."""
        try:
            body = await request.json()
            count = int(body.get("workers", 3))
            from .retry_policy import get_daily_budget
            budget = get_daily_budget()
            actual = budget.set_workers(count)
            status = budget.get_status()
            msg = f"🔧 Worker count set to {actual}"
            state.record_activity("system", msg)
            await state.broadcast_state()
            logger.info("🔧  [API] Worker count set to %d via UI.", actual)
            return JSONResponse({
                "status": "ok",
                "workers": actual,
                "message": msg,
                "budget": status,
            })
        except Exception as exc:
            return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)

    @app.get("/api/budget")
    async def get_budget_status():
        """Return current daily budget and worker count status."""
        try:
            from .retry_policy import get_daily_budget
            return JSONResponse(get_daily_budget().get_status())
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/quota-probe")
    async def get_quota_probe_status():
        """V62: Return live per-model quota data from CLI output parsing."""
        try:
            from .retry_policy import get_quota_probe
            return JSONResponse(get_quota_probe().get_quota_snapshot())
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── V64: Gemini Lite Intelligence (replaces Ollama) ──────────
    # Usage tracking for monitoring how many calls go through lite vs flash
    _lite_usage = {"calls_today": 0, "fallback_count": 0, "day": "", "model": ""}
    _lite_usage_file = Path(__file__).parent / "_lite_usage.json"

    def _load_lite_usage():
        today = time.strftime("%Y-%m-%d")
        if _lite_usage["day"] != today:
            # Day rolled over — reset counters
            _lite_usage.update({"calls_today": 0, "fallback_count": 0, "day": today})
        # Try loading persisted data
        try:
            if _lite_usage_file.exists():
                import json as _ju
                data = _ju.loads(_lite_usage_file.read_text(encoding="utf-8"))
                if data.get("day") == today:
                    _lite_usage.update(data)
                else:
                    _lite_usage.update({"calls_today": 0, "fallback_count": 0, "day": today})
        except Exception:
            pass

    def _save_lite_usage():
        try:
            import json as _ju
            _lite_usage_file.write_text(_ju.dumps(_lite_usage), encoding="utf-8")
        except Exception:
            pass

    _load_lite_usage()

    @app.post("/api/lite/ask")
    async def lite_ask(request: Request):
        """V64: Gemini Lite Intelligence — replaces Ollama for project Q&A.

        Streams NDJSON response: {token, done, model, elapsed, error}.
        Fallback chain: lite → flash if lite fails.
        """
        from starlette.responses import StreamingResponse

        body = await request.json()
        
        # Support both legacy single-question and new history array
        question = (body.get("question") or "").strip()
        messages = body.get("messages") or []
        
        if not question and not messages:
            return JSONResponse({"error": "No question or messages provided"}, status_code=400)

        # Refresh usage counters for the day
        today = time.strftime("%Y-%m-%d")
        if _lite_usage["day"] != today:
            _lite_usage.update({"calls_today": 0, "fallback_count": 0, "day": today})

        # Build enriched context: platform knowledge + live session state
        # ── Static platform knowledge (Supervisor AI V65) ──────────────────
        _PLATFORM_KNOWLEDGE = """\
=== SUPERVISOR AI — PLATFORM KNOWLEDGE (V65) ===

WHAT IT IS:
Supervisor AI is a fully autonomous Python software engineering agent. The user
gives it a high-level goal (e.g. "build a landing page") and it plans, codes,
tests, deploys, and iterates — hands-free. It runs on the host machine using an
authenticated Gemini CLI session, while all code execution happens inside an
isolated Docker sandbox container. It builds, fixes, and monitors until the
project is working.

ARCHITECTURE ("Host Intelligence, Sandboxed Hands"):
- Gemini CLI runs on the HOST with full credentials and code access.
- The Docker sandbox is a dumb terminal: no credentials, no network, runs code.
- The Supervisor orchestrates the pipeline: plan → code → execute → test → fix.
- The Command Centre web UI (http://localhost:8420) is the human control panel.

KEY CONCEPTS:
- Goal: The top-level objective the user wants to achieve (e.g. "build a SaaS dashboard").
- DAG: Directed Acyclic Graph of tasks. The planner decomposes goals into 5-50
  atomic tasks with dependencies. Workers execute them in parallel.
- Worker pool: Up to 4 concurrent Gemini CLI workers. Each picks the next
  unblocked DAG node, runs it, and handles failures autonomously.
- Auto-fix: If a task fails, the supervisor diagnoses with Gemini and retries
  with enriched context. If the retry also fails, an audit task is created.
- Audit loop: After DAG completion, runs code quality scans and injects
  fix-tasks back into the DAG. Max 3 audit cycles.
- Self-healing: The supervisor can patch its own Python source code (reads all
  modules, sends to Gemini with traceback, receives fix, validates, reboots).
- Quota system: Tracks usage across 3 model buckets: Pro (~500 RPD), Flash
  (~1500 RPD), Lite (~5000 RPD). PTY probe refreshes estimates every 15s.
- Models (current as of V65): gemini-3.1-pro-preview (primary), gemini-3-flash-preview
  (flash), gemini-3.1-flash-lite-preview (lite, replaced gemini-2.5-flash-lite in V65). Flash-lite for Gemini Lite Q&A.
- File conflict protection: 5 layers — claim registry, current-file injection,
  regression guard, git commit per task, SEARCH/REPLACE patch protocol.
- Session: Persists across restarts. DAG state, history, and prompts survive crashes.
- Stop/resume: Stopping saves all pending tasks. They resume automatically next session.

COMMAND CENTRE UI TABS:
- Logs tab: Real-time Glass Brain console — colored output from Gemini, workers,
  and the supervisor engine. Cyan=prompt, Yellow=response, Red=error, Green=success.
- Graph tab: Visual DAG of all tasks — nodes colored by status (green=complete,
  red=failed, yellow=running/queued, gray=pending, padlock=blocked). Click any
  node to expand its full description.
- Timeline tab: Activity feed — timestamped events (task start, complete, fail,
  quota warnings, audits, replans). Good for understanding what happened when.
- History tab: All previous DAG runs from this project. Each collapsible card
  shows the run timestamp and all tasks with their statuses. Persists across sessions.
- Health tab: BUILD_ISSUES.md + CONSOLE_ISSUES.md + Vite errors aggregated into
  severity cards. System resources (CPU, memory, Docker, uptime) at the top.
  Header pill shows ✓ CLEAN or ✕ N ISSUES.
- Console tab: Browser console relay from the live preview iframe. Level filter
  (All/Errors/Warnings/Log/Info). Smart scroll. Error count badge.
- Stats tab: DAG progress bar, session timing, model availability with cooldown
  countdowns, CPU/memory/disk.
- Files tab: Project file tree. Click to expand directories. Shows which files
  changed in the current session.
- Ports tab: All localhost listening ports. Flags the project preview port.
  Kill button for non-project ports.
- Gemini Lite (⚡ button): THIS PANEL — powered by gemini-2.5-flash-lite with
  automatic flash fallback. Use it to ask questions about the project or the
  Supervisor platform itself.

SIDEBAR (RIGHT PANEL):
- DAG status strip: Running/queued tasks with progress.
- Goal display and edit (E key).
- Manual/Auto mode toggle.
- Stop/Resume button.
- Model activity pill: shows current active model + quota %.

LAUNCHER (start screen):
- Project path selector. Start/resume session buttons.
- Quota panel: animated bars per model with countdown to reset.
- Version: V65 Command Centre.

KEY FILES AND ENDPOINTS:
- http://localhost:8420 — Command Centre UI
- /api/goal, /api/instruct — send goal or instructions
- /api/lit/ask — Gemini Lite Q&A (this endpoint)
- /api/lite/stats — usage stats (calls_today, fallback_count)
- /api/status — full session state (WebSocket preferred)
- /api/dag/history — all past DAG runs
- /api/issues — parsed build/console issues
- /api/ports — listening ports
- .ag-supervisor/ — supervisor working directory inside project
- supervisor.log — full timestamped log
- _lite_usage.json — daily Gemini Lite call counts
"""

        # ── Dynamic live session state ──────────────────────────────────────
        project_path = state.project_path or os.getcwd()
        active_model = getattr(state, "active_model", "unknown")
        session_status = getattr(state, "status", "unknown")
        quota_info = ""
        try:
            from .retry_policy import get_quota_probe as _gqp
            snap = _gqp().get_quota_snapshot()
            quota_lines = []
            for m, d in snap.items():
                if isinstance(d, dict):
                    quota_lines.append(f"  {m}: {d.get('remaining_pct', '?'):.1f}% remaining")
            quota_info = "\n".join(quota_lines[:5])
        except Exception:
            quota_info = "  (unavailable)"

        system_context = (
            f"{_PLATFORM_KNOWLEDGE}\n"
            f"=== CURRENT SESSION STATE ===\n"
            f"Project path: {project_path}\n"
            f"Current goal: {state.goal or 'No goal set'}\n"
            f"Session status: {session_status}\n"
            f"Active model: {active_model}\n"
            f"Quota snapshot:\n{quota_info}\n\n"
            f"=== YOUR ROLE ===\n"
            f"You are Gemini Lite — the user's intelligent assistant for EVERYTHING related to "
            f"their project and the Supervisor platform. You answer ANY question the user asks: "
            f"technical implementation, architecture, debugging, business strategy, market viability, "
            f"pricing, UX critique, competitive analysis, or general brainstorming.\n"
            f"Be direct, honest, and opinionated when the user asks for brutally honest feedback. "
            f"Do NOT refuse questions about business, strategy, pricing, or opinions — the user "
            f"relies on you as a knowledgeable thought partner, not just a code tool.\n"
            f"When relevant, reference specific files, tabs, or features from the project.\n"
        )
        
        # Format chat history from messages array if present
        convo_history = ""
        if messages:
            for msg in messages:
                role = "User" if msg.get("role") == "user" else "Assistant"
                convo_history += f"{role}: {msg.get('text', '')}\n\n"
            full_prompt = f"{system_context}\n{convo_history}Assistant: "
        else:
            full_prompt = f"{system_context}\nUser question: {question}\n\nAssistant: "

        async def _stream_response():
            import json as _sj
            from . import config as _cfg

            # V65: Build model try-chain dynamically from tier buckets.
            _lite_primary = getattr(_cfg, "GEMINI_DEFAULT_LITE", "gemini-3.1-flash-lite-preview")
            _flash_fallback = getattr(_cfg, "GEMINI_DEFAULT_FLASH", "gemini-3-flash-preview")
            _buckets = getattr(_cfg, "QUOTA_BUCKETS", {})
            _lite_bucket_models = _buckets.get("lite", {}).get("models", [])
            _flash_bucket_models = _buckets.get("flash", {}).get("models", [])

            # Primary lite first, then rest of lite tier, then flash tier — deduplicated
            models_to_try = [_lite_primary]
            for _m in _lite_bucket_models + _flash_bucket_models + [_flash_fallback]:
                if _m not in models_to_try:
                    models_to_try.append(_m)

            used_model = models_to_try[0]
            is_fallback = False
            start = time.time()


            for i, model in enumerate(models_to_try):
                try:
                    from .gemini_advisor import stream_gemini
                    used_model = model
                    is_fallback = (i > 0)
                    
                    # Yield initial metadata
                    yield _sj.dumps({
                        "token": "",
                        "done": False,
                        "model": used_model,
                        "fallback": is_fallback,
                    }) + "\n"

                    # Live stream the chunks
                    async for chunk in stream_gemini(
                        full_prompt, 
                        timeout=60, 
                        model_override=model, 
                        max_retries=1  # Rely on the outer tier fallback loop instead of retrying the same tier multiple times
                    ):
                        yield _sj.dumps({"token": chunk, "done": False}) + "\n"

                    # Track usage on success
                    _lite_usage["calls_today"] += 1
                    _lite_usage["model"] = used_model
                    if is_fallback:
                        _lite_usage["fallback_count"] += 1
                    _save_lite_usage()

                    elapsed = round(time.time() - start, 1)

                    # Final "done" message
                    yield _sj.dumps({
                        "token": "",
                        "done": True,
                        "model": used_model,
                        "elapsed": f"{elapsed}s",
                        "fallback": is_fallback,
                    }) + "\n"

                    logger.info(
                        "⚡ [Lite] Q&A streaming completed: model=%s, fallback=%s, elapsed=%ss",
                        used_model, is_fallback, elapsed,
                    )
                    return  # Success — stop trying models

                except Exception as err:
                    if i < len(models_to_try) - 1:
                        logger.warning(
                            "⚡ [Lite] %s failed (%s), falling back to %s",
                            model, err, models_to_try[i + 1],
                        )
                        continue  # Try next model
                    else:
                        # All models failed
                        _lite_usage["calls_today"] += 1
                        _lite_usage["fallback_count"] += 1
                        _save_lite_usage()
                        yield _sj.dumps({
                            "token": "",
                            "done": True,
                            "error": f"All models failed: {err}",
                        }) + "\n"

        return StreamingResponse(
            _stream_response(),
            media_type="application/x-ndjson",
        )

    @app.get("/api/lite/stats")
    async def lite_stats():
        """V64: Return Gemini Lite usage statistics for monitoring."""
        from . import config as _cfg
        today = time.strftime("%Y-%m-%d")
        if _lite_usage["day"] != today:
            _lite_usage.update({"calls_today": 0, "fallback_count": 0, "day": today})
        return JSONResponse({
            "calls_today": _lite_usage["calls_today"],
            "fallback_count": _lite_usage["fallback_count"],
            "model": getattr(_cfg, "GEMINI_DEFAULT_LITE", "gemini-2.5-flash-lite"),
            "fallback_model": getattr(_cfg, "GEMINI_DEFAULT_FLASH", "gemini-3-flash-preview"),
            "day": _lite_usage["day"],
        })

    @app.get("/api/ports")
    async def list_ports():
        """V44: List all localhost listening ports and flag the project's preview port."""
        import subprocess as _sp
        ports = []
        project_port = state.preview_port

        try:
            if os.name == "nt":
                # Windows: netstat -ano | find LISTENING
                result = _sp.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
                )
                seen = set()
                for line in result.stdout.splitlines():
                    if "LISTENING" not in line:
                        continue
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    addr = parts[1]
                    pid = int(parts[4]) if parts[4].isdigit() else 0
                    if ":" not in addr:
                        continue
                    port_str = addr.rsplit(":", 1)[1]
                    if not port_str.isdigit():
                        continue
                    port = int(port_str)
                    if port in seen or port < 1024:
                        continue
                    seen.add(port)

                    # Get process name
                    pname = "unknown"
                    if pid > 0:
                        try:
                            pr = _sp.run(
                                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                capture_output=True, text=True, timeout=3,
                            )
                            first = pr.stdout.strip().split("\n")[0]
                            if '"' in first:
                                pname = first.split('"')[1]
                        except Exception:
                            pass

                    ports.append({
                        "port": port,
                        "pid": pid,
                        "process": pname,
                        "is_project": port == project_port,
                    })
            else:
                # Unix/Mac: lsof
                result = _sp.run(
                    ["lsof", "-iTCP", "-sTCP:LISTEN", "-nP"],
                    capture_output=True, text=True, timeout=5,
                )
                seen = set()
                for line in result.stdout.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 9:
                        continue
                    pname = parts[0]
                    pid = int(parts[1]) if parts[1].isdigit() else 0
                    addr = parts[8]
                    port_str = addr.rsplit(":", 1)[1] if ":" in addr else ""
                    if not port_str.isdigit():
                        continue
                    port = int(port_str)
                    if port in seen or port < 1024:
                        continue
                    seen.add(port)
                    ports.append({
                        "port": port,
                        "pid": pid,
                        "process": pname,
                        "is_project": port == project_port,
                    })
        except Exception as exc:
            logger.warning("Port scan failed: %s", exc)

        ports.sort(key=lambda p: p["port"])
        return JSONResponse({
            "ports": ports,
            "project_port": project_port,
        })

    @app.post("/api/ports/kill")
    async def kill_port(body: dict):
        """V44: Kill the process on a given port. Standalone — not queued."""
        import subprocess as _sp
        port = body.get("port", 0)
        if not isinstance(port, int) or port < 1024:
            return JSONResponse({"error": "Invalid port"}, status_code=400)
        if port == state.preview_port:
            return JSONResponse({"error": "Cannot kill project preview port"}, status_code=400)

        killed = False
        try:
            if os.name == "nt":
                result = _sp.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
                for line in result.stdout.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.split()
                        pid = int(parts[-1]) if parts[-1].isdigit() else 0
                        if pid > 0 and pid != os.getpid():
                            _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
                            killed = True
                            logger.info("🔌  [Ports] Killed PID %d on port %d", pid, port)
            else:
                result = _sp.run(
                    ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5,
                )
                for pid_str in result.stdout.strip().split():
                    pid = int(pid_str)
                    if pid > 0 and pid != os.getpid():
                        os.kill(pid, 9)
                        killed = True
                        logger.info("🔌  [Ports] Killed PID %d on port %d", pid, port)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

        if killed:
            state.record_activity("system", f"Killed process on port {port}")
            return JSONResponse({"status": "killed", "port": port})
        return JSONResponse({"error": f"No process found on port {port}"}, status_code=404)


    @app.get("/api/changes")
    async def get_changes(limit: int = 200):
        """V44: Return code change history."""
        changes = state.change_history[-limit:]
        return JSONResponse({
            "changes": changes,
            "total": len(state.change_history),
        })

    @app.get("/api/timeline")
    async def get_timeline(limit: int = 200):
        """V44: Return activity timeline."""
        events = state.activity_timeline[-limit:]
        return JSONResponse({
            "events": events,
            "total": len(state.activity_timeline),
        })

    @app.get("/api/issues")
    async def get_issues():
        """V60: Return BUILD_ISSUES.md and CONSOLE_ISSUES.md content for the Health tab."""
        project_path = state.project_path or os.getcwd()
        sup_dir = Path(project_path) / ".ag-supervisor"

        def _read(filename: str) -> str:
            p = sup_dir / filename
            if p.exists():
                try:
                    return p.read_text(encoding="utf-8").strip()
                except Exception:
                    return ""
            # Also check project root
            p2 = Path(project_path) / filename
            if p2.exists():
                try:
                    return p2.read_text(encoding="utf-8").strip()
                except Exception:
                    return ""
            return ""

        build_raw  = _read("BUILD_ISSUES.md")
        console_raw = _read("CONSOLE_ISSUES.md")

        # Parse issue blocks — each issue is separated by "## " header or "---"
        def _parse_issues(raw: str) -> list[dict]:
            if not raw:
                return []
            issues = []
            current: dict | None = None
            for line in raw.splitlines():
                if line.startswith("## ") or line.startswith("# "):
                    if current:
                        issues.append(current)
                    severity = "error"
                    if any(w in line.lower() for w in ["warn", "caution"]):
                        severity = "warning"
                    elif any(w in line.lower() for w in ["info", "note"]):
                        severity = "info"
                    current = {"title": line.lstrip("# ").strip(), "body": "", "severity": severity}
                elif current:
                    current["body"] = (current["body"] + "\n" + line).strip()
            if current:
                issues.append(current)
            return issues

        build_issues   = _parse_issues(build_raw)
        console_issues = _parse_issues(console_raw)

        # Also include the latest Vite scanner errors from state if any
        vite_errors = []
        try:
            from .main import _vite_log_fps as _vfps
            vite_errors = list(_vfps)[:10]
        except Exception:
            pass

        total = len(build_issues) + len(console_issues)
        return JSONResponse({
            "total":          total,
            "build_issues":   build_issues,
            "console_issues": console_issues,
            "vite_errors":    vite_errors,
            "build_raw":      build_raw[:8000],   # cap for payload size
            "console_raw":    console_raw[:4000],
        })

    @app.get("/api/health")
    async def get_health():
        """V44: Return system health metrics."""
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory()
            health = {
                "cpu_percent": cpu,
                "memory_used_mb": round(mem.used / 1024 / 1024),
                "memory_total_mb": round(mem.total / 1024 / 1024),
                "memory_percent": mem.percent,
            }
        except Exception:
            health = {"cpu_percent": 0, "memory_used_mb": 0, "memory_total_mb": 0, "memory_percent": 0}

        health["docker_status"] = state.container_health
        # V41 FIX (Bug 4): Live Docker container check
        try:
            if state.container_id:
                import asyncio as _aio
                _dp = await _aio.create_subprocess_exec(
                    "docker", "inspect", "--format", "{{.State.Running}}", state.container_id,
                    stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
                )
                _do, _ = await _aio.wait_for(_dp.communicate(), timeout=2)
                _docker_live = _do.decode().strip().lower() == "true"
                health["docker_status"] = "running" if _docker_live else "stopped"
                state.container_health = health["docker_status"]
        except Exception:
            pass  # Keep whatever state had
        # V41: Live check Ollama instead of relying on stale boot-time value
        try:
            import aiohttp as _aio2
            import json as _j2
            async with _aio2.ClientSession() as _s:
                async with _s.get("http://localhost:11434/api/tags",
                                  timeout=_aio2.ClientTimeout(total=1.5)) as _r:
                    _ollama_live = _r.status == 200
                    if _ollama_live and not getattr(state, 'ollama_model', None):
                        # Boot hasn't set ollama_model yet — resolve it now so
                        # the UI gets the real model name with the first broadcast.
                        try:
                            _tags = _j2.loads(await _r.text())
                            _mods = [m.get('name', '') for m in _tags.get('models', [])]
                            _txt  = [m for m in _mods
                                     if 'llama' in m.lower() and 'llava' not in m.lower()]
                            state.ollama_model = _txt[0] if _txt else (_mods[0] if _mods else None)
                        except Exception:
                            pass
        except Exception:
            _ollama_live = False
        state.ollama_online = _ollama_live  # Keep state in sync
        health["ollama_online"] = _ollama_live
        health["engine_running"] = state.engine_running
        health["uptime_s"] = round(state.uptime_s, 1)
        return JSONResponse(health)
    # ── V61 Helpers ──────────────────────────────────────────────────────────

    async def _resolve_ollama_host_model():
        """Return (host, model) for Ollama requests.
        Priority: state.ollama_model (set at boot) → env var → live /api/tags probe.
        """
        import json as _j
        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        # 1. state.ollama_model is set at boot from the executor's local_brain.model
        m = getattr(state, 'ollama_model', None)
        if m:
            return host, m
        # 2. OLLAMA_MODEL env var
        m = os.getenv("OLLAMA_MODEL", "")
        if m:
            return host, m
        # 3. Live probe — pick first non-vision llama model
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as _s:
                async with _s.get(f"{host}/api/tags",
                                  timeout=_aio.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        raw = await r.text()
                        data = _j.loads(raw)
                        models = [m.get("name", "") for m in data.get("models", [])]
                        text = [m for m in models
                                if "llama" in m.lower() and "llava" not in m.lower()]
                        return host, (text[0] if text else (models[0] if models else "llama3:latest"))
        except Exception:
            pass
        return host, "llama3:latest"

    # ── V61 Ollama Local Intelligence Endpoints ────────────────────────────

    @app.post("/api/ollama/ask")
    async def ollama_ask(body: dict):
        """
        V61: On-demand Q&A against the local Ollama model.

        Body: { question: str, context?: str, files?: list[str] }
        Returns: { answer: str, model: str, elapsed_ms: int }

        'context' is free-form text (e.g. file snippets, error output).
        'files' is a list of file paths inside the project to read and include.
        Zero Gemini quota cost — runs entirely locally.
        """
        if not state.ollama_online:
            return JSONResponse({"error": "Ollama is offline"}, status_code=503)

        question = (body.get("question") or "").strip()
        if not question:
            return JSONResponse({"error": "question is required"}, status_code=400)

        extra_context = (body.get("context") or "").strip()
        extra_files   = body.get("files") or []
        project_path  = Path(state.project_path or os.getcwd())

        # ── Auto-build rich project context ──────────────────────────────────
        # Read key project files so the model knows exactly what's being built.
        _KEY_DOCS = [
            "SUPERVISOR_MANDATE.md", "GEMINI.md", "README.md",
            ".ag-supervisor/GEMINI.md",
            "package.json", "PROJECT_STATE.md",
            ".ag-supervisor/PROGRESS.md", ".ag-supervisor/EPIC.md",
        ]
        auto_snippets: list[str] = []

        # 1. Compact file tree (flat list, max 100 entries)
        try:
            _SKIP = {"node_modules", ".git", "__pycache__", ".ag-supervisor",
                     ".next", "dist", "build", ".cache", ".vite", "coverage",
                     "venv", ".venv", ".env"}
            _tree_lines: list[str] = []
            for root, dirs, files in os.walk(project_path):
                dirs[:] = [d for d in sorted(dirs) if d not in _SKIP]
                rel_root = os.path.relpath(root, project_path)
                for f in sorted(files):
                    rel = os.path.join(rel_root, f).replace("\\", "/").lstrip("./")
                    _tree_lines.append(rel)
                    if len(_tree_lines) >= 50:
                        break
                if len(_tree_lines) >= 50:
                    break
            if _tree_lines:
                auto_snippets.append("FILE TREE:\n" + "\n".join(_tree_lines))
        except Exception:
            pass

        # 2. Key documentation files
        for rel in _KEY_DOCS:
            try:
                p = project_path / rel
                if p.exists() and p.is_file():
                    txt = p.read_text(encoding="utf-8", errors="replace")
                    auto_snippets.append(f"=== {rel} ===\n{txt[:4000]}")
            except Exception:
                pass

        # 3. Explicitly requested files (from UI)
        for rel_path in extra_files[:5]:
            try:
                abs_path = project_path / rel_path
                if abs_path.exists() and abs_path.is_file():
                    txt = abs_path.read_text(encoding="utf-8", errors="replace")
                    auto_snippets.append(f"=== {rel_path} ===\n{txt[:4000]}")
            except Exception:
                pass

        # ── Build system + user messages ──────────────────────────────────────
        proj_name = project_path.name
        system_msg = (
            f"You are a precise, senior-level code assistant for the project '{proj_name}'. "
            f"You have full knowledge of the project's file structure, goal, and documentation provided below. "
            f"ALWAYS answer using this context — explain what the project IS, what it does, "
            f"its architecture, and reference specific files from the tree when relevant. "
            f"Use markdown. If you reference a file, use its exact path from the tree. "
            f"Keep answers under 500 words unless more detail is essential."
        )
        if auto_snippets:
            system_msg += "\n\n--- PROJECT CONTEXT ---\n" + "\n\n".join(auto_snippets)

        user_parts = [question]
        if extra_context:
            user_parts.append(f"\n\nAdditional context:\n```\n{extra_context[:3000]}\n```")
        user_msg = "\n".join(user_parts)

        try:
            import aiohttp as _aio
            import json as _json
            import time as _t
            _t0 = _t.monotonic()
            ollama_host, ollama_model = await _resolve_ollama_host_model()
            logger.info("🧠  [Ollama/ask] host=%s model=%s (streaming)", ollama_host, ollama_model)

            # We stream NDJSON from Ollama → browser for real-time token display.
            # Each Ollama line: {"response":"token","done":false}
            # Final line:       {"response":"","done":true,"total_duration":...}
            async def _stream_generator():
                import asyncio as _asyncio
                _queue: _asyncio.Queue = _asyncio.Queue()
                _SENTINEL = object()

                async def _ollama_reader():
                    """Background task: read from Ollama, push NDJSON to queue."""
                    try:
                        async with _aio.ClientSession() as _s:
                            async with _s.post(
                                f"{ollama_host}/api/generate",
                                json={
                                    "model": ollama_model,
                                    "system": system_msg,
                                    "prompt": user_msg,
                                    "stream": True,
                                    "options": {"temperature": 0.2, "num_predict": 900},
                                },
                                timeout=_aio.ClientTimeout(total=300),
                            ) as resp:
                                if resp.status != 200:
                                    raw = await resp.text()
                                    logger.warning("🧠  [Ollama/ask] non-200: %d %s", resp.status, raw[:200])
                                    await _queue.put(_json.dumps({"error": f"Ollama {resp.status}: {raw[:120]}"}))
                                    return
                                async for line in resp.content:
                                    decoded = line.decode("utf-8", errors="replace").strip()
                                    if not decoded:
                                        continue
                                    try:
                                        chunk = _json.loads(decoded)
                                        out = {"token": chunk.get("response", "")}
                                        if chunk.get("done"):
                                            elapsed = int((_t.monotonic() - _t0) * 1000)
                                            out["done"] = True
                                            out["model"] = ollama_model
                                            out["elapsed_ms"] = elapsed
                                        await _queue.put(_json.dumps(out))
                                    except Exception:
                                        pass
                    except Exception as exc:
                        logger.warning("🧠  [Ollama/ask] stream exception: %s", exc)
                        await _queue.put(_json.dumps({"error": str(exc) or "Connection lost"}))
                    finally:
                        await _queue.put(_SENTINEL)

                # Start reader in background
                _reader_task = _asyncio.create_task(_ollama_reader())

                try:
                    while True:
                        try:
                            item = await _asyncio.wait_for(_queue.get(), timeout=3.0)
                        except _asyncio.TimeoutError:
                            # No data yet (model loading) — send heartbeat
                            yield _json.dumps({"thinking": True}) + "\n"
                            continue
                        if item is _SENTINEL:
                            break
                        yield item + "\n"
                finally:
                    if not _reader_task.done():
                        _reader_task.cancel()

            from starlette.responses import StreamingResponse
            return StreamingResponse(
                _stream_generator(),
                media_type="application/x-ndjson",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )
        except Exception as exc:
            logger.warning("🧠  [Ollama/ask] exception: %s", exc)
            return JSONResponse({"error": str(exc) or "Ollama request failed"}, status_code=502)


    @app.post("/api/ollama/search")
    async def ollama_search(body: dict):
        """
        V61: Semantic file relevance search using the local Ollama model.

        Body: { query: str, max_results?: int }
        Returns: { results: [{path, relevance, reason}], model: str }

        Scans project files (respecting gitignore patterns), asks Ollama to rank
        by relevance to the query, returns top N with a one-line reason each.
        """
        if not state.ollama_online:
            return JSONResponse({"error": "Ollama is offline"}, status_code=503)

        query = (body.get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "query is required"}, status_code=400)
        max_results = min(int(body.get("max_results") or 8), 20)

        project_path = state.project_path or os.getcwd()

        # Collect file list (respecting common ignore patterns)
        IGNORE = {
            "node_modules", ".git", "dist", ".next", ".nuxt", "__pycache__",
            ".vite", ".turbo", "coverage", ".ag-supervisor", "build", ".output",
        }
        SRC_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".css", ".scss",
                    ".html", ".json", ".md", ".yml", ".yaml", ".env"}
        file_list = []
        try:
            for root, dirs, files in os.walk(project_path):
                dirs[:] = [d for d in dirs if d not in IGNORE and not d.startswith(".")]
                rel_root = Path(root).relative_to(project_path)
                for f in files:
                    rel = str(rel_root / f)
                    if Path(f).suffix in SRC_EXTS:
                        file_list.append(rel)
            file_list = file_list[:120]  # cap
        except Exception:
            file_list = []

        if not file_list:
            return JSONResponse({"results": [], "model": "", "note": "No source files found"})

        # Ask Ollama to rank the files
        prompt = (
            f"You are a code search assistant. Given this search query:\n\"{query}\"\n\n"
            f"Here is a list of project files:\n"
            + "\n".join(f"- {p}" for p in file_list)
            + f"\n\nIdentify the {max_results} most relevant files and explain why in one line each.\n"
            "Respond ONLY with a JSON array: "
            '[{"path": "...", "reason": "..."}, ...]\n'
            "Only include files that are genuinely relevant. If none are relevant, return []."
        )

        try:
            import aiohttp as _aio
            import json as _json
            import re as _re
            ollama_host, ollama_model = await _resolve_ollama_host_model()
            async with _aio.ClientSession() as _s:
                async with _s.post(
                    f"{ollama_host}/api/generate",
                    json={
                        "model": ollama_model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 800},
                    },
                    timeout=_aio.ClientTimeout(total=120),
                ) as resp:
                    raw_text = await resp.text()  # avoid aiohttp ContentTypeError
                    if resp.status != 200:
                        logger.warning("🧠  [Ollama/search] non-200: %d %s", resp.status, raw_text[:200])
                        return JSONResponse({"error": f"Ollama {resp.status}: {raw_text[:120]}"}, status_code=502)
                    data = _json.loads(raw_text)
                    raw = data.get("response", "")
                    # Extract JSON array from response (robust to markdown fences)
                    m = _re.search(r"\[.*\]", raw, _re.DOTALL)
                    results = []
                    if m:
                        try:
                            parsed = _json.loads(m.group(0))
                            for item in parsed[:max_results]:
                                if isinstance(item, dict) and "path" in item:
                                    results.append({
                                        "path": str(item.get("path", "")),
                                        "reason": str(item.get("reason", "")),
                                    })
                        except Exception:
                            pass
                    return JSONResponse({"results": results, "model": ollama_model})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.get("/api/logs")
    async def get_logs(n: int = 100):
        """Return recent log entries from the Glass Brain ring buffer."""
        entries = list(state._log_buffer)[-n:]
        return JSONResponse({"logs": entries, "total": len(state._log_buffer)})

    # ── V69: Dashboard Context Notes (uses active project automatically) ──

    @app.get("/api/notes")
    async def get_active_notes():
        """Return context notes for the currently active project."""
        if not state.project_path:
            return JSONResponse({"notes": [], "path": ""})
        notes = _load_notes(state.project_path)
        return JSONResponse({"notes": notes, "path": state.project_path})

    @app.post("/api/notes/add")
    async def add_active_note(body: dict):
        """Add a context note to the currently active project."""
        if not state.project_path:
            return JSONResponse({"error": "No active project"}, status_code=400)
        text = body.get("text", "").strip()
        if not text:
            return JSONResponse({"error": "text required"}, status_code=400)
        notes = _load_notes(state.project_path)
        now = time.time()
        import hashlib as _hl
        note_id = f"cn_{int(now)}_{_hl.md5(text.encode()).hexdigest()[:6]}"
        notes.append({"id": note_id, "text": text, "created_at": now, "updated_at": now})
        _save_notes(state.project_path, notes)
        _refresh_mandate_files(state.project_path, notes)
        logger.info("📌 [Notes/Dashboard] Added note %s", note_id)
        return JSONResponse({"notes": notes, "id": note_id})

    @app.put("/api/notes")
    async def update_active_note(body: dict):
        """Edit a context note on the currently active project."""
        if not state.project_path:
            return JSONResponse({"error": "No active project"}, status_code=400)
        note_id = body.get("id", "").strip()
        text = body.get("text", "").strip()
        if not note_id or not text:
            return JSONResponse({"error": "id and text required"}, status_code=400)
        notes = _load_notes(state.project_path)
        for n in notes:
            if n["id"] == note_id:
                n["text"] = text
                n["updated_at"] = time.time()
                _save_notes(state.project_path, notes)
                _refresh_mandate_files(state.project_path, notes)
                return JSONResponse({"notes": notes})
        return JSONResponse({"error": "Note not found"}, status_code=404)

    @app.post("/api/notes/delete")
    async def delete_active_note(body: dict):
        """Delete a context note from the currently active project."""
        if not state.project_path:
            return JSONResponse({"error": "No active project"}, status_code=400)
        note_id = body.get("id", "").strip()
        if not note_id:
            return JSONResponse({"error": "id required"}, status_code=400)
        notes = _load_notes(state.project_path)
        filtered = [n for n in notes if n["id"] != note_id]
        if len(filtered) == len(notes):
            return JSONResponse({"error": "Note not found"}, status_code=404)
        _save_notes(state.project_path, filtered)
        _refresh_mandate_files(state.project_path, filtered)
        return JSONResponse({"notes": filtered})

    @app.post("/api/instruct")
    async def post_instruction(body: dict):
        """Push a user instruction onto the queue."""
        text = body.get("instruction", "").strip()
        if not text:
            return JSONResponse({"error": "Empty instruction"}, status_code=400)

        instruction = await state.queue.push(text, source="ui")

        # Broadcast to all WebSocket clients immediately
        await state._broadcast({
            "type": "instruction_queued",
            "instruction": instruction.to_dict(),
        })

        logger.info("📬 UI instruction received: %s", text)
        return JSONResponse({
            "status": "queued",
            "queue_size": state.queue.size,
            "instruction": instruction.to_dict(),
        })

    @app.get("/api/filetree")
    async def get_filetree(max_depth: int = 8):
        """Return a nested JSON file tree for the active project directory."""
        import os as _os

        _SKIP_DIRS = {
            "node_modules", ".git", "__pycache__", ".ag-supervisor",
            ".next", "dist", "build", ".cache", ".vite", "coverage",
            ".turbo", ".parcel-cache", "venv", ".env", ".venv",
        }
        _SKIP_FILES = {".supervisor_lock", ".DS_Store", "Thumbs.db"}

        def _fmt_size(b: int) -> str:
            if b < 1024: return f"{b} B"
            if b < 1048576: return f"{b/1024:.1f} KB"
            return f"{b/1048576:.1f} MB"

        def _walk(path: str, depth: int) -> dict:
            name = _os.path.basename(path) or path
            try:
                st = _os.stat(path)
            except OSError:
                return {"name": name, "type": "file", "size": "?", "modified": 0, "size_bytes": 0}

            is_dir = _os.path.isdir(path)
            node = {
                "name": name,
                "path": path,
                "type": "dir" if is_dir else "file",
                "modified": int(st.st_mtime),
                "size_bytes": 0 if is_dir else st.st_size,
                "size": "" if is_dir else _fmt_size(st.st_size),
            }
            if is_dir and depth > 0:
                children = []
                try:
                    entries = sorted(_os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
                    for entry in entries:
                        if entry.name in _SKIP_DIRS and entry.is_dir():
                            continue
                        if entry.name in _SKIP_FILES:
                            continue
                        if entry.name.startswith(".") and entry.is_dir():
                            continue
                        children.append(_walk(entry.path, depth - 1))
                except PermissionError:
                    pass
                node["children"] = children
                # Roll up total directory size
                node["size_bytes"] = sum(c.get("size_bytes", 0) for c in children)
                node["size"] = _fmt_size(node["size_bytes"])
            elif is_dir:
                node["children"] = []
            return node

        project = state.project_path
        if not project or not _os.path.isdir(project):
            return JSONResponse({"error": "No active project"}, status_code=404)

        tree = _walk(project, max_depth)
        return JSONResponse(tree)

    @app.get("/api/preview")

    async def get_preview():
        """Return live preview information."""
        url = ""
        if state.preview_port > 0 and state.preview_running:
            url = f"http://localhost:{state.preview_port}"
        return JSONResponse({
            "running": state.preview_running,
            "port": state.preview_port,
            "url": url,
        })

    @app.get("/api/queue")
    async def get_queue():
        """Return instruction queue state."""
        return JSONResponse({
            "size": state.queue.size,
            "history": state.queue.history,
        })

    # ── Context Notes Helpers ─────────────────────────────────

    def _notes_path(proj_path: str) -> Path:
        return Path(proj_path) / ".ag-supervisor" / "context_notes.json"

    def _load_notes(proj_path: str) -> list:
        p = _notes_path(proj_path)
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _save_notes(proj_path: str, notes: list) -> None:
        p = _notes_path(proj_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")

    def _mandate_with_notes(goal: str, notes: list) -> str:
        """Return the full mandate text: goal + appended notes block."""
        base = f"## YOUR MISSION\n\n{goal}\n\n---"
        if not notes:
            return base
        lines = ["\n\n## Persistent Context Notes\n"]
        for i, n in enumerate(notes, 1):
            lines.append(f"{i}. {n['text']}")
        return base + "\n".join(lines)

    def _extract_goal_from_mandate(proj_path: str) -> str:
        """
        Read the clean goal text from SUPERVISOR_MANDATE.md, stripping any
        previously-written '## Persistent Context Notes' block so we don't
        double-append notes on each refresh.
        Falls back to state.goal if mandate doesn't exist yet.
        """
        mandate_file = Path(proj_path) / "SUPERVISOR_MANDATE.md"
        if mandate_file.exists():
            text = mandate_file.read_text(encoding="utf-8")
            # Strip the dynamically appended notes block
            cutoff = text.find("\n\n## Persistent Context Notes")
            if cutoff != -1:
                text = text[:cutoff]
            # Strip the wrapper added by bootstrap ("## YOUR MISSION\n\n" prefix, "\n\n---" suffix)
            text = text.removeprefix("## YOUR MISSION\n\n")
            text = text.removesuffix("\n\n---").removesuffix("---")
            return text.strip()
        # Fallback: use the live goal from state if available
        return getattr(state, 'goal', '') or ''

    def _refresh_mandate_files(proj_path: str, notes: list) -> None:
        """
        Re-write SUPERVISOR_MANDATE.md and GEMINI.md immediately so the
        running engine sees note changes on its very next Gemini task call.
        Called after every note add / edit / delete.
        """
        try:
            goal = _extract_goal_from_mandate(proj_path)
            if not goal:
                logger.debug("[Notes] Mandate refresh skipped — no goal found for %s", proj_path)
                return

            # 1. Re-write SUPERVISOR_MANDATE.md with goal + all current notes
            mandate_file = Path(proj_path) / "SUPERVISOR_MANDATE.md"
            new_content = _mandate_with_notes(goal, notes)
            mandate_file.write_text(new_content, encoding="utf-8")

            # 2. Re-write GEMINI.md via bootstrap so its context also updates
            try:
                from supervisor import bootstrap
                full_goal = goal
                if notes:
                    lines = ["\n\n## Persistent Context Notes\n"]
                    for i, n in enumerate(notes, 1):
                        lines.append(f"{i}. {n['text']}")
                    full_goal = goal + "\n".join(lines)
                bootstrap.bootstrap_workspace(proj_path, full_goal)
            except Exception as _be:
                logger.debug("[Notes] GEMINI.md refresh failed (non-fatal): %s", _be)

            logger.info("📌 [Notes] Refreshed SUPERVISOR_MANDATE.md + GEMINI.md for %s (%d note(s))",
                        proj_path, len(notes))
        except Exception as e:
            logger.warning("[Notes] Mandate refresh error (non-fatal): %s", e)

    # ── Project Management Endpoints ───────────────────────

    # ── Context Notes Endpoints ──────────────────────────────

    @app.get("/api/projects/notes")
    async def get_project_notes(path: str = ""):
        """Return all context notes for a project."""
        if not path:
            return JSONResponse({"error": "path required"}, status_code=400)
        return JSONResponse({"notes": _load_notes(path)})

    @app.post("/api/projects/notes")
    async def add_project_note(body: dict):
        """Append a new context note to a project."""
        proj_path = body.get("path", "").strip()
        text = body.get("text", "").strip()
        if not proj_path or not text:
            return JSONResponse({"error": "path and text required"}, status_code=400)
        notes = _load_notes(proj_path)
        now = time.time()
        import hashlib as _hl
        note_id = f"cn_{int(now)}_{_hl.md5(text.encode()).hexdigest()[:6]}"
        notes.append({"id": note_id, "text": text, "created_at": now, "updated_at": now})
        _save_notes(proj_path, notes)
        _refresh_mandate_files(proj_path, notes)  # ← immediate propagation
        logger.info("📌 [Notes] Added note %s to %s", note_id, proj_path)
        return JSONResponse({"notes": notes, "id": note_id})

    @app.put("/api/projects/notes")
    async def update_project_note(body: dict):
        """Edit the text of a context note."""
        proj_path = body.get("path", "").strip()
        note_id = body.get("id", "").strip()
        text = body.get("text", "").strip()
        if not proj_path or not note_id or not text:
            return JSONResponse({"error": "path, id and text required"}, status_code=400)
        notes = _load_notes(proj_path)
        for n in notes:
            if n["id"] == note_id:
                n["text"] = text
                n["updated_at"] = time.time()
                _save_notes(proj_path, notes)
                _refresh_mandate_files(proj_path, notes)  # ← immediate propagation
                logger.info("✏️  [Notes] Updated note %s", note_id)
                return JSONResponse({"notes": notes})
        return JSONResponse({"error": "Note not found"}, status_code=404)

    @app.delete("/api/projects/notes")
    async def delete_project_note(body: dict):
        """Delete a context note (REST DELETE — kept for API clients)."""
        proj_path = body.get("path", "").strip()
        note_id = body.get("id", "").strip()
        if not proj_path or not note_id:
            return JSONResponse({"error": "path and id required"}, status_code=400)
        notes = _load_notes(proj_path)
        filtered = [n for n in notes if n["id"] != note_id]
        if len(filtered) == len(notes):
            return JSONResponse({"error": "Note not found"}, status_code=404)
        _save_notes(proj_path, filtered)
        _refresh_mandate_files(proj_path, filtered)  # ← immediate propagation
        logger.info("🗑️  [Notes] Deleted note %s from %s", note_id, proj_path)
        return JSONResponse({"notes": filtered})

    @app.post("/api/projects/notes/delete")
    async def delete_project_note_post(body: dict):
        """Browser-safe alias: POST body instead of DELETE body."""
        proj_path = body.get("path", "").strip()
        note_id = body.get("id", "").strip()
        if not proj_path or not note_id:
            return JSONResponse({"error": "path and id required"}, status_code=400)
        notes = _load_notes(proj_path)
        filtered = [n for n in notes if n["id"] != note_id]
        if len(filtered) == len(notes):
            return JSONResponse({"error": "Note not found"}, status_code=404)
        _save_notes(proj_path, filtered)
        _refresh_mandate_files(proj_path, filtered)  # ← immediate propagation
        logger.info("🗑️  [Notes] Deleted note %s from %s", note_id, proj_path)
        return JSONResponse({"notes": filtered})

    @app.get("/api/projects")
    async def list_projects():
        """List all workspaces in the Experiments directory with optional session state."""
        projects = []
        exp_dir = state.experiments_dir

        try:
            if exp_dir.is_dir():
                for d in sorted(exp_dir.iterdir()):
                    if not d.is_dir() or d.name.startswith('.'):
                        continue

                    proj = {
                        "name": d.name,
                        "path": str(d),
                        "has_session": False,
                        "goal": "",
                        "last_active": 0,
                        "checkpoint": None,  # V38.1: resume checkpoint
                    }

                    # V41: Priority 1 — Always read from SUPERVISOR_MANDATE.md first.
                    # If the user edited this file manually, it is the source of truth.
                    mandate_file = d / "SUPERVISOR_MANDATE.md"
                    if mandate_file.exists():
                        try:
                            text = mandate_file.read_text(encoding="utf-8")
                            if "## YOUR MISSION" in text:
                                mission = text.split("## YOUR MISSION", 1)[1]
                                # Only split on the FIRST --- separator (the section divider)
                                # Goals may contain --- inside them, so don't split
                                # on every occurrence.
                                if "\n---" in mission:
                                    mission = mission.split("\n---", 1)[0]
                                mission = mission.strip()
                                if mission:
                                    proj["goal"] = mission
                        except Exception:
                            pass

                    # V38.1: Check for resume checkpoint (for history and progress, fallback for goal)
                    checkpoint_file = d / ".ag-supervisor" / "checkpoint.json"
                    if checkpoint_file.exists():
                        try:
                            cp = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                            proj["checkpoint"] = {
                                "goal": cp.get("goal", ""),
                                "timestamp": cp.get("timestamp", 0),
                                "dag_completed": cp.get("dag_completed", 0),
                                "dag_total": cp.get("dag_total", 0),
                                "dag_pending": cp.get("dag_pending", 0),
                                "dag_cancelled": cp.get("dag_cancelled", 0),
                            }
                            proj["has_session"] = True
                            proj["last_active"] = cp.get("timestamp", 0)
                            if not proj["goal"]:
                                proj["goal"] = cp.get("goal", "")
                        except Exception:
                            pass

                    # V54: Also read epic_state.json for accurate post-run counts.
                    # checkpoint.json is written periodically during execution but
                    # may have zeros if last checkpoint was before tasks completed.
                    epic_state_file = d / ".ag-supervisor" / "epic_state.json"
                    if epic_state_file.exists():
                        try:
                            es = json.loads(epic_state_file.read_text(encoding="utf-8"))
                            nodes = es.get("nodes", {})
                            if nodes:
                                _es_complete = sum(1 for n in nodes.values() if n.get("status") == "complete")
                                _es_total = len(nodes)
                                _es_pending = sum(1 for n in nodes.values() if n.get("status") == "pending")
                                # Update checkpoint counts if epic_state has better data
                                if proj.get("checkpoint"):
                                    if _es_total > proj["checkpoint"].get("dag_total", 0):
                                        proj["checkpoint"]["dag_completed"] = _es_complete
                                        proj["checkpoint"]["dag_total"] = _es_total
                                        proj["checkpoint"]["dag_pending"] = _es_pending
                                else:
                                    # No checkpoint at all — synthesize from epic_state
                                    proj["checkpoint"] = {
                                        "goal": proj.get("goal", ""),
                                        "timestamp": es.get("timestamp", 0) or proj.get("last_active", 0),
                                        "dag_completed": _es_complete,
                                        "dag_total": _es_total,
                                        "dag_pending": _es_pending,
                                        "dag_cancelled": 0,
                                    }
                                    if _es_complete > 0 or _es_pending > 0:
                                        proj["has_session"] = True
                        except Exception:
                            pass

                    # Check for saved session state (fallback for goal if mission block was empty)
                    session_file = d / ".ag-supervisor" / "session_state.json"
                    if not session_file.exists():
                        # Also check the supervisor's own session state
                        sup_session = Path(__file__).parent / "_session_state.json"
                        if sup_session.exists():
                            try:
                                data = json.loads(sup_session.read_text(encoding="utf-8"))
                                if data.get("project_path") and Path(data["project_path"]).resolve() == d.resolve():
                                    proj["has_session"] = True
                                    proj["last_active"] = data.get("timestamp", 0)
                                    if not proj["goal"]:
                                        proj["goal"] = data.get("goal", "")
                                    # Synthesize a minimal checkpoint so the resume panel shows
                                    if not proj["checkpoint"]:
                                        proj["checkpoint"] = {
                                            "goal": data.get("goal", proj["goal"]),
                                            "timestamp": data.get("timestamp", 0),
                                            "dag_completed": 0, "dag_total": 0,
                                            "dag_pending": 0, "dag_cancelled": 0,
                                        }
                            except Exception:
                                pass
                    else:
                        try:
                            data = json.loads(session_file.read_text(encoding="utf-8"))
                            proj["has_session"] = True
                            proj["last_active"] = data.get("timestamp", 0)
                            if not proj["goal"]:
                                proj["goal"] = data.get("goal", "")
                            # Synthesize a minimal checkpoint so the resume panel shows
                            # for projects that have a session but no checkpoint.json yet
                            # (e.g. projects started before periodic-checkpoint was deployed).
                            if not proj["checkpoint"]:
                                proj["checkpoint"] = {
                                    "goal": data.get("goal", proj["goal"]),
                                    "timestamp": data.get("timestamp", 0),
                                    "dag_completed": 0, "dag_total": 0,
                                    "dag_pending": 0, "dag_cancelled": 0,
                                }
                        except Exception:
                            pass

                    # V42 FIX: Fast file count — shallow only (top-level files).
                    # rglob('*') was hanging the event loop on heavy dirs (.venv, .git, node_modules).
                    try:
                        file_count = sum(1 for f in d.iterdir() if f.is_file())
                        proj["file_count"] = file_count
                    except Exception:
                        proj["file_count"] = 0

                    projects.append(proj)
        except Exception as exc:
            logger.error("Failed to list projects: %s", exc, exc_info=True)

        return JSONResponse({
            "projects": projects,
            "experiments_dir": str(exp_dir),
            "engine_running": state.engine_running,
            "current_project": state.project_path,
        })

    @app.post("/api/projects/launch")
    async def launch_project(body: dict):
        """Launch or continue a project."""
        project_path = body.get("project_path", "").strip()
        goal = body.get("goal", "").strip()
        new_note_text = body.get("instructions", "").strip()
        launch_mode = body.get("mode", "auto").strip().lower()
        if launch_mode in ("auto", "manual"):
            state.execution_mode = launch_mode

        if not project_path:
            return JSONResponse({"error": "No project_path provided"}, status_code=400)
        if not goal:
            return JSONResponse({"error": "No goal provided"}, status_code=400)

        # V56: Persist any new context note immediately before bootstrap
        if new_note_text:
            _notes = _load_notes(project_path)
            _now = time.time()
            import hashlib as _hl
            _nid = f"cn_{int(_now)}_{_hl.md5(new_note_text.encode()).hexdigest()[:6]}"
            _notes.append({"id": _nid, "text": new_note_text, "created_at": _now, "updated_at": _now})
            _save_notes(project_path, _notes)
            logger.info("📌 [Notes] Saved launch note %s", _nid)

        # V41: Persist directive immediately — don't wait for engine startup.
        # If the engine crashes before main.run() reaches _save_session_state(),
        # the goal would be lost. Save it NOW so it survives across restarts.
        # 1) Session state file (for auto-resume on reboot)
        try:
            from supervisor.main import _save_session_state
            _save_session_state(goal, project_path)
        except Exception as e:
            logger.debug("Session state save failed (non-fatal): %s", e)

        # 2) SUPERVISOR_MANDATE.md — goal + all persisted context notes
        try:
            from supervisor import bootstrap
            _all_notes = _load_notes(project_path)
            _full_goal = goal
            if _all_notes:
                _notes_lines = ["\n\n## Persistent Context Notes\n"]
                for _i, _n in enumerate(_all_notes, 1):
                    _notes_lines.append(f"{_i}. {_n['text']}")
                _full_goal = goal + "\n".join(_notes_lines)
            bootstrap.bootstrap_workspace(project_path, _full_goal)
            if _all_notes:
                logger.info("📌 [Notes] Appended %d context note(s) to mandate", len(_all_notes))
        except Exception as e:
            logger.debug("Bootstrap failed (will retry in engine): %s", e)

        # V51: Fresh Audit flag — if True, engine will discard old DAG
        # and re-audit the current codebase instead of resuming
        state.fresh_audit = bool(body.get("fresh_audit", False))

        # V54: Reset Phases flag — if True, phase_state.json is deleted
        # so Gemini plans fresh phases on next run
        state.reset_phases = bool(body.get("reset_phases", False))
        _fresh = state.fresh_audit  # already set above
        if state.reset_phases:
            _ag_dir = Path(project_path) / ".ag-supervisor"
            _phase_file = _ag_dir / "phase_state.json"
            _epic_file  = _ag_dir / "epic_state.json"

            # — Extract context BEFORE deletion so _create_initial_plan is informed —
            _completed_tasks, _pending_tasks = [], []
            try:
                if _phase_file.exists():
                    _ps = json.loads(_phase_file.read_text(encoding="utf-8"))
                    for ph in _ps.get("phases", []):
                        for t in ph.get("tasks", []):
                            # Phase system uses "done" (not "complete")
                            if t.get("status") in ("done", "complete"):
                                _completed_tasks.append(t.get("title", t.get("id", "?")))
                            elif t.get("status") != "done":
                                _pending_tasks.append(t.get("title", t.get("id", "?"))[:80])
            except Exception:
                pass
            try:
                if _epic_file.exists():
                    _es = json.loads(_epic_file.read_text(encoding="utf-8"))
                    for nid, nd in _es.get("nodes", {}).items():
                        if nd.get("status") == "complete":
                            _completed_tasks.append(nd.get("description", nid)[:80])
                        elif nd.get("status") in ("pending", "running"):
                            _pending_tasks.append(nd.get("description", nid)[:80])
            except Exception:
                pass

            # Write reset context — phase_manager._create_initial_plan reads this
            _ctx = {
                "reset_at": time.time(),
                "fresh_audit_also": _fresh,
                # Completed tasks always included so Gemini knows what’s done
                "completed_task_summaries": _completed_tasks,
                # Pending tasks only included on fresh audit — on reset-only,
                # the new phase plan should decide how to re-scope them
                "pending_task_summaries": _pending_tasks if _fresh else [],
            }
            try:
                _ag_dir.mkdir(parents=True, exist_ok=True)
                (_ag_dir / "phase_reset_context.json").write_text(
                    json.dumps(_ctx, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception as _we:
                logger.warning("📋  [Phase Reset] Could not write context: %s", _we)

            # Now delete phase_state.json
            if _phase_file.exists():
                try:
                    _phase_file.unlink()
                    logger.info("📋  [Phase Reset] Deleted phase_state.json (%d completed, %d pending tasks captured)",
                                len(_completed_tasks), len(_pending_tasks))
                except Exception as _pe:
                    logger.warning("📋  [Phase Reset] Could not delete phase_state.json: %s", _pe)

        # Signal to main.py to start the engine
        state.goal = goal
        state.project_path = project_path

        if state._run_callback:
            asyncio.create_task(state._run_callback(goal, project_path))
            return JSONResponse({"status": "launched", "goal": goal, "project": project_path})

        return JSONResponse({"status": "queued", "msg": "Engine will pick up on next cycle"})

    @app.post("/api/projects/create")
    async def create_project(request: Request):
        """Create a new project directory and optionally write the goal."""
        try:
            body = await request.json()
            name = str(body.get("name", "") or "").strip()
            goal = str(body.get("goal", "") or "").strip()

            if not name:
                return JSONResponse({"error": "No project name provided"}, status_code=400)

            # Sanitize name: allow only safe characters
            safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
            if not safe_name:
                return JSONResponse({"error": "Invalid project name"}, status_code=400)

            # Truncate to avoid exceeding Windows MAX_PATH (260 chars).
            if len(safe_name) > 80:
                safe_name = safe_name[:80].rstrip()

            project_dir = state.experiments_dir / safe_name
            logger.info("Creating project '%s' at: %s", safe_name, project_dir)
            project_dir.mkdir(parents=True, exist_ok=True)

            # If a goal was provided, write SUPERVISOR_MANDATE.md so
            # the launch panel pre-fills with it.
            if goal:
                mandate = project_dir / "SUPERVISOR_MANDATE.md"
                mandate.write_text(
                    f"# {safe_name}\n\n## YOUR MISSION\n\n{goal}\n\n---\n",
                    encoding="utf-8",
                )

            # V58: If research data was provided, write research.md
            research_content = str(body.get("research_content", "") or "").strip()
            if research_content:
                (project_dir / "research.md").write_text(research_content, encoding="utf-8")
                logger.info("📄  [Create] Wrote research.md (%d chars) for '%s'", len(research_content), safe_name)

            return JSONResponse({
                "status": "created",
                "name": safe_name,
                "path": str(project_dir),
            })
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error("Failed to create project: %s\n%s", str(e), tb)
            return JSONResponse({"error": f"Failed to create project: {str(e)}"}, status_code=500)

    # ── Research.md API ──────────────────────────────────────

    @app.get("/api/projects/research")
    async def get_project_research(path: str = ""):
        """Return the contents of research.md for the given project path."""
        if not path:
            return JSONResponse({"content": ""})
        try:
            proj = Path(path)
            for fname in ("research.md", "RESEARCH.md"):
                rpath = proj / fname
                if rpath.exists():
                    return JSONResponse({"content": rpath.read_text(encoding="utf-8")})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse({"content": ""})

    @app.post("/api/projects/research")
    async def save_project_research(body: dict):
        """Write or update research.md for the given project path."""
        path = body.get("path", "")
        content = body.get("content", "")
        if not path:
            return JSONResponse({"error": "path required"}, status_code=400)
        try:
            proj = Path(path)
            proj.mkdir(parents=True, exist_ok=True)
            rpath = proj / "research.md"
            rpath.write_text(content, encoding="utf-8")
            logger.info("📄  [API] Saved research.md to: %s (%d chars)", rpath, len(content))
            return JSONResponse({"status": "ok", "path": str(rpath)})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── WebSocket — Glass Brain Stream ──────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        """
        Glass Brain WebSocket.

        On connect: sends full state snapshot + recent logs.
        Then streams: log entries, state changes, instruction events.
        """
        await ws.accept()
        state._ws_clients.append(ws)
        logger.info("🔌 WebSocket client connected (%d total)", len(state._ws_clients))

        try:
            # Send initial state snapshot
            await ws.send_text(json.dumps({
                "type": "init",
                "state": state.to_dict(),
                "logs": list(state._log_buffer)[-100:],
            }))

            # V46: Server-side ping loop — sends {"type":"ping"} every 10s.
            # This gives the client's zombie detector a signal to check against,
            # AND if the send fails, we know the connection is dead.
            async def _server_ping():
                import asyncio as _aio
                try:
                    while True:
                        await _aio.sleep(10)
                        try:
                            # V46: Use write lock to prevent concurrent drain crash
                            async with state._ws_write_lock:
                                await ws.send_text('{"type":"ping"}')
                        except Exception:
                            break  # Connection dead — exit ping loop
                except _aio.CancelledError:
                    pass

            ping_task = asyncio.create_task(_server_ping())

            try:
                # Keep alive — wait for client messages
                while True:
                    data = await ws.receive_text()
                    try:
                        msg = json.loads(data)
                        if msg.get("type") == "instruction":
                            text = msg.get("text", "").strip()
                            if text:
                                await state.queue.push(text, source="ws")
                    except json.JSONDecodeError:
                        pass
            finally:
                ping_task.cancel()

        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if ws in state._ws_clients:
                state._ws_clients.remove(ws)
            logger.info("🔌 WebSocket client disconnected (%d remaining)", len(state._ws_clients))

    # ── Serve UI static files ───────────────────────────────

    @app.get("/")
    async def serve_index():
        """Serve the Command Centre dashboard."""
        index_path = _UI_DIR / "index.html"
        if index_path.exists():
            return HTMLResponse(
                index_path.read_text(encoding="utf-8", errors="replace"),
                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
            )
        return HTMLResponse("<h1>Supervisor AI — V61 Command Centre</h1><p>UI not found.</p>")

    # Mount static files for CSS/JS/assets if they exist
    if _UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")

    return app


# ─────────────────────────────────────────────────────────────
# Start API Server as Background Task
# ─────────────────────────────────────────────────────────────

async def start_api_server(
    state: SupervisorState,
    # V37 SECURITY FIX (H-7): Bind to localhost only.
    # Previously 0.0.0.0 exposed the API to any device on the LAN.
    host: str = "127.0.0.1",
    port: int = 8420,
) -> None:
    """
    Start the FastAPI server as an asyncio task inside the existing event loop.

    This runs ALONGSIDE the supervisor engine — same loop, shared state.
    Closing the browser tab has zero impact on the engine.
    """
    import uvicorn

    app = create_app(state)

    # V45: Install the WebSocket log handler on the ROOT logger so ALL Python
    # logs feed the Glass Brain Logs tab — not just supervisor.* children.
    # Previously only attached to getLogger("supervisor") which missed some
    # edge cases where logs didn't propagate during async waits.
    ws_handler = WebSocketLogHandler(state)
    ws_handler.setLevel(logging.INFO)
    ws_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(name)-30s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.getLogger().addHandler(ws_handler)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",  # Suppress uvicorn's own logs
        access_log=False,
        # V46: Disable websockets keepalive pings — they crash with
        # AssertionError in _drain_helper when heavy concurrent I/O
        # (multiple Gemini CLI spawns) creates back-pressure on the WS
        # connection. The client already has auto-reconnect with
        # exponential backoff, so server-side pings are unnecessary.
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
    server = uvicorn.Server(config)

    logger.info("🌐 Command Centre starting on http://localhost:%d", port)
    print(f"\n  \033[1;36m🌐 V61 Command Centre: http://localhost:{port}\033[0m\n")

    # V74: Smart periodic /stats probe — keeps dashboard quota data fresh,
    # but suppresses probing during known long cooldowns to save PTY resources.
    async def _periodic_stats_probe():
        import asyncio as _ps_aio
        await _ps_aio.sleep(30)  # Initial delay — let boot finish first
        while True:
            # V74: Exit on shutdown — prevents orphaned PTY calls after clean stop
            if state and getattr(state, 'stop_requested', False):
                logger.debug("📊  [Periodic] Stop requested — exiting probe loop")
                break

            _sleep_interval = 60  # Default: probe every 60s
            try:
                from .retry_policy import get_quota_probe, get_daily_budget, get_failover_chain
                _qp = get_quota_probe()

                # V74: Smart suppression — skip probes when quota is paused
                # or all models are exhausted. Sleep until ~5min before reset.
                _budget = get_daily_budget()
                _fc = get_failover_chain()
                _all_exhausted = _budget.quota_paused or _fc.all_models_on_cooldown()

                if _all_exhausted:
                    # Find soonest known reset time from probe snapshots
                    _now = __import__('time').time()
                    _soonest_reset = 0.0
                    for _snap in _qp._snapshots.values():
                        if isinstance(_snap, dict):
                            _rat = _snap.get("resets_at", 0)
                            if _rat > _now and (_soonest_reset == 0 or _rat < _soonest_reset):
                                _soonest_reset = _rat
                    # Also check budget's resume_at
                    if _budget._quota_resume_at > _now:
                        if _soonest_reset == 0 or _budget._quota_resume_at < _soonest_reset:
                            _soonest_reset = _budget._quota_resume_at

                    if _soonest_reset > 0:
                        _until_reset = _soonest_reset - _now
                        # Sleep until 5 minutes before reset, minimum 60s
                        _sleep_interval = max(60, _until_reset - 300)
                        _h, _m = int(_sleep_interval) // 3600, (int(_sleep_interval) % 3600) // 60
                        logger.info(
                            "📊  [Periodic] Quota exhausted — suppressing probes for %dh%02dm "
                            "(will resume ~5min before reset)",
                            _h, _m,
                        )
                    else:
                        # No known reset time — probe every 5min as fallback
                        _sleep_interval = 300
                        logger.debug("📊  [Periodic] Quota exhausted, no reset time known — sleeping 5min")
                else:
                    # Normal operation — run the probe
                    _qp.run_stats_probe()
                    logger.debug("📊  [Periodic] /stats probe completed")
            except Exception as _ps_exc:
                logger.debug("📊  [Periodic] /stats probe failed: %s", _ps_exc)

            # V74: Stop-aware sleep — breaks long intervals into 10s chunks
            # so the loop can exit within 10s of a stop signal
            _slept = 0
            while _slept < _sleep_interval:
                if state and getattr(state, 'stop_requested', False):
                    break
                _chunk = min(10, _sleep_interval - _slept)
                await _ps_aio.sleep(_chunk)
                _slept += _chunk

    asyncio.create_task(_periodic_stats_probe())

    await server.serve()

