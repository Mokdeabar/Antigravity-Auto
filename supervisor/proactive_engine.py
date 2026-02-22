"""
proactive_engine.py — Proactive Autonomy Engine V1 (The Initiator).

Instead of just reacting to problems (WAITING, CRASHED), the supervisor
now proactively manages the IDE environment. Inspired by OpenClaw's
heartbeat scheduler and session model.

Capabilities:
  1. Auto-open Antigravity Browser Extension when dev server detected
  2. Auto-detect server port from terminal output
  3. Auto-refresh browser preview when files change
  4. Answer agent questions using Gemini + session context
  5. Trigger quality refinement when task appears complete
  6. Detect and resolve common blockers before they stall the agent
"""

import asyncio
import logging
import re
import time
from typing import Optional

from playwright.async_api import Page, BrowserContext

from . import config

logger = logging.getLogger("supervisor.proactive_engine")


# V34: Resolve PowerShell executable reliably
def _resolve_powershell() -> str:
    """Find the PowerShell executable, preferring pwsh then powershell.exe."""
    import os
    import shutil
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    # Fallback: Windows system32 path
    sys32 = os.path.join(
        os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    if os.path.exists(sys32):
        return sys32
    return "powershell.exe"  # last resort


# ─────────────────────────────────────────────────────────────
# Server detection patterns
# ─────────────────────────────────────────────────────────────

_SERVER_PORT_PATTERNS = [
    re.compile(r"(?:listening|running|started|ready)\s+(?:on|at)\s+.*?(?::|port\s*)(\d{4,5})", re.IGNORECASE),
    re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{4,5})", re.IGNORECASE),
    re.compile(r"Local:\s*https?://.*?:(\d{4,5})", re.IGNORECASE),
    re.compile(r"Network:\s*https?://.*?:(\d{4,5})", re.IGNORECASE),
    re.compile(r"port\s+(\d{4,5})", re.IGNORECASE),
]


# ─────────────────────────────────────────────────────────────
# Proactive Engine
# ─────────────────────────────────────────────────────────────

