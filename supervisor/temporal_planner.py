"""
temporal_planner.py — V19 Temporal Planning Engine (Product Manager)

Ingests high-level EPIC.md feature requests and decomposes them into a
Directed Acyclic Graph (DAG) of atomic, sequentially committed micro-tasks.

Each node in the DAG is a single Git transaction — small enough to fit in the
Gemini Worker's context window, testable in isolation, and independently
committable.

Execution Flow:
  1. Ingest EPIC.md → Local Manager decomposes → JSON DAG
  2. Execute unblocked nodes sequentially via V15 Git transactions
  3. On success: commit becomes the new baseline, mark node complete
  4. On failure: reflect via V17, then replan remainder (MAX_REPLAN_COUNT=3)
  5. Workspace hash validation before every node execution

State persists to `.ag-memory/epic_state.json` so the agent can resume
after crashes.
"""

import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("supervisor.temporal_planner")

_MEMORY_DIR = Path(__file__).resolve().parent.parent / ".ag-memory"
_EPIC_STATE_PATH = _MEMORY_DIR / "epic_state.json"
MAX_REPLAN_COUNT = 3


class TaskNode:
    """A single atomic task in the DAG."""

    def __init__(
        self,
        task_id: str,
        description: str,
        dependencies: List[str] = None,
        status: str = "pending",  # pending | running | complete | failed | skipped
        knowledge_gaps: List[str] = None,
    ):
        self.task_id = task_id
        self.description = description
        self.dependencies = dependencies or []
        self.knowledge_gaps = knowledge_gaps or []
        self.status = status
        self.result: str = ""
        self.commit_sha: str = ""

    # V23: UI keyword detection for Visual Phase triggering
    _UI_KEYWORDS = [
        "component", "frontend", "view", "page", "layout", "ui",
        "button", "form", "modal", "dialog", "sidebar", "navbar",
        "header", "footer", "card", "dashboard", "chart", "table",
        "css", "style", "theme", "responsive", "animation", "menu",
        "checkout", "login", "signup", "html", "template", "render",
    ]

    @property
    def is_visual(self) -> bool:
        """Check if this node is a frontend/UI task that needs Visual QA."""
        lower = self.description.lower()
        return any(kw in lower for kw in self._UI_KEYWORDS)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "dependencies": self.dependencies,
            "knowledge_gaps": self.knowledge_gaps,
            "status": self.status,
            "result": self.result,
            "commit_sha": self.commit_sha,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskNode":
        node = cls(
            task_id=data["task_id"],
            description=data["description"],
            dependencies=data.get("dependencies", []),
            status=data.get("status", "pending"),
            knowledge_gaps=data.get("knowledge_gaps", []),
        )
        node.result = data.get("result", "")
        node.commit_sha = data.get("commit_sha", "")
        return node


