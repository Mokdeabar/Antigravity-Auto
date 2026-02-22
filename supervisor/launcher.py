"""
launcher.py — V35 Command Centre Standalone Launcher.

Starts the API server in STANDBY mode (no engine, no goal required).
The user opens http://localhost:8420, selects a project from the launcher,
and clicks Launch. The engine starts as a background task.

Usage:
    python -m supervisor.launcher
    python supervisor/launcher.py

This is the recommended entry point for the V35 Command Centre.
"""

import asyncio
import logging
import os
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

# Ensure the supervisor package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor.api_server import SupervisorState, WebSocketLogHandler, create_app, start_api_server

logger = logging.getLogger("supervisor.launcher")

# ─────────────────────────────────────────────────────────────
# ANSI colours
# ─────────────────────────────────────────────────────────────
C = "\033[1;36m"   # Cyan
G = "\033[1;32m"   # Green
Y = "\033[1;33m"   # Yellow
R = "\033[0m"      # Reset


def _kill_stale_server(port: int) -> bool:
    """
    Check if the port is already in use and kill the owning process.
    Returns True if a stale process was killed.
    """
    # Quick socket probe
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
        # Connected — port is in use
    except (ConnectionRefusedError, OSError):
        return False  # Port is free

    print(f"  {Y}⚠ Port {port} is in use. Killing stale process…{R}")

    if sys.platform == "win32":
        # Use netstat to find the PID, then taskkill
        try:
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=5,
                    )
                    print(f"  {G}✓ Killed stale process PID {pid}{R}")
                    import time
                    time.sleep(1)  # Give OS time to release the port
                    return True
        except Exception:
            pass
    else:
        # Unix: fuser
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                capture_output=True, timeout=5,
            )
            import time
            time.sleep(1)
            return True
        except Exception:
            pass

    return False


async def _run_engine(state: SupervisorState, goal: str, project_path: str):
    """
    Start the full supervisor engine as a background task.

    This imports the heavy run() function only when needed (lazy import)
    so the launcher boots fast. Passes the existing state so run() doesn't
    create a duplicate API server.
    """
    try:
        from supervisor.main import run
        state.engine_running = True
        state.status = "initializing"
        await state.broadcast_state()

        logger.info("🚀 Engine starting: goal=%s, project=%s", goal[:60], project_path)
        await run(goal, project_path, dry_run=False, existing_state=state)

    except Exception as e:
        logger.error("❌ Engine error: %s", e, exc_info=True)
        state.status = "error"
        state.engine_running = False
        await state.broadcast_state()


async def main():
    """Start the Command Centre in standby mode."""

    # ── Setup logging ──
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-30s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    port = int(os.getenv("COMMAND_CENTRE_PORT", "8420"))

    # ── Kill any stale server on this port ──
    _kill_stale_server(port)

    # ── Create shared state (no goal yet — standby mode) ──
    state = SupervisorState(goal="", project_path="")

    # Wire up the run callback so the UI can launch the engine
    state._run_callback = lambda goal, path: _run_engine(state, goal, path)

    print(f"""
  {C}╔══════════════════════════════════════════════════╗
  ║        ⚡ Supervisor AI — V35 Command Centre      ║
  ║                                                    ║
  ║   {G}http://localhost:{port}{C}                            ║
  ║                                                    ║
  ║   Standing by. Select a project in the UI.         ║
  ╚══════════════════════════════════════════════════╝{R}
""")

    # ── Open browser automatically ──
    webbrowser.open(f"http://localhost:{port}")

    # ── Start API server (blocks forever) ──
    await start_api_server(state, host="0.0.0.0", port=port)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  {Y}⚡ Command Centre shut down.{R}\n")
