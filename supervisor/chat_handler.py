"""
chat_handler.py — Runtime Slash Command Engine for Supervisor V11.

Allows the user to type /commands directly into the IDE agent chat to control
the supervisor orchestrator at runtime without API calls.
"""
import logging
import sys
import time

from .context_engine import ContextSnapshot
from . import config

logger = logging.getLogger("supervisor.chat_handler")

# Global pause state
IS_PAUSED = False

class ChatHandler:
    """Processes user /slash commands from the chat context."""

    def __init__(self, session_memory):
        self.session_memory = session_memory
        self._last_cmd_content = ""
        self._last_cmd_time = 0.0

    async def process_slash_commands(self, snapshot: ContextSnapshot) -> bool:
        """
        Check if the latest user message is a /slash command.
        If it is, execute it and return True.
        """
        global IS_PAUSED

        if not snapshot.chat_messages:
            return False

        # Find the last user message
        user_msgs = [m for m in snapshot.chat_messages if m.role == "user"]
        if not user_msgs:
            return False

        last_user = user_msgs[-1]
        text = last_user.content.strip()

        if not text.startswith("/"):
            return False

        # Anti-spam: Ignore if we already processed this exact slash command recently
        if text == self._last_cmd_content and (time.time() - self._last_cmd_time) < 30:
            return False

        self._last_cmd_content = text
        self._last_cmd_time = time.time()

        cmd_parts = text.split()
        cmd = cmd_parts[0].lower()
        args = cmd_parts[1:]

        logger.info("⚡  Intercepted slash command: %s", text)
        
        C = config.ANSI_CYAN
        G = config.ANSI_GREEN
        Y = config.ANSI_YELLOW
        R = config.ANSI_RESET

        print(f"\n  {C}⚡ Executing Slash Command: {text}{R}")

        try:
            if cmd == "/status":
                from .context_engine import format_context_report
                report = format_context_report(snapshot)
                print(f"\n{report}\n")

            elif cmd == "/compact":
                from .retry_policy import get_context_budget
                budget = get_context_budget()
                budget.trim_history()
                print(f"  {G}✅ Context budget history trimmed.{R}")

            elif cmd == "/model":
                if args:
                    new_model = args[0]
                    config.DEFAULT_MODEL = new_model
                    print(f"  {G}✅ Model switched to: {new_model}{R}")
                else:
                    print(f"  {Y}⚠️ Usage: /model <model_name>{R}")

            elif cmd in ("/pause", "/stop"):
                IS_PAUSED = True
                print(f"  {Y}⏸️ Supervisor paused. Type /resume to continue.{R}")

            elif cmd == "/resume":
                IS_PAUSED = False
                print(f"  {G}▶️ Supervisor resumed.{R}")
                
            elif cmd == "/exit":
                print(f"  {config.ANSI_RED}🛑 Supervisor shutting down via user command.{R}")
                sys.exit(0)

            else:
                print(f"  {Y}⚠️ Unknown slash command: {cmd}{R}")

            # Record that we handled a slash command
            if hasattr(self.session_memory, "record_event"):
                self.session_memory.record_event("slash_command", text)
            
            return True

        except Exception as exc:
            logger.error("⚡  Slash command error: %s", exc)
            return False