class TemporalPlanner:
    """
    Decomposes epics into DAGs and executes them as atomic Git transactions.
    """

    DECOMPOSITION_PROMPT = (
        "You are a strict technical project planner. You will receive a high-level "
        "feature request (epic). Decompose it into the smallest possible atomic tasks "
        "that can each be completed in a single code edit.\n\n"
        "RULES:\n"
        "1. Each task must modify at most 2–3 files.\n"
        "2. Each task must be independently testable.\n"
        "3. Dependencies must form a DAG (no cycles).\n"
        "4. task_id must be short alphanumeric (e.g., 't1', 't2').\n"
        "5. First task must have empty dependencies.\n"
        "6. Maximum 15 tasks per epic.\n"
        "7. For each task, include a 'knowledge_gaps' array listing any external "
        "libraries, APIs, or frameworks the agent needs documentation for. "
        "Each entry should be a search query. Use an empty array if no gaps exist.\n\n"
        'Output strict JSON: {"tasks": [{"task_id": "t1", "description": "...", '
        '"dependencies": [], "knowledge_gaps": ["Moyasar payment API integration"]}, ...]}'
    )

    REPLAN_PROMPT = (
        "You are a strict technical project planner. A multi-step plan has encountered "
        "a failure. You must rewrite the REMAINING tasks to route around the blocker.\n\n"
        "RULES:\n"
        "1. Do NOT change any completed tasks.\n"
        "2. The failed task's lesson tells you what to avoid.\n"
        "3. Rewrite only the pending tasks to find an alternative approach.\n"
        "4. Maintain valid DAG structure (no cycles).\n"
        "5. Keep task_ids consistent where possible.\n\n"
        'Output strict JSON: {"tasks": [{"task_id": "...", "description": "...", '
        '"dependencies": []}, ...]}'
    )

    def __init__(self, local_manager, workspace_path: str):
        self._manager = local_manager
        self._workspace = workspace_path
        self._nodes: Dict[str, TaskNode] = {}
        self._epic_text: str = ""
        self._replan_count: int = 0
        self._workspace_hash: str = ""
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # ────────────────────────────────────────────────
    # Epic Ingestion
    # ────────────────────────────────────────────────

    def load_epic(self, epic_path: Optional[str] = None) -> Tuple[bool, str]:
        """Load an EPIC.md file from the workspace root."""
        path = Path(epic_path) if epic_path else Path(self._workspace) / "EPIC.md"
        if not path.exists():
            return False, f"Epic file not found: {path}"

        self._epic_text = path.read_text(encoding="utf-8").strip()
        if not self._epic_text:
            return False, "Epic file is empty."

        logger.info("📋 Loaded epic from %s (%d chars)", path.name, len(self._epic_text))
        return True, self._epic_text

    # ────────────────────────────────────────────────
    # DAG Decomposition
    # ────────────────────────────────────────────────

    async def decompose_epic(self, epic_text: Optional[str] = None) -> Tuple[bool, str]:
        """Route the epic to the Local Manager for DAG decomposition."""
        text = epic_text or self._epic_text
        if not text:
            return False, "No epic text to decompose."

        raw = await self._manager.ask_local_model(
            system_prompt=self.DECOMPOSITION_PROMPT,
            user_prompt=f"EPIC:\n{text[:3000]}",
            temperature=0.0,
        )

        if not raw or raw == "{}":
            return False, "Local Manager returned empty decomposition."

        return self._parse_dag(raw)

    def _parse_dag(self, raw_json: str) -> Tuple[bool, str]:
        """Parse and validate the JSON DAG from the LLM."""
        try:
            data = json.loads(raw_json)
            tasks = data.get("tasks", [])
        except (json.JSONDecodeError, AttributeError) as e:
            return False, f"Invalid JSON from decomposition: {e}"

        if not tasks:
            return False, "Decomposition returned zero tasks."

        if len(tasks) > 15:
            tasks = tasks[:15]

        # Validate DAG structure
        ids = {t["task_id"] for t in tasks}
        self._nodes = {}

        for t in tasks:
            tid = t.get("task_id", "")
            desc = t.get("description", "")
            deps = t.get("dependencies", [])

            if not tid or not desc:
                return False, f"Task missing task_id or description: {t}"

            # Validate dependencies exist
            for dep in deps:
                if dep not in ids:
                    return False, f"Task {tid} has unknown dependency: {dep}"

            gaps = t.get("knowledge_gaps", [])
            self._nodes[tid] = TaskNode(tid, desc, deps, knowledge_gaps=gaps)

        # Verify no cycles (topological sort)
        if not self._is_dag():
            return False, "Decomposition contains a cycle — invalid DAG."

        self._save_state()
        logger.info("📋 Decomposed epic into %d atomic tasks.", len(self._nodes))
        return True, f"DAG created with {len(self._nodes)} tasks."

    def _is_dag(self) -> bool:
        """Verify the graph is acyclic using Kahn's algorithm."""
        in_degree = {tid: 0 for tid in self._nodes}
        for node in self._nodes.values():
            for dep in node.dependencies:
                if dep in in_degree:
                    in_degree[dep] = in_degree.get(dep, 0)

        adj: Dict[str, List[str]] = {tid: [] for tid in self._nodes}
        for node in self._nodes.values():
            for dep in node.dependencies:
                adj[dep].append(node.task_id)
                in_degree[node.task_id] += 1

        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        visited = 0

        while queue:
            tid = queue.pop(0)
            visited += 1
            for child in adj.get(tid, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        return visited == len(self._nodes)

    # ────────────────────────────────────────────────
    # Execution Queries
    # ────────────────────────────────────────────────

    def get_next_unblocked(self) -> Optional[TaskNode]:
        """Return the first pending task whose dependencies are all complete."""
        for node in self._nodes.values():
            if node.status != "pending":
                continue
            deps_met = all(
                self._nodes[dep].status == "complete"
                for dep in node.dependencies
                if dep in self._nodes
            )
            if deps_met:
                return node
        return None

    def get_parallel_batch(self, max_workers: int = 2) -> List[TaskNode]:
        """
        V20: Return ALL unblocked nodes (up to max_workers) for parallel execution.
        Independent branches of the DAG can run concurrently in isolated worktrees.
        """
        batch = []
        for node in self._nodes.values():
            if node.status != "pending":
                continue
            deps_met = all(
                self._nodes[dep].status == "complete"
                for dep in node.dependencies
                if dep in self._nodes
            )
            if deps_met:
                batch.append(node)
                if len(batch) >= max_workers:
                    break
        return batch

    def get_completed_summary(self) -> str:
        """Build a brief summary of completed prerequisite steps for context scoping."""
        completed = [
            n for n in self._nodes.values() if n.status == "complete"
        ]
        if not completed:
            return ""

        lines = ["[COMPLETED PREREQUISITE STEPS]"]
        for c in completed:
            lines.append(f"  ✓ {c.task_id}: {c.description[:100]}")
        return "\n".join(lines)

    def get_progress(self) -> Dict[str, int]:
        """Return task count by status."""
        counts = {"pending": 0, "complete": 0, "failed": 0, "skipped": 0, "running": 0}
        for n in self._nodes.values():
            counts[n.status] = counts.get(n.status, 0) + 1
        return counts

    def is_epic_complete(self) -> bool:
        """Check if all tasks are complete."""
        return all(n.status == "complete" for n in self._nodes.values())

    def mark_complete(self, task_id: str, commit_sha: str = ""):
        """Mark a task as successfully completed."""
        if task_id in self._nodes:
            self._nodes[task_id].status = "complete"
            self._nodes[task_id].commit_sha = commit_sha
            self._save_state()

    def mark_failed(self, task_id: str, result: str = ""):
        """Mark a task as failed."""
        if task_id in self._nodes:
            self._nodes[task_id].status = "failed"
            self._nodes[task_id].result = result[:500]
            self._save_state()

    # ────────────────────────────────────────────────
    # Workspace Hash Validation
    # ────────────────────────────────────────────────

    def compute_workspace_hash(self) -> str:
        """Hash the current HEAD SHA to detect external edits."""
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self._workspace,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.stdout.strip() if proc.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    def validate_workspace(self) -> Tuple[bool, str]:
        """
        Verify the workspace hasn't been externally modified since last node execution.
        If the hash changed unexpectedly, the plan is unsafe to continue.
        """
        current_hash = self.compute_workspace_hash()

        if not self._workspace_hash:
            # First execution — baseline it
            self._workspace_hash = current_hash
            return True, "Baseline workspace hash recorded."

        if current_hash != self._workspace_hash:
            logger.warning(
                "⚠️ Workspace modified externally (%s → %s). Plan must be re-evaluated.",
                self._workspace_hash[:7], current_hash[:7],
            )
            return False, "Workspace modified externally. Re-evaluation required."

        return True, "Workspace consistent."

    def update_workspace_hash(self):
        """Update the stored hash after a successful commit."""
        self._workspace_hash = self.compute_workspace_hash()

    # ────────────────────────────────────────────────
    # Replan on Failure
    # ────────────────────────────────────────────────

    async def replan(self, failed_task_id: str, lesson: str) -> Tuple[bool, str]:
        """
        Rewrite the remaining DAG to route around a failed task.
        Returns (success, message). Enforces MAX_REPLAN_COUNT.
        """
        self._replan_count += 1
        if self._replan_count > MAX_REPLAN_COUNT:
            logger.error(
                "🚫 MAX_REPLAN_COUNT (%d) exceeded. Aborting epic.", MAX_REPLAN_COUNT
            )
            return False, f"Replan limit ({MAX_REPLAN_COUNT}) exceeded. Epic aborted."

        # Build context for the replanner
        completed = [n.to_dict() for n in self._nodes.values() if n.status == "complete"]
        pending = [n.to_dict() for n in self._nodes.values() if n.status == "pending"]
        failed = [n.to_dict() for n in self._nodes.values() if n.status == "failed"]

        user_prompt = (
            f"ORIGINAL EPIC:\n{self._epic_text[:1500]}\n\n"
            f"COMPLETED TASKS:\n{json.dumps(completed, indent=2)[:1000]}\n\n"
            f"FAILED TASK: {failed_task_id}\n"
            f"FAILURE LESSON: {lesson}\n\n"
            f"REMAINING PENDING TASKS:\n{json.dumps(pending, indent=2)[:1000]}\n\n"
            "Rewrite the remaining pending tasks to route around the failure."
        )

        raw = await self._manager.ask_local_model(
            system_prompt=self.REPLAN_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,
        )

        if not raw or raw == "{}":
            return False, "Replanner returned empty response."

        try:
            data = json.loads(raw)
            new_tasks = data.get("tasks", [])
        except Exception as e:
            return False, f"Replan JSON parse error: {e}"

        if not new_tasks:
            return False, "Replanner returned zero tasks."

        # Preserve completed tasks, replace pending/failed with rewritten ones
        preserved = {tid: n for tid, n in self._nodes.items() if n.status == "complete"}
        new_ids = {t["task_id"] for t in new_tasks}

        for t in new_tasks:
            tid = t.get("task_id", "")
            if tid in preserved:
                continue  # Don't overwrite completed tasks
            self._nodes[tid] = TaskNode(
                tid,
                t.get("description", ""),
                t.get("dependencies", []),
            )

        # Remove old pending/failed tasks that aren't in the new plan
        to_remove = [
            tid for tid in list(self._nodes.keys())
            if self._nodes[tid].status in ("pending", "failed") and tid not in new_ids
        ]
        for tid in to_remove:
            del self._nodes[tid]

        self._save_state()
        logger.info(
            "🔄 Replanned DAG (attempt %d/%d). %d tasks remain.",
            self._replan_count, MAX_REPLAN_COUNT, len(self._nodes),
        )
        return True, f"Replanned successfully (attempt {self._replan_count}/{MAX_REPLAN_COUNT})."

    # ────────────────────────────────────────────────
    # State Persistence
    # ────────────────────────────────────────────────

    def _save_state(self):
        """Persist the DAG state to disk so the agent can resume after crashes."""
        state = {
            "epic_text": self._epic_text[:5000],
            "replan_count": self._replan_count,
            "workspace_hash": self._workspace_hash,
            "timestamp": time.time(),
            "nodes": {tid: n.to_dict() for tid, n in self._nodes.items()},
        }
        _EPIC_STATE_PATH.write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )

    def load_state(self) -> bool:
        """Load a previously persisted DAG state."""
        if not _EPIC_STATE_PATH.exists():
            return False
        try:
            state = json.loads(_EPIC_STATE_PATH.read_text(encoding="utf-8"))
            self._epic_text = state.get("epic_text", "")
            self._replan_count = state.get("replan_count", 0)
            self._workspace_hash = state.get("workspace_hash", "")
            nodes_data = state.get("nodes", {})
            self._nodes = {
                tid: TaskNode.from_dict(data) for tid, data in nodes_data.items()
            }
            logger.info("📋 Resumed epic state: %d tasks.", len(self._nodes))
            return True
        except Exception as e:
            logger.error("Failed to load epic state: %s", e)
            return False

    def clear_state(self):
        """Wipe the epic state file after completion or abort."""
        if _EPIC_STATE_PATH.exists():
            _EPIC_STATE_PATH.unlink()
        self._nodes = {}
        self._replan_count = 0
