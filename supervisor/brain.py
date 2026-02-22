"""
brain.py — The Brain (Zero API Cost).

Calls the local Gemini CLI tool via the centralized gemini_advisor module
to generate prompts that unblock the agent, answer its questions, or
request refinements.

UPGRADE: For refinement / completion / loop situations, the ULTIMATE_MANDATE
is prepended to the Gemini-generated response before it's injected into
Antigravity, forcing the agent toward 2026 Awwwards-level excellence.

UPGRADE V2: Now uses gemini_advisor instead of raw subprocess calls.
"""

import logging
import textwrap

from . import config
from .gemini_advisor import ask_gemini

logger = logging.getLogger("supervisor.brain")

# ─────────────────────────────────────────────────────────────
# Situations that trigger the ULTIMATE_MANDATE prepend.
# "question" is excluded — questions need direct answers, not mandates.
# ─────────────────────────────────────────────────────────────
_MANDATE_SITUATIONS = {"loop", "completion", "generic", "refinement", "enhancement"}


# ── prompt templates ───────────────────────────────────────────

def _build_prompt(
    situation: str,
    recent_messages: list[str],
    goal: str,
    error_key: str | None = None,
) -> str:
    """
    Build the prompt that will be piped to the Gemini CLI.
    """
    chat_log = "\n---\n".join(recent_messages[-config.LOOP_HISTORY_SIZE:])

    if situation == "loop":
        return textwrap.dedent(f"""\
            You are a senior AI supervisor and elite art director. An AI coding agent
            inside the Antigravity IDE is working toward this goal:

            GOAL: {goal}

            The agent is STUCK IN A LOOP. Here are its recent messages:

            {chat_log}

            The specific repeating error/pattern is:
            {error_key or "unknown"}

            CRITICAL RULES:
            - If any action or tool execution has failed twice in a row, DO NOT tell the
              agent to retry it. It must immediately skip that step and pivot to a
              completely different workaround.
            - Diagnose WHY the agent is stuck.
            - Generate a SHORT, ACTIONABLE prompt (under 300 words) that tells the agent
              exactly what to do differently to make progress.
            - Do NOT suggest retrying the same failing approach.
            - If the error is unfixable, tell the agent to skip it and move on.
            - Always push for the highest quality implementation.

            Respond with ONLY the prompt text to send to the agent. No preamble.
        """)

    elif situation == "question":
        return textwrap.dedent(f"""\
            You are a senior AI supervisor. An AI coding agent inside the Antigravity IDE
            is working toward this goal:

            GOAL: {goal}

            The agent is asking a question and waiting for human input.
            Here are its recent messages:

            {chat_log}

            Generate a SHORT, DECISIVE answer (under 200 words) that unblocks the agent.
            Choose the most reasonable option. If multiple options exist, pick the one that
            best serves the goal. Be specific and actionable.

            Respond with ONLY the answer text to send to the agent. No preamble.
        """)

    elif situation == "completion":
        return textwrap.dedent(f"""\
            You are a senior AI supervisor and elite art director. An AI coding agent
            inside the Antigravity IDE has just declared this task complete:

            GOAL: {goal}

            Here are its recent messages:

            {chat_log}

            Your job is to ensure the result is WORLD-CLASS, not just "done". Generate a
            prompt (under 300 words) that tells the agent to:
            1. Review the code for bugs, edge cases, and missed requirements.
            2. Refine the implementation to be production-quality and visually stunning.
            3. Fix any issues found.
            4. Make the overall result significantly better — push toward award-winning quality.
            5. Ensure modern best practices, latest tools, and beautiful UI/UX.

            Be specific about what to check based on the goal and the agent's messages.

            Respond with ONLY the refinement prompt to send to the agent. No preamble.
        """)

    elif situation == "error_diagnosis":
        return textwrap.dedent(f"""\
            You are a senior AI supervisor. An AI coding agent inside the Antigravity IDE
            is working toward this goal:

            GOAL: {goal}

            The agent appears to be encountering errors. Here are its recent messages:

            {chat_log}

            The error pattern detected is:
            {error_key or "unknown"}

            Analyze the error and provide a SHORT, ACTIONABLE diagnosis (under 200 words).
            - What is the root cause?
            - What specific steps should the agent take to fix this?
            - If the error seems transient, should the agent retry or pivot?

            Respond with ONLY the guidance to send to the agent. No preamble.
        """)

    else:
        # Generic intervention.
        return textwrap.dedent(f"""\
            You are a senior AI supervisor and elite art director. An AI coding agent
            inside the Antigravity IDE is working toward this goal:

            GOAL: {goal}

            Here are its recent messages:

            {chat_log}

            The agent seems to need guidance. Generate a SHORT, ACTIONABLE prompt
            (under 200 words) that helps the agent make progress toward the goal.
            Push for the highest quality and most modern implementation possible.

            Respond with ONLY the prompt text to send to the agent. No preamble.
        """)


# ── Gemini CLI invocation ─────────────────────────────────────

async def consult_gemini(
    situation: str,
    recent_messages: list[str],
    goal: str,
    error_key: str | None = None,
) -> str:
    """
    Call the local Gemini CLI with the constructed prompt.
    Returns the generated response text.

    For situations in _MANDATE_SITUATIONS, the ULTIMATE_MANDATE is
    prepended to the response so the agent receives the directive.

    Raises RuntimeError on timeout or non-zero exit.
    """
    prompt = _build_prompt(situation, recent_messages, goal, error_key)

    logger.info(
        "🧠  Consulting Gemini CLI (situation=%s, prompt_len=%d) …",
        situation,
        len(prompt),
    )
    logger.debug("Prompt:\n%s", prompt)

    # Use the centralized advisor — includes retry logic.
    response = await ask_gemini(prompt, use_cache=False)

    # ── ULTIMATE MANDATE PREPEND ──────────────────────
    # For refinement / completion / loop / generic situations,
    # prepend the mandate so the agent always receives it.
    if situation in _MANDATE_SITUATIONS:
        response = f"{config.ULTIMATE_MANDATE}\n\n{response}"
        logger.info("🏆  ULTIMATE_MANDATE prepended to response.")

    return response
