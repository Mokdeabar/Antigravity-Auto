"""
merge_arbiter.py — V20 Parallel Merge Arbiter

When concurrent DAG nodes execute in isolated Git worktrees, the Arbiter
merges each completed worktree back into the main repository HEAD.

If a merge conflict occurs, the Arbiter extracts the Git conflict markers
and routes the exact diff to the Local Manager for LLM-assisted resolution.
"""

import logging
import subprocess
import shutil
from pathlib import Path
from typing import Tuple, Optional

logger = logging.getLogger("supervisor.merge_arbiter")

_WORKTREE_DIR = ".ag-worktrees"


class MergeArbiter:
    """Merges isolated worktrees back into the main branch."""

    def __init__(self, main_repo_path: str, local_manager=None):
        self._repo = main_repo_path
        self._manager = local_manager
        self._worktree_base = Path(main_repo_path) / _WORKTREE_DIR

    def _run_git(self, *args, cwd: Optional[str] = None) -> Tuple[int, str, str]:
        """Execute a git command."""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd or self._repo,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except Exception as e:
            return -1, "", str(e)

    # ────────────────────────────────────────────────
    # Worktree Lifecycle
    # ────────────────────────────────────────────────

    def create_worktree(self, node_id: str, base_sha: str) -> Tuple[bool, str]:
        """
        Create an isolated worktree for a DAG node.
        `git worktree add <path> <base_sha> --detach`
        Returns (success, worktree_path_or_error).
        """
        wt_path = self._worktree_base / node_id
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale worktree if it exists
        if wt_path.exists():
            self._run_git("worktree", "remove", str(wt_path), "--force")
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)

        code, out, err = self._run_git(
            "worktree", "add", str(wt_path), base_sha, "--detach"
        )
        if code != 0:
            return False, f"Failed to create worktree for {node_id}: {err}"

        logger.info("🌿 Created worktree for [%s] at %s", node_id, wt_path)
        return True, str(wt_path)

    def remove_worktree(self, node_id: str) -> bool:
        """Remove a worktree after merge or failure."""
        wt_path = self._worktree_base / node_id
        code, _, err = self._run_git("worktree", "remove", str(wt_path), "--force")
        if code != 0:
            # Force cleanup
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
            self._run_git("worktree", "prune")
        logger.info("🌿 Removed worktree for [%s]", node_id)
        return True

    def prune_worktrees(self) -> str:
        """Clean up orphaned worktrees from crashed workers."""
        code, out, err = self._run_git("worktree", "prune")
        # Also remove the directory if it's empty
        if self._worktree_base.exists():
            remaining = list(self._worktree_base.iterdir())
            if not remaining:
                self._worktree_base.rmdir()
        return "Worktrees pruned." if code == 0 else f"Prune warning: {err}"

    # ────────────────────────────────────────────────
    # Merge Back to Main
    # ────────────────────────────────────────────────

    def merge_worktree(self, node_id: str) -> Tuple[bool, str]:
        """
        Merge a completed worktree's changes back into the main repo.

        Strategy:
        1. In the worktree, stage and commit all changes.
        2. In the main repo, cherry-pick the worktree's HEAD commit.
        3. If conflict → attempt LLM resolution.
        4. Clean up the worktree.
        """
        wt_path = str(self._worktree_base / node_id)

        # Step 1: Commit changes in the worktree
        self._run_git("add", "-A", cwd=wt_path)
        code_s, status, _ = self._run_git("status", "--porcelain", cwd=wt_path)
        if not status:
            self.remove_worktree(node_id)
            return True, "No changes to merge (neutral mutation)."

        self._run_git(
            "commit", "-m", f"V20: parallel node [{node_id}] complete",
            cwd=wt_path,
        )

        # Step 2: Get the worktree's HEAD SHA
        code_h, wt_sha, _ = self._run_git("rev-parse", "HEAD", cwd=wt_path)
        if code_h != 0:
            self.remove_worktree(node_id)
            return False, "Failed to get worktree HEAD SHA."

        # Step 3: Cherry-pick into main repo
        code_cp, out_cp, err_cp = self._run_git("cherry-pick", wt_sha, "--no-commit")

        if code_cp == 0:
            # Clean merge — commit it
            self._run_git("commit", "-m", f"V20: merged parallel node [{node_id}]")
            self.remove_worktree(node_id)
            logger.info("✅ Merged [%s] cleanly.", node_id)
            return True, "Merged cleanly."

        # Step 4: Conflict detected — attempt LLM resolution
        logger.warning("⚠️ Merge conflict on [%s]. Attempting LLM resolution.", node_id)
        return self._resolve_conflict(node_id)

    async def resolve_conflict_async(self, node_id: str) -> Tuple[bool, str]:
        """Async wrapper for conflict resolution with LLM."""
        return self._resolve_conflict(node_id)

    def _resolve_conflict(self, node_id: str) -> Tuple[bool, str]:
        """
        Extract conflict markers and attempt resolution.
        If no LLM manager is available, abort the cherry-pick.
        """
        # Extract conflicted files
        code, status, _ = self._run_git("diff", "--name-only", "--diff-filter=U")
        conflicted_files = [f for f in status.split("\n") if f.strip()]

        if not conflicted_files:
            self._run_git("cherry-pick", "--abort")
            self.remove_worktree(node_id)
            return False, "Conflict detected but no conflicted files found."

        # Read conflict markers
        conflict_content = []
        for cf in conflicted_files[:3]:  # Limit to 3 files
            try:
                fp = Path(self._repo) / cf
                if fp.exists():
                    content = fp.read_text(encoding="utf-8", errors="replace")
                    # Extract only the conflicted sections
                    lines = []
                    in_conflict = False
                    for line in content.split("\n"):
                        if line.startswith("<<<<<<<"):
                            in_conflict = True
                        if in_conflict:
                            lines.append(line)
                        if line.startswith(">>>>>>>"):
                            in_conflict = False
                    conflict_content.append(f"FILE: {cf}\n" + "\n".join(lines[:50]))
            except Exception:
                pass

        if not conflict_content and not self._manager:
            self._run_git("cherry-pick", "--abort")
            self.remove_worktree(node_id)
            return False, f"Merge conflict on {conflicted_files}. No LLM available for resolution."

        if self._manager:
            # Route to LLM for resolution
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                # If we're already in an async context, we can't use asyncio.run
                # The caller should use resolve_conflict_async instead
                self._run_git("cherry-pick", "--abort")
                self.remove_worktree(node_id)
                return False, f"Merge conflict on {conflicted_files}. LLM resolution requires async context."
            except RuntimeError:
                resolved = asyncio.run(self._llm_resolve(conflict_content, conflicted_files))
                if resolved:
                    self._run_git("add", "-A")
                    self._run_git("commit", "-m", f"V20: resolved conflict for [{node_id}]")
                    self.remove_worktree(node_id)
                    return True, "Conflict resolved by LLM."

        self._run_git("cherry-pick", "--abort")
        self.remove_worktree(node_id)
        return False, f"Failed to resolve merge conflict on: {', '.join(conflicted_files)}"

    async def _llm_resolve(self, conflict_blocks: list, files: list) -> bool:
        """Ask the Local Manager to resolve the conflict."""
        import json

        prompt = (
            "You are a merge conflict resolver. The following files have Git conflicts. "
            "For each file, output the RESOLVED content that combines both sides correctly.\n"
            'Output strict JSON: {"resolutions": [{"file": "path", "content": "resolved code"}]}'
        )

        user_prompt = "\n\n".join(conflict_blocks[:3])

        try:
            raw = await self._manager.ask_local_model(
                system_prompt=prompt,
                user_prompt=user_prompt[:3000],
                temperature=0.0,
            )
            data = json.loads(raw)
            resolutions = data.get("resolutions", [])

            for res in resolutions:
                fp = Path(self._repo) / res["file"]
                if fp.exists():
                    fp.write_text(res["content"], encoding="utf-8")

            return bool(resolutions)
        except Exception as exc:
            logger.error("LLM conflict resolution failed: %s", exc)
            return False
