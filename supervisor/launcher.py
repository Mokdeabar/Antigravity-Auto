"""
launcher.py — V44 Command Centre Standalone Launcher.

Starts the API server in STANDBY mode (no engine, no goal required).
The user opens http://localhost:8420, selects a project from the launcher,
and clicks Launch. The engine starts as a background task.

Usage:
    python -m supervisor.launcher
    python supervisor/launcher.py

This is the recommended entry point for the V44 Command Centre.
"""

import asyncio
import logging
import os
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

# V42: Ensure UTF-8 output on Windows to prevent UnicodeEncodeError
# when printing box-drawing characters to cp1252 terminals.
if sys.platform == "win32" and not os.environ.get("PYTHONIOENCODING"):
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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

        logger.info("🚀 Engine starting: goal=%s, project=%s", goal, project_path)
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

    # ── V73: Early PTY probe — warm up Gemini CLI while user browses ──
    # Start the /stats probe NOW so the PTY is warm (~25s cold start)
    # by the time the user picks a project. Stored on state so run()
    # can reuse it instead of spawning a duplicate.
    import threading
    def _early_probe():
        try:
            from supervisor.retry_policy import get_quota_probe
            _qp = get_quota_probe()
            for _m in list(_qp._snapshots.keys()):
                _qp._auto_reset_if_due(_m)
            _count = _qp.run_stats_probe()
            if _count:
                logger.info("📊  [Early] PTY probe: %d models loaded from CLI /stats.", _count)
            else:
                logger.info("📊  [Early] PTY probe returned no data — estimation active.")
        except Exception as _exc:
            logger.debug("📊  [Early] Probe failed: %s", _exc)

    state._early_probe_thread = threading.Thread(
        target=_early_probe, daemon=True, name="early-pty-probe"
    )
    state._early_probe_thread.start()
    logger.info("📊  [Early] Background PTY probe started at Command Centre open.")

    # Wire up the run callback so the UI can launch the engine
    state._run_callback = lambda goal, path: _run_engine(state, goal, path)

    try:
        print(f"""
  {C}╔══════════════════════════════════════════════════╗
  ║        ⚡ Supervisor AI — V62 Command Centre      ║
  ║                                                    ║
  ║   {G}http://localhost:{port}{C}                            ║
  ║                                                    ║
  ║   Standing by. Select a project in the UI.         ║
  ╚══════════════════════════════════════════════════╝{R}
""")
    except UnicodeEncodeError:
        # Fallback for terminals that can't render box-drawing chars (cp1252)
        print(f"""
  {C}=== Supervisor AI - V62 Command Centre ==={R}
  {G}http://localhost:{port}{R}
  Standing by. Select a project in the UI.
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
