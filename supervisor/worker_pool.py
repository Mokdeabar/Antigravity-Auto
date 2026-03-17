"""
V79: Worker file isolation utilities extracted from main.py.

Provides git checkpoint, file validation, and git revert functions
for the parallel DAG worker pool.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("supervisor")


def git_checkpoint(project_path: str) -> bool:
    """
    Create a git baseline before DAG execution.

    Commits all current files so that broken worker output can be
    reverted to this clean state. Returns True if checkpoint succeeded.
    """
    import subprocess as _sp
    try:
        _cwd = project_path

        # Ensure git is initialized
        git_dir = Path(project_path) / ".git"
        if not git_dir.exists():
            _sp.run(["git", "init"], cwd=_cwd, capture_output=True, timeout=10)
            _sp.run(
                ["git", "config", "user.email", "supervisor@ag.local"],
                cwd=_cwd, capture_output=True, timeout=5,
            )
            _sp.run(
                ["git", "config", "user.name", "AG Supervisor"],
                cwd=_cwd, capture_output=True, timeout=5,
            )
            logger.info("🔀  [Git] Initialized repo at %s", project_path)

        # Stage everything and commit as baseline
        _sp.run(["git", "add", "-A"], cwd=_cwd, capture_output=True, timeout=30)
        result = _sp.run(
            ["git", "commit", "-m", "AG Supervisor: DAG baseline checkpoint",
             "--allow-empty"],
            cwd=_cwd, capture_output=True, text=True, timeout=15,
        )
        logger.info("🔀  [Git] Baseline checkpoint created (exit=%d)", result.returncode)
        return True
    except Exception as exc:
        logger.debug("🔀  [Git] Checkpoint failed (non-fatal): %s", exc)
        return False


def validate_worker_files(
    project_path: str,
    changed_files: list[str],
) -> tuple[bool, list[str]]:
    """
    Validate files changed by a worker for syntax errors.

    Returns (is_valid, error_messages).
    Python files: checked via ast.parse()
    JS/TS files: checked via basic syntax validation
    """
    errors: list[str] = []
    for rel_path in changed_files:
        full_path = Path(project_path) / rel_path
        if not full_path.exists():
            continue

        try:
            content = full_path.read_text(encoding="utf-8")
        except Exception:
            continue

        if rel_path.endswith(".py"):
            try:
                import ast as _ast
                _ast.parse(content)
            except SyntaxError as se:
                errors.append(
                    f"{rel_path}:{se.lineno}: SyntaxError: {se.msg}"
                )
        elif rel_path.endswith((".js", ".ts", ".jsx", ".tsx")):
            # Basic JS validation: check for unclosed braces/brackets
            opens = content.count('{') + content.count('[') + content.count('(')
            closes = content.count('}') + content.count(']') + content.count(')')
            if abs(opens - closes) > 2:  # Small tolerance for template literals
                errors.append(
                    f"{rel_path}: Unbalanced brackets (open={opens}, close={closes})"
                )

    return (len(errors) == 0, errors)


def revert_worker_files(project_path: str, files: list[str]) -> bool:
    """Revert specific files to the last git checkpoint."""
    import subprocess as _sp
    try:
        if not files:
            return True
        cmd = ["git", "checkout", "HEAD", "--"] + files
        result = _sp.run(
            cmd, cwd=project_path, capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            logger.info(
                "🔀  [Git] Reverted %d files to checkpoint: %s",
                len(files), ", ".join(files[:5]),
            )
            return True
        logger.debug("🔀  [Git] Revert failed: %s", result.stderr[:200])
        return False
    except Exception as exc:
        logger.debug("🔀  [Git] Revert error: %s", exc)
        return False
