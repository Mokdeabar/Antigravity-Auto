"""
api_server.py — V35 Command Centre API Bridge.

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

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .instruction_queue import InstructionQueue

logger = logging.getLogger("supervisor.api_server")

# UI static files directory
_UI_DIR = Path(__file__).parent / "ui"


# ─────────────────────────────────────────────────────────────
# Supervisor State — Shared singleton between engine + API
# ─────────────────────────────────────────────────────────────

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
        self._log_buffer: deque[dict] = deque(maxlen=500)

        # WebSocket clients
        self._ws_clients: list[WebSocket] = []

        # Ollama status
        self.ollama_online = False

        # Task result
        self.last_task_status = ""
        self.last_task_duration = 0.0
        self.files_changed: list[str] = []

        # Engine launch state (for project launcher)
        self.engine_running = False
        self._run_callback = None  # set by main.py to start the engine

        # Experiments directory
        self.experiments_dir = Path(r"c:\Users\mokde\Desktop\Experiments")

    @property
    def uptime_s(self) -> float:
        return time.time() - self.uptime_start

    def log(self, level: str, message: str, **extra) -> None:
        """Add a log entry and broadcast to all WebSocket clients."""
        entry = {
            "ts": time.time(),
            "level": level,
            "msg": message,
            **extra,
        }
        self._log_buffer.append(entry)
        # Fire-and-forget broadcast
        asyncio.ensure_future(self._broadcast(entry))

    async def _broadcast(self, data: dict) -> None:
        """Push data to all connected WebSocket clients."""
        if not self._ws_clients:
            return
        payload = json.dumps(data)
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)

    async def broadcast_state(self) -> None:
        """Push full state snapshot to all WebSocket clients."""
        await self._broadcast({
            "type": "state",
            **self.to_dict(),
        })

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "project_path": self.project_path,
            "status": self.status,
            "active_model": self.active_model,
            "container_id": self.container_id,
            "container_health": self.container_health,
            "mount_mode": self.mount_mode,
            "uptime_s": round(self.uptime_s, 1),
            "loop_count": self.loop_count,
            "last_action": self.last_action,
            "preview_port": self.preview_port,
            "preview_running": self.preview_running,
            "ollama_online": self.ollama_online,
            "last_task_status": self.last_task_status,
            "last_task_duration": round(self.last_task_duration, 1),
            "files_changed": self.files_changed[:20],
            "queue_size": self.queue.size,
        }


# ─────────────────────────────────────────────────────────────
# WebSocket Log Handler — pipes Python logging to Glass Brain
# ─────────────────────────────────────────────────────────────

class WebSocketLogHandler(logging.Handler):
    """Logging handler that feeds log records into the SupervisorState ring buffer."""

    def __init__(self, state: SupervisorState):
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord):
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
        title="Supervisor AI — V35 Command Centre",
        version="35.0",
        docs_url=None,  # Disable Swagger — this is a UI server
        redoc_url=None,
    )

    # Allow file:// origin (null) and localhost for cross-origin access
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── REST Endpoints ──────────────────────────────────────

    @app.get("/api/state")
    async def get_state():
        """Return current supervisor state."""
        return JSONResponse(state.to_dict())

    @app.get("/api/logs")
    async def get_logs(n: int = 100):
        """Return recent log entries from the Glass Brain ring buffer."""
        entries = list(state._log_buffer)[-n:]
        return JSONResponse({"logs": entries, "total": len(state._log_buffer)})

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

        logger.info("📬 UI instruction received: %s", text[:80])
        return JSONResponse({
            "status": "queued",
            "queue_size": state.queue.size,
            "instruction": instruction.to_dict(),
        })

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

    # ── Project Management Endpoints ───────────────────────

    @app.get("/api/projects")
    async def list_projects():
        """List all workspaces in the Experiments directory with optional session state."""
        projects = []
        exp_dir = state.experiments_dir

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
                }

                # Check for saved session state
                session_file = d / ".ag-supervisor" / "session_state.json"
                if not session_file.exists():
                    # Also check the supervisor's own session state
                    sup_session = Path(__file__).parent / "_session_state.json"
                    if sup_session.exists():
                        try:
                            data = json.loads(sup_session.read_text(encoding="utf-8"))
                            if data.get("project_path") and Path(data["project_path"]).resolve() == d.resolve():
                                proj["has_session"] = True
                                proj["goal"] = data.get("goal", "")[:120]
                                proj["last_active"] = data.get("timestamp", 0)
                        except Exception:
                            pass
                else:
                    try:
                        data = json.loads(session_file.read_text(encoding="utf-8"))
                        proj["has_session"] = True
                        proj["goal"] = data.get("goal", "")[:120]
                        proj["last_active"] = data.get("timestamp", 0)
                    except Exception:
                        pass

                # Get file count and rough size
                try:
                    file_count = sum(1 for _ in d.rglob('*') if _.is_file())
                    proj["file_count"] = min(file_count, 9999)
                except Exception:
                    proj["file_count"] = 0

                projects.append(proj)

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
        pre_instructions = body.get("instructions", "").strip()

        if not project_path:
            return JSONResponse({"error": "No project_path provided"}, status_code=400)
        if not goal:
            return JSONResponse({"error": "No goal provided"}, status_code=400)

        # Queue pre-flight instructions if provided
        if pre_instructions:
            await state.queue.push(pre_instructions, source="launcher")

        # Signal to main.py to start the engine
        state.goal = goal
        state.project_path = project_path

        if state._run_callback:
            asyncio.create_task(state._run_callback(goal, project_path))
            return JSONResponse({"status": "launched", "goal": goal, "project": project_path})

        return JSONResponse({"status": "queued", "msg": "Engine will pick up on next cycle"})

    @app.post("/api/projects/create")
    async def create_project(body: dict):
        """Create a new project directory in the Experiments folder."""
        name = body.get("name", "").strip()
        if not name:
            return JSONResponse({"error": "No project name provided"}, status_code=400)

        # Sanitize name
        safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
        if not safe_name:
            return JSONResponse({"error": "Invalid project name"}, status_code=400)

        project_dir = state.experiments_dir / safe_name
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
            return JSONResponse({
                "status": "created",
                "name": safe_name,
                "path": str(project_dir),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

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

            # Keep alive — wait for client messages (instructions can also come via WS)
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
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Supervisor AI — V35 Command Centre</h1><p>UI not found.</p>")

    # Mount static files for CSS/JS/assets if they exist
    if _UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")

    return app


# ─────────────────────────────────────────────────────────────
# Start API Server as Background Task
# ─────────────────────────────────────────────────────────────

async def start_api_server(
    state: SupervisorState,
    host: str = "0.0.0.0",
    port: int = 8420,
) -> None:
    """
    Start the FastAPI server as an asyncio task inside the existing event loop.

    This runs ALONGSIDE the supervisor engine — same loop, shared state.
    Closing the browser tab has zero impact on the engine.
    """
    import uvicorn

    app = create_app(state)

    # Install the WebSocket log handler so all Python logs feed the Glass Brain
    ws_handler = WebSocketLogHandler(state)
    ws_handler.setLevel(logging.INFO)
    ws_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("supervisor").addHandler(ws_handler)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",  # Suppress uvicorn's own logs
        access_log=False,
    )
    server = uvicorn.Server(config)

    logger.info("🌐 Command Centre starting on http://localhost:%d", port)
    print(f"\n  \033[1;36m🌐 V35 Command Centre: http://localhost:{port}\033[0m\n")

    await server.serve()
