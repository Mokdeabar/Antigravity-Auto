"""
session_memory.py — Persistent Session Memory V2 (The Hippocampus + OpenClaw).

Persistent memory of what happened in this supervisor session.
Survives restarts via JSON file. Inspired by OpenClaw's session model.

V2 UPGRADE — OpenClaw-Inspired:
  1. Gemini context pruning: soft-trim old tool results (keep head/tail),
     hard-clear stale ones to manage context window pressure.
  2. History compaction: summarize events older than N minutes into a
     single summary event, keeping recent events detailed.
  3. Pre-compaction flush: store key learnings before clearing history.
  4. JSONL transcript: full audit trail written to _transcript.jsonl.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("supervisor.session_memory")


# ─────────────────────────────────────────────────────────────
# Session Memory
# ─────────────────────────────────────────────────────────────

def _ensure_dir(path: Path) -> Path:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    return path

class SessionMemory:
    """
    Persistent memory of the current supervisor session.

    Stores:
      - Session start time
      - Goal text
      - History of events (injections, approvals, errors, progress)
      - Files the agent has worked on
      - Detected ports and server states
      - Number of vision calls, context gathers, approvals

    All data is persisted to .supervisor_memory.json in the project dir.
    """

    _MEMORY_FILENAME = ".supervisor_memory.json"
    _TRANSCRIPT_FILENAME = "_transcript.jsonl"
    _MAX_EVENTS = 200
    _MAX_EVENT_AGE_HOURS = 12
    _COMPACTION_AGE_MINUTES = 30  # Compact events older than this
    _PRUNE_MIN_CHARS = 50_000    # Min total chars before pruning kicks in
    _PRUNE_KEEP_LAST = 3         # Keep last N assistant events unpruned

    def __init__(self, project_path: str | None = None):
        self._project_path = project_path
        if project_path:
            self._path = Path(project_path) / self._MEMORY_FILENAME
            self._transcript_path = Path(project_path) / self._TRANSCRIPT_FILENAME
            self._snapshots_dir = _ensure_dir(Path(project_path) / ".ag-supervisor" / "snapshots")
        else:
            # Import config here to avoid circular dependency
            from . import config
            state_dir = config.get_state_dir()
            self._path = state_dir / self._MEMORY_FILENAME
            self._transcript_path = state_dir / self._TRANSCRIPT_FILENAME
            self._snapshots_dir = _ensure_dir(state_dir / "snapshots")

        self._data = self._load()
        self._dirty = False

    # ────────────────────────────────────────────────
    # Initialization
    # ────────────────────────────────────────────────

    def _load(self) -> dict:
        """Load session memory from disk."""
        default = {
            "session_start": time.time(),
            "goal": "",
            "events": [],
            "counters": {
                "approvals": 0,
                "injections": 0,
                "vision_calls": 0,
                "context_gathers": 0,
                "screenshots_skipped": 0,
                "errors_resolved": 0,
                "questions_answered": 0,
                "compactions": 0,
                "prunings": 0,
            },
            "files_worked_on": [],
            "detected_ports": [],
            "last_agent_status": "UNKNOWN",
            "last_update": time.time(),
            "learnings": [],  # Pre-compaction key learnings
        }

        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "events" in data:
                    # Prune old events
                    data["events"] = self._prune_events(data.get("events", []))
                    # Ensure new fields exist
                    data.setdefault("learnings", [])
                    data.setdefault("counters", {}).setdefault("compactions", 0)
                    data.setdefault("counters", {}).setdefault("prunings", 0)
                    logger.info(
                        "🧠  Resuming session with %d events in memory (started %.0fm ago)",
                        len(data["events"]),
                        (time.time() - data.get("session_start", time.time())) / 60,
                    )
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("🧠  Failed to load session memory: %s — starting fresh", exc)

        logger.info("🧠  Starting new session memory")
        return default

    def _save(self) -> None:
        """Save session memory to disk atomically."""
        if not self._dirty:
            return

        self._data["last_update"] = time.time()

        try:
            tmp_path = self._path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, default=str)

            # Retry up to 3 times to handle WinError 5 (Access Denied)
            # caused by antivirus or file indexer locks on Windows.
            for attempt in range(3):
                try:
                    tmp_path.replace(self._path)
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(0.5)
                    else:
                        logger.warning("🧠  WinError 5: Could not replace %s after 3 retries", self._path)

            self._dirty = False
        except Exception as exc:
            logger.warning("🧠  Failed to save session memory: %s", exc)

    def _prune_events(self, events: list[dict]) -> list[dict]:
        """Remove events older than MAX_EVENT_AGE_HOURS."""
        cutoff = time.time() - (self._MAX_EVENT_AGE_HOURS * 3600)
        return [e for e in events if e.get("timestamp", 0) > cutoff][-self._MAX_EVENTS:]

    # ────────────────────────────────────────────────
    # Recording Events
    # ────────────────────────────────────────────────

    def set_goal(self, goal: str) -> None:
        """Set the session goal."""
        self._data["goal"] = goal
        self._dirty = True
        self._save()

    def record_event(self, event_type: str, detail: str) -> None:
        """
        Record a semantic event.

        Event types:
          goal_injected, approval_clicked, error_detected, error_resolved,
          question_answered, refinement_triggered, screenshot_taken,
          screenshot_skipped, simple_browser_opened, server_detected,
          loop_detected, recovery_attempted, agent_status_change,
          compaction_performed, pruning_performed
        """
        event = {
            "type": event_type,
            "detail": detail[:500],
            "timestamp": time.time(),
        }
        self._data["events"].append(event)

        # Write to JSONL transcript for full audit trail
        self._write_transcript(event)

        # Auto-increment counters
        counter_map = {
            "approval_clicked": "approvals",
            "goal_injected": "injections",
            "screenshot_taken": "vision_calls",
            "screenshot_skipped": "screenshots_skipped",
            "error_resolved": "errors_resolved",
            "question_answered": "questions_answered",
            "compaction_performed": "compactions",
            "pruning_performed": "prunings",
        }
        if event_type in counter_map:
            key = counter_map[event_type]
            self._data["counters"][key] = self._data["counters"].get(key, 0) + 1

        # Prune if too many
        if len(self._data["events"]) > self._MAX_EVENTS:
            self._data["events"] = self._data["events"][-self._MAX_EVENTS:]

        self._dirty = True
        self._save()

    def record_context_gather(self) -> None:
        """Lightweight counter update for context gathers (no event entry)."""
        self._data["counters"]["context_gathers"] = (
            self._data["counters"].get("context_gathers", 0) + 1
        )
        self._dirty = True
        # Batch save — don't save on every gather, only every 5th
        if self._data["counters"]["context_gathers"] % 5 == 0:
            self._save()

    def record_files(self, filenames: list[str]) -> None:
        """Record files the agent has worked on."""
        existing = set(self._data.get("files_worked_on", []))
        for f in filenames:
            if f not in existing:
                self._data.setdefault("files_worked_on", []).append(f)
                existing.add(f)
        self._dirty = True

    def record_port(self, port: int) -> None:
        """Record a detected server port."""
        ports = self._data.get("detected_ports", [])
        if port not in ports:
            ports.append(port)
            self._data["detected_ports"] = ports
            self._dirty = True
            self._save()

    def update_status(self, status: str) -> None:
        """Update the last known agent status."""
        if status != self._data.get("last_agent_status"):
            self._data["last_agent_status"] = status
            self._dirty = True

    # ────────────────────────────────────────────────
    # Reading Memory
    # ────────────────────────────────────────────────

    def get_session_summary(self) -> str:
        """
        Generate a paragraph summarizing what's happened so far.
        This is fed to Gemini for context instead of raw logs.
        """
        duration = (time.time() - self._data.get("session_start", time.time())) / 60
        counters = self._data.get("counters", {})
        files = self._data.get("files_worked_on", [])
        ports = self._data.get("detected_ports", [])
        goal = self._data.get("goal", "")
        events = self._data.get("events", [])
        learnings = self._data.get("learnings", [])

        parts = [
            f"Session running for {duration:.0f} minutes.",
            f"Goal: {goal}",
        ]

        if counters.get("approvals"):
            parts.append(f"Approved {counters['approvals']} commands.")
        if counters.get("injections"):
            parts.append(f"Injected {counters['injections']} prompts.")
        if counters.get("vision_calls"):
            skipped = counters.get("screenshots_skipped", 0)
            total = counters["vision_calls"] + skipped
            parts.append(
                f"Vision: {counters['vision_calls']}/{total} screenshots analyzed "
                f"({skipped} skipped)."
            )
        if counters.get("compactions"):
            parts.append(f"Compacted history {counters['compactions']} time(s).")
        if counters.get("prunings"):
            parts.append(f"Pruned context {counters['prunings']} time(s).")
        if files:
            parts.append(f"Files worked on: {', '.join(files[-10:])}")
        if ports:
            parts.append(f"Dev servers detected on ports: {', '.join(map(str, ports))}")
        if learnings:
            parts.append(f"Key learnings: {'; '.join(learnings[-5:])}")

        # Last few events
        if events:
            recent = events[-5:]
            parts.append("Recent events: " + "; ".join(
                f"{e['type']}" for e in recent
            ))

        return " ".join(parts)

    def get_last_n_events(self, n: int = 10) -> list[dict]:
        """Get recent events for decision-making."""
        return self._data.get("events", [])[-n:]

    def get_recent_events(self, n: int = 10) -> list[dict]:
        """Alias for get_last_n_events (used by scheduler actions)."""
        return self.get_last_n_events(n)

    @property
    def total_approvals(self) -> int:
        return self._data.get("counters", {}).get("approvals", 0)

    @property
    def total_injections(self) -> int:
        return self._data.get("counters", {}).get("injections", 0)

    @property
    def session_duration_minutes(self) -> float:
        return (time.time() - self._data.get("session_start", time.time())) / 60

    @property
    def last_agent_status(self) -> str:
        return self._data.get("last_agent_status", "UNKNOWN")

    # V37 FIX (L-7): Public accessors to avoid _data coupling in HUD/telemetry.
    def get_event_count(self) -> int:
        """Return the number of events currently in memory."""
        return len(self._data.get("events", []))

    def get_counter(self, key: str) -> int:
        """Return a specific counter value by key name."""
        return self._data.get("counters", {}).get(key, 0)

    # ────────────────────────────────────────────────
    # Flush
    # ────────────────────────────────────────────────

    def flush(self) -> None:
        """Force save to disk."""
        self._dirty = True
        self._save()

    # ────────────────────────────────────────────────
    # Time-Travel Engine (Snapshots)
    # ────────────────────────────────────────────────

    def snapshot_state(self, context: dict | None = None) -> str | None:
        """
        V12 Flagship: Time-Travel Engine.
        Serialize a context snapshot right before taking action.
        Saves to .ag-supervisor/snapshots/<timestamp>.json.
        Returns the filename if successful.

        V75: Accepts a generic dict (or None). The old ContextSnapshot
        class no longer exists in the headless architecture.
        """
        try:
            timestamp = int(time.time())
            filename = f"snapshot_{timestamp}.json"
            filepath = self._snapshots_dir / filename

            payload = {
                "timestamp": timestamp,
                **(context or {}),
            }

            filepath.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            
            # Limit total snapshots to 10
            snapshots = sorted(self._snapshots_dir.glob("snapshot_*.json"))
            if len(snapshots) > 10:
                for s in snapshots[:-10]:
                    try:
                        s.unlink()
                    except Exception:
                        pass
                        
            return str(filepath)
        except Exception as exc:
            logger.debug("Failed to take Time-Travel snapshot: %s", exc)
            return None

    def get_latest_snapshot(self) -> dict | None:
        """Load the most recent pre-action snapshot."""
        try:
            snapshots = sorted(self._snapshots_dir.glob("snapshot_*.json"))
            if not snapshots:
                return None
            
            latest = snapshots[-1]
            return json.loads(latest.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to load latest snapshot: %s", exc)
            return None

    # ────────────────────────────────────────────────
    # OpenClaw-Inspired: JSONL Transcript
    # ────────────────────────────────────────────────

    def _write_transcript(self, event: dict) -> None:
        """Append event to JSONL transcript file for full audit trail."""
        try:
            with open(self._transcript_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception:
            pass  # Non-critical — don't crash on transcript write failure

    # ────────────────────────────────────────────────
    # OpenClaw-Inspired: Gemini Context Pruning
    # ────────────────────────────────────────────────

    def prune_gemini_context(self, messages: list[dict]) -> list[dict]:
        """
        Prune Gemini conversation context to reduce token usage.

        Inspired by OpenClaw's session pruning:
          - Soft-trim: Keep head (500 chars) + tail (500 chars) of old tool results
          - Hard-clear: Replace very old results with placeholder
          - Protect: Skip image blocks, keep last 3 assistant messages intact

        Args:
            messages: List of message dicts with 'role' and 'content' keys.

        Returns:
            Pruned list of messages.
        """
        total_chars = sum(len(m.get("content", "")) for m in messages)
        if total_chars < self._PRUNE_MIN_CHARS:
            return messages  # Not enough to warrant pruning

        # Find assistant message boundaries
        assistant_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "assistant"
        ]
        if len(assistant_indices) <= self._PRUNE_KEEP_LAST:
            return messages  # Keep all recent assistant messages

        cutoff = assistant_indices[-self._PRUNE_KEEP_LAST]

        pruned = []
        chars_saved = 0
        for i, msg in enumerate(messages):
            if i < cutoff and msg.get("role") == "tool":
                content = msg.get("content", "")
                if len(content) > 1000:
                    # Soft-trim: keep head + tail
                    trimmed = content[:500] + "\n...[pruned by session pruning]...\n" + content[-500:]
                    chars_saved += len(content) - len(trimmed)
                    msg = {**msg, "content": trimmed}
            pruned.append(msg)

        if chars_saved > 0:
            self.record_event(
                "pruning_performed",
                f"Pruned {chars_saved:,} chars from {len(messages)} messages",
            )
            logger.info(
                "✂️  Session pruning saved %d chars (%.1f%% reduction)",
                chars_saved,
                (chars_saved / total_chars) * 100,
            )

        return pruned

    # ────────────────────────────────────────────────
    # OpenClaw-Inspired: History Compaction
    # ────────────────────────────────────────────────

    def compact_history(self) -> None:
        """
        Compact old events into a single summary event.

        Inspired by OpenClaw's compaction system:
          - Events older than COMPACTION_AGE_MINUTES are summarized
          - Recent events are kept detailed
          - A summary event replaces the old ones
          - Pre-compaction flush stores key learnings first
        """
        events = self._data.get("events", [])
        if len(events) < 20:
            return  # Not enough events to compact

        cutoff = time.time() - (self._COMPACTION_AGE_MINUTES * 60)
        old_events = [e for e in events if e.get("timestamp", 0) < cutoff]
        recent_events = [e for e in events if e.get("timestamp", 0) >= cutoff]

        if len(old_events) < 10:
            return  # Not enough old events

        # Pre-compaction flush: extract key learnings
        self._pre_compaction_flush(old_events)

        # Create summary of old events
        event_counts: dict[str, int] = {}
        for e in old_events:
            etype = e.get("type", "unknown")
            event_counts[etype] = event_counts.get(etype, 0) + 1

        summary_parts = [
            f"{count}x {etype}"
            for etype, count in sorted(event_counts.items(), key=lambda x: -x[1])
        ]
        summary = f"Compacted {len(old_events)} events: {', '.join(summary_parts)}"

        # Replace old events with single summary
        compaction_event = {
            "type": "compaction_summary",
            "detail": summary[:500],
            "timestamp": cutoff,
            "compacted_count": len(old_events),
        }

        self._data["events"] = [compaction_event] + recent_events
        self._data["counters"]["compactions"] = (
            self._data["counters"].get("compactions", 0) + 1
        )
        self._dirty = True
        self._save()

        self._write_transcript({
            "type": "compaction_performed",
            "detail": summary,
            "timestamp": time.time(),
        })

        logger.info(
            "📦  Compacted %d old events into summary. %d recent events kept.",
            len(old_events), len(recent_events),
        )

    def _pre_compaction_flush(self, events: list[dict]) -> None:
        """
        Extract and store key learnings before compacting events.

        V12 UPGRADE — Persistent Learnings (OpenClaw MEMORY.md pattern):
          - Extracts patterns from error_resolved, recovery_attempted events
          - Stores in session JSON for runtime use
          - Also writes to _LEARNINGS.md for cross-session persistence
        """
        learnings = self._data.get("learnings", [])
        valuable_types = {"error_resolved", "recovery_attempted", "agent_status_change"}
        new_learnings = []

        for event in events:
            if event.get("type") in valuable_types:
                detail = event.get("detail", "")
                if detail and detail not in learnings:
                    learnings.append(detail[:200])
                    new_learnings.append(detail[:200])

        # Cap learnings at 50
        self._data["learnings"] = learnings[-50:]

        # Persist new learnings to _LEARNINGS.md
        if new_learnings:
            self._write_learnings_file(new_learnings)

    def _write_learnings_file(self, new_learnings: list[str]) -> None:
        """
        Append learnings to _LEARNINGS.md for persistent cross-session memory.

        This file acts as OpenClaw's MEMORY.md — a curated list of patterns
        the supervisor has discovered through operation.
        """
        # Import config here to avoid circular dependency
        from . import config

        if not new_learnings:
            return

        learnings_path = config.get_state_dir() / "_LEARNINGS.md"
        try:
            if not learnings_path.exists():
                learnings_path.write_text(
                    "# Supervisor Learnings\n\n"
                    "Auto-generated persistent memory from error patterns and recoveries.\n\n",
                    encoding="utf-8",
                )
            with open(learnings_path, "a", encoding="utf-8") as f:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime())
                for learning in new_learnings:
                    f.write(f"- [{ts}] {learning}\n")
            logger.debug("📝  Wrote %d learnings to _LEARNINGS.md", len(new_learnings))
        except Exception as exc:
            logger.debug("📝  Failed to write learnings file: %s", exc)

    def get_learnings_context(self, max_items: int = 5) -> str:
        """
        Return recent learnings formatted for injection into Gemini prompts.

        This enables the supervisor to remember patterns across calls,
        inspired by OpenClaw's MEMORY.md context injection.

        Returns:
            Formatted string with recent learnings, or empty string if none.
        """
        learnings = self._data.get("learnings", [])
        if not learnings:
            return ""
        recent = learnings[-max_items:]
        header = "SUPERVISOR LEARNINGS (remember these patterns):\n"
        items = "\n".join(f"  • {l}" for l in recent)
        return header + items


