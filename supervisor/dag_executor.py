"""
V79: DAG execution utilities extracted from main.py.

Provides timeout computation and DAG progress updates for the
recursive DAG decomposition and execution engine.
"""

import logging

from . import config

logger = logging.getLogger("supervisor")


async def compute_chunk_timeout(local_brain, description: str) -> int:
    """
    V41: Instant per-chunk timeout using description length heuristic.

    DAG chunks are already decomposed atomic tasks — Ollama classification
    added ~22s of latency per node with zero value (always returned 3600s).
    Simple heuristic: short descriptions = simpler tasks, long = complex.

    Returns timeout in seconds, clamped to [180, GEMINI_TIMEOUT_SECONDS].
    Floor is 180s because even atomic tasks need time for Gemini to read
    project files, plan changes, and write code.
    """
    desc_len = len(description)
    if desc_len < 150:
        timeout = 180   # Simple, focused task — 3 min minimum
    elif desc_len < 400:
        timeout = 300   # Medium complexity — 5 min
    else:
        timeout = config.GEMINI_TIMEOUT_SECONDS  # Full timeout for detailed tasks
    return min(timeout, config.GEMINI_TIMEOUT_SECONDS)


async def update_dag_progress(planner, depth: int, running: list[str] | None = None, state=None, queued_ids: set | None = None):
    """Update the global DAG progress dict for UI consumption and broadcast.

    queued_ids: task IDs that are in active_tasks but still at status='pending'
    (i.e. submitted to asyncio pool, waiting at the semaphore). Exposed as
    status='queued' so the UI can distinguish them from unscheduled pending tasks.
    """
    _queued = queued_ids or set()
    nodes_list = []
    _pending_count = 0
    for n in planner._nodes.values():
        # Override: if node is pending but already in the asyncio pool, show as queued
        effective_status = "queued" if (n.status == "pending" and n.task_id in _queued) else n.status
        nodes_list.append({
            "id": n.task_id,
            "desc": n.description,
            "status": effective_status,
            "deps": n.dependencies,
            "priority": getattr(n, "priority", 0),
        })
        if n.status == "pending":
            _pending_count += 1
    # V54: Expose pending count on state so the monitor heuristic
    # can detect stuck DAG work even when state.planner is None.
    if state:
        state._dag_pending_count = _pending_count

    progress = planner.get_progress()
    # V51: Exclude cancelled from total — they're historical, not active work
    _cancelled = progress.get("cancelled", 0)

    dag_progress = {
        "active": True,
        "depth": depth,
        "total": sum(progress.values()) - _cancelled,
        "completed": progress.get("complete", 0),
        "running": running or [],
        "pending": progress.get("pending", 0),
        "failed": progress.get("failed", 0),
        "cancelled": _cancelled,
        "nodes": nodes_list,
    }

    # V40: Broadcast to UI immediately so progress is live
    if state:
        try:
            await state.broadcast_state()
        except Exception:
            pass

    return dag_progress
