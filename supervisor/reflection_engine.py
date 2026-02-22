"""
reflection_engine.py — V17 Semantic Compression Layer

When an execution attempt fails, routes the raw git diff and test error
through the Local Manager to produce a single, grounded semantic lesson.

The lesson MUST cite specific function/file names from the test error
to prevent vague or hallucinated reflections from poisoning the memory.
"""

import logging
from typing import Optional

logger = logging.getLogger("supervisor.reflection_engine")


class ReflectionEngine:
    """Compresses raw failure artifacts into concise semantic lessons."""

    REFLECTION_PROMPT = (
        "You are a strict code failure analyst. You will receive a git diff of code changes "
        "and the resulting test error output. Your job is to produce exactly ONE sentence "
        "explaining why this approach failed.\n\n"
        "RULES:\n"
        "1. You MUST cite at least one specific function name or file name from the test error.\n"
        "2. You MUST ground your analysis ONLY in the provided test error — do not speculate.\n"
        "3. Your sentence must be actionable: it should tell the next developer what NOT to do.\n"
        "4. Maximum 50 words.\n\n"
        'Output strict JSON: {"lesson": "your one-sentence lesson here"}'
    )

    def __init__(self, local_manager):
        """
        Args:
            local_manager: An instance of LocalManager from local_orchestrator.py
        """
        self._manager = local_manager

    async def reflect(
        self,
        diff_text: str,
        test_error: str,
        objective: str = "",
    ) -> str:
        """
        Compress a failed diff + test error into a single semantic lesson.

        Returns:
            A concise, grounded natural language lesson string.
            Falls back to a truncated test error if reflection fails.
        """
        import json

        # Truncate inputs to protect the local LLM context window
        diff_truncated = diff_text[:1500] if diff_text else "(no diff)"
        error_truncated = test_error[:1000] if test_error else "(no error)"

        user_prompt = (
            f"OBJECTIVE: {objective[:200]}\n\n"
            f"GIT DIFF (failed attempt):\n{diff_truncated}\n\n"
            f"TEST ERROR OUTPUT:\n{error_truncated}"
        )

        try:
            raw = await self._manager.ask_local_model(
                system_prompt=self.REFLECTION_PROMPT,
                user_prompt=user_prompt,
                temperature=0.0,
            )

            if not raw or raw == "{}":
                logger.warning("🪞 Reflection returned empty. Falling back to raw error.")
                return self._fallback(test_error)

            data = json.loads(raw)
            lesson = data.get("lesson", "").strip()

            if not lesson:
                return self._fallback(test_error)

            logger.info("🪞 Reflection: %s", lesson)
            return lesson

        except Exception as exc:
            logger.warning("🪞 Reflection failed (%s). Using fallback.", exc)
            return self._fallback(test_error)

    @staticmethod
    def _fallback(test_error: str) -> str:
        """Deterministic fallback: first meaningful line of the test error."""
        lines = [l.strip() for l in test_error.split("\n") if l.strip()]
        # Find the first line that looks like an actual error (not a traceback header)
        for line in reversed(lines):
            if any(kw in line.lower() for kw in ("error", "fail", "assert", "exception")):
                return line[:150]
        return lines[-1][:150] if lines else "Unknown failure."
