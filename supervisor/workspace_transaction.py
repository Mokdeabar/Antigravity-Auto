"""
workspace_transaction.py — V15.1 Autonomous Git Transactions (Hard Reset Edition)

Treats the file system as a transactional database. Before the Omni-Brain mutates
any files, a pristine commit lock is acquired. Mutations happen against a dirty
working tree. Tests run against that dirty tree.

On FAILURE: `git reset --hard <SHA>` + `git clean -fd` annihilates all tracked
            and untracked changes. Zero junk commits are ever created.

On SUCCESS: `git add . && git commit --amend` overwrites the pre-execution lock
            into a single clean commit. The Git history shows one entry.

V40: WorkspaceFileLock — per-file asyncio mutex for concurrent worker safety.
"""

from __future__ import annotations

import asyncio
import subprocess
import logging
from collections import defaultdict
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("supervisor.workspace_transaction")


# ────────────────────────────────────────────────
# V40: File-Level Mutex for Concurrent Workers
# ────────────────────────────────────────────────

class WorkspaceFileLock:
    """
    Two-tier concurrency manager for the DAG worker pool.

    TIER 1 — Global Sandbox Lock (`acquire_sandbox()`):
        Serializes ALL access to the single Docker container. Reserved for
        bulk operations like `sync_files_to_sandbox()` where the entire
        workspace is being copied. NOT used for normal task execution.

    TIER 2 — Per-File Locks (`acquire_files(paths)`) [V41: ACTIVE]:
        Fine-grained asyncio mutex on individual file paths. Two workers
        touching different files run concurrently; two workers touching
        the SAME file serialize only on that file. Locks are acquired
        in sorted path order to prevent deadlocks between workers.

        Used after task execution to protect shared state mutation
        (recording file changes, updating planner progress) when
        workers happen to modify overlapping files.

    Design Rationale (V41):
        Docker's daemon handles concurrent `docker cp` and `docker exec`
        to different destination paths safely. The global lock (Tier 1)
        was creating a parallel illusion — 3 workers would queue behind
        each other. Per-file locks (Tier 2) allow true parallel execution
        while preventing corruption when two tasks touch the same file.

    Usage:
        lock = get_workspace_lock()

        # Tier 1: Global (bulk sync only)
        async with lock.acquire_sandbox():
            await sandbox.sync_files_to_sandbox()

        # Tier 2: Per-file (normal task execution — V41 ACTIVE)
        async with lock.acquire_files(changed_files):
            all_files_changed.extend(changed_files)
            planner.mark_complete(task_id)
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._global_lock = asyncio.Lock()  # Tier 1: Serializes all sandbox exec
        self._contention_count = 0          # How many times a lock was already held

    def _normalize(self, path: str) -> str:
        """Normalize to forward-slash relative path for consistent locking."""
        return str(Path(path)).replace("\\", "/").lower()

    class _MultiFileLock:
        """Context manager that acquires locks on multiple files in sorted order."""

        def __init__(self, locks: list[asyncio.Lock], parent: "WorkspaceFileLock"):
            self._locks = locks
            self._parent = parent

        async def __aenter__(self):
            for lock in self._locks:
                if lock.locked():
                    self._parent._contention_count += 1
                await lock.acquire()
            return self

        async def __aexit__(self, *args):
            for lock in reversed(self._locks):
                lock.release()

    def acquire_files(self, file_paths: list[str]) -> _MultiFileLock:
        """
        Acquire locks on a set of file paths. Always acquires in sorted
        order to prevent deadlocks between workers.
        """
        normalized = sorted(set(self._normalize(p) for p in file_paths))
        locks = [self._locks[p] for p in normalized]
        return self._MultiFileLock(locks, self)

    def acquire_sandbox(self):
        """
        Acquire the global sandbox execution lock. Use when a worker
        needs exclusive access to the single Docker container.
        """
        return self._global_lock

    def get_stats(self) -> dict:
        """Return lock statistics for debugging and UI display."""
        active = sum(1 for lock in self._locks.values() if lock.locked())
        return {
            "tracked_files": len(self._locks),
            "active_file_locks": active,
            "global_lock_held": self._global_lock.locked(),
            "contention_events": self._contention_count,
        }


_workspace_lock: WorkspaceFileLock | None = None


def get_workspace_lock() -> WorkspaceFileLock:
    """Get or create the singleton WorkspaceFileLock."""
    global _workspace_lock
    if _workspace_lock is None:
        _workspace_lock = WorkspaceFileLock()
    return _workspace_lock


class GitTransactionManager:
    """Handles atomic Git snapshotting, testing, hard-reset rollback, and amend-commit."""

    def __init__(self, workspace_path: str):
        self.cwd = workspace_path
        self._pristine_sha: str | None = None

    def _run_git(self, *args) -> Tuple[int, str, str]:
        """Core wrapper for running git commands."""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=30
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except Exception as e:
            return -1, "", str(e)

    # ────────────────────────────────────────────────
    # Safety Guards
    # ────────────────────────────────────────────────

    def is_git_repo(self) -> bool:
        """Check if the target directory is a git repository."""
        code, _, _ = self._run_git("rev-parse", "--is-inside-work-tree")
        return code == 0

    def is_detached_head(self) -> bool:
        """Check if HEAD is detached. Transactions MUST NOT run in detached state."""
        code, out, _ = self._run_git("symbolic-ref", "-q", "HEAD")
        # symbolic-ref fails (code != 0) when HEAD is detached
        return code != 0

    def get_current_sha(self) -> str | None:
        """Get the current HEAD SHA."""
        code, out, _ = self._run_git("rev-parse", "HEAD")
        return out if code == 0 else None

    # ────────────────────────────────────────────────
    # Phase 1: Acquire the Pre-Execution Lock
    # ────────────────────────────────────────────────

    def commit_pre_execution_state(self) -> Tuple[bool, str]:
        """
        Stage all files (tracked + untracked) and commit before the agent touches anything.
        Returns (success, pristine_sha_or_error).
        """
        if not self.is_git_repo():
            return False, "Target directory is not a Git repository."

        if self.is_detached_head():
            return False, "ABORT: Detached HEAD detected. Cannot safely transact."

        # Stage everything including untracked files
        self._run_git("add", "-A")

        # Check if there's anything to commit
        code, status, _ = self._run_git("status", "--porcelain")
        if status:
            # There are staged changes — commit the pristine state
            code_c, _, err_c = self._run_git("commit", "-m", "auto: pre execution pristine state")
            if code_c != 0:
                return False, f"Failed to commit pristine state: {err_c}"

        sha = self.get_current_sha()
        if not sha:
            return False, "Failed to get starting SHA."

        self._pristine_sha = sha
        logger.info("📦 Pre-execution lock acquired. SHA: %s", sha[:7])
        return True, sha

    # ────────────────────────────────────────────────
    # Phase 2: Test against the dirty working tree
    # ────────────────────────────────────────────────

    def run_tests(self, test_command: str) -> Tuple[bool, str]:
        """
        Execute the test suite against the DIRTY working tree.
        No intermediate commits are made.
        """
        logger.info("🧪 Running validation against dirty tree: %s", test_command)
        try:
            # V37 FIX (H-5): Avoid shell=True where possible.
            # On non-Windows, parse the command safely with shlex.
            # On Windows, keep shell=True because npm/npx commands need shell resolution.
            import shlex
            from . import config as _cfg
            if _cfg.IS_WINDOWS:
                cmd_arg = test_command
                use_shell = True
            else:
                cmd_arg = shlex.split(test_command)
                use_shell = False

            proc = subprocess.run(
                cmd_arg,
                cwd=self.cwd,
                shell=use_shell,
                capture_output=True,
                text=True,
                timeout=120
            )
            logs = (proc.stdout + "\n" + proc.stderr).strip()
            return (proc.returncode == 0), logs
        except subprocess.TimeoutExpired:
            return False, "Test suite timed out after 120 seconds."
        except Exception as e:
            return False, f"Exception while running test suite: {e}"

    # ────────────────────────────────────────────────
    # Phase 3a: FAILURE — Capture diff, then hard reset + clean
    # ────────────────────────────────────────────────

    def capture_dirty_diff(self) -> str:
        """
        V16: Snapshot the dirty working tree BEFORE hard reset destroys it.
        Returns the raw `git diff HEAD` output.
        """
        code, diff, _ = self._run_git("diff", "HEAD")
        # Also capture untracked files
        code2, untracked, _ = self._run_git("diff", "--no-index", "/dev/null", ".")
        # The untracked diff is unreliable cross-platform, just use status
        code3, status_out, _ = self._run_git("status", "--porcelain")
        
        untracked_files = [
            line[3:] for line in status_out.split("\n")
            if line.startswith("??")
        ] if status_out else []

        result = diff or ""
        if untracked_files:
            result += f"\n\n[UNTRACKED FILES CREATED]: {', '.join(untracked_files)}"

        logger.info("🧠 Captured dirty diff (%d chars) before hard reset.", len(result))
        return result

    def hard_reset_to_pristine(self, pristine_sha: str | None = None) -> bool:
        """
        Annihilate all tracked and untracked changes.
        `git reset --hard <SHA>` wipes tracked modifications.
        `git clean -fd` purges untracked files and directories.
        """
        sha = pristine_sha or self._pristine_sha
        if not sha:
            logger.error("♻️ CRITICAL: No pristine SHA to reset to.")
            return False

        logger.warning("♻️ Hard resetting to pristine SHA %s...", sha[:7])

        # Step 1: Destroy all tracked modifications
        code, _, err = self._run_git("reset", "--hard", sha)
        if code != 0:
            logger.error("♻️ git reset --hard failed: %s", err)
            return False

        # Step 2: Purge all untracked files and directories
        code2, _, err2 = self._run_git("clean", "-fd")
        if code2 != 0:
            logger.warning("♻️ git clean -fd warning: %s", err2)
            # Non-fatal: .gitignore'd files may remain

        logger.info("♻️ Workspace annihilated. Pristine state restored.")
        return True

    # ────────────────────────────────────────────────
    # Phase 3b: SUCCESS — Amend the lock commit
    # ────────────────────────────────────────────────

    def amend_success_commit(self) -> bool:
        """
        Stage all agent modifications and amend the pre-execution lock
        into a single clean success commit. Zero intermediate commits.
        """
        logger.info("✨ Amending pre-execution lock into success commit...")

        self._run_git("add", "-A")

        # Check if there's actually anything to amend
        code_s, status, _ = self._run_git("status", "--porcelain")
        if not status:
            logger.info("✨ No changes to amend (neutral mutation).")
            return True

        code, _, err = self._run_git(
            "commit", "--amend", "-m",
            "V15: Omni-Brain autonomous execution success"
        )
        if code != 0:
            logger.error("✨ Amend commit failed: %s", err)
            return False

        logger.info("✨ Git history clean. Single success commit recorded.")
        return True
