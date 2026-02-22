"""
polish_engine.py — V29 Infinite Polish Engine.

Transforms the Temporal Planner from fire-and-forget into a Socratic,
user-confirmed iterative builder. The system refuses to commit until
the user explicitly says "perfect" or "approved".

Core capabilities:
  1. Socratic Pre-Flight — Detects ambiguity, forces clarification MCQs
  2. Live Preview Feedback — Renders in Antigravity Browser Extension, asks for approval
  3. Infinite User-Initiated Loops — No MAX_REPLAN_COUNT for user tweaks
  4. Dynamic DAG Injection — Add nodes mid-execution without breaking tests
  5. Context Compression — Aggressive pruning during polish loops
  6. User Injection Port — Monitor for live feedback without breaking flow
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("supervisor.polish_engine")

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Phrases that terminate the polish loop
TERMINATION_PHRASES = [
    "perfect",
    "approved",
    "looks great",
    "ship it",
    "lgtm",
    "all good",
    "done",
    "that's exactly right",
    "this is perfect",
    "no more changes",
    "finalize",
    "merge it",
]

# Ambiguity indicators — if the prompt matches these patterns,
# the system forces clarification before proceeding
AMBIGUITY_PATTERNS = [
    # Vague component names
    re.compile(r"(?:build|create|make|add)\s+(?:a|an|the)\s+(?:page|component|section|feature)\b", re.IGNORECASE),
    # No technical specifics
    re.compile(r"(?:nice|good|cool|beautiful|modern|clean)\s+(?:looking|design|ui|interface)", re.IGNORECASE),
    # Single-word commands
    re.compile(r"^(?:checkout|dashboard|landing|settings|profile|homepage)$", re.IGNORECASE),
    # Missing constraints
    re.compile(r"(?:something like|similar to|inspired by)\b", re.IGNORECASE),
]

# Questions to probe for missing constraints
CLARIFICATION_DIMENSIONS = [
    "layout_structure",     # Grid, sidebar, modal, full-page?
    "data_requirements",    # What data is displayed/collected?
    "interaction_model",    # Static display, form, interactive?
    "responsive_targets",   # Mobile-first? Desktop-only? Both?
    "existing_patterns",    # Follow existing UI patterns or new?
]

# Maximum polish iterations before suggesting the user finalize
SOFT_LIMIT_ITERATIONS = 20

# How many messages to retain during polish compression
POLISH_CONTEXT_WINDOW = 5


class PolishEngine:
    """
    Infinite Polish Engine.

    Intercepts user prompts before they enter the Temporal Planner,
    forces clarification when ambiguous, renders live previews for
    feedback, and loops until explicit user confirmation.

    Attributes:
        workspace_path: Root of the project workspace.
        _state: Current polish session state.
        _iteration_count: How many polish iterations have occurred.
        _is_polish_active: Whether we're in an active polish loop.
    """

    def __init__(self, workspace_path: str = "."):
        self._workspace = Path(workspace_path)
        self._memory_dir = self._workspace / ".ag-memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        self._state_path = self._memory_dir / "polish_state.json"
        self._injection_path = self._memory_dir / "user_injection.txt"
        self._state: dict = {}
        self._iteration_count: int = 0
        self._is_polish_active: bool = False
        self._compressed_history: list[str] = []

        self._load_state()

    # ─────────────────────────────────────────────────────────
    # State Management
    # ─────────────────────────────────────────────────────────

    def _load_state(self):
        """Load polish state from disk."""
        if self._state_path.exists():
            try:
                self._state = json.loads(
                    self._state_path.read_text(encoding="utf-8")
                )
                self._iteration_count = self._state.get("iteration_count", 0)
                self._is_polish_active = self._state.get("is_active", False)
                self._compressed_history = self._state.get("compressed_history", [])
            except (json.JSONDecodeError, OSError):
                self._state = {}

    def _save_state(self):
        """Persist polish state to disk."""
        self._state.update({
            "iteration_count": self._iteration_count,
            "is_active": self._is_polish_active,
            "compressed_history": self._compressed_history,
            "last_updated": time.time(),
        })
        try:
            self._state_path.write_text(
                json.dumps(self._state, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.error("Failed to save polish state: %s", exc)

    # ─────────────────────────────────────────────────────────
    # 1. Socratic Pre-Flight
    # ─────────────────────────────────────────────────────────

    def detect_ambiguity(self, prompt: str) -> dict:
        """
        Analyze a user prompt for missing technical constraints.

        Returns:
        {
            "is_ambiguous": True/False,
            "missing_dimensions": ["layout_structure", "data_requirements", ...],
            "ambiguity_score": 0.0-1.0,
            "patterns_matched": ["pattern description", ...]
        }
        """
        patterns_matched = []
        for pattern in AMBIGUITY_PATTERNS:
            if pattern.search(prompt):
                patterns_matched.append(pattern.pattern[:60])

        # Count how many CLARIFICATION_DIMENSIONS are addressed
        addressed = 0
        dimension_keywords = {
            "layout_structure": ["grid", "sidebar", "modal", "layout", "column", "full-page", "split", "tabs"],
            "data_requirements": ["data", "api", "endpoint", "database", "fetch", "display", "form", "input"],
            "interaction_model": ["click", "drag", "submit", "filter", "sort", "search", "toggle", "accordion"],
            "responsive_targets": ["mobile", "responsive", "desktop", "tablet", "breakpoint"],
            "existing_patterns": ["like the", "same as", "match", "consistent", "follow", "existing"],
        }

        missing = []
        prompt_lower = prompt.lower()
        for dim, keywords in dimension_keywords.items():
            if any(kw in prompt_lower for kw in keywords):
                addressed += 1
            else:
                missing.append(dim)

        ambiguity_score = len(missing) / len(CLARIFICATION_DIMENSIONS)

        # Low word count increases ambiguity
        word_count = len(prompt.split())
        if word_count < 15:
            ambiguity_score = min(1.0, ambiguity_score + 0.2)

        is_ambiguous = ambiguity_score > 0.5 or len(patterns_matched) >= 2

        return {
            "is_ambiguous": is_ambiguous,
            "missing_dimensions": missing,
            "ambiguity_score": round(ambiguity_score, 2),
            "patterns_matched": patterns_matched,
        }

    def generate_clarification_mcq(self, prompt: str, missing_dimensions: list[str]) -> str:
        """
        Generate a strictly-formatted multiple-choice question
        to clarify the user's intent before proceeding.

        Returns a formatted MCQ string for IDE chat injection.
        """
        prompt_text = (
            "You are a senior UX architect. The user gave a vague feature request. "
            "Generate a concise multiple-choice clarification question.\n\n"
            f"USER REQUEST: {prompt}\n"
            f"MISSING DIMENSIONS: {json.dumps(missing_dimensions)}\n\n"
            "RULES:\n"
            "- Ask ONE question that covers the most critical missing dimension\n"
            "- Provide exactly 4 options (A/B/C/D)\n"
            "- Option D should always be 'Other (please describe)'\n"
            "- Each option should suggest a concrete implementation approach\n"
            "- Include features the user might not have considered\n"
            "- Keep it under 200 words total\n\n"
            "Format:\n"
            "**Before I build this, I want to make sure I get it exactly right.**\n\n"
            "🔍 [Your question here]\n\n"
            "A) [option]\n"
            "B) [option]\n"
            "C) [option]\n"
            "D) Other (please describe)\n\n"
            "*Reply with A, B, C, or D (or describe what you want)*\n"
        )

        try:
            from .gemini_advisor import ask_gemini
            return ask_gemini(prompt_text, timeout=60)
        except Exception as exc:
            logger.error("MCQ generation failed: %s", exc)
            # Fallback static MCQ
            dim = missing_dimensions[0] if missing_dimensions else "layout_structure"
            return (
                f"**Before I build this, I want to make sure I get it exactly right.**\n\n"
                f"🔍 What {dim.replace('_', ' ')} are you envisioning?\n\n"
                f"A) Clean, minimal single-column layout\n"
                f"B) Dashboard-style with sidebar navigation\n"
                f"C) Card-based grid with interactive elements\n"
                f"D) Other (please describe)\n\n"
                f"*Reply with A, B, C, or D (or describe what you want)*\n"
            )

    # ─────────────────────────────────────────────────────────
    # 2. Live Preview Feedback Loop
    # ─────────────────────────────────────────────────────────

    def generate_preview_prompt(self, component_name: str, file_path: str) -> str:
        """
        Generate the prompt to show the user a live preview
        and ask for visual confirmation.

        Returns a formatted feedback request for IDE chat injection.
        """
        return (
            f"✅ **{component_name}** is functionally complete and all tests pass.\n\n"
            f"📎 I've rendered it in the Antigravity Browser Extension for you to review.\n\n"
            f"**Does this match your visual expectations?**\n\n"
            f"- Reply **\"perfect\"** or **\"approved\"** to finalize and merge\n"
            f"- Or describe any adjustments (e.g., *\"make the header darker\"*, "
            f"*\"add more padding to the cards\"*, *\"swap the font to Inter\"*)\n\n"
            f"I'll keep iterating until you're 100% satisfied. No rush.\n"
        )

    # ─────────────────────────────────────────────────────────
    # 3. Termination Detection
    # ─────────────────────────────────────────────────────────

    def is_termination(self, user_message: str) -> bool:
        """
        Check if the user's message signals satisfaction.

        Looks for explicit termination phrases.
        Returns True if the user is confirming completion.
        """
        msg_lower = user_message.strip().lower()

        # Exact match or message starts with termination phrase
        for phrase in TERMINATION_PHRASES:
            if msg_lower == phrase or msg_lower.startswith(phrase):
                return True

        # Thumbs up / emoji confirmation
        if msg_lower in ("👍", "👌", "✅", "💯", "🎉", "yes", "y"):
            return True

        return False

    def is_change_request(self, user_message: str) -> bool:
        """
        Detect if the user is requesting a change vs confirming.

        Returns True if the message contains change language.
        """
        change_patterns = [
            r"(?:make|change|adjust|tweak|modify|update|fix|move|swap|increase|decrease)",
            r"(?:too\s+(?:big|small|dark|light|wide|narrow|tall|short))",
            r"(?:more|less)\s+(?:padding|margin|space|contrast|opacity)",
            r"(?:different|another|new)\s+(?:color|font|size|layout|style)",
            r"(?:add|remove|hide|show)\s+",
            r"(?:can you|could you|please|i want|i'd like|instead)",
        ]

        msg_lower = user_message.lower()
        for pattern in change_patterns:
            if re.search(pattern, msg_lower):
                return True

        return False

    # ─────────────────────────────────────────────────────────
    # 4. Dynamic DAG Injection
    # ─────────────────────────────────────────────────────────

    def inject_dag_node(self, dag_state: dict, new_task: str, after_node: str) -> dict:
        """
        Dynamically inject a new node into an active DAG without
        breaking existing test locks or baseline state.

        Args:
            dag_state: Current temporal planner state.
            new_task: Description of the new task to inject.
            after_node: ID of the node after which to inject.

        Returns:
            Updated dag_state with new node.
        """
        nodes = dag_state.get("nodes", [])

        # Find insertion point
        insert_idx = len(nodes)
        for i, node in enumerate(nodes):
            if node.get("id") == after_node:
                insert_idx = i + 1
                break

        # Generate node ID
        node_id = f"polish_{hashlib.sha256(new_task.encode()).hexdigest()[:8]}"

        new_node = {
            "id": node_id,
            "task": new_task,
            "status": "pending",
            "injected": True,
            "injected_at": time.time(),
            "depends_on": [after_node] if after_node else [],
            "type": "user_polish",
        }

        nodes.insert(insert_idx, new_node)
        dag_state["nodes"] = nodes
        dag_state["total_nodes"] = len(nodes)

        logger.info("Injected DAG node '%s' after '%s'", node_id, after_node)
        return dag_state

    # ─────────────────────────────────────────────────────────
    # 5. Context Compression
    # ─────────────────────────────────────────────────────────

    def compress_polish_context(self, chat_history: list[dict]) -> list[dict]:
        """
        Aggressively compress chat history during polish loops.

        Retains:
          - Current visual objective
          - Latest user critique
          - Last POLISH_CONTEXT_WINDOW messages

        Discards:
          - Old CSS discussions
          - Redundant confirmations
          - Intermediate Gemini analysis
        """
        if len(chat_history) <= POLISH_CONTEXT_WINDOW:
            return chat_history

        # Keep first message (original goal) and last N messages
        compressed = [chat_history[0]]

        # Summarize the middle section
        middle = chat_history[1:-POLISH_CONTEXT_WINDOW]
        if middle:
            changes_made = []
            for msg in middle:
                content = msg.get("content", "")
                # Extract just the change descriptions
                if any(kw in content.lower() for kw in ["changed", "updated", "adjusted", "fixed", "modified"]):
                    # Keep only first line of each change
                    first_line = content.split("\n")[0][:100]
                    changes_made.append(first_line)

            if changes_made:
                summary = {
                    "role": "system",
                    "content": (
                        f"[COMPRESSED: {len(middle)} previous messages summarized]\n"
                        f"Changes made so far:\n" +
                        "\n".join(f"- {c}" for c in changes_made[-10:])
                    ),
                }
                compressed.append(summary)

        # Append the recent messages
        compressed.extend(chat_history[-POLISH_CONTEXT_WINDOW:])

        self._compressed_history = [
            msg.get("content", "")[:200] for msg in compressed
        ]
        self._save_state()

        return compressed

    # ─────────────────────────────────────────────────────────
    # 6. User Injection Port
    # ─────────────────────────────────────────────────────────

    def check_user_injection(self) -> Optional[str]:
        """
        Check if the user has dropped a feedback file for
        mid-execution injection.

        The user can write to .ag-memory/user_injection.txt at any time.
        The engine reads it, processes it, and deletes the file.

        Returns the injection content, or None.
        """
        if self._injection_path.exists():
            try:
                content = self._injection_path.read_text(encoding="utf-8").strip()
                self._injection_path.unlink()

                if content:
                    logger.info("User injection received: %s", content[:100])
                    return content
            except OSError:
                pass

        return None

    # ─────────────────────────────────────────────────────────
    # Polish Session Management
    # ─────────────────────────────────────────────────────────

    def start_polish_session(self, original_prompt: str, epic_id: str = ""):
        """Start a new polish session."""
        self._is_polish_active = True
        self._iteration_count = 0
        self._compressed_history = []
        self._state = {
            "original_prompt": original_prompt,
            "epic_id": epic_id,
            "started_at": time.time(),
            "current_objective": original_prompt,
        }
        self._save_state()
        logger.info("Polish session started for: %s", original_prompt[:100])

    def record_iteration(self, user_feedback: str, changes_made: str):
        """Record a polish iteration."""
        self._iteration_count += 1
        self._state["current_objective"] = user_feedback
        self._state["last_changes"] = changes_made
        self._save_state()

        if self._iteration_count >= SOFT_LIMIT_ITERATIONS:
            logger.warning(
                "Polish loop at %d iterations. Consider suggesting finalization.",
                self._iteration_count,
            )

    def end_polish_session(self) -> dict:
        """End the polish session and return summary."""
        summary = {
            "iterations": self._iteration_count,
            "original_prompt": self._state.get("original_prompt", ""),
            "duration_minutes": (time.time() - self._state.get("started_at", time.time())) / 60,
        }
        self._is_polish_active = False
        self._iteration_count = 0
        self._state = {}
        self._save_state()

        # Clean up injection file
        if self._injection_path.exists():
            self._injection_path.unlink(missing_ok=True)

        logger.info("Polish session ended after %d iterations", summary["iterations"])
        return summary

    @property
    def is_active(self) -> bool:
        return self._is_polish_active

    @property
    def iterations(self) -> int:
        return self._iteration_count

    def should_request_feedback(self, node_type: str, is_architectural: bool = False) -> bool:
        """
        Determine if the system should pause and request user feedback.

        Feedback is requested:
          - At completion of major DAG nodes
          - When critical architectural assumptions are needed
          - NOT after every line of CSS

        Returns True if feedback should be requested.
        """
        # Always ask for architectural decisions
        if is_architectural:
            return True

        # Ask at major node boundaries
        if node_type in ("component_complete", "page_complete", "feature_complete"):
            return True

        # Don't ask for minor code changes
        if node_type in ("style_tweak", "fix_lint", "add_test", "refactor"):
            return False

        # Default: ask every 3 iterations to avoid fatigue
        return self._iteration_count % 3 == 0
