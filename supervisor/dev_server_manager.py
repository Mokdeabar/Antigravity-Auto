"""
V74: Dev Server Manager (Audit §4.5 — headless_executor split)

Extracted from headless_executor.py: all dev server lifecycle management
including server detection, startup, health polling, port discovery,
console scanning, and cleanup.

The original HeadlessExecutor methods remain in headless_executor.py
(user instruction: do not delete dead code). New code should import
from this module for dev server operations.

Integration points:
  - main.py: call DevServerManager.start() after environment bootstrap
  - lighthouse_runner.py: use get_active_port() for audit target
  - visual_qa_engine.py: use get_active_port() for screenshot capture
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("supervisor.dev_server_manager")


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

DEFAULT_DEV_PORTS = [5173, 3000, 3001, 4173, 8080, 8000]
MAX_STARTUP_WAIT_S = 45
POLL_INTERVAL_S = 1.0

# Dev server start commands by framework
DEV_COMMANDS = {
    "vite": "npx vite --host 0.0.0.0",
    "next": "npx next dev",
    "react-scripts": "npx react-scripts start",
    "webpack": "npx webpack serve",
    "nuxt": "npx nuxt dev",
    "astro": "npx astro dev --host 0.0.0.0",
    "remix": "npx remix dev",
    "default": "npm run dev",
}


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class DevServerState:
    """Current state of the development server."""
    running: bool = False
    port: int = 0
    url: str = ""
    framework: str = ""
    pid: int = 0
    start_command: str = ""
    startup_duration_s: float = 0.0
    console_errors: list[str] = field(default_factory=list)
    console_warnings: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.running and self.port > 0 and not self.console_errors

    def summary(self) -> str:
        if not self.running:
            return "❌ Dev server not running"
        status = "✅" if self.healthy else "⚠️"
        parts = [f"{status} {self.framework or 'dev'} server on :{self.port}"]
        if self.console_errors:
            parts.append(f"({len(self.console_errors)} errors)")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "port": self.port,
            "url": self.url,
            "framework": self.framework,
            "healthy": self.healthy,
            "startup_duration_s": round(self.startup_duration_s, 1),
            "console_errors": self.console_errors[:10],
        }


# ─────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────

class DevServerManager:
    """
    Manages dev server lifecycle inside the sandbox container.

    Features:
      - Auto-detects framework and appropriate start command
      - Polls for server readiness with configurable timeout
      - Discovers active port via process inspection or response
      - Scans console output for errors and warnings
      - Provides clean shutdown

    Usage:
        mgr = DevServerManager(sandbox)
        state = await mgr.start(workspace="/workspace")
        if state.healthy:
            port = state.port  # use for Lighthouse, Visual QA, etc.
        await mgr.stop()
    """

    def __init__(self, sandbox, workspace: str = "/workspace"):
        self._sandbox = sandbox
        self._workspace = workspace
        self._state = DevServerState()

    async def start(
        self,
        workspace: str | None = None,
        port: int | None = None,
        command: str | None = None,
        timeout_s: int = MAX_STARTUP_WAIT_S,
    ) -> DevServerState:
        """
        Start the dev server and wait for it to become healthy.

        Args:
            workspace: Project directory (default: /workspace)
            port: Force a specific port. Auto-detected if None.
            command: Force a specific command. Auto-detected if None.
            timeout_s: Max seconds to wait for server readiness

        Returns:
            DevServerState with running status, port, and health info
        """
        ws = workspace or self._workspace
        start_time = time.time()

        # Detect framework if command not specified
        if not command:
            framework, command = await self._detect_framework(ws)
            self._state.framework = framework
        else:
            self._state.framework = "custom"

        self._state.start_command = command

        # If port specified, inject it
        if port:
            command = f"PORT={port} {command}"

        logger.info("🖥️  [DevServer] Starting: %s", command)

        # Start in background
        try:
            await self._sandbox.exec_command(
                f"cd {ws} && nohup {command} > /tmp/dev-server.log 2>&1 &",
                timeout=10,
            )
        except Exception as exc:
            self._state.running = False
            logger.warning("🖥️  [DevServer] Start failed: %s", exc)
            return self._state

        # Wait for server to respond
        detected_port = port or 0
        for _ in range(timeout_s):
            import asyncio
            await asyncio.sleep(POLL_INTERVAL_S)

            # If no port specified, try to detect it
            if not detected_port:
                detected_port = await self._detect_port()

            if detected_port:
                if await self._check_health(detected_port):
                    self._state.running = True
                    self._state.port = detected_port
                    self._state.url = f"http://localhost:{detected_port}"
                    break

        self._state.startup_duration_s = time.time() - start_time

        if self._state.running:
            # Scan console for issues
            await self._scan_console()
            logger.info("🖥️  %s (%.1fs)", self._state.summary(), self._state.startup_duration_s)
        else:
            logger.warning("🖥️  [DevServer] Failed to start within %ds", timeout_s)
            # Capture console output for debugging
            await self._scan_console()

        return self._state

    async def stop(self) -> bool:
        """Stop the dev server cleanly."""
        if not self._state.running:
            return True

        try:
            # Kill by port or process name
            if self._state.port:
                await self._sandbox.exec_command(
                    f"fuser -k {self._state.port}/tcp 2>/dev/null || true",
                    timeout=10,
                )

            await self._sandbox.exec_command(
                f"pkill -f '{self._state.start_command}' 2>/dev/null || true",
                timeout=10,
            )

            self._state.running = False
            logger.info("🖥️  [DevServer] Stopped")
            return True

        except Exception as exc:
            logger.warning("🖥️  [DevServer] Stop error: %s", exc)
            return False

    async def restart(self) -> DevServerState:
        """Stop and restart the dev server."""
        await self.stop()
        import asyncio
        await asyncio.sleep(2)
        return await self.start(
            port=self._state.port if self._state.port else None,
            command=self._state.start_command if self._state.start_command else None,
        )

    def get_active_port(self) -> int:
        """Get the active dev server port (or 0 if not running)."""
        return self._state.port if self._state.running else 0

    @property
    def state(self) -> DevServerState:
        return self._state

    async def _detect_framework(self, workspace: str) -> tuple[str, str]:
        """Detect the project framework and appropriate dev command."""
        # Check package.json scripts
        try:
            result = await self._sandbox.exec_command(
                f"cat {workspace}/package.json 2>/dev/null | head -c 5000",
                timeout=5,
            )
            if result.stdout:
                import json
                pkg = json.loads(result.stdout)
                scripts = pkg.get("scripts", {})
                deps = set(pkg.get("dependencies", {}).keys())
                deps |= set(pkg.get("devDependencies", {}).keys())

                # Check for "dev" script (most common)
                if "dev" in scripts:
                    # Detect framework from deps
                    if "vite" in deps:
                        return "vite", "npm run dev"
                    elif "next" in deps:
                        return "next", "npm run dev"
                    elif "nuxt" in deps:
                        return "nuxt", "npm run dev"
                    elif "astro" in deps:
                        return "astro", "npm run dev"
                    elif "react-scripts" in deps:
                        return "react-scripts", "npm run start"
                    return "npm", "npm run dev"

                # "start" script as fallback
                if "start" in scripts:
                    return "npm", "npm start"

        except Exception:
            pass

        # Check for Vite config
        for cfg in ("vite.config.ts", "vite.config.js", "vite.config.mjs"):
            try:
                check = await self._sandbox.exec_command(
                    f"test -f {workspace}/{cfg} && echo 'EXISTS'",
                    timeout=5,
                )
                if "EXISTS" in (check.stdout or ""):
                    return "vite", DEV_COMMANDS["vite"]
            except Exception:
                pass

        return "default", DEV_COMMANDS["default"]

    async def _detect_port(self) -> int:
        """Try to detect which port the dev server is listening on."""
        # Check common ports
        for port in DEFAULT_DEV_PORTS:
            if await self._check_health(port):
                return port

        # Try to find listening port from process list
        try:
            result = await self._sandbox.exec_command(
                "ss -tlnp 2>/dev/null | grep -E ':(3000|3001|4173|5173|8080|8000)' | head -5",
                timeout=5,
            )
            if result.stdout:
                # Extract port number
                match = re.search(r':(\d{4,5})\s', result.stdout)
                if match:
                    return int(match.group(1))
        except Exception:
            pass

        return 0

    async def _check_health(self, port: int) -> bool:
        """Check if dev server is responding on given port."""
        try:
            result = await self._sandbox.exec_command(
                f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/ 2>/dev/null",
                timeout=5,
            )
            status = (result.stdout or "").strip()
            return status in ("200", "301", "302", "304")
        except Exception:
            return False

    async def _scan_console(self) -> None:
        """Scan dev server console output for errors and warnings."""
        try:
            result = await self._sandbox.exec_command(
                "cat /tmp/dev-server.log 2>/dev/null | tail -100",
                timeout=5,
            )
            if not result.stdout:
                return

            for line in result.stdout.splitlines():
                line_lower = line.lower()
                if any(k in line_lower for k in ("error", "err!", "failed", "enoent", "cannot find")):
                    if line.strip() not in self._state.console_errors:
                        self._state.console_errors.append(line.strip()[:200])
                elif any(k in line_lower for k in ("warn", "deprecat")):
                    if line.strip() not in self._state.console_warnings:
                        self._state.console_warnings.append(line.strip()[:200])

            # Cap lists
            self._state.console_errors = self._state.console_errors[:20]
            self._state.console_warnings = self._state.console_warnings[:20]

        except Exception:
            pass