class ProactiveEngine:
    """
    Fires proactive actions based on context analysis.
    Called on a heartbeat from the main monitoring loop.
    """

    def __init__(self):
        self._last_detected_port: int = 0
        self._simple_browser_opened: bool = False
        self._last_refinement_trigger: float = 0.0
        self._refinement_cooldown: float = 300.0  # 5 min between refinement triggers
        self._questions_answered: int = 0
        self._max_auto_answers: int = 5  # Don't answer more than N questions per session

    async def heartbeat(self, ctx, page: Page, context: BrowserContext) -> list[str]:
        """
        Called every HEARTBEAT_INTERVAL_SECONDS.
        Analyzes the context snapshot and takes proactive actions.

        Returns a list of action descriptions for logging.
        """
        actions: list[str] = []

        try:
            # 1. Browser preview management
            browser_action = await self._manage_simple_browser(ctx, page, context)
            if browser_action:
                actions.append(browser_action)

            # 2. Port detection from terminal
            port_action = self._detect_port(ctx)
            if port_action:
                actions.append(port_action)

            # 3. Auto-answer agent questions
            answer_action = await self._try_auto_answer(ctx, page, context)
            if answer_action:
                actions.append(answer_action)

            # 4. Refinement trigger
            refine_action = await self._check_refinement(ctx, page, context)
            if refine_action:
                actions.append(refine_action)

        except Exception as exc:
            logger.warning("💡  Heartbeat error: %s", exc)

        return actions

    # ────────────────────────────────────────────────────
    # 1. Browser Preview Management
    # ────────────────────────────────────────────────────

    async def _manage_simple_browser(
        self, ctx, page: Page, context: BrowserContext,
    ) -> Optional[str]:
        """Auto-open Antigravity Browser Extension when a dev server is detected."""
        if not getattr(config, "SIMPLE_BROWSER_AUTO_OPEN", True):
            return None

        # Server running + browser not open → open it
        if ctx.dev_server_status.running and not ctx.simple_browser_open:
            port = ctx.dev_server_status.port
            if port and not self._simple_browser_opened:
                opened = await self._open_simple_browser(page, port)
                if opened:
                    self._simple_browser_opened = True
                    return f"Opened Antigravity Browser Extension → http://localhost:{port}"

        return None

    async def _open_simple_browser(self, page: Page, port: int) -> bool:
        """
        Open the Simple Browser side panel by running open_browser.ps1 externally.
        
        CRITICAL: Do NOT use Command Palette keystrokes via Playwright.
        Injecting 'Simple Browser: Show' through the controlled page causes
        a top-level navigation that the route guard blocks mid-flight,
        corrupting Electron's renderer and leaving a blank screen.
        
        Instead, we shell out to the golden open_browser.ps1 script which
        uses Windows SendKeys externally (outside the Playwright context).
        """
        import subprocess
        url = f"http://localhost:{port}"

        try:
            # Find the script in the project root
            project_path = config.get_project_path()
            if not project_path:
                logger.warning("🌐  No project path set — cannot locate open_browser.ps1")
                return False

            script_path = project_path / "open_browser.ps1"
            if not script_path.exists():
                logger.warning("🌐  open_browser.ps1 not found at %s", script_path)
                return False

            # Fire-and-forget: run externally, outside of Playwright
            ps_exe = _resolve_powershell()
            subprocess.Popen(
                [ps_exe, "-ExecutionPolicy", "Bypass", "-File",
                 str(script_path), "-Url", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if config.IS_WINDOWS else 0,
            )
            logger.info("🌐  Launched open_browser.ps1 → %s", url)
            # Give the script time to execute SendKeys
            await asyncio.sleep(5.0)
            return True

        except Exception as exc:
            logger.warning("🌐  Failed to launch open_browser.ps1: %s", exc)

        return False

    # ────────────────────────────────────────────────────
    # 2. Port Detection
    # ────────────────────────────────────────────────────

    def _detect_port(self, ctx) -> Optional[str]:
        """Check terminal output for newly started server and detect its port."""
        if not ctx.terminal_output:
            return None

        # Check the last 10 terminal lines for server startup
        recent_lines = ctx.terminal_output[-10:]
        for line in recent_lines:
            for pattern in _SERVER_PORT_PATTERNS:
                match = pattern.search(line)
                if match:
                    port = int(match.group(1))
                    # Ignore obviously wrong ports
                    if 1024 <= port <= 65535 and port != self._last_detected_port:
                        old_port = self._last_detected_port
                        self._last_detected_port = port
                        # Update config so context engine checks the right port
                        if hasattr(config, "DEV_SERVER_CHECK_PORT"):
                            config.DEV_SERVER_CHECK_PORT = port
                        if old_port:
                            return f"Server port changed: {old_port} → {port}"
                        else:
                            return f"Detected dev server on port {port}"

        return None

    # ────────────────────────────────────────────────────
    # 3. Auto-Answer Agent Questions
    # ────────────────────────────────────────────────────

    async def _try_auto_answer(
        self, ctx, page: Page, context: BrowserContext,
    ) -> Optional[str]:
        """
        When the agent asks a question (ASKING state), try to auto-answer it
        using the session context. Only answers simple yes/no or choice questions.
        """
        if ctx.agent_status != "ASKING":
            return None

        if self._questions_answered >= self._max_auto_answers:
            logger.debug("💡  Max auto-answers reached (%d)", self._max_auto_answers)
            return None

        # Get the question from the last message
        if not ctx.chat_messages:
            return None

        last_msg = ctx.chat_messages[-1]
        if last_msg.message_type != "question":
            return None

        question = last_msg.content

        # Only auto-answer safe questions
        answer = self._generate_safe_answer(question)
        if not answer:
            return None

        # Inject the answer
        try:
            from .injector import inject_text
            success = await inject_text(context, answer)
            if success:
                self._questions_answered += 1
                return f"Auto-answered: '{answer[:50]}' to question: '{question[:50]}...'"
        except Exception as exc:
            logger.warning("💡  Auto-answer injection failed: %s", exc)

        return None

    def _generate_safe_answer(self, question: str) -> Optional[str]:
        """
        Generate a safe auto-answer for simple questions.
        Only answers VERY obvious questions to avoid misguiding the agent.
        """
        q_lower = question.lower()

        # "Would you like me to continue?" → Yes
        if any(phrase in q_lower for phrase in [
            "would you like me to continue",
            "shall i continue",
            "should i continue",
            "want me to continue",
            "may i continue",
            "proceed with",
            "shall i proceed",
            "should i proceed",
            "want me to proceed",
        ]):
            return "Yes, please continue."

        # "Would you like me to install X?" → Yes
        if any(phrase in q_lower for phrase in [
            "would you like me to install",
            "shall i install",
            "should i install",
            "want me to install",
        ]):
            return "Yes, please install it."

        # "Would you like me to create/fix/update X?" → Yes
        if any(phrase in q_lower for phrase in [
            "would you like me to create",
            "would you like me to fix",
            "would you like me to update",
            "shall i create",
            "shall i fix",
            "shall i update",
            "should i create",
            "should i fix",
            "should i update",
        ]):
            return "Yes, go ahead."

        # "Would you like to see X?" → No, just keep working
        if any(phrase in q_lower for phrase in [
            "would you like to see",
            "want to see",
            "shall i show",
        ]):
            return "No need to show, please keep working on the task."

        # Default: don't auto-answer complex questions
        return None

    # ────────────────────────────────────────────────────
    # 4. Refinement Trigger
    # ────────────────────────────────────────────────────

    async def _check_refinement(
        self, ctx, page: Page, context: BrowserContext,
    ) -> Optional[str]:
        """
        When the agent appears to have completed the task (IDLE + high progress),
        trigger a quality refinement cycle by injecting a refinement prompt.
        """
        if ctx.agent_status != "IDLE":
            return None

        if ctx.progress.percent_complete < 0.5:
            return None

        now = time.time()
        if now - self._last_refinement_trigger < self._refinement_cooldown:
            return None

        # Don't refine if there are errors
        if ctx.progress.errors_seen:
            return None

        # Build refinement prompt
        refinement = (
            "Great work so far! Now please review what you've built and make "
            "it even better:\n"
            "1. Check for any visual polish opportunities (spacing, colors, animations)\n"
            "2. Ensure responsive design works on mobile and desktop\n"
            "3. Add any missing error handling\n"
            "4. Make sure the Antigravity Browser Extension at localhost shows the latest changes\n"
            f"\n{config.ULTIMATE_MANDATE}"
        )

        try:
            from .injector import inject_text
            success = await inject_text(context, refinement)
            if success:
                self._last_refinement_trigger = now
                return "Triggered quality refinement cycle"
        except Exception as exc:
            logger.warning("💡  Refinement injection failed: %s", exc)

        return None

    # ────────────────────────────────────────────────────
    # Status
    # ────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return engine status for debugging."""
        return {
            "detected_port": self._last_detected_port,
            "simple_browser_opened": self._simple_browser_opened,
            "questions_answered": self._questions_answered,
            "last_refinement": self._last_refinement_trigger,
        }
