"""
error_memory.py — V56 Persistent Error Pattern Memory.

Stores error→fix pairs learned during task execution so Gemini
doesn't have to re-discover the same fixes session after session.

Schema of .ag-supervisor/error_memory.json:
[
  {
    "error_pattern": "Cannot find module '@/components'",
    "fix": "Added paths alias to tsconfig.json and vite.config.ts",
    "count": 3,
    "last_seen": 1741340000.0
  }
]
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("supervisor.error_memory")

_MEMORY_FILE = "error_memory.json"
_MAX_PATTERNS = 50      # cap the store to avoid unbounded growth
_MAX_PROMPT_INJECT = 5  # top-N patterns to inject into each task prompt


def _memory_path(project_path: str) -> Path:
    return Path(project_path) / ".ag-supervisor" / _MEMORY_FILE


def load_memory(project_path: str) -> list[dict]:
    p = _memory_path(project_path)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def save_memory(project_path: str, patterns: list[dict]) -> None:
    p = _memory_path(project_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Sort by count desc, cap at max
    patterns = sorted(patterns, key=lambda x: x.get("count", 1), reverse=True)[:_MAX_PATTERNS]
    p.write_text(json.dumps(patterns, indent=2, ensure_ascii=False), encoding="utf-8")


def record_error_fix(project_path: str, error_text: str, fix_summary: str) -> None:
    """
    Record that `error_text` was encountered and `fix_summary` resolved it.
    Deduplicated by error_pattern similarity (first 120 chars of normalised text).
    """
    if not project_path or not error_text or not fix_summary:
        return
    _key = " ".join(error_text.lower().split())[:120]
    patterns = load_memory(project_path)
    for p in patterns:
        if p.get("error_pattern", "")[:120] == _key:
            p["count"] = p.get("count", 1) + 1
            p["last_seen"] = time.time()
            p["fix"] = fix_summary  # update with most recent fix
            save_memory(project_path, patterns)
            logger.debug("[ErrorMemory] Updated pattern (count=%d): %.80s", p["count"], _key)
            return
    patterns.append({
        "error_pattern": error_text[:200],
        "fix": fix_summary[:400],
        "count": 1,
        "last_seen": time.time(),
    })
    save_memory(project_path, patterns)
    logger.info("[ErrorMemory] Recorded new error pattern: %.80s", _key)


def build_error_memory_block(project_path: str, task_prompt: str) -> str:
    """
    Return a prompt section listing the top-N error patterns relevant to this task.
    Relevance = keyword overlap between error_pattern and task_prompt.
    Returns empty string if no patterns or project not set.
    """
    if not project_path:
        return ""
    patterns = load_memory(project_path)
    if not patterns:
        return ""

    prompt_words = set(task_prompt.lower().split())

    def _relevance(pat: dict) -> float:
        pat_words = set(pat.get("error_pattern", "").lower().split())
        overlap = len(prompt_words & pat_words)
        return overlap + pat.get("count", 1) * 0.1

    ranked = sorted(patterns, key=_relevance, reverse=True)[:_MAX_PROMPT_INJECT]
    # Filter out zero-relevance patterns
    relevant = [p for p in ranked if any(w in task_prompt.lower() for w in p.get("error_pattern", "").lower().split()[:6])]
    if not relevant:
        # Fall back to top-3 by count regardless
        relevant = sorted(patterns, key=lambda x: x.get("count", 1), reverse=True)[:3]
    if not relevant:
        return ""

    lines = ["## Known Error Patterns (from previous sessions)", ""]
    for i, p in enumerate(relevant, 1):
        lines.append(f"{i}. **Error**: `{p['error_pattern'][:100]}`")
        lines.append(f"   **Fix applied**: {p['fix'][:200]}")
        lines.append("")
    lines.append("Apply these same fixes if you encounter identical or similar errors.")
    return "\n".join(lines)
