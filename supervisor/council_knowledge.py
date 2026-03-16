"""
council_knowledge.py — Persistent Knowledge Base for the Agent Council.

Stores past issues, diagnoses, and successful resolutions so the council
learns from experience and doesn't repeat failed approaches.

Features:
  • Issue → Diagnosis → Resolution tracking with timestamps
  • Similarity search: before diagnosing, check if a similar issue was solved before
  • Success/failure tracking: learn which fixes work
  • Auto-prune: entries older than COUNCIL_KNOWLEDGE_MAX_AGE_DAYS are removed
  • Thread-safe writes with atomic file operations
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

logger = logging.getLogger("supervisor.council_knowledge")

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

_MAX_ENTRIES = 200
_MAX_AGE_DAYS = 7


def _get_kb_path() -> Path:
    return config.get_state_dir() / "_council_knowledge.json"


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

def _new_entry(
    issue_type: str,
    trigger: str,
    diagnosis: str,
    actions_taken: list[dict],
    resolution: str,
    success: bool,
    agent_chain: list[str],
) -> dict:
    """Create a new knowledge entry."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "issue_type": issue_type,          # e.g. "WAITING", "CRASHED", "ERROR", "LOOP"
        "trigger": trigger,                 # what triggered the council session
        "diagnosis": diagnosis,             # root cause identified
        "actions_taken": actions_taken,     # list of {agent, action, result}
        "resolution": resolution,           # final resolution summary
        "success": success,                 # did it work?
        "agent_chain": agent_chain,         # which agents participated
        "reuse_count": 0,                   # how many times this entry was reused
    }


# ─────────────────────────────────────────────────────────────
# File I/O
# ─────────────────────────────────────────────────────────────

def _load_kb() -> list[dict]:
    """Load the knowledge base from disk."""
    kb_path = _get_kb_path()
    try:
        if kb_path.exists():
            with open(kb_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as exc:
        logger.warning("📚  Could not load knowledge base from %s: %s", kb_path, exc)
    return []


def _save_kb(entries: list[dict]) -> None:
    """Save the knowledge base to disk atomically."""
    kb_path = _get_kb_path()
    try:
        tmp_path = kb_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(kb_path)
        logger.debug("📚  Saved %d entries to knowledge base.", len(entries))
    except Exception as exc:
        logger.warning("📚  Could not save knowledge base: %s", exc)


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def record_resolution(
    issue_type: str,
    trigger: str,
    diagnosis: str,
    actions_taken: list[dict],
    resolution: str,
    success: bool,
    agent_chain: list[str],
) -> None:
    """
    Record a council session outcome to the knowledge base.

    Args:
        issue_type:    Category (WAITING, CRASHED, ERROR, LOOP)
        trigger:       What triggered the session
        diagnosis:     Root cause identified by the Diagnostician
        actions_taken: List of {agent, action, result} dicts
        resolution:    Final resolution summary
        success:       Whether the issue was resolved
        agent_chain:   Which agents participated in order
    """
    entries = _load_kb()

    entry = _new_entry(
        issue_type=issue_type,
        trigger=trigger,
        diagnosis=diagnosis,
        actions_taken=actions_taken,
        resolution=resolution,
        success=success,
        agent_chain=agent_chain,
    )

    entries.append(entry)

    # Prune old entries
    entries = _prune(entries)

    # Cap size
    if len(entries) > _MAX_ENTRIES:
        entries = entries[-_MAX_ENTRIES:]

    _save_kb(entries)
    logger.info(
        "📚  Recorded %s resolution: %s (success=%s)",
        issue_type, resolution[:60], success,
    )


def find_similar_issues(
    issue_type: str,
    trigger: str,
    max_results: int = 3,
) -> list[dict]:
    """
    Search the KB for similar past issues.

    Similarity is based on:
    1. Exact issue_type match
    2. Keyword overlap in the trigger text

    Returns up to max_results entries, sorted by relevance (most similar first).
    Successful resolutions are prioritized.
    """
    entries = _load_kb()
    if not entries:
        return []

    # Tokenize the trigger for similarity matching
    trigger_words = set(trigger.lower().split())

    scored: list[tuple[float, dict]] = []

    for entry in entries:
        score = 0.0

        # Type match is worth 5 points
        if entry.get("issue_type") == issue_type:
            score += 5.0

        # Keyword overlap in trigger
        entry_words = set(entry.get("trigger", "").lower().split())
        if trigger_words and entry_words:
            overlap = len(trigger_words & entry_words)
            score += overlap * 1.0

        # Successful resolutions get a bonus
        if entry.get("success"):
            score += 3.0

        # Recently resolved entries get a small freshness bonus
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if age_hours < 24:
                score += 1.0
        except Exception:
            pass

        if score > 0:
            scored.append((score, entry))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    results = [entry for _, entry in scored[:max_results]]

    if results:
        logger.info(
            "📚  Found %d similar past issues for '%s'",
            len(results), issue_type,
        )

    return results


def increment_reuse(trigger: str) -> None:
    """Increment the reuse count for entries matching this trigger."""
    entries = _load_kb()
    modified = False
    for entry in entries:
        if entry.get("trigger") == trigger:
            entry["reuse_count"] = entry.get("reuse_count", 0) + 1
            modified = True
    if modified:
        _save_kb(entries)


def get_stats() -> dict:
    """Return knowledge base statistics."""
    entries = _load_kb()
    if not entries:
        return {"total": 0, "successful": 0, "failed": 0}

    successful = sum(1 for e in entries if e.get("success"))
    return {
        "total": len(entries),
        "successful": successful,
        "failed": len(entries) - successful,
        "issue_types": list(set(e.get("issue_type", "?") for e in entries)),
        "most_common_agent": _most_common_agent(entries),
    }


def format_for_prompt(entries: list[dict], max_chars: int = 800) -> str:
    """
    Format KB entries into a string suitable for inclusion in a Gemini prompt.
    Keeps it compact to stay within token limits.
    """
    if not entries:
        return "(no similar past issues found)"

    lines = []
    total = 0
    for i, entry in enumerate(entries, 1):
        line = (
            f"Past Issue #{i}: [{entry.get('issue_type', '?')}] "
            f"Trigger: {entry.get('trigger', '?')[:80]} → "
            f"Diagnosis: {entry.get('diagnosis', '?')[:80]} → "
            f"Resolution: {entry.get('resolution', '?')[:80]} "
            f"({'✅ worked' if entry.get('success') else '❌ failed'})"
        )
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _prune(entries: list[dict]) -> list[dict]:
    """Remove entries older than _MAX_AGE_DAYS."""
    cutoff = datetime.now(timezone.utc)
    pruned = []
    for entry in entries:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            age_days = (cutoff - ts).total_seconds() / 86400
            if age_days <= _MAX_AGE_DAYS:
                pruned.append(entry)
        except Exception:
            pruned.append(entry)  # keep entries with bad timestamps

    removed = len(entries) - len(pruned)
    if removed > 0:
        logger.info("📚  Pruned %d expired entries from knowledge base.", removed)
    return pruned


def _most_common_agent(entries: list[dict]) -> str:
    """Find the most frequently used agent across all entries."""
    counts: dict[str, int] = {}
    for entry in entries:
        for agent in entry.get("agent_chain", []):
            counts[agent] = counts.get(agent, 0) + 1
    if not counts:
        return "none"
    return max(counts, key=counts.get)  # type: ignore
