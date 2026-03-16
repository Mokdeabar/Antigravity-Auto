"""
ts_regression_guard.py — TypeScript Regression Guard

Prevents fixed TypeScript errors from being re-broken by later tasks.

Architecture:
  1. capture_ts_errors(project_path)  – run tsc on the HOST, parse errors
  2. check_regressions(pre, post)     – find NEW errors not in pre-state
  3. build_regression_contract(pre)   – build a prompt block for Gemini
  4. load/save_baseline()             – persist across task boundaries

All functions are synchronous (tsc runs in a subprocess via asyncio).
The module has zero dependencies beyond the standard library.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("supervisor.ts_regression_guard")

# ── Persistent baseline file ──────────────────────────────────────────────────
_BASELINE_FILENAME = "ts_error_baseline.json"

# ── tsc output line regex ─────────────────────────────────────────────────────
# Matches: "src/foo/bar.ts(42,7): error TS2552: Cannot find name 'X'"
# Also:    "src/foo/bar.ts:42:7 - error TS2552: Cannot find name 'X'"  (newer tsc)
_TSC_ERR_RE = re.compile(
    r"^(?P<file>[^(\n]+?)"          # filename
    r"[\(:]"                         # separator ( or :
    r"(?P<line>\d+)"                 # line number
    r"[,:]"                          # , or :
    r"\d+"                           # column (ignored)
    r"[)\s]*"                        # ) or space
    r".*?error\s+(?P<code>TS\d+)"   # error code
    r"[:\s]+(?P<msg>.*?)$",         # message
    re.MULTILINE,
)

# Error fingerprint = stable string not dependent on message wording
def _fp(file: str, line: str, code: str) -> str:
    rel = file.replace("\\", "/").lstrip("/")
    return f"{rel}:{line}:{code}"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def capture_ts_errors(project_path: str) -> dict[str, list[str]]:
    """
    Run `npx tsc --noEmit` in project_path and return a dict mapping
    file paths → list of error fingerprints ("TS2552:L42").

    Returns an empty dict if tsc isn't available or tsconfig.json doesn't exist.
    Runs in a subprocess via asyncio so it doesn't block the event loop.
    """
    proj = Path(project_path)
    if not (proj / "tsconfig.json").exists():
        return {}

    try:
        proc = await asyncio.create_subprocess_exec(
            "npx", "tsc", "--noEmit", "--pretty", "false",
            cwd=str(proj),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "CI": "1"},
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            logger.debug("[TSGuard] tsc timed out after 90s")
            return {}

        output = stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        logger.debug("[TSGuard] tsc/npx not found — skipping")
        return {}
    except Exception as exc:
        logger.debug("[TSGuard] tsc capture error: %s", exc)
        return {}

    result: dict[str, list[str]] = {}
    for m in _TSC_ERR_RE.finditer(output):
        file = m.group("file").strip()
        line = m.group("line")
        code = m.group("code")
        key = _fp(file, line, code)
        result.setdefault(file, []).append(key)

    logger.info(
        "[TSGuard] Captured %d errors across %d files.",
        sum(len(v) for v in result.values()), len(result),
    )
    return result


def check_regressions(
    pre: dict[str, list[str]],
    post: dict[str, list[str]],
) -> list[str]:
    """
    Return a list of NEW error fingerprints that appear in post but not pre.

    A regression = any error (file, line, code) present in post but absent
    from the pre-task snapshot.  Pre-existing errors are never regressions.
    """
    pre_fps: set[str] = set()
    for fps in pre.values():
        pre_fps.update(fps)

    regressions: list[str] = []
    for file, fps in post.items():
        for fp in fps:
            if fp not in pre_fps:
                regressions.append(fp)

    if regressions:
        logger.warning(
            "[TSGuard] %d regression(s) detected: %s",
            len(regressions), ", ".join(regressions[:5]),
        )
    return regressions


def build_regression_contract(pre: dict[str, list[str]]) -> str:
    """
    Build a prompt block for Gemini listing:
      - Files currently clean (no TS errors) — must not be touched carelessly
      - Files with known pre-existing errors — do not make them worse

    Returns empty string if pre is empty (no tsconfig / tsc not available).
    """
    if not pre and pre is not None:
        # pre is {} — tsc ran, zero errors, all clean
        return (
            "\n══════════════════════════════════════════════════════════\n"
            "TYPESCRIPT REGRESSION CONTRACT:\n"
            "══════════════════════════════════════════════════════════\n"
            "✅ TypeScript is currently ERROR-FREE (0 errors).\n"
            "Your task MUST NOT introduce any new TypeScript errors.\n"
            "After your changes, `npx tsc --noEmit` must still pass.\n"
        )

    if pre is None:
        # tsc not available / no tsconfig — skip contract
        return ""

    dirty_files = {f: fps for f, fps in pre.items() if fps}
    # All files that exist in the project but aren't in dirty_files are clean
    # (we can't enumerate all files here — just mention the dirty ones)

    lines = [
        "\n══════════════════════════════════════════════════════════",
        "TYPESCRIPT REGRESSION CONTRACT:",
        "══════════════════════════════════════════════════════════",
        "The following files have PRE-EXISTING TypeScript errors.",
        "You may fix them, but MUST NOT introduce any NEW errors into",
        "files that are currently clean or make the error count worse.\n",
    ]

    if dirty_files:
        lines.append("Files with known pre-existing errors (do not worsen):")
        for file, fps in sorted(dirty_files.items()):
            codes = ", ".join(fp.split(":")[-1] for fp in fps[:5])
            lines.append(f"  ⚠  {file} — {len(fps)} error(s): {codes}")
    else:
        lines.append("✅ All tracked files are currently error-free.")

    lines.append(
        "\n🚨 RULE: After your task, `npx tsc --noEmit` must have the SAME OR FEWER "
        "errors than before. Any new errors = task will be rejected and retried."
    )
    return "\n".join(lines) + "\n"


def build_microfix_description(
    regressions: list[str],
    parent_task_id: str = "",
) -> str:
    """
    Build a targeted micro-fix task description for injection into the DAG.

    Instead of rejecting/retrying the original task (which wastes the full
    quota it already consumed), the supervisor accepts the original task and
    injects this tiny follow-up that fixes ONLY the new regressions.
    A micro-fix is typically 30-90s vs 5+ minutes for the original task.
    """
    parent_ref = f" (introduced by {parent_task_id})" if parent_task_id else ""
    lines = [
        f"[FUNC] Fix TypeScript regression errors introduced by a previous task{parent_ref}. "
        f"Run `npx tsc --noEmit` to confirm the errors, then fix ONLY these specific "
        f"regressions — do not change any other logic:\n"
    ]
    for r in regressions[:20]:
        parts = r.rsplit(":", 2)  # file:line:TScode
        file = parts[0] if parts else r
        line = parts[1] if len(parts) > 1 else "?"
        code = parts[2] if len(parts) > 2 else "?"
        lines.append(f"  • {file} line {line}: {code}")
    lines.append(
        "\nAfter fixing, verify with `npx tsc --noEmit` — it must report the SAME OR FEWER "
        "errors than before these regressions were introduced."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Baseline persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_baseline(project_path: str) -> dict[str, list[str]] | None:
    """
    Load the persisted TS error baseline from .ag-supervisor/ts_error_baseline.json.
    Returns None if no baseline exists yet.
    """
    path = Path(project_path) / ".ag-supervisor" / _BASELINE_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("errors", {})
    except Exception as exc:
        logger.debug("[TSGuard] Could not load baseline: %s", exc)
        return None


def save_baseline(project_path: str, errors: dict[str, list[str]]) -> None:
    """Persist the current TS error state as the new baseline."""
    path = Path(project_path) / ".ag-supervisor" / _BASELINE_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"errors": errors, "saved_at": time.time()}, indent=2),
            encoding="utf-8",
        )
        total = sum(len(v) for v in errors.values())
        logger.info("[TSGuard] Saved baseline: %d errors across %d files.", total, len(errors))
    except Exception as exc:
        logger.debug("[TSGuard] Could not save baseline: %s", exc)


def update_baseline_after_task(
    project_path: str,
    pre: dict[str, list[str]],
    post: dict[str, list[str]],
) -> None:
    """
    After a successfully accepted task, update the baseline to post-task state
    (which has the same or fewer errors than pre).
    """
    save_baseline(project_path, post)
