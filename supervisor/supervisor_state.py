"""
supervisor_state.py — V44 Structured State Machine for the Supervisor.

Replaces ad-hoc string comparisons and consecutive_* counters with
a proper state enum and transition tracking. The state machine logs
all transitions and enforces valid state changes.

States:
  BOOTING     → Initial startup, looking for chat frame
  INJECTING   → Injecting the mandate into the chat
  WORKING     → Agent is active and making progress
  WAITING     → Agent is idle or asking a question
  UNKNOWN     → Can't determine state (chat frame lost?)
  CRASHED     → Renderer or extension host has crashed
  RESUSCITATING → Active recovery attempt in progress
  DEAD        → Unrecoverable failure, shutting down
"""

from __future__ import annotations

import enum
import logging
import time

logger = logging.getLogger("supervisor.state")


class SupervisorState(enum.Enum):
    """All possible supervisor states."""
    BOOTING = "BOOTING"
    INJECTING = "INJECTING"
    WORKING = "WORKING"
    WAITING = "WAITING"
    UNKNOWN = "UNKNOWN"
    CRASHED = "CRASHED"
    RESUSCITATING = "RESUSCITATING"
    DEAD = "DEAD"


# Valid transitions: from_state → set of allowed to_states
_VALID_TRANSITIONS: dict[SupervisorState, set[SupervisorState]] = {
    SupervisorState.BOOTING: {
        SupervisorState.INJECTING,
        SupervisorState.CRASHED,
        SupervisorState.WORKING,  # resume from lockfile
    },
    SupervisorState.INJECTING: {
        SupervisorState.WORKING,
        SupervisorState.CRASHED,
        SupervisorState.UNKNOWN,
    },
    SupervisorState.WORKING: {
        SupervisorState.WAITING,
        SupervisorState.UNKNOWN,
        SupervisorState.CRASHED,
        SupervisorState.WORKING,  # self-transition for staleness tracking
    },
    SupervisorState.WAITING: {
        SupervisorState.WORKING,
        SupervisorState.UNKNOWN,
        SupervisorState.CRASHED,
        SupervisorState.WAITING,  # self-transition for escalation
    },
    SupervisorState.UNKNOWN: {
        SupervisorState.WORKING,
        SupervisorState.CRASHED,
        SupervisorState.RESUSCITATING,
        SupervisorState.UNKNOWN,  # self-transition for coma detection
    },
    SupervisorState.CRASHED: {
        SupervisorState.RESUSCITATING,
        SupervisorState.DEAD,
    },
    SupervisorState.RESUSCITATING: {
        SupervisorState.BOOTING,
        SupervisorState.WORKING,
        SupervisorState.DEAD,
    },
    SupervisorState.DEAD: set(),  # terminal state
}


class StateTracker:
    """
    Tracks the supervisor's current state with transition logging,
    duration tracking, and consecutive-state counting.
    
    Replaces the ad-hoc `consecutive_unknown`, `consecutive_waiting`,
    `consecutive_working` counters with a unified system.
    """

    def __init__(self):
        self._state = SupervisorState.BOOTING
        self._previous_state: SupervisorState | None = None
        self._state_entered_at: float = time.time()
        self._consecutive_count: int = 0
        self._transition_count: int = 0
        self._history: list[tuple[float, SupervisorState, str]] = []  # (time, state, reason)

    @property
    def state(self) -> SupervisorState:
        return self._state

    @property
    def previous_state(self) -> SupervisorState | None:
        return self._previous_state

    @property
    def consecutive_count(self) -> int:
        """How many times we've been in the current state consecutively."""
        return self._consecutive_count

    @property
    def state_duration(self) -> float:
        """Seconds since entering the current state."""
        return time.time() - self._state_entered_at

    def transition(self, new_state: SupervisorState, reason: str = "") -> bool:
        """
        Attempt to transition to a new state.
        
        Returns True if the transition was valid and executed.
        Logs a warning and still transitions if invalid (for robustness).
        """
        old_state = self._state
        valid = new_state in _VALID_TRANSITIONS.get(old_state, set())

        if not valid and new_state != old_state:
            logger.warning(
                "⚠️  Invalid state transition: %s → %s (reason: %s). Allowing anyway.",
                old_state.value, new_state.value, reason,
            )

        if new_state == old_state:
            self._consecutive_count += 1
        else:
            # V37 FIX (H-6): Capture duration BEFORE resetting timer.
            duration_in_old_state = time.time() - self._state_entered_at
            self._previous_state = old_state
            self._state = new_state
            self._state_entered_at = time.time()
            self._consecutive_count = 1
            self._transition_count += 1

            logger.info(
                "🔄  State: %s → %s (reason: %s, was in %s for %.1fs)",
                old_state.value, new_state.value, reason,
                old_state.value, duration_in_old_state,
            )

        # Keep bounded history
        self._history.append((time.time(), new_state, reason))
        if len(self._history) > 100:
            self._history = self._history[-50:]

        return valid

    def is_stuck(self, threshold: int = 6) -> bool:
        """Check if we've been in the same state for too many consecutive ticks."""
        return self._consecutive_count >= threshold

    def get_summary(self) -> dict:
        """Return a summary dict for logging/telemetry."""
        return {
            "state": self._state.value,
            "previous": self._previous_state.value if self._previous_state else None,
            "consecutive": self._consecutive_count,
            "duration_s": round(self.state_duration, 1),
            "total_transitions": self._transition_count,
        }

    def map_from_vision(self, vision_state: str) -> SupervisorState:
        """Map a vision/context engine state string to SupervisorState."""
        _MAP = {
            "WORKING": SupervisorState.WORKING,
            "IDLE": SupervisorState.WORKING,
            "WAITING": SupervisorState.WAITING,
            "ASKING": SupervisorState.WAITING,
            "CRASHED": SupervisorState.CRASHED,
            "UNKNOWN": SupervisorState.UNKNOWN,
        }
        return _MAP.get(vision_state.upper(), SupervisorState.UNKNOWN)
