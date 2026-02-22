"""
git_manager.py — Git Operations.

Handles git init, add, commit, push using the user's configured
git account on their machine. Can create new repos or continue
with existing ones.

Uses subprocess for reliability (not terminal automation) since
git commands don't need the IDE's terminal UI.
"""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("supervisor.git_manager")


def _run_git(args: list[str], cwd: str, timeout: int = 30) -> tuple[bool, str]:
    """
    Run a git command and return (success, output).
    """
    cmd = ["git"] + args
    logger.info("Running: git %s (cwd=%s)", " ".join(args), cwd)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            shell=config.IS_WINDOWS,
        )

        output = (result.stdout + result.stderr).strip()

        if result.returncode == 0:
            logger.info("✅  git %s succeeded: %s", args[0], output[:200])
            return True, output
        else:
            logger.warning("git %s failed (code %d): %s", args[0], result.returncode, output[:300])
            return False, output

    except subprocess.TimeoutExpired:
        logger.error("git %s timed out.", args[0])
        return False, "timeout"
    except FileNotFoundError:
        logger.error("git not found. Is git installed and on PATH?")
        return False, "git not found"
    except Exception as exc:
        logger.error("git error: %s", exc)
        return False, str(exc)


# ─────────────────────────────────────────────────────────────
# High-Level Git Operations
# ─────────────────────────────────────────────────────────────

def is_git_repo(project_path: str) -> bool:
    """Check if the project path is already a git repository."""
    git_dir = Path(project_path) / ".git"
    return git_dir.is_dir()


def git_init(project_path: str) -> bool:
    """Initialize a new git repository."""
    if is_git_repo(project_path):
        logger.info("Already a git repo: %s", project_path)
        return True

    success, _ = _run_git(["init"], cwd=project_path)
    return success


def git_add_all(project_path: str) -> bool:
    """Stage all changes."""
    success, _ = _run_git(["add", "."], cwd=project_path)
    return success


def git_commit(project_path: str, message: str) -> bool:
    """Commit staged changes."""
    # First check if there are changes to commit.
    success, output = _run_git(["status", "--porcelain"], cwd=project_path)
    if success and not output.strip():
        logger.info("Nothing to commit.")
        return True

    success, _ = _run_git(["commit", "-m", message], cwd=project_path)
    return success


def git_push(project_path: str, remote: str = "origin", branch: str = "main") -> bool:
    """Push to remote."""
    success, _ = _run_git(["push", remote, branch], cwd=project_path, timeout=60)
    if not success:
        # Try push with -u flag to set upstream.
        success, _ = _run_git(["push", "-u", remote, branch], cwd=project_path, timeout=60)
    return success


def git_set_remote(project_path: str, remote_url: str, name: str = "origin") -> bool:
    """Set or update the remote URL."""
    # Check if remote already exists.
    success, output = _run_git(["remote", "-v"], cwd=project_path)
    if success and name in output:
        # Update existing remote.
        success, _ = _run_git(["remote", "set-url", name, remote_url], cwd=project_path)
    else:
        # Add new remote.
        success, _ = _run_git(["remote", "add", name, remote_url], cwd=project_path)
    return success


def git_get_user_info() -> dict[str, str]:
    """Get the user's configured git name and email."""
    info = {}
    for key in ("user.name", "user.email"):
        success, output = _run_git(["config", "--global", key], cwd=".")
        if success and output.strip():
            info[key] = output.strip()
    return info


def git_status(project_path: str) -> str:
    """Get git status output."""
    success, output = _run_git(["status", "--short"], cwd=project_path)
    return output if success else ""


def git_log(project_path: str, n: int = 5) -> str:
    """Get recent git log."""
    success, output = _run_git(
        ["log", f"-{n}", "--oneline", "--graph"],
        cwd=project_path,
    )
    return output if success else ""


# ─────────────────────────────────────────────────────────────
# GitHub CLI Integration (if available)
# ─────────────────────────────────────────────────────────────

def _has_gh_cli() -> bool:
    """Check if GitHub CLI (gh) is available."""
    import shutil
    return shutil.which("gh") is not None


def create_github_repo(project_path: str, repo_name: str, private: bool = True) -> bool:
    """
    Create a new GitHub repository using the gh CLI.
    Requires gh to be installed and authenticated.
    """
    if not _has_gh_cli():
        logger.warning("GitHub CLI (gh) not found. Cannot create repo automatically.")
        return False

    visibility = "--private" if private else "--public"
    try:
        result = subprocess.run(
            ["gh", "repo", "create", repo_name, visibility, "--source", project_path, "--push"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_path,
            shell=config.IS_WINDOWS,
        )

        if result.returncode == 0:
            logger.info("✅  GitHub repo created: %s", repo_name)
            return True
        else:
            logger.error("GitHub repo creation failed: %s", result.stderr[:300])
            return False

    except Exception as exc:
        logger.error("GitHub CLI error: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────
# Convenience: Full git workflow
# ─────────────────────────────────────────────────────────────

def full_git_flow(
    project_path: str,
    commit_message: str = "Auto-commit by Supervisor AI",
    remote_url: Optional[str] = None,
    create_repo: bool = False,
    repo_name: Optional[str] = None,
) -> bool:
    """
    Run the full git workflow: init → add → commit → push.
    Optionally creates a GitHub repo if gh CLI is available.
    """
    # Step 1: Init.
    if not git_init(project_path):
        return False

    # Step 2: Set remote if provided.
    if remote_url:
        git_set_remote(project_path, remote_url)
    elif create_repo and repo_name and _has_gh_cli():
        create_github_repo(project_path, repo_name)

    # Step 3: Add all.
    if not git_add_all(project_path):
        return False

    # Step 4: Commit.
    if not git_commit(project_path, commit_message):
        return False

    # Step 5: Push (only if remote is configured).
    success, output = _run_git(["remote", "-v"], cwd=project_path)
    if success and output.strip():
        return git_push(project_path)
    else:
        logger.info("No remote configured — skipping push.")
        return True
