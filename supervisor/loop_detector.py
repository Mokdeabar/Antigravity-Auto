"""
loop_detector.py — Loop Detection Engine.

Maintains a rolling window of recent agent messages and detects:
  1. Exact duplicate messages (repeated ≥ DUPLICATE_THRESHOLD times)
  2. Same error substring appearing ≥ ERROR_SUBSTRING_THRESHOLD times
  3. Same tool failure appearing ≥ CONSECUTIVE_FAIL_THRESHOLD times in a row
     (CRITICAL RULE — triggers an immediate pivot, no retry)

Also tracks per-error intervention counts and escalates to human
if any single error reaches MAX_SAME_ERROR_INTERVENTIONS.
"""

import logging
import re
from collections import deque
from enum import Enum, auto

from . import config

logger = logging.getLogger("supervisor.loop_detector")


class LoopStatus(Enum):
    OK = auto()
    LOOP_DETECTED = auto()
    HUMAN_ESCALATION = auto()


# Common error patterns to extract a normalised "error key".
_ERROR_PATTERNS = [
    re.compile(r"(?:Error|Exception|error|exception)[:\s]+(.{10,80})"),
    re.compile(r"(failed to .{10,60})", re.IGNORECASE),
    re.compile(r"(cannot .{10,60})", re.IGNORECASE),
    re.compile(r"(unable to .{10,60})", re.IGNORECASE),
    re.compile(r"(ENOENT|EACCES|EPERM|ETIMEDOUT)[:\s].*"),
    re.compile(r"(screenshot .{0,30}fail)", re.IGNORECASE),
    re.compile(r"(command .{0,30}fail)", re.IGNORECASE),
    re.compile(r"(tool .{0,30}fail)", re.IGNORECASE),
]


def _extract_error_key(message: str) -> str | None:
    """
    Try to extract a normalised error substring from a message.
    Returns None if the message doesn't look like an error.
    """
    for pattern in _ERROR_PATTERNS:
        match = pattern.search(message)
        if match:
            return match.group(0).strip().lower()[:120]
    return None


class LoopDetector:
    """Stateful detector fed one message at a time."""

    def __init__(self) -> None:
        self._history: deque[str] = deque(maxlen=config.LOOP_HISTORY_SIZE)
        self._error_interventions: dict[str, int] = {}
        # Track consecutive identical messages for CRITICAL RULE.
        self._prev_message: str | None = None
        self._consecutive_same: int = 0

    # ── public API ─────────────────────────────────────────────

    def feed(self, message: str) -> LoopStatus:
        """
        Feed a new agent message.  Returns:
          OK                — nothing wrong
          LOOP_DETECTED     — the agent is stuck; supervisor should intervene
          HUMAN_ESCALATION  — too many interventions on the same error; need a human
        """
        # Track consecutive duplicates.
        if message == self._prev_message:
            self._consecutive_same += 1
        else:
            self._consecutive_same = 1
        self._prev_message = message

        self._history.append(message)

        # --- Check 1: consecutive duplicates (CRITICAL RULE — 2× → pivot) ---
        if self._consecutive_same >= config.CONSECUTIVE_FAIL_THRESHOLD:
            error_key = _extract_error_key(message) or message[:120].lower()
            logger.warning(
                "🔁  Consecutive duplicate detected (%d×): %.80s",
                self._consecutive_same,
                error_key,
            )
            return self._handle_intervention(error_key)

        # --- Check 2: exact duplicates in rolling window ---
        if self._count_exact_duplicates(message) >= config.DUPLICATE_THRESHOLD:
            error_key = _extract_error_key(message) or message[:120].lower()
            logger.warning("🔁  Duplicate message in window: %.80s", error_key)
            return self._handle_intervention(error_key)

        # --- Check 3: same error substring across window ---
        error_key = _extract_error_key(message)
        if error_key and self._count_error_in_window(error_key) >= config.ERROR_SUBSTRING_THRESHOLD:
            logger.warning(
                "🔁  Error repeated %d× in window: %.80s",
                self._count_error_in_window(error_key),
                error_key,
            )
            return self._handle_intervention(error_key)

        return LoopStatus.OK

    def reset_intervention_count(self, error_key: str | None = None) -> None:
        """Reset intervention counter for a specific error or all errors."""
        if error_key:
            self._error_interventions.pop(error_key, None)
        else:
            self._error_interventions.clear()

    @property
    def intervention_counts(self) -> dict[str, int]:
        return dict(self._error_interventions)

    # ── internals ──────────────────────────────────────────────

    def _handle_intervention(self, error_key: str) -> LoopStatus:
        """Increment intervention counter and decide status."""
        count = self._error_interventions.get(error_key, 0) + 1
        self._error_interventions[error_key] = count

        if count >= config.MAX_SAME_ERROR_INTERVENTIONS:
            logger.error(
                "🚨  Escalating to human — %d interventions on: %.80s",
                count,
                error_key,
            )
            return LoopStatus.HUMAN_ESCALATION
        return LoopStatus.LOOP_DETECTED

    def _count_exact_duplicates(self, message: str) -> int:
        return sum(1 for m in self._history if m == message)

    def _count_error_in_window(self, error_key: str) -> int:
        count = 0
        for m in self._history:
            key = _extract_error_key(m)
            if key and key == error_key:
                count += 1
        return count
