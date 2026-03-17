"""
main.py — The Orchestrator (V65 Command Centre).

Entry point for the Supervisor AI. V44 adds the Command Centre — a thin-client
web UI that communicates with this engine via FastAPI/WebSocket. The UI is
strictly a passive observer. Closing the browser tab has ZERO impact on execution.

Core systems:

  1. LOCKFILE MEMORY (Anti-Amnesia)
  2. AUTO-RECOVERY ENGINE (Exponential backoff + strategy rotation)
  3. SANDBOX EXECUTION LAYER (Docker containers → file/shell/LSP)
  4. DUAL-BRAIN ARCHITECTURE (Ollama local LLM + Gemini CLI)
  5. SMART WORKSPACE MOUNTING (bind-mount vs. copy-in/copy-out)
  6. GLOBAL TIMEOUTS (180-second safety net)
  7. AGENT COUNCIL (Multi-agent consensus for complex decisions)
  8. BACKGROUND MODE: No input() anywhere. Auto-recover, auto-restart,
     auto-resume. Runs unattended while user works.

Removed in V8 (formerly GUI-dependent):
  - Playwright / CDP connection
  - DOM injection / approval sniper / frame walker
  - Navigation defense / vision system / screenshot analysis
  - Command palette engine / command resolver
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# ── Ensure Windows System32 is in PATH ──
if sys.platform == "win32":
    _sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if _sys32.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = f"{_sys32}{os.pathsep}{os.environ.get('PATH', '')}"

from . import config
from . import bootstrap
# V40: self_evolve import REMOVED — supervisor does not modify itself.\n# from .self_evolver import self_evolve
from .gemini_advisor import (
    ask_gemini,
    ask_gemini_sync,
    call_gemini_with_file,
    call_gemini_with_file_json,
    set_gemini_status_callback,
)
from .agent_council import AgentCouncil, Issue as CouncilIssue
from .sandbox_manager import SandboxManager, SandboxError, DockerNotAvailableError
from .tool_server import ToolServer
from .headless_executor import HeadlessExecutor, OllamaLocalBrain

logger = logging.getLogger("supervisor")

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────
_SUPERVISOR_DIR = Path(__file__).resolve().parent
EXPERIMENTS_DIR = Path(r"c:\Users\mokde\Desktop\Experiments")

# Session state file — persists goal + project_path across reboots.
_SESSION_STATE_PATH = _SUPERVISOR_DIR / "_session_state.json"

# Log file for Infinite Memory.
_LOG_FILE = _SUPERVISOR_DIR / "supervisor.log"


# ─────────────────────────────────────────────────────────────
# Alert sound (human escalation)
# ─────────────────────────────────────────────────────────────

def _play_alert() -> None:
    """Play an audible alert to summon a human."""
    if platform.system() == "Windows":
        try:
            import winsound
            for _ in range(config.ALERT_REPEAT):
                winsound.Beep(1000, 400)
                time.sleep(0.2)
            return
        except Exception:
            pass
    # Fallback: terminal bell.
    for _ in range(config.ALERT_REPEAT):
        print("\a", end="", flush=True)
        time.sleep(0.3)


# ─────────────────────────────────────────────────────────────
# Auto-Recovery Engine (retained from V7.2)
# ─────────────────────────────────────────────────────────────

class AutoRecoveryEngine:
    """
    V8 Recovery Engine — adapted for headless sandbox architecture.

    Strategy rotation:
      1. RESTART_SANDBOX — destroy + recreate the Docker container
      2. SWITCH_MOUNT — switch from bind to copy mount mode (or vice versa)
      3. REBUILD_IMAGE — force-rebuild the sandbox Docker image
      4. EVOLVE — trigger self-evolution via Gemini
    """

    STRATEGIES = ["RESTART_SANDBOX", "SWITCH_MOUNT", "REBUILD_IMAGE", "EVOLVE"]
    MIN_BACKOFF = 10
    MAX_BACKOFF = 300

    def __init__(self):
        self._consecutive_failures = 0
        self._total_failures = 0
        self._backoff = self.MIN_BACKOFF
        self._strategy_index = 0
        self._crash_log: list[dict] = []

    @property
    def current_strategy(self) -> str:
        idx = min(self._strategy_index, len(self.STRATEGIES) - 1)
        return self.STRATEGIES[idx]

    def record_success(self) -> None:
        """Call after a successful monitoring loop iteration."""
        if self._consecutive_failures > 0:
            logger.info(
                "💚  Recovery engine: health restored after %d consecutive failures.",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._strategy_index = 0
        self._backoff = max(self.MIN_BACKOFF, self._backoff // 2)

    def recover(self, error_context: str = "") -> str:
        """
        Called when the supervisor encounters a failure.
        Returns the recommended strategy string.
        """
        self._consecutive_failures += 1
        self._total_failures += 1

        strategy = self.current_strategy

        crash_entry = {
            "time": datetime.now().isoformat(),
            "failure_num": self._consecutive_failures,
            "total": self._total_failures,
            "strategy": strategy,
            "backoff": self._backoff,
            "context": error_context[:200],
        }
        self._crash_log.append(crash_entry)
        if len(self._crash_log) > 50:
            self._crash_log = self._crash_log[-50:]

        M = config.ANSI_MAGENTA
        B = config.ANSI_BOLD
        Y = config.ANSI_YELLOW
        R = config.ANSI_RESET

        logger.warning(
            "🔄  Recovery Engine [failure #%d | lifetime #%d | strategy: %s | backoff: %ds]",
            self._consecutive_failures, self._total_failures, strategy, self._backoff,
        )
        print(f"\n  {B}{M}{'═' * 56}{R}")
        print(f"  {B}{M}  🔄 AUTO-RECOVERY ENGINE V8 (Headless){R}")
        print(f"  {M}  Failure:  #{self._consecutive_failures} (lifetime: #{self._total_failures}){R}")
        print(f"  {M}  Strategy: {strategy}{R}")
        print(f"  {M}  Backoff:  {self._backoff}s{R}")
        if error_context:
            print(f"  {Y}  Context:  {error_context[:80]}{R}")
        print(f"  {B}{M}{'═' * 56}{R}\n")

        # Sleep with backoff
        time.sleep(self._backoff)

        # Escalate
        self._backoff = min(self._backoff * 2, self.MAX_BACKOFF)
        self._strategy_index += 1

        if self._strategy_index >= len(self.STRATEGIES):
            logger.critical(
                "🧬  Recovery Engine: ALL strategies exhausted. Forcing evolution reboot."
            )
            print(f"  {B}{config.ANSI_RED}🧬 All recovery strategies exhausted. Forcing reboot...{R}")
            sys.exit(42)

        return strategy

    def get_crash_log(self) -> list[dict]:
        """Return crash forensics for debugging or Gemini context."""
        return list(self._crash_log)


# Global recovery engine instance
_recovery_engine = AutoRecoveryEngine()


# ─────────────────────────────────────────────────────────────
# Lockfile Memory (Anti-Amnesia)
# ─────────────────────────────────────────────────────────────

def _lockfile_exists(project_path: str) -> bool:
    """Check if .supervisor_lock exists AND its PID is still alive."""
    lock_path = Path(project_path) / config.LOCKFILE_NAME
    if not lock_path.exists():
        return False

    try:
        content = lock_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("pid="):
                pid = int(line.split("=", 1)[1].strip())
                try:
                    os.kill(pid, 0)
                    logger.debug("🔐  Lockfile PID %d is alive.", pid)
                    return True
                except (OSError, ProcessLookupError):
                    logger.warning(
                        "🔐  STALE LOCKFILE: PID %d is dead. Auto-removing.", pid
                    )
                    try:
                        lock_path.unlink()
                    except Exception:
                        pass
                    return False
    except Exception as exc:
        logger.debug("🔐  Could not verify lockfile PID: %s", exc)

    return True


def _create_lockfile(project_path: str) -> None:
    """Create the .supervisor_lock file with PID."""
    lock_path = Path(project_path) / config.LOCKFILE_NAME
    lock_path.write_text(
        f"pid={os.getpid()}\nlocked={datetime.now().isoformat()}\n",
        encoding="utf-8",
    )
    logger.info("🔐  Created lockfile: %s (pid=%d)", lock_path, os.getpid())
    print(f"  🔐 Lockfile created: {lock_path} (pid={os.getpid()})")


def _remove_lockfile(project_path: str) -> None:
    """Remove lockfile on graceful exit."""
    lock_path = Path(project_path) / config.LOCKFILE_NAME
    try:
        if lock_path.exists():
            lock_path.unlink()
            logger.info("🔓  Removed lockfile: %s", lock_path)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Preview Port Persistence (V41)
# ─────────────────────────────────────────────────────────────

_PREVIEW_PORT_FILE = "_preview_port.json"
# V55 Fix #5: Track last saved port — skip redundant disk writes
_last_saved_preview_port: int = 0


def _save_preview_port(project_path: str, host_port: int, container_port: int = 3000) -> None:
    """Save the active preview port mapping to disk for crash recovery."""
    global _last_saved_preview_port
    # V55 Fix #5: Skip write when the port has not changed — the monitoring
    # loop can trigger this several times per task cycle for the same value.
    if host_port == _last_saved_preview_port:
        return
    _last_saved_preview_port = host_port
    try:
        ag_dir = Path(project_path) / ".ag-supervisor"
        ag_dir.mkdir(parents=True, exist_ok=True)
        port_file = ag_dir / _PREVIEW_PORT_FILE
        port_file.write_text(json.dumps({
            "host_port": host_port,
            "container_port": container_port,
            "pid": os.getpid(),
            "timestamp": time.time(),
        }), encoding="utf-8")
        logger.info("🔌  [Port] Saved preview port %d → %s", host_port, port_file)
    except Exception as exc:
        logger.debug("🔌  [Port] Failed to save preview port: %s", exc)


def _release_stale_preview(project_path: str) -> None:
    """On startup, release any previously-saved preview ports.

    Kills the process bound to the saved host port (if any) so Docker
    can re-bind it cleanly. Also deletes the stale port file.
    """
    try:
        port_file = Path(project_path) / ".ag-supervisor" / _PREVIEW_PORT_FILE
        if not port_file.exists():
            return
        data = json.loads(port_file.read_text(encoding="utf-8"))
        host_port = data.get("host_port")
        stale_pid = data.get("pid")
        if not host_port:
            port_file.unlink(missing_ok=True)
            return

        # Don't kill ourselves
        if stale_pid and stale_pid == os.getpid():
            return

        logger.info("🔌  [Port] Releasing stale preview port %d (from pid %s) …", host_port, stale_pid)

        # Step 1: Cleanly remove any orphaned Docker container holding this port
        import subprocess as _sp
        try:
            # Find any container publishing this exact port
            res = _sp.run(
                ["docker", "ps", "-q", "--filter", f"publish={host_port}"],
                capture_output=True, text=True, timeout=5
            )
            docker_cid = res.stdout.strip()
            if docker_cid:
                logger.info("🔌  [Port] Removing orphaned container %s holding port %d", docker_cid[:8], host_port)
                _sp.run(["docker", "rm", "-f", docker_cid], capture_output=True, timeout=5)
                time.sleep(1)  # Give OS networking stack a moment to release
        except Exception as _e:
            logger.debug("🔌  [Port] Docker port release check failed: %s", _e)

        # Step 2: Platform-specific process release (only if port is STILL bound by a host process)
        if os.name == "nt":
            # Windows: find PID on port, then kill, safely ignoring Docker Desktop backend processes
            try:
                result = _sp.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.splitlines():
                    if f":{host_port}" in line and "LISTENING" in line:
                        parts = line.split()
                        pid = int(parts[-1])
                        if pid > 0 and pid != os.getpid():
                            # Protect Docker Desktop backend processes from being murdered
                            task_res = _sp.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
                            if "docker" in task_res.stdout.lower() or "wsl" in task_res.stdout.lower() or "vpnkit" in task_res.stdout.lower():
                                logger.debug("🔌  [Port] Port %d held by Docker/WSL proxy (PID %d). Skipping taskkill.", host_port, pid)
                                continue

                            _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
                            logger.info("🔌  [Port] Killed native PID %d on port %d", pid, host_port)
            except Exception as _e:
                logger.debug("🔌  [Port] Windows native port release failed: %s", _e)
        else:
            # Unix: lsof + kill
            try:
                result = _sp.run(
                    ["lsof", "-ti", f":{host_port}"],
                    capture_output=True, text=True, timeout=5,
                )
                for pid_str in result.stdout.strip().split():
                    pid = int(pid_str)
                    if pid > 0 and pid != os.getpid():
                        # Protect Docker Desktop backend processes on Mac/Linux
                        try:
                            ps_res = _sp.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True)
                            pname = ps_res.stdout.lower()
                            if "docker" in pname or "vpnkit" in pname or "com.docker" in pname:
                                logger.debug("🔌  [Port] Port %d held by Docker proxy (PID %d). Skipping kill -9.", host_port, pid)
                                continue
                        except Exception:
                            pass

                        os.kill(pid, 9)
                        logger.info("🔌  [Port] Killed native PID %d on port %d", pid, host_port)
            except Exception as _e:
                logger.debug("🔌  [Port] Unix port release failed: %s", _e)

        port_file.unlink(missing_ok=True)
        logger.info("🔌  [Port] Stale preview port released.")
    except Exception as exc:
        logger.debug("🔌  [Port] Release failed: %s", exc)


def _clear_preview_port(project_path: str) -> None:
    """On shutdown, remove the preview port file."""
    try:
        port_file = Path(project_path) / ".ag-supervisor" / _PREVIEW_PORT_FILE
        if port_file.exists():
            port_file.unlink(missing_ok=True)
            logger.info("🔌  [Port] Cleared preview port file.")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Worker Isolation via Git Checkpoint (V41)
# ─────────────────────────────────────────────────────────────

def _git_checkpoint(project_path: str) -> bool:
    """V79: Proxy → supervisor.worker_pool.git_checkpoint"""
    from .worker_pool import git_checkpoint
    return git_checkpoint(project_path)


def _validate_worker_files(
    project_path: str,
    changed_files: list[str],
) -> tuple[bool, list[str]]:
    """V79: Proxy → supervisor.worker_pool.validate_worker_files"""
    from .worker_pool import validate_worker_files
    return validate_worker_files(project_path, changed_files)


def _revert_worker_files(project_path: str, files: list[str]) -> bool:
    """V79: Proxy → supervisor.worker_pool.revert_worker_files"""
    from .worker_pool import revert_worker_files
    return revert_worker_files(project_path, files)


# ─────────────────────────────────────────────────────────────
# V59: Pre-DAG Deep Analysis (fire-and-forget background pass)
# ─────────────────────────────────────────────────────────────

async def _run_deep_analysis(
    goal: str,
    project_path: str,
    state=None,
) -> None:
    """
    V59: Fire-and-forget deep analysis pass.

    Runs in parallel with DAG workers immediately after session start.
    Sends the goal + lightweight project context to Gemini with a structured
    prompt asking it to reason about risks, architectural concerns, likely
    failure modes, and quality gaps BEFORE code is written.

    Output is written to .ag-supervisor/DEEP_ANALYSIS.md and consumed by
    _audit_completed_work() as additional context — giving the audit a
    pre-thought north star that workers didn't have access to.

    Uses model=auto so the CLI routes to the highest-capability model
    (Pro with extended thinking for complex goals). Non-blocking: all
    failures are swallowed so workers are never affected.
    """
    import asyncio as _asyncio_da
    try:
        from pathlib import Path as _Path
        from .gemini_advisor import ask_gemini as _ask

        _proj = _Path(project_path)
        _out_path = _proj / ".ag-supervisor" / "DEEP_ANALYSIS.md"

        # V73: Use @file references instead of inlining — Gemini CLI reads these
        # files natively via its own tools, keeping the prompt lean.
        _at_refs = []
        for _fname in ["PROJECT_STATE.md", "VISION.md", "SUPERVISOR_MANDATE.md"]:
            _fp = _proj / _fname
            if _fp.exists():
                _at_refs.append(f"@{_fname}")
        _at_refs_str = " ".join(_at_refs)

        # V75: Inject Tier 1 file index for structural awareness
        _structure_ctx = ""
        try:
            from .file_index import get_file_index
            _fidx = get_file_index(project_path)
            _structure_ctx = _fidx.get_tier1_context()
        except Exception:
            pass

        _prompt = (
            (_at_refs_str + "\n\n" if _at_refs_str else "")
            + (_structure_ctx + "\n\n" if _structure_ctx else "")
            + "You are a world-class senior engineer conducting a pre-build deep analysis.\n"
            "Think step by step about the following project goal and any available context.\n"
            "The project context files listed above (if any) have been loaded into your context.\n\n"
            f"## GOAL\n{goal}\n\n"
            "Produce a structured DEEP_ANALYSIS.md with the following sections:\n\n"
            "### 1. ARCHITECTURAL RISKS\n"
            "What could go wrong at a structural/design level? What patterns should be avoided?\n\n"
            "### 2. LIKELY FAILURE MODES\n"
            "Based on the goal and stack, where are bugs most likely to occur? "
            "(e.g. race conditions, type mismatches, missing error boundaries, broken async flows)\n\n"
            "### 3. DEPENDENCY & INTEGRATION CONCERNS\n"
            "Any third-party packages, APIs, or integrations that are known to be tricky? "
            "Version conflicts? Breaking changes to watch for?\n\n"
            "### 4. QUALITY GAPS TO WATCH\n"
            "What quality aspects are most often neglected for this type of project? "
            "(accessibility, mobile layout, empty states, error handling, loading states, SEO)\n\n"
            "### 5. AUDIT CHECKLIST\n"
            "A concise checklist (max 20 items) the post-build audit should verify to consider "
            "this project complete and production-ready.\n\n"
            "Be specific and opinionated. Generic advice is useless — tailor everything to THIS goal."
        )

        logger.info("🔬  [DeepAnalysis] Starting background analysis pass (non-blocking) …")
        if state:
            state.record_activity("system", "🔬 Deep analysis pass started (background)")

        # Small delay so the first real workers get a head-start on the semaphore
        await _asyncio_da.sleep(5)

        _result = await _ask(_prompt, timeout=300, use_cache=False)
        if _result and _result.strip():
            _out_path.parent.mkdir(parents=True, exist_ok=True)
            _out_path.write_text(_result, encoding="utf-8")
            logger.info(
                "🔬  [DeepAnalysis] DEEP_ANALYSIS.md written (%d chars) → will inform audit.",
                len(_result),
            )
            # V60: Refresh GEMINI.md now that DEEP_ANALYSIS.md exists on disk.
            # bootstrap_workspace() reads it and bakes it into the ## Pre-Build Risk Analysis
            # section — all task workers that start AFTER this point will see the findings.
            try:
                from . import bootstrap as _bootstrap
                from . import config as _cfg
                _effective_goal = goal
                _effective_path = project_path
                _bootstrap.bootstrap_workspace(_effective_path, _effective_goal)
                logger.info("🔬  [DeepAnalysis] GEMINI.md refreshed with deep analysis findings.")
            except Exception as _refresh_exc:
                logger.debug("🔬  [DeepAnalysis] GEMINI.md refresh failed (non-fatal): %s", _refresh_exc)
            if state:
                state.record_activity(
                    "system",
                    f"🔬 Deep analysis complete ({len(_result)} chars) — GEMINI.md refreshed for all workers",
                )
        else:
            logger.warning("🔬  [DeepAnalysis] Empty response — DEEP_ANALYSIS.md not written.")
    except Exception as _da_exc:
        logger.debug("🔬  [DeepAnalysis] Background pass failed (non-fatal): %s", _da_exc)


# ─────────────────────────────────────────────────────────────
# Gemini-assisted diagnostics
# ─────────────────────────────────────────────────────────────

def _read_recent_logs(n_lines: int = 40) -> str:
    """Read the last N lines of supervisor.log for diagnostic context."""
    try:
        if _LOG_FILE.exists():
            lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n_lines:])
    except Exception:
        pass
    return "(no log data)"


async def _triage_fatal_error(tb_str: str) -> str | None:
    """Ask Gemini if a fatal error is transient or a code bug."""
    prompt = (
        "You are a DevOps engineer analyzing a supervisor crash.\n\n"
        f"Traceback:\n```\n{tb_str[:2000]}\n```\n\n"
        "Is this transient (retry) or a code bug (needs fix)? Reply concisely."
    )
    logger.info("🩺  [Triage→Gemini] Fatal error triage prompt (%d chars): %.300s…", len(prompt), prompt)
    try:
        result = await ask_gemini(prompt, timeout=180)
        if result:
            logger.info("🩺  [Triage←Gemini] Triage response (%d chars): %.300s…", len(result), result)
        return result
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# V38: Recursive DAG Decomposition & Execution Engine
# V38.1: Parallel lanes, adaptive timeouts, coherence gate
# ─────────────────────────────────────────────────────────────

# Global DAG progress — read by api_server.py via SupervisorState
_dag_progress: dict = {"active": False}


async def _update_dag_progress(planner, depth: int, running: list[str] | None = None, state=None, queued_ids: set | None = None):
    """Update the global DAG progress dict for UI consumption and broadcast.

    queued_ids: task IDs that are in active_tasks but still at status='pending'
    (i.e. submitted to asyncio pool, waiting at the semaphore). Exposed as
    status='queued' so the UI can distinguish them from unscheduled pending tasks.
    """
    global _dag_progress
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
    _dag_progress = {
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



async def _compute_chunk_timeout(local_brain, description: str) -> int:
    """V79: Proxy → supervisor.dag_executor.compute_chunk_timeout"""
    from .dag_executor import compute_chunk_timeout
    return await compute_chunk_timeout(local_brain, description)


async def _execute_single_chunk(
    node,
    planner,
    local_brain,
    executor,
    session_mem,
    effective_project: str,
    depth: int,
    max_depth: int,
    indent: str,
    state=None,
    sandbox=None,
    tools=None,
    project_path: str = "",
    task_intel=None,
):
    """
    Execute a single DAG node. Handles recursive sub-decomposition,
    adaptive timeouts, and context scoping.

    Returns:
        TaskResult for this chunk.
    """
    from .headless_executor import TaskResult

    C = config.ANSI_CYAN
    G = config.ANSI_GREEN
    Y = config.ANSI_YELLOW
    R = config.ANSI_RESET

    threshold = getattr(config, "COMPLEX_TASK_CHAR_THRESHOLD", 2000)

    # ── Multi-layer: recursively sub-decompose if chunk is still complex ──
    if depth < max_depth and len(node.description) > threshold:
        logger.info(
            "📋  [Planner] Node %s is complex (%d chars) — sub-decomposing (depth %d→%d) …",
            node.task_id, len(node.description), depth, depth + 1,
        )
        print(f"{indent}  {Y}↳ Sub-decomposing complex chunk …{R}")
        return await _execute_dag_recursive(
            goal=node.description,
            local_brain=local_brain,
            executor=executor,
            session_mem=session_mem,
            effective_project=effective_project,
            depth=depth + 1,
            max_depth=max_depth,
            state=state,
            sandbox=sandbox,
            tools=tools,
            project_path=project_path,
        )

    # ── Adaptive timeout ──
    chunk_timeout = await _compute_chunk_timeout(local_brain, node.description)

    # ── Build focused prompt with context from completed steps ──
    # V46: Write progress to file and reference it — avoids inlining all
    # completed task descriptions into every prompt (saves tokens).
    planner.write_progress_file(effective_project)
    completed_count = sum(1 for n in planner._nodes.values() if n.status == "complete")
    focused_prompt = node.description
    if completed_count > 0:
        focused_prompt = (
            f"Check @PROGRESS.md for {completed_count} completed prerequisite steps.\n\n"
            f"NOW DO THIS NEXT STEP:\n{node.description}"
        )

    # V77: TaskIntelligence per-task guidance — use the DAG-level instance
    # (threaded from _pool_worker) instead of creating a new one per task.
    try:
        if task_intel:
            _tag = _extract_tag(node.description)
            _retry_guidance = task_intel.get_retry_guidance(_tag)
            # V77: Improved file extraction — regex matches filenames with extensions
            # (e.g. App.tsx, index.css, package.json) not just paths with '/'
            import re as _re_files
            _files_in_desc = _re_files.findall(r'\b[\w./-]+\.(?:tsx?|jsx?|css|scss|html|json|py|md)\b', node.description)
            _file_risk = task_intel.get_file_risk_warnings(_files_in_desc)
            if _retry_guidance or _file_risk:
                focused_prompt = (_retry_guidance + _file_risk + "\n" + focused_prompt)
    except Exception:
        pass  # Non-fatal — proceed without intelligence guidance

    logger.info(
        "💉  [DAG→Gemini] Node %s prompt (%d chars): %s",
        node.task_id, len(focused_prompt), focused_prompt,
    )
    if state:
        state.record_activity(
            "llm_prompt",
            f"Gemini prompt for {node.task_id}",
            focused_prompt[:2000],
        )

    return await executor.execute_task(
        focused_prompt,
        timeout=chunk_timeout,
        task_label=f"{node.task_id}: {node.description}",
    )


# ─────────────────────────────────────────────────────────────
# V51: Build Health Check Pipeline
# ─────────────────────────────────────────────────────────────

async def _auto_dep_install(
    sandbox,
    executor,
    state,
    changed_files: list,
    task_counter: int,
) -> None:
    """
    V53: Auto-install missing / newly required npm dependencies after a task.

    Two triggers:
      1. ALWAYS   — if package.json is in changed_files, run `npm install` immediately.
      2. EVERY 3  — scan all changed .ts/.tsx/.js/.jsx files for import statements,
                    cross-reference against node_modules/, and `npm install` any
                    packages that are missing. This is the higher-overhead path.

    Uses `npm install` (not --force) so it only fetches what's missing and updates
    the lock file without nuking the cache unnecessarily.
    """
    if not sandbox or not sandbox.is_running or not executor:
        return
    if state and getattr(state, 'stop_requested', False):
        return

    try:
        _did_install = False

        # ── Trigger 1: package.json changed ─────────────────────────────────
        if any('package.json' in (f or '') for f in changed_files):
            # V55 Fix #4: Debounce — if we already ran npm install for package.json
            # within the last 5 seconds (parallel tasks both touching package.json),
            # skip. Prevents double npm install from two simultaneous task completions.
            _now_ts = time.time()
            _last_pkg_install = getattr(state, '_last_pkg_install_ts', 0)
            if state and (_now_ts - _last_pkg_install) < 5.0:
                logger.debug("📦  [AutoDep] Debouncing package.json install (%.1fs since last)", _now_ts - _last_pkg_install)
                return
            if state:
                state._last_pkg_install_ts = _now_ts
            logger.info("📦  [AutoDep] package.json changed — clearing Vite cache + running npm install …")
            if state:
                state.record_activity("system", "Auto-installing dependencies (package.json changed)")
            _res = await sandbox.exec_command(
                # Always wipe the Vite optimizer cache before npm install.
                # Vite pre-bundles deps into hashed chunks in node_modules/.vite/.
                # If packages change but the cache isn't cleared, Vite references
                # stale chunk hashes that no longer exist → 'Cannot find module dep-*.js'.
                # V55: Use prefer-offline — package.json-triggered installs usually just
                # version-bump existing deps that are already in the npm cache (~5s vs ~30s).
                "cd /workspace && rm -rf node_modules/.vite .vite node_modules/.cache 2>/dev/null; "
                "npm install --prefer-offline --no-audit --no-fund --loglevel=warn 2>&1 | tail -5",
                timeout=120,
            )
            if _res.exit_code == 0:
                logger.info("📦  [AutoDep] npm install succeeded after package.json change")
            else:
                logger.warning("📦  [AutoDep] npm install had warnings: %s", (_res.stdout or '')[-200:])
            _did_install = True

        # ── Trigger 2: Every 3 tasks — scan imports ───────────────────────────
        if task_counter % 3 == 0 and not _did_install:
            _src_files = [
                f for f in changed_files
                if f and any(f.endswith(ext) for ext in ('.ts', '.tsx', '.js', '.jsx', '.mjs'))
            ]
            if _src_files:
                # Extract bare package names from import/require statements
                _file_list = ' '.join(f'"/workspace/{f}"' for f in _src_files[:20])
                _scan_cmd = (
                    f"grep -hE \"(^|\\s)(import|from|require)\\s+['\\\""
                    f"(@?[a-z0-9_-][a-z0-9_@./-]*)\" {_file_list} 2>/dev/null "
                    "| grep -oE \"['\"](@?[a-zA-Z0-9_-][a-zA-Z0-9_@./-]*)['\"]\" "
                    "| tr -d \"'\" | tr -d '\"' | cut -d'/' -f1,2 "
                    "| grep -vE '^[./]' | sort -u"
                )
                _scan_res = await sandbox.exec_command(_scan_cmd, timeout=10)
                _imported = [p.strip() for p in (_scan_res.stdout or '').splitlines() if p.strip()]

                if _imported:
                    # Check which are missing from node_modules
                    _check_cmd = (
                        "cd /workspace && node -e \""
                        "const pkgs = " + str(_imported).replace("'", '"') + ";"
                        "const missing = pkgs.filter(p => { try { require.resolve(p); return false; } "
                        "catch(e) { return true; } });"
                        "console.log(missing.join(' '));\""
                    )
                    _check_res = await sandbox.exec_command(_check_cmd, timeout=15)
                    _missing = [p for p in (_check_res.stdout or '').split() if p]

                    if _missing:
                        _pkgs = ' '.join(_missing[:20])  # cap at 20 packages
                        logger.info("📦  [AutoDep] Installing %d missing package(s): %s", len(_missing), _pkgs)
                        if state:
                            state.record_activity("system", f"Auto-installing {len(_missing)} missing package(s): {_pkgs}")
                        _inst_res = await sandbox.exec_command(
                            # Wipe Vite optimizer cache so new packages' chunk hashes
                            # are regenerated cleanly, preventing dep-*.js errors.
                            # V55: Prefer-offline first — packages are often in cache if
                            # they were previously installed or are common packages.
                            f"cd /workspace && rm -rf node_modules/.vite .vite 2>/dev/null; "
                            f"npm install --prefer-offline {_pkgs} --no-audit --no-fund --loglevel=warn "
                            f"--save 2>&1 | tail -5",
                            timeout=120,
                        )
                        if _inst_res.exit_code == 0:
                            logger.info("📦  [AutoDep] Missing packages installed successfully")
                        else:
                            logger.warning("📦  [AutoDep] Install issues: %s", (_inst_res.stdout or '')[-200:])
                    else:
                        logger.debug("📦  [AutoDep] Import scan: all packages present")

    except Exception as exc:
        logger.debug("📦  [AutoDep] Auto-install failed (non-critical): %s", exc)


async def _recover_corrupt_node_modules(sandbox, state) -> None:
    """
    V54: Recovery for 'Cannot find module .../node_modules/vite/...' errors.
    Wipes node_modules inside the container and runs a clean npm install.
    Called automatically when vite chunk corruption is detected in task output.
    """
    if not sandbox or not sandbox.is_running:
        return
    try:
        logger.warning("📦  [Recovery] Vite chunk corruption detected — running clean reinstall …")
        if state:
            state.record_activity("system", "⚠️ Vite chunk corruption detected — clean npm reinstall starting …")
        container = sandbox._active.container_name or sandbox._active.container_id
        workspace = sandbox._active.workspace_path or "/workspace"
        await sandbox._container_npm_install(container, workspace)
        if state:
            state.record_activity("system", "📦 Clean npm reinstall complete — vite corruption resolved")
    except Exception as exc:
        logger.debug("📦  [Recovery] Clean reinstall failed: %s", exc)


async def _static_health_scan(project_path: str, state=None) -> list[str]:
    """
    V54: Host-side static health scan — runs BEFORE the sandbox boots.

    Detects a wide range of issues common to any JS/TS project and writes them
    to BUILD_ISSUES.md in the project root so the build-health-boot injector
    (and Gemini) can fix them in a single targeted task.

    Checks performed:
      1. TypeScript compile errors (tsc --noEmit)
      2. ESLint violations (npx eslint --format json)
      3. npm security audit (npm audit --json)
      4. Code pattern checks (dangerous as any casts, Suspense gaps,
         hidden nodes counted in metrics, hardcoded env values,
         console.log left in production code, TODO/FIXME density)

    Returns a list of issue summary strings (for the caller's logging).
    Writes / updates BUILD_ISSUES.md in the project root.
    """
    import asyncio as _aio
    import subprocess as _sub
    import json as _json_s
    import re as _re_s
    from pathlib import Path as _Path

    _proj = _Path(project_path)
    _issues: list[tuple[str, str, str]] = []   # (severity, title, detail)
    _found_any = False

    async def _run(cmd: str, timeout: int = 30) -> tuple[str, str, int]:
        """Run a shell command, return (stdout, stderr, returncode)."""
        try:
            proc = await _aio.wait_for(
                _aio.create_subprocess_shell(
                    cmd,
                    cwd=str(_proj),
                    stdout=_sub.PIPE,
                    stderr=_sub.PIPE,
                ),
                timeout=timeout,
            )
            stdout, stderr = await _aio.wait_for(proc.communicate(), timeout=timeout)
            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                proc.returncode or 0,
            )
        except Exception:
            return ("", "", -1)

    # ── 1. TypeScript errors (tsc --noEmit) ──────────────────────────────
    _tsconfig = _proj / "tsconfig.json"
    if _tsconfig.exists():
        _ts_out, _ts_err, _ts_rc = await _run(
            "npx --yes tsc --noEmit --pretty false 2>&1 | head -60",
            timeout=60,
        )
        _ts_combined = (_ts_out + _ts_err).strip()
        if _ts_rc != 0 and _ts_combined:
            # Parse error lines: src/Foo.tsx(12,5): error TS1234: ...
            _ts_errors = [
                l for l in _ts_combined.splitlines()
                if ": error TS" in l or ": warning TS" in l
            ][:20]
            if _ts_errors:
                _issues.append((
                    "❌",
                    "TypeScript compile errors (tsc --noEmit)",
                    "Fix all TypeScript errors before any other changes. "
                    "Remove `as any` / `as unknown` casts that mask these.\n\n"
                    "Errors:\n" + "\n".join(f"  {e}" for e in _ts_errors),
                ))

    # ── 2. ESLint violations ──────────────────────────────────────────────
    _eslintrc = any(
        (_proj / f).exists()
        for f in (".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs",
                  ".eslintrc.yaml", ".eslintrc.yml", "eslint.config.js",
                  "eslint.config.cjs", "eslint.config.mjs")
    )
    if _eslintrc:
        _el_out, _, _el_rc = await _run(
            "npx eslint . --format json --max-warnings=0 2>/dev/null | head -c 8000",
            timeout=45,
        )
        if _el_out.strip().startswith("["):
            try:
                _el_data = _json_s.loads(_el_out)
                _el_errors = sum(f.get("errorCount", 0) for f in _el_data)
                _el_warnings = sum(f.get("warningCount", 0) for f in _el_data)
                if _el_errors > 0:
                    # Collect up to 10 specific messages
                    _el_msgs = []
                    for _f in _el_data:
                        for _m in _f.get("messages", []):
                            if _m.get("severity", 0) == 2:
                                _rel = str(_Path(_f["filePath"]).relative_to(_proj))
                                _el_msgs.append(f"  {_rel}:{_m['line']}: {_m['message']}")
                            if len(_el_msgs) >= 10:
                                break
                        if len(_el_msgs) >= 10:
                            break
                    _issues.append((
                        "❌",
                        f"ESLint: {_el_errors} error(s), {_el_warnings} warning(s)",
                        "Run `npx eslint . --fix` for auto-fixable issues, "
                        "then manually resolve the rest.\n\nTop errors:\n"
                        + "\n".join(_el_msgs),
                    ))
            except Exception:
                pass

    # ── 3. npm security audit ─────────────────────────────────────────────
    if (_proj / "package.json").exists():
        _au_out, _, _au_rc = await _run(
            "npm audit --json 2>/dev/null | head -c 6000",
            timeout=30,
        )
        if _au_out.strip().startswith("{"):
            try:
                _au_data = _json_s.loads(_au_out)
                _au_vulns = _au_data.get("metadata", {}).get("vulnerabilities", {})
                _critical = _au_vulns.get("critical", 0)
                _high = _au_vulns.get("high", 0)
                _moderate = _au_vulns.get("moderate", 0)
                if _critical + _high > 0:
                    _issues.append((
                        "❌",
                        f"npm audit: {_critical} critical, {_high} high, {_moderate} moderate vulnerabilities",
                        "Run `npm audit fix` for auto-fixable issues. "
                        "For breaking-change fixes run `npm audit fix --force` and "
                        "check for API changes. Critical/High vulns must be resolved.",
                    ))
                elif _moderate > 0:
                    _issues.append((
                        "⚠️",
                        f"npm audit: {_moderate} moderate vulnerabilities",
                        "Run `npm audit fix` to address moderate severity issues.",
                    ))
            except Exception:
                pass

    # ── 4. Code pattern checks (host-side grep) ───────────────────────────
    _src_dirs = [d for d in ("src", "app", "lib", "components") if (_proj / d).is_dir()]
    _src_glob_base = " ".join(str(_proj / d) for d in _src_dirs) if _src_dirs else str(_proj)

    # 4a. Dangerous `as any` casts masking type errors
    _any_out, _, _ = await _run(
        f"grep -rn 'as any' {_src_glob_base} --include='*.ts' --include='*.tsx' | head -20",
        timeout=10,
    )
    _any_lines = [l for l in _any_out.splitlines() if l.strip()]
    if len(_any_lines) >= 5:
        _issues.append((
            "⚠️",
            f"`as any` casts detected ({len(_any_lines)} occurrences) — masking TypeScript errors",
            "Each `as any` cast hides a type error. Replace with proper types or "
            "`as unknown as TargetType` with runtime validation. Start with:\n"
            + "\n".join(f"  {l}" for l in _any_lines[:8]),
        ))

    # 4b. 3D/heavy components without Suspense boundaries
    _suspense_out, _, _ = await _run(
        f"grep -rln 'Canvas\\|drei\\|@react-three' {_src_glob_base} "
        f"--include='*.tsx' --include='*.jsx' | head -10",
        timeout=10,
    )
    _r3f_files = [l.strip() for l in _suspense_out.splitlines() if l.strip()]
    if _r3f_files:
        _sus_out, _, _ = await _run(
            f"grep -rLn 'Suspense' {' '.join(_r3f_files[:5])} 2>/dev/null",
            timeout=10,
        )
        _missing_sus = [l.strip() for l in _sus_out.splitlines() if l.strip()]
        if _missing_sus:
            _issues.append((
                "⚠️",
                "React Three Fiber components missing <Suspense> boundary",
                "Files using @react-three/fiber or drei must wrap lazy/async components "
                "in <Suspense fallback={...}>. Missing in:\n"
                + "\n".join(f"  {f}" for f in _missing_sus[:5]),
            ))

    # 4c. Hidden nodes / items counted in cost/metric computations
    _hidden_out, _, _ = await _run(
        f"grep -rn 'hidden.*true\\|isHidden' {_src_glob_base} "
        f"--include='*.ts' --include='*.tsx' | grep -v test | head -10",
        timeout=10,
    )
    _metric_out, _, _ = await _run(
        f"grep -rn 'computeMetrics\\|calcCost\\|totalCost\\|\\.reduce.*cost' {_src_glob_base} "
        f"--include='*.ts' --include='*.tsx' | head -10",
        timeout=10,
    )
    if _hidden_out.strip() and _metric_out.strip():
        _issues.append((
            "⚠️",
            "Hidden nodes may be counted in cost/metric calculations",
            "Nodes with `hidden: true` or `isHidden` should be filtered out of "
            "`computeMetrics`/cost calculations to avoid displaying incorrect totals. "
            "Add `nodes.filter(n => !n.hidden)` before computing metrics.",
        ))

    # 4d. Hardcoded localhost / environment values in source
    _hc_out, _, _ = await _run(
        f"grep -rn 'localhost:[0-9]\\+\\|127\\.0\\.0\\.1' {_src_glob_base} "
        f"--include='*.ts' --include='*.tsx' --include='*.js' | grep -v node_modules | head -10",
        timeout=10,
    )
    _hc_lines = [l for l in _hc_out.splitlines() if l.strip()]
    if len(_hc_lines) >= 3:
        _issues.append((
            "⚠️",
            f"Hardcoded localhost URLs in source ({len(_hc_lines)} occurrences)",
            "Replace hardcoded `localhost:PORT` with environment variables "
            "(e.g. `import.meta.env.VITE_API_URL`). Hardcoded URLs break "
            "staging/production deployments.\n"
            + "\n".join(f"  {l}" for l in _hc_lines[:6]),
        ))

    # 4e. console.log left in production source
    _cl_out, _, _ = await _run(
        f"grep -rn 'console\\.log(' {_src_glob_base} "
        f"--include='*.ts' --include='*.tsx' --include='*.js' | grep -v node_modules | wc -l",
        timeout=10,
    )
    _cl_count = int(_cl_out.strip()) if _cl_out.strip().isdigit() else 0
    if _cl_count >= 10:
        _issues.append((
            "⚠️",
            f"{_cl_count} console.log() calls in production source",
            "Remove or replace `console.log()` calls with a proper logger. "
            "They expose internals and hurt performance. "
            "Run: `grep -rn 'console.log' src/ --include='*.ts' --include='*.tsx'`",
        ))

    # ── Write / merge into BUILD_ISSUES.md ───────────────────────────────
    if _issues:
        _build_issues_path = _proj / "BUILD_ISSUES.md"
        # Load existing content (may already have user-written issues)
        _existing = ""
        if _build_issues_path.exists():
            _existing = _build_issues_path.read_text(encoding="utf-8")

        # Build new sections block
        _new_sections = ""
        for _sev, _title, _detail in _issues:
            # Don't duplicate a section that already exists verbatim
            if _title not in _existing:
                _new_sections += f"\n## {_sev} {_title}\n\n{_detail}\n\n---\n"

        if _new_sections:
            if _existing.strip():
                # Append before the ✅ Resolved section if it exists
                if "## ✅ Resolved" in _existing:
                    _existing = _existing.replace(
                        "## ✅ Resolved",
                        _new_sections + "## ✅ Resolved",
                    )
                else:
                    _existing = _existing.rstrip() + "\n" + _new_sections
            else:
                _existing = (
                    "# Build Issues\n\n"
                    "*Auto-generated by Supervisor AI static health scan.*\n\n---\n"
                    + _new_sections
                    + "\n## ✅ Resolved\n\n*(Move fixed issues here)*\n"
                )
            _build_issues_path.write_text(_existing, encoding="utf-8")
            logger.info(
                "🔍  [Static Scan] Wrote %d issue(s) to BUILD_ISSUES.md", len(_issues)
            )
            if state:
                state.record_activity(
                    "warning",
                    f"Static health scan: {len(_issues)} issue(s) written to BUILD_ISSUES.md",
                )

    summaries = [f"{sev} {title}" for sev, title, _ in _issues]
    return summaries


async def _build_health_check(
    executor,
    sandbox,
    state,
    project_path: str,
    planner=None,
):
    """
    V51: Run build health validation, sync BUILD_ISSUES.md, and inject fix tasks.

    Called:
      - At boot (after dependencies installed)
      - After coherence gate (every 5th task)

    When issues are found, injects a high-priority fix task into the active DAG
    referencing BUILD_ISSUES.md so Gemini addresses them first.
    """
    try:
        if not executor or not sandbox or not sandbox.is_running:
            return
        # V51: Don't run health checks during shutdown
        if state and getattr(state, 'stop_requested', False):
            return

        # V55: Show activity pill at start; always clear on exit via finally
        def _set_op(label: str) -> None:
            if state and hasattr(state, 'set_current_operation'):
                state.set_current_operation(label)

        _set_op('🔍 Build health check…')

        try:
            # V53: Pre-check — detect missing node_modules before running health checks.
            # When a project is resumed in a fresh container, node_modules doesn't exist.
            # Without this guard, the health check generates a misleading report where
            # ALL packages show Current:? and are flagged as "Major version bumps",
            # Vite/TypeScript fail, etc. — all cascading from one missing npm install.
            # Fix it directly here instead of injecting a DAG task for Gemini to handle.
            _nm_check = await sandbox.exec_command(
                "test -d /workspace/node_modules && echo EXISTS || echo MISSING",
                timeout=5,
            )
            _nm_missing = "MISSING" in (_nm_check.stdout or "")

            _has_pkg = await sandbox.exec_command(
                "test -f /workspace/package.json && echo YES || echo NO",
                timeout=5,
            )
            _has_package_json = "YES" in (_has_pkg.stdout or "")

            if _nm_missing and _has_package_json:
                # V55: Skip if the post-sync proactive install is already running
                # (sandbox_manager.sync_files_to_sandbox triggers one immediately after wipe)
                if getattr(sandbox, '_nm_install_in_progress', False):
                    logger.info("🔍  [Build Health] node_modules install already in progress (post-sync) — skipping")
                    return
                logger.info(
                    "🔍  [Build Health] node_modules missing — running npm install before health check …"
                )
                if state:
                    state.record_activity("system", "Auto-installing dependencies (node_modules missing)")
                _set_op('📦 npm install (node_modules missing)…')
                # V55: Try prefer-offline first — if this project has run before, the cache
                # is warm and this saves ~25-30s vs a full network install.
                _install = await sandbox.exec_command(
                    "cd /workspace && npm install --prefer-offline --no-audit --no-fund "
                    "--no-update-notifier --legacy-peer-deps --loglevel=error 2>&1 | tail -10",
                    timeout=120,
                )
                if _install.exit_code != 0:
                    # Prefer-offline failed (first run / cold cache) — fall back to full install
                    logger.info("🔍  [Build Health] prefer-offline install failed — falling back to full install …")
                    _install = await sandbox.exec_command(
                        "cd /workspace && npm install --no-audit --no-fund "
                        "--no-update-notifier --legacy-peer-deps --loglevel=error 2>&1 | tail -10",
                        timeout=300,
                    )
                if _install.exit_code == 0:
                    logger.info("🔍  [Build Health] npm install succeeded — health check will run next cycle")
                    if state:
                        state.record_activity("system", "npm install complete ✅")
                    # Don't run health checks yet — packages are now installed but the
                    # build cache is stale. Let the next coherence gate tick do a clean check.
                    return
                else:
                    # Install failed — inject a targeted task rather than a misleading report
                    _err = (_install.stdout or "")[-400:]
                    logger.warning("🔍  [Build Health] npm install failed: %s", _err)
                    if planner:
                        _bh_in_flight = any(
                            (n.task_id.startswith("build-health") or
                             n.task_id.endswith("-BUILD") or n.task_id.endswith("-DEPS"))
                            and n.status in ("pending", "running")
                            for n in planner._nodes.values()
                        )
                        if not _bh_in_flight:
                            from .temporal_planner import TaskNode as _TN
                            _nm_id = f"build-health-npm-{int(__import__('time').time())}"
                            planner.inject_task(
                                task_id=_nm_id,
                                description=(
                                    "[Build Health Fix] npm install failed in /workspace. "
                                    f"Error: {_err[:300]}. "
                                    "Diagnose the failure: check package.json for invalid entries, "
                                    "registry connectivity, or conflicting peer dependencies. "
                                    "Fix the root cause then run `npm install` successfully."
                                ),
                                dependencies=[],
                                priority=90,
                            )
                    return

            _set_op('🔍 Running build health scan…')
            result = await executor.build_health_check(timeout=120)

            issues = result.errors or []

            if state:
                if issues:
                    state.record_activity(
                        "warning",
                        f"Build health: {len(issues)} issue(s) — see BUILD_ISSUES.md",
                    )
                else:
                    state.record_activity(
                        "system", "Build health: all clear ✅",
                    )

            # Sync BUILD_ISSUES.md from sandbox to host
            build_issues_content = ""
            try:
                content = await sandbox.read_file("BUILD_ISSUES.md")
                if content and project_path:
                    build_issues_content = content
                    issues_path = Path(project_path) / "BUILD_ISSUES.md"
                    issues_path.write_text(content, encoding="utf-8")
                    logger.info(
                        "🔍  [Build Health] Synced BUILD_ISSUES.md → %s",
                        issues_path,
                    )
            except Exception:
                pass  # File may not exist if no package.json

            # V53: Guard — don't inject if a build-health task is already pending/running.
            if issues and planner and build_issues_content:
                _bh_in_flight = any(
                    (n.task_id.startswith("build-health") or
                     n.task_id.endswith("-BUILD") or n.task_id.endswith("-DEPS"))
                    and n.status in ("pending", "running")
                    for n in planner._nodes.values()
                )
                if _bh_in_flight:
                    logger.info(
                        "🔍  [Build Health] build-health task already in-flight — skipping injection"
                    )
                    return

            # V51: Inject a high-priority fix task into the DAG when issues are found
            if issues and planner and build_issues_content:
                _issue_lines = [
                    line for line in build_issues_content.splitlines()
                    if line.startswith("## ❌") or line.startswith("## ⚠")
                    or line.startswith("- ") or line.startswith("| ")
                ]
                _issue_summary = "\n".join(_issue_lines[:20])

                _fix_desc = (
                    "[BUILD] Read BUILD_ISSUES.md in the project root "
                    "and fix ALL issues listed under ❌ and ⚠ sections.\n\n"
                    "FOR OUTDATED DEPENDENCIES (Major Version Bumps): These are "
                    "breaking changes that npm update cannot auto-resolve. For each "
                    "major bump, check the package's changelog/migration guide for "
                    "API changes, update any imports or config accordingly, then "
                    "bump the version in package.json. After updating, delete "
                    "package-lock.json so the next install picks up the new versions. "
                    "(Non-breaking patch/minor updates were already auto-applied.)\n\n"
                    "FOR OTHER ISSUES: Fix TypeScript errors, Vite config problems, "
                    "missing modules, and ESM/CJS mismatches directly in the source.\n\n"
                    "After fixing each issue, update BUILD_ISSUES.md to move it "
                    "to the ✅ Resolved section. Once ALL issues are resolved, "
                    "delete BUILD_ISSUES.md entirely — a clean project has no "
                    f"issues file.\n\nKey issues:\n{_issue_summary}"
                )

                _bh_counter = getattr(state, '_build_health_counter', 0) + 1 if state else 1
                if state:
                    state._build_health_counter = _bh_counter
                _bh_off = planner.get_task_offset() + 1 if planner else _bh_counter
                _task_id = f"t{_bh_off}-BUILD"

                injected = planner.inject_task(
                    task_id=_task_id,
                    description=_fix_desc,
                    dependencies=[],
                    priority=90,
                )
                if injected:
                    logger.info(
                        "🔍  [Build Health] Injected fix task %s (priority=90) into DAG",
                        _task_id,
                    )
                    if state:
                        state.record_activity(
                            "task",
                            f"Build health issues injected as priority task: {_task_id}",
                        )

        except Exception as exc:
            logger.debug("🔍  [Build Health] Check failed: %s", exc)
        finally:
            # V55: Always clear the activity pill when we exit (success, error, or early return)
            if state and hasattr(state, 'set_current_operation'):
                state.set_current_operation("")

    except Exception as _outer_exc:
        # Outer guard (catches errors before _set_op was called, e.g. bad arguments)
        logger.debug("🔍  [Build Health] Outer guard caught: %s", _outer_exc)


# V55 Fix #3: Asyncio lock to prevent concurrent dev-server start races.
# When two parallel tasks complete simultaneously, both trigger _auto_preview_check().
# Without the lock both see 'server not running' and both try to reinstall + start,
# causing EADDRINUSE and double npm-install. The lock ensures only one start at a time.
_auto_preview_start_lock: asyncio.Lock | None = None


def _get_preview_lock() -> asyncio.Lock:
    """Lazily create the preview start lock (must be created inside an event loop)."""
    global _auto_preview_start_lock
    if _auto_preview_start_lock is None:
        _auto_preview_start_lock = asyncio.Lock()
    return _auto_preview_start_lock


async def _auto_preview_check(
    sandbox,
    executor,
    tools,
    state,
    project_path: str,
):
    """
    Auto-detect buildable projects and start a dev server in the sandbox.

    Called:
      - After boot (once sandbox + executor are ready)
      - After every task completion with file changes
      - Every monitoring tick (lightweight — skips sync if recent)

    Steps:
      1. Sync files from HOST → sandbox (copy mode only)
      2. Check if dev server is already running
      3. If not, detect if project is buildable and start server
      4. Update preview state for the UI
    """
    try:
        if not sandbox or not sandbox.is_running:
            return

        # V46: Check if dev server is already running FIRST — before syncing.
        # Previously, sync_files_to_sandbox ran on every tick even when the
        # server was already serving, wasting I/O and sometimes invalidating
        # node_modules (causing npm reinstall on every monitoring cycle).
        # V53 FIX: Do NOT early-return when server is running. In copy-mode
        # the container has an isolated volume — host changes made by Gemini
        # are never visible to the dev server until we sync them in.
        # Sync is guarded to only run when there are recent changes (< 30s).
        server = await tools.dev_server_check()
        if server.running:
            state.preview_running = True
            host_port = await sandbox.resolve_host_port(
                sandbox.active_sandbox.preview_port
            )
            if host_port:
                state.preview_port = host_port
                _save_preview_port(project_path, host_port, sandbox.active_sandbox.preview_port)
            elif sandbox.active_sandbox.host_preview_port:
                state.preview_port = sandbox.active_sandbox.host_preview_port
            await state.broadcast_state()
            # V53: Also sync recent changes into the container so the dev
            # server sees them. Only do this if there were recent changes
            # (avoid thrashing on idle monitoring ticks).
            if state and getattr(state, '_last_change_ts', 0):
                import time as _t
                if _t.time() - state._last_change_ts < 30:  # changed within last 30s
                    await sandbox.sync_files_to_sandbox(project_path)

            # V55: Comprehensive dev-server.log local self-healer.
            # Runs every 60s while server is running. Each pattern has its own
            # targeted fix — no Gemini needed for pure infrastructure issues.
            #
            # V55 FIX: Track byte offset into dev-server.log so we only scan NEW
            # output. Without this, old errors re-match every cooldown interval
            # (e.g. Tailwind fix fires every 300s forever on stale log content).
            _now = __import__('time').time()
            _last_scan = getattr(state, '_last_devlog_scan_ts', 0)
            if _now - _last_scan > 60:
                state._last_devlog_scan_ts = _now
                try:
                    # Get current log size
                    _szr = await sandbox.exec_command(
                        "wc -c < /tmp/dev-server.log 2>/dev/null || echo 0", timeout=5
                    )
                    _log_size = int((_szr.stdout or '0').strip())
                    _last_offset = getattr(state, '_devlog_offset', 0)

                    if _log_size > _last_offset:
                        # Only read bytes since the last scan
                        _dsl = await sandbox.exec_command(
                            f"tail -c +{_last_offset + 1} /tmp/dev-server.log 2>/dev/null | tail -120",
                            timeout=8,
                        )
                        _dsl_txt = _dsl.stdout or ""
                        state._devlog_offset = _log_size
                    else:
                        # No new log content — nothing to act on
                        _dsl_txt = ""
                    def _set_op(label: str) -> None:
                        if hasattr(state, 'set_current_operation'):
                            state.set_current_operation(label)

                    # ── Pattern 1: node_modules corruption ─────────────────
                    # Any 'Cannot find module' pointing inside node_modules → reinstall.
                    # Tier 1 (≤2 attempts): queue reinstall.
                    # Tier 2 (3+): Gemini diagnoses the import/package causing corruption.
                    if (
                        "Cannot find module" in _dsl_txt
                        and "node_modules" in _dsl_txt
                        and not getattr(state, 'restart_dev_server_requested', None)
                    ):
                        _snippet = next(
                            (l for l in _dsl_txt.splitlines() if "Cannot find module" in l), ""
                        )[:120]
                        _nm_attempts = getattr(state, '_nm_corrupt_attempts', 0) + 1
                        state._nm_corrupt_attempts = _nm_attempts

                        if _nm_attempts <= 2:
                            logger.warning(
                                "🖥️  [Dev Log] node_modules corruption → queueing reinstall (attempt %d/2): %s",
                                _nm_attempts, _snippet
                            )
                            state.restart_dev_server_requested = "reinstall"
                            state.record_activity("system", f"⚠️ node_modules corrupt — reinstall queued (attempt {_nm_attempts}/2): {_snippet}")
                            _set_op('🔧 node_modules reinstall queued…')
                        else:
                            # Tier 2: local analysis first — is this package actually in package.json?
                            _set_op('🔍 Diagnosing node_modules error…')
                            try:
                                # Extract missing package name from error
                                import re as _re
                                _pkg_match = _re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", _snippet)
                                _missing_pkg = _pkg_match.group(1).split('/')[0] if _pkg_match else ''
                                # Scoped packages keep the @scope prefix
                                if _pkg_match:
                                    _parts = _pkg_match.group(1).lstrip('@').split('/')
                                    _missing_pkg = ('@' + _parts[0] + '/' + _parts[1]) if _pkg_match.group(1).startswith('@') and len(_parts) > 1 else _pkg_match.group(1).split('/')[0]

                                _in_pkgjson = ''
                                _local_fixed = False
                                if _missing_pkg:
                                    # Check if it's listed in package.json
                                    _pkgchk = await sandbox.exec_command(
                                        f"node -e \"const p=require('/workspace/package.json');console.log((p.dependencies||{{}})['{_missing_pkg}']||(p.devDependencies||{{}})['{_missing_pkg}']||'MISSING')\"",
                                        timeout=8,
                                    )
                                    _in_pkgjson = (_pkgchk.stdout or '').strip()

                                    if _in_pkgjson and _in_pkgjson != 'MISSING':
                                        # Package IS in package.json but missing from node_modules — targeted reinstall
                                        logger.info("🔧  [Dev Log] '%s' in package.json but missing from node_modules — targeted reinstall…", _missing_pkg)
                                        state.record_activity("system", f"🔧 Targeted npm install for missing '{_missing_pkg}'…")
                                        _tgt_res = await sandbox.exec_command(
                                            f"cd /workspace && npm install --prefer-offline --no-audit --no-fund {_missing_pkg} 2>&1 | tail -5",
                                            timeout=120,
                                        )
                                        if _tgt_res.exit_code == 0:
                                            logger.info("🔧  [Dev Log] Targeted install of '%s' succeeded ✅", _missing_pkg)
                                            state.record_activity("system", f"✅ '{_missing_pkg}' reinstalled — restarting dev server")
                                            await executor.start_dev_server()
                                            state._nm_corrupt_attempts = 0
                                            _local_fixed = True

                                if not _local_fixed:
                                    # Not in package.json or install failed — Gemini must add/fix it
                                    logger.warning(
                                        "🖥️  [Dev Log] node_modules still corrupt after %d reinstalls — escalating to Gemini",
                                        _nm_attempts
                                    )
                                    state.record_activity("warning", f"⚠️ node_modules corrupt after {_nm_attempts} reinstalls — asking Gemini to diagnose")
                                    _active_planner = getattr(state, 'planner', None)
                                    if _active_planner:
                                        _nm_task_id = f"fix-nm-corrupt-{int(time.time())}"
                                        _bh_in_flight = any(
                                            n.task_id.startswith("fix-nm-corrupt") and n.status in ("pending", "running")
                                            for n in _active_planner._nodes.values()
                                        )
                                        if not _bh_in_flight:
                                            _pkg_context = f"Package '{_missing_pkg}' {'IS listed in package.json as ' + _in_pkgjson + ' but targeted install failed' if _in_pkgjson and _in_pkgjson != 'MISSING' else 'is NOT listed in package.json at all'}."
                                            _active_planner.inject_task(
                                                task_id=_nm_task_id,
                                                description=(
                                                    "[node_modules Fix] Dev server keeps throwing 'Cannot find module' errors "
                                                    f"even after {_nm_attempts} reinstalls. Error: {_snippet}. {_pkg_context} "
                                                    "Fix permanently: if the package is missing from package.json add it; "
                                                    "if the import path is wrong correct it; if it's an internal module check the file exists."
                                                ),
                                                dependencies=[],
                                                priority=90,
                                            )
                                            logger.info("🔧  [Dev Log] Injected Gemini task %s for node_modules diagnosis", _nm_task_id)
                                            state._nm_corrupt_attempts = 0
                            except Exception as _nme:
                                logger.debug("🔧  [Dev Log] node_modules local analysis failed: %s", _nme)
                            finally:
                                _set_op("")

                    # ── Pattern 2: Port already in use ────────────────────
                    # EADDRINUSE → the old process is still holding the port → kill it.
                    # Tier 1 (≤2 attempts): hard kill + restart.
                    # Tier 2 (3+): Gemini investigates what's binding the port persistently.
                    elif (
                        "EADDRINUSE" in _dsl_txt or "address already in use" in _dsl_txt.lower()
                    ) and (__import__('time').time() - getattr(state, '_port_kill_ts', 0)) > 120:
                        state._port_kill_ts = __import__('time').time()
                        _port_attempts = getattr(state, '_port_kill_attempts', 0) + 1
                        state._port_kill_attempts = _port_attempts

                        if _port_attempts <= 2:
                            logger.warning(
                                "🖥️  [Dev Log] EADDRINUSE → killing stale process + restarting (attempt %d/2)…",
                                _port_attempts
                            )
                            state.record_activity("system", f"⚠️ Port in use — killing stale dev server (attempt {_port_attempts}/2)")
                            _set_op('🔄 Killed stale process, restarting server…')
                            try:
                                await sandbox.exec_command(
                                    "fuser -k 3000/tcp 2>/dev/null; fuser -k 5173/tcp 2>/dev/null; "
                                    "pkill -f 'vite|next|webpack' 2>/dev/null; sleep 1; true",
                                    timeout=12,
                                )
                                await executor.start_dev_server()
                                state.record_activity("system", "Dev server restarted after port conflict ✅")
                            except Exception as _pe:
                                logger.debug("🖥️  [Dev Log] Port kill failed: %s", _pe)
                            finally:
                                _set_op("")
                        else:
                            # Tier 2: local PID-targeted kill before config change
                            _set_op('🔍 Identifying process holding port…')
                            _local_port_fixed = False
                            try:
                                # Find the PID(s) holding the dev port(s)
                                _ss_res = await sandbox.exec_command(
                                    "ss -tlnp 2>/dev/null | grep -E ':(3000|5173|4173)\\s' | grep -oP 'pid=\\K[0-9]+' | sort -u",
                                    timeout=8,
                                )
                                _pids = [p.strip() for p in (_ss_res.stdout or '').splitlines() if p.strip()]
                                if _pids:
                                    _pid_str = ' '.join(_pids)
                                    logger.info("🔄  [Dev Log] Found PIDs holding port: %s — killing with SIGKILL", _pid_str)
                                    _kill_res = await sandbox.exec_command(
                                        f"kill -9 {_pid_str} 2>/dev/null; sleep 1; true",
                                        timeout=8,
                                    )
                                    # Verify port is now free
                                    _verify = await sandbox.exec_command(
                                        "ss -tlnp 2>/dev/null | grep -cE ':(3000|5173|4173)\\s' || echo 0",
                                        timeout=5,
                                    )
                                    if int((_verify.stdout or '1').strip()) == 0:
                                        logger.info("🔄  [Dev Log] Port cleared via targeted kill ✅ — restarting…")
                                        state.record_activity("system", f"Port cleared (killed PIDs: {_pid_str}) — restarting dev server")
                                        await executor.start_dev_server()
                                        state._port_kill_attempts = 0
                                        _local_port_fixed = True

                                if not _local_port_fixed:
                                    # Port still busy — likely hardcoded in config; Gemini must fix it
                                    # Gather port check context for Gemini
                                    _vite_port_cfg = await sandbox.exec_command(
                                        "grep -r 'port' /workspace/vite.config* 2>/dev/null | head -10",
                                        timeout=5,
                                    )
                                    _port_ctx = (_vite_port_cfg.stdout or '').strip()[:300]
                                    logger.warning(
                                        "🖥️  [Dev Log] EADDRINUSE persists after targeted kill — escalating to Gemini"
                                    )
                                    state.record_activity("warning", f"⚠️ Port still in use after kill — asking Gemini to change port config")
                                    _active_planner = getattr(state, 'planner', None)
                                    if _active_planner:
                                        _port_task_id = f"fix-port-conflict-{int(time.time())}"
                                        _bh_in_flight = any(
                                            n.task_id.startswith("fix-port-conflict") and n.status in ("pending", "running")
                                            for n in _active_planner._nodes.values()
                                        )
                                        if not _bh_in_flight:
                                            _active_planner.inject_task(
                                                task_id=_port_task_id,
                                                description=(
                                                    "[Port Conflict Fix] Dev server EADDRINUSE persists even after targeted process kills. "
                                                    f"Current vite.config port references: {_port_ctx or 'could not read'}. "
                                                    "Change the dev server port in vite.config.js to 5174 (or another unused port) "
                                                    "and update any matching port references in package.json scripts to match."
                                                ),
                                                dependencies=[],
                                                priority=90,
                                            )
                                            logger.info("🔄  [Dev Log] Injected Gemini task %s for port config change", _port_task_id)
                                            state._port_kill_attempts = 0
                            except Exception as _ppe:
                                logger.debug("🖥️  [Dev Log] Port local analysis failed: %s", _ppe)
                            finally:
                                _set_op("")

                    # ── Pattern 3: Vite cache stale signal ─────────────────
                    # Vite logs 'new dependencies optimized' when its pre-bundle cache is
                    # outdated. Clearing prevents the 504 Outdated Optimize Dep loop.
                    # Tier 1 (≤2 attempts): clear cache + restart.
                    # Tier 2 (3+): Gemini diagnoses why deps keep dirtying (likely a
                    #   dynamic import, a barrel re-export, or a missing optimizeDeps entry).
                    elif (
                        ("new dependencies optimized" in _dsl_txt.lower()
                         or "deps changed, restarting server" in _dsl_txt.lower()
                         or ("504" in _dsl_txt and ".vite" in _dsl_txt))
                        and (__import__('time').time() - getattr(state, '_vite_cache_clear_ts', 0)) > 120
                    ):
                        state._vite_cache_clear_ts = __import__('time').time()
                        _vite_attempts = getattr(state, '_vite_cache_attempts', 0) + 1
                        state._vite_cache_attempts = _vite_attempts

                        if _vite_attempts <= 2:
                            logger.warning(
                                "🖥️  [Dev Log] Vite cache stale → clearing + restart (attempt %d/2)…",
                                _vite_attempts
                            )
                            state.record_activity("system", f"⚠️ Vite cache stale — clearing + restarting (attempt {_vite_attempts}/2)")
                            _set_op('🧹 Clearing stale Vite cache…')
                            try:
                                await sandbox.exec_command(
                                    "rm -rf /workspace/node_modules/.vite /workspace/.vite "
                                    "/workspace/node_modules/.cache 2>/dev/null; true",
                                    timeout=10,
                                )
                                await sandbox.exec_command("pkill -f 'vite' 2>/dev/null || true", timeout=5)
                                await __import__('asyncio').sleep(2)
                                await executor.start_dev_server()
                                state.record_activity("system", "Vite cache cleared + dev server restarted ✅")
                            except Exception as _ve:
                                logger.debug("🖥️  [Dev Log] Vite cache clear failed: %s", _ve)
                            finally:
                                _set_op("")
                        else:
                            # Tier 2: extract which packages keep re-optimizing, try local patch first
                            _set_op('🔍 Analysing Vite optimizer loop…')
                            _local_vite_fixed = False
                            try:
                                # Extract package names from Vite's 're-optimized' log lines
                                import re as _re
                                _opt_pkgs = list(dict.fromkeys(
                                    _re.findall(r"new dependencies optimized: ([\w@/.-]+(?:, [\w@/.-]+)*)", _dsl_txt.lower())
                                ))[:10]
                                _opt_pkg_flat = ', '.join(_opt_pkgs) if _opt_pkgs else ''

                                # Also grab current vite.config for context
                                _vcfg_res = await sandbox.exec_command(
                                    "cat /workspace/vite.config.ts 2>/dev/null || cat /workspace/vite.config.js 2>/dev/null | head -80",
                                    timeout=6,
                                )
                                _vcfg_txt = (_vcfg_res.stdout or '').strip()

                                # Try to inject optimizeDeps.include via sed if it's a simple JS/TS config
                                # Only if the packages were identified AND optimizeDeps block doesn't already list them
                                if _opt_pkgs and _vcfg_txt and 'optimizeDeps' not in _vcfg_txt:
                                    _pkg_list = ', '.join(f"'{p}'" for p in _opt_pkgs)
                                    _patch_cmd = (
                                        "cd /workspace && "
                                        "VITE_CFG=$(ls vite.config.ts vite.config.js 2>/dev/null | head -1); "
                                        f"sed -i 's/defineConfig(/defineConfig(/' $VITE_CFG; "
                                        # Non-destructive: append optimizeDeps if defineConfig({ found
                                        f"python3 -c \""
                                        f"import re, sys; txt=open('$VITE_CFG').read(); "
                                        f"patched=re.sub(r'defineConfig\\(\\{{', 'defineConfig({{optimizeDeps:{{include:[{_pkg_list}]}},', txt, count=1); "
                                        f"open('$VITE_CFG','w').write(patched) if patched != txt else sys.exit(1)\""
                                    )
                                    _patch_res = await sandbox.exec_command(_patch_cmd, timeout=10)
                                    if _patch_res.exit_code == 0:
                                        logger.info("🧹  [Dev Log] Patched vite.config with optimizeDeps.include: %s", _opt_pkg_flat)
                                        state.record_activity("system", f"✅ Added optimizeDeps.include for: {_opt_pkg_flat}")
                                        # Clear cache + restart to apply
                                        await sandbox.exec_command(
                                            "rm -rf /workspace/node_modules/.vite /workspace/.vite 2>/dev/null; true",
                                            timeout=8,
                                        )
                                        await executor.start_dev_server()
                                        state._vite_cache_attempts = 0
                                        _local_vite_fixed = True

                                if not _local_vite_fixed:
                                    # Local patch couldn't be applied safely — Gemini gets full context
                                    logger.warning(
                                        "🖥️  [Dev Log] Vite cache keeps dirtying after %d clears — escalating with context",
                                        _vite_attempts
                                    )
                                    state.record_activity("warning", f"⚠️ Vite cache loop — asking Gemini to add optimizeDeps config")
                                    _active_planner = getattr(state, 'planner', None)
                                    if _active_planner:
                                        _vite_task_id = f"fix-vite-cache-loop-{int(time.time())}"
                                        _bh_in_flight = any(
                                            n.task_id.startswith("fix-vite-cache-loop") and n.status in ("pending", "running")
                                            for n in _active_planner._nodes.values()
                                        )
                                        if not _bh_in_flight:
                                            _active_planner.inject_task(
                                                task_id=_vite_task_id,
                                                description=(
                                                    "[Vite Cache Fix] Vite optimizer cache keeps becoming stale "
                                                    f"after {_vite_attempts} cache-clears. "
                                                    f"Packages frequently re-optimized: {_opt_pkg_flat or '(extract from vite.config logs)'}. "
                                                    f"Current vite.config:\n{_vcfg_txt[:600] if _vcfg_txt else '(could not read)'}\n"
                                                    "Add the repeatedly re-optimized packages to optimizeDeps.include in vite.config, "
                                                    "or fix any dynamic imports/barrel exports causing the churn."
                                                ),
                                                dependencies=[],
                                                priority=80,
                                            )
                                            logger.info("🧹  [Dev Log] Injected Gemini task %s with Vite config context", _vite_task_id)
                                            state._vite_cache_attempts = 0
                            except Exception as _vce:
                                logger.debug("🧹  [Dev Log] Vite local analysis failed: %s", _vce)
                            finally:
                                _set_op("")

                    # ── Pattern 4: PostCSS / Tailwind theme() resolution errors ──
                    # 'Could not resolve value for theme function' = Tailwind config
                    # mismatch or missing CSS custom property.
                    # Fix tier 1 (≤2 attempts): clear Vite cache — fixes transient
                    #   optimizer races.
                    # Fix tier 2 (3+ attempts): escalate to Gemini — root cause is
                    #   likely a bad tailwind.config.js / postcss.config.js that
                    #   cache-clearing can't fix.
                    elif (
                        ("theme(" in _dsl_txt and "Could not resolve" in _dsl_txt)
                        or ("tailwindcss" in _dsl_txt.lower() and "error" in _dsl_txt.lower()
                            and "index.css" in _dsl_txt)
                    ) and (time.time() - getattr(state, '_tailwind_fix_ts', 0)) > 300:
                        state._tailwind_fix_ts = time.time()
                        _tw_attempts = getattr(state, '_tailwind_fix_attempts', 0) + 1
                        state._tailwind_fix_attempts = _tw_attempts

                        if _tw_attempts <= 2:
                            # Tier 1: try clearing Vite cache (transient race condition)
                            logger.warning(
                                "🖥️  [Dev Log] Tailwind/PostCSS config error → clearing Vite cache (attempt %d/2)…",
                                _tw_attempts,
                            )
                            state.record_activity("system", f"⚠️ Tailwind config error — clearing Vite cache (attempt {_tw_attempts}/2)")
                            _set_op('🎨 Fixing Tailwind/CSS config error…')
                            try:
                                await sandbox.exec_command(
                                    "rm -rf /workspace/node_modules/.vite /workspace/.vite 2>/dev/null; true",
                                    timeout=10,
                                )
                                await sandbox.exec_command("pkill -f 'vite' 2>/dev/null || true", timeout=5)
                                await __import__('asyncio').sleep(2)
                                await executor.start_dev_server()
                                state.record_activity("system", "Vite cache cleared after Tailwind fix ✅")
                            except Exception as _te:
                                logger.debug("🖥️  [Dev Log] Tailwind fix failed: %s", _te)
                            finally:
                                _set_op("")
                        else:
                            # Tier 2: read configs locally, send full context to Gemini
                            _set_op('🔍 Reading Tailwind/CSS config for diagnosis…')
                            try:
                                _err_excerpt = next(
                                    (l for l in _dsl_txt.splitlines()
                                     if 'tailwind' in l.lower() or 'theme(' in l or 'Could not resolve' in l),
                                    _dsl_txt[-300:]
                                )
                                # Read the actual config files so Gemini has everything it needs
                                _tw_cfg = await sandbox.exec_command(
                                    "cat /workspace/tailwind.config.ts 2>/dev/null || cat /workspace/tailwind.config.js 2>/dev/null | head -80",
                                    timeout=6,
                                )
                                _pc_cfg = await sandbox.exec_command(
                                    "cat /workspace/postcss.config.js 2>/dev/null || cat /workspace/postcss.config.cjs 2>/dev/null | head -40",
                                    timeout=5,
                                )
                                _css_head = await sandbox.exec_command(
                                    "head -60 /workspace/src/index.css 2>/dev/null || head -60 /workspace/index.css 2>/dev/null",
                                    timeout=5,
                                )
                                _tw_cfg_txt  = (_tw_cfg.stdout  or '').strip()[:600]
                                _pc_cfg_txt  = (_pc_cfg.stdout  or '').strip()[:300]
                                _css_head_txt = (_css_head.stdout or '').strip()[:300]

                                logger.warning(
                                    "🖥️  [Dev Log] Tailwind error persists after %d cache-clear attempts — "
                                    "escalating to Gemini for config fix",
                                    _tw_attempts,
                                )
                                state.record_activity(
                                    "warning",
                                    f"⚠️ Tailwind/PostCSS config error persists after {_tw_attempts} cache-clear attempts — asking Gemini to fix config",
                                )
                                _active_planner = getattr(state, 'planner', None)
                                if _active_planner:
                                    _tw_task_id = f"fix-tailwind-config-{int(time.time())}"
                                    _bh_in_flight = any(
                                        n.task_id.startswith("fix-tailwind") and n.status in ("pending", "running")
                                        for n in _active_planner._nodes.values()
                                    )
                                    if not _bh_in_flight:
                                        _active_planner.inject_task(
                                            task_id=_tw_task_id,
                                            description=(
                                                "[CSS Config Fix] The dev server keeps throwing a Tailwind/PostCSS error "
                                                f"that clearing the Vite cache has not resolved. Error: {_err_excerpt[:400]}.\n"
                                                f"tailwind.config: {_tw_cfg_txt or '(not found)'}\n"
                                                f"postcss.config: {_pc_cfg_txt or '(not found)'}\n"
                                                f"index.css (first 60 lines): {_css_head_txt or '(not found)'}\n"
                                                "Fix the root cause using the above file contents: correct invalid theme() references, "
                                                "fix plugin order in postcss.config, or fix @apply calls referencing non-existent values. "
                                                "Make the minimal change to resolve the error permanently."
                                            ),
                                            dependencies=[],
                                            priority=85,
                                        )
                                        logger.info("🎨  [Dev Log] Injected Gemini task %s to fix Tailwind config (with config context)", _tw_task_id)
                                        state._tailwind_fix_attempts = 0
                            except Exception as _twe:
                                logger.debug("🎨  [Dev Log] Tailwind local analysis failed: %s", _twe)
                            finally:
                                _set_op("")

                except Exception:
                    pass
            return

        # Server not running — sync files then try to start it
        # V55 Fix #3: Use a lock so concurrent calls don't both try to install + start.
        _plock = _get_preview_lock()
        if _plock.locked():
            # Another coroutine is already starting the server — skip.
            logger.debug("🖥️  [Auto-Preview] Server start already in progress — skipping concurrent attempt.")
            return
        async with _plock:
            await sandbox.sync_files_to_sandbox(project_path)

            # Not running — check if project is buildable
            has_package_json = await sandbox.file_exists("package.json")
            has_index_html = await sandbox.file_exists("index.html")
            has_index_php = await sandbox.file_exists("index.php")

            if not has_package_json and not has_index_html and not has_index_php:
                # V42: Check for any serveable files at all (HTML, PHP, Python)
                file_check = await sandbox.exec_command(
                    "ls /workspace/*.html /workspace/*.php /workspace/*.py 2>/dev/null | head -1"
                )
                if not file_check.stdout.strip():
                    return  # Nothing buildable yet

            # Start dev server in background
            logger.info("🖥️  [Auto-Preview] Buildable project detected — starting dev server …")
            await executor.start_dev_server()

            # V52: Propagate build error to state for UI display
            _dse = getattr(executor, '_last_dev_server_error', '')
            if _dse:
                state.dev_server_error = _dse

            # Re-check if it started
            server = await tools.dev_server_check()
            if server.running:
                state.preview_running = True
                host_port = await sandbox.resolve_host_port(
                    sandbox.active_sandbox.preview_port
                )
                if host_port:
                    state.preview_port = host_port
                    _save_preview_port(project_path, host_port, sandbox.active_sandbox.preview_port)
                elif sandbox.active_sandbox.host_preview_port:
                    state.preview_port = sandbox.active_sandbox.host_preview_port
                logger.info(
                    "🖥️  [Auto-Preview] Dev server started on port %d (host: %d)",
                    server.port, state.preview_port,
                )
                await state.broadcast_state()

                # V44: Start console error capture
                await _start_error_collector(sandbox)
                await _inject_error_hook(sandbox)
            else:
                # Dev server failed to start — check log for Vite chunk corruption
                logger.info("🖥️  [Auto-Preview] Dev server did not start (may not be buildable yet)")
                try:
                    _dsl = await sandbox.exec_command(
                        "cat /tmp/dev-server.log 2>/dev/null | tail -40", timeout=8
                    )
                    _dsl_txt = _dsl.stdout or ""
                    if (
                        "node_modules/vite" in _dsl_txt
                        and "Cannot find module" in _dsl_txt
                        and not getattr(state, 'restart_dev_server_requested', None)
                    ):
                        logger.warning(
                            "🖥️  [Auto-Preview] Vite chunk corruption detected in dev-server.log — "
                            "auto-triggering clean reinstall …"
                        )
                        state.restart_dev_server_requested = "reinstall"
                        state.record_activity(
                            "system",
                            "⚠️ Vite chunk error detected in dev log — queuing clean reinstall …"
                        )
                except Exception:
                    pass

    except Exception as exc:
        logger.debug("🖥️  [Auto-Preview] Check failed: %s", exc)


# ── Console Error Capture (V44) ──────────────────────────────────

_error_collector_started = False


async def _start_error_collector(sandbox) -> None:
    """
    Copy the error collector Node.js script into the sandbox and start it
    on port 9999. The collector receives POSTed errors from the injected
    hook script and stores them in /tmp/console_errors.json.
    """
    global _error_collector_started
    if _error_collector_started:
        return
    try:
        # Copy the collector script into the sandbox
        collector_path = Path(__file__).parent / "console_error_collector.js"
        if not collector_path.exists():
            logger.debug("🖥️  [Error Capture] Collector script not found: %s", collector_path)
            return
        await sandbox.copy_file_in(str(collector_path), "/tmp/console_error_collector.js")

        # Start collector in background
        await sandbox.exec_command(
            "nohup node /tmp/console_error_collector.js > /tmp/error_collector.log 2>&1 &",
            timeout=5,
        )
        _error_collector_started = True
        logger.info("🖥️  [Error Capture] Error collector started on port 9999")
    except Exception as exc:
        logger.debug("🖥️  [Error Capture] Failed to start collector: %s", exc)


# V60: Track byte offset + fingerprints for Vite dev-log scanner
_vite_log_offset: int = 0
_vite_log_fps: set = set()

_VITE_ERROR_PATTERNS = [
    # plugin:vite:react-babel JSX/Babel parse errors
    __import__('re').compile(r'\[plugin:vite[^\]]*\].*?(?:Expected|SyntaxError|Unexpected)', __import__('re').I),
    # TypeScript compiler errors in watch/dev mode
    __import__('re').compile(r'error\s+TS\d+:', __import__('re').I),
    # Vite production build failure line
    __import__('re').compile(r'Build failed|failed to compile|\u2728\s*\[ERROR\]', __import__('re').I),
]


async def _scan_vite_dev_log(sandbox, planner, state=None) -> None:
    """
    V60: Tail /tmp/dev-server.log in the sandbox for Vite/Babel/TS compile errors.

    Runs on a 30s timer. New error lines are fingerprinted so the same error
    isn't re-injected. Priority 92 (above build-health 90) so blocking compile
    errors pre-empt normal work.
    """
    import re as _re_vl
    global _vite_log_offset, _vite_log_fps
    if not sandbox or not planner:
        return
    if state and getattr(state, 'stop_requested', False):
        return
    try:
        result = await sandbox.exec_command(
            f"tail -c +{_vite_log_offset + 1} /tmp/dev-server.log 2>/dev/null | head -c 8192",
            timeout=8,
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return
        _vite_log_offset += len(raw.encode("utf-8", errors="replace"))

        new_errors: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            for pat in _VITE_ERROR_PATTERNS:
                if pat.search(line):
                    fp = _re_vl.sub(r'\s+', ' ', line[:120]).lower()
                    if fp not in _vite_log_fps:
                        _vite_log_fps.add(fp)
                        new_errors.append(line)
                    break

        if not new_errors:
            return

        _err_block = "\n".join(f"  {e}" for e in new_errors[:5])
        _fix_desc = (
            "[BUILD] Vite dev-server reported compile error(s) blocking the live preview. "
            "Fix the root cause in the source files.\n\n"
            "ERRORS FROM DEV SERVER LOG:\n"
            f"{_err_block}\n\n"
            "REQUIRED STEPS:\n"
            "1. Run `npx tsc --noEmit 2>&1 | head -40` to see full TypeScript/JSX errors.\n"
            "2. Fix all syntax/type errors in the referenced files.\n"
            "3. Confirm the Vite dev server restarts cleanly (no error overlay in preview).\n"
            "4. For 'Expected JSX closing tag' errors: check the entire function's JSX tree "
            "for unclosed or mismatched tags."
        )
        _vl_off = planner.get_task_offset() + 1
        _vl_id = f"t{_vl_off}-BUILD"
        _injected = planner.inject_task(
            task_id=_vl_id,
            description=_fix_desc,
            dependencies=[],
            priority=92,
        )
        if _injected:
            logger.warning(
                "🔥  [Vite Log] Compile error — injected %s (priority=92), %d new error(s)",
                _vl_id, len(new_errors),
            )
            if state:
                state.record_activity("warning", f"Vite compile error: {new_errors[0][:80]}")
    except Exception as exc:
        logger.debug("🔥  [Vite Log] Scanner error (non-fatal): %s", exc)


async def _inject_error_hook(sandbox) -> None:
    """
    V53: Inject the error capture hook into the project using all available
    strategies, covering every project type the supervisor might build.

    Strategy priority (all applied in parallel):
      1. Static HTML injection  — sed </head> in all *.html files
      2. Vite/Next/Nuxt public/ — copy hook to public/ folder which the
         framework serves verbatim at /_supervisor_error_hook.js
      3. Vite index.html template — the source index.html used by Vite as
         an entry point (not the built output)
      4. Next.js _document / layout — inject script tag into layout entry

    Idempotent — all paths check for existing injection before modifying.
    """
    try:
        hook_path = Path(__file__).parent / "console_error_hook.js"
        if not hook_path.exists():
            logger.debug("🖥️  [Error Capture] Hook script not found: %s", hook_path)
            return

        # Deploy the hook script into the workspace root (for static HTML projects)
        await sandbox.copy_file_in(str(hook_path), "/workspace/_supervisor_error_hook.js")

        # ── Strategy 1: sed-inject into all HTML files ──
        inject_cmd = (
            "find /workspace -maxdepth 4 -name '*.html'"
            r" ! -path '*/node_modules/*' ! -path '*/.git/*'"
            " -exec grep -L '_supervisor_error_hook' {} \\; | "
            "xargs -r sed -i "
            "'s|</head>|<script src=\"/_supervisor_error_hook.js\"></script>\\n</head>|'"
        )
        await sandbox.exec_command(inject_cmd, timeout=10)

        # ── Strategy 2: Deploy to public/ for Vite / Next.js / Nuxt / CRA ──
        # These frameworks serve the public/ folder as the web root, so
        # _supervisor_error_hook.js becomes available at /_supervisor_error_hook.js
        # automatically — no sed injection needed, the existing <script> tag works.
        pub_dirs = ["public", "static", "assets", "www"]
        for pub_dir in pub_dirs:
            pub_check = await sandbox.exec_command(
                f"test -d /workspace/{pub_dir} && echo exists || true", timeout=3
            )
            if "exists" in (pub_check.stdout or ""):
                await sandbox.exec_command(
                    f"cp /workspace/_supervisor_error_hook.js /workspace/{pub_dir}/_supervisor_error_hook.js",
                    timeout=5,
                )
                logger.info("🖥️  [Error Capture] Hook deployed to /%s/", pub_dir)

        # ── Strategy 3: Vite index.html template (project root index.html) ──
        # Vite treats the root index.html as the entry — inject there too.
        vite_idx = await sandbox.exec_command(
            "test -f /workspace/index.html && "
            "grep -L '_supervisor_error_hook' /workspace/index.html && echo needs_inject || true",
            timeout=3,
        )
        if "needs_inject" in (vite_idx.stdout or ""):
            await sandbox.exec_command(
                r"sed -i 's|</head>|<script src=\"/_supervisor_error_hook.js\"></script>\n</head>|' "
                "/workspace/index.html",
                timeout=5,
            )
            logger.info("🖥️  [Error Capture] Hook injected into root index.html (Vite template)")

        # ── Strategy 4: Next.js layout entry points ──
        next_entries = [
            "src/app/layout.tsx", "src/app/layout.jsx", "app/layout.tsx", "app/layout.jsx",
            "src/pages/_document.tsx", "src/pages/_document.jsx",
            "pages/_document.tsx", "pages/_document.jsx",
        ]
        for entry in next_entries:
            check = await sandbox.exec_command(
                f"test -f /workspace/{entry} && "
                f"grep -L '_supervisor_error_hook' /workspace/{entry} && echo needs_inject || true",
                timeout=3,
            )
            if "needs_inject" in (check.stdout or ""):
                # For layout.tsx: append a <Script> component import + usage after <body>
                # For _document.tsx: inject into <Head>
                if "layout" in entry:
                    await sandbox.exec_command(
                        f"""sed -i 's|<body|<script src=\"/_supervisor_error_hook.js\"></script><body|' /workspace/{entry}""",
                        timeout=5,
                    )
                    logger.info("🖥️  [Error Capture] Hook injected into %s", entry)
                break  # Only inject into the first found entry

        logger.info("🖥️  [Error Capture] Hook injection complete (all strategies applied)")
    except Exception as exc:
        logger.debug("🖥️  [Error Capture] Injection failed: %s", exc)


# V53: Per-category fix instructions for Gemini — actionable, specific, project-agnostic.
_CATEGORY_FIX_HINTS: dict[str, str] = {
    "vite_stale_cache": (
        "FIX (Vite stale cache / 504 Outdated Optimize Dep): Delete the Vite optimizer "
        "cache: `rm -rf node_modules/.vite .vite`. Then restart the dev server. "
        "If the error is on a specific package (e.g. cesium.js, resium.js), add it to "
        "`optimizeDeps.exclude` or `optimizeDeps.include` in vite.config.ts as appropriate."
    ),
    "dynamic_import_fail": (
        "FIX (Dynamic import / lazy chunk failure): The module failed to load at runtime. "
        "Check: 1) The import path is correct and the file exists. 2) The Vite config does "
        "not accidentally exclude the module from bundling. 3) For large packages like Cesium, "
        "verify `vite-plugin-cesium` (or equivalent) is installed and configured in vite.config.ts. "
        "4) If using React.lazy(), wrap the component with an ErrorBoundary."
    ),
    "ws_connection_fail": (
        "FIX (WebSocket connection failure): The app is trying to connect to a WebSocket server "
        "that is not running. Check: 1) If Socket.IO / a backend server is needed, ensure a start "
        "script exists and is running (e.g. `node server.js`). 2) If the URL is hardcoded to "
        "localhost:3000 but the dev server is on a different port, update the WS URL to use "
        "relative paths or window.location.host. 3) If this is HMR noise and not the app's own WS, "
        "it can be ignored."
    ),
    "missing_resource_404": (
        "FIX (Missing resource 404): A file the page needs does not exist at the requested URL. "
        "Check: 1) The asset path in the source code (import, src, href). 2) Whether the file "
        "should be in the public/ folder. 3) Whether the file was accidentally deleted or renamed."
    ),
    "resource_load_fail": (
        "FIX (Resource load failure): A script, stylesheet, image, or WASM file failed to load. "
        "Check the network tab for the exact URL and status code. Common causes: wrong base URL, "
        "missing file in public/, or a CDN URL that is blocked inside the sandbox."
    ),
    "deprecated_meta": (
        "FIX (Deprecated meta tag): Replace `<meta name='apple-mobile-web-app-capable'>` with "
        "`<meta name='mobile-web-app-capable' content='yes'>` in index.html."
    ),
    "deprecated_api": (
        "FIX (Deprecated API): Check the warning message for which API is deprecated and update "
        "the relevant code to use the modern replacement."
    ),
    "js_type_error": (
        "FIX (TypeError): A JavaScript TypeError occurred at runtime. Read the stack trace to "
        "identify the file and line. Common causes: accessing a property on undefined/null, "
        "calling a non-function, or a missing import."
    ),
    "js_reference_error": (
        "FIX (ReferenceError): A variable or function was used before it was defined. "
        "Check the stack trace and ensure all imports and variable declarations are correct."
    ),
    "js_syntax_error": (
        "FIX (SyntaxError): There is a syntax error in the JavaScript source. Run `npx tsc --noEmit` "
        "or check the Vite build log for the exact location."
    ),
    "uncategorised": (
        "FIX (Unknown browser error): Read the full error message and stack trace. "
        "Identify the failing file, fix the root cause and verify the preview loads cleanly."
    ),
}


async def _capture_console_errors(sandbox) -> dict:
    """
    V53: Read collected browser errors from the sandbox.

    Returns a dict with:
      'formatted'   — list[str] for use in _diagnose_and_retry() (one entry per error)
      'categories'  — Counter[category_str] of how many errors per type
      'raw'         — list[dict] of the raw error objects

    Clears the error file after reading.
    """
    try:
        result = await sandbox.exec_command(
            "cat /tmp/console_errors.json 2>/dev/null || echo '[]'",
            timeout=5,
        )
        import json
        errors_raw = json.loads(result.stdout.strip() or "[]")
        if not errors_raw:
            return {"formatted": [], "categories": {}, "raw": []}

        # Clear after reading
        await sandbox.exec_command("echo '[]' > /tmp/console_errors.json", timeout=3)

        # Build category counters and formatted strings
        from collections import Counter
        cat_counts: Counter = Counter()
        formatted = []
        for err in errors_raw:
            msg = err.get("message", "")
            err_type = err.get("type", "unknown")
            category = err.get("category", "uncategorised")
            source = err.get("source", "") or err.get("url", "")
            line = err.get("line", 0)
            stack = err.get("stack", "")
            status = err.get("status", 0)

            cat_counts[category] += 1

            parts = [f"[{err_type}|{category}] {msg}"]
            if source:
                parts.append(f"  at {source}" + (f":{line}" if line else "") + (f" (HTTP {status})" if status else ""))
            if stack:
                parts.append(f"  {stack[:300]}")
            formatted.append("\n".join(parts))

        if formatted:
            logger.warning(
                "🖥️  [Error Capture] %d browser error(s): %s",
                len(formatted),
                dict(cat_counts),
            )
        return {"formatted": formatted, "categories": dict(cat_counts), "raw": errors_raw}

    except Exception as exc:
        logger.debug("🖥️  [Error Capture] Failed to read errors: %s", exc)
        return {"formatted": [], "categories": {}, "raw": []}



def _compress_errors_for_retry(errors: list[str], max_per_error: int = 1500) -> list[str]:
    """V79: Proxy → supervisor.prompt_builder.compress_errors_for_retry"""
    from .prompt_builder import compress_errors_for_retry
    return compress_errors_for_retry(errors, max_per_error)


async def _diagnose_and_retry(
    task_description: str,
    failure_errors: list[str],
    executor,
    local_brain,
    session_mem,
    timeout: int = 450,  # V60: Raised from 300 → 450s — retry prompts are larger (original + errors)
    task_id: str = "",
    state=None,
    silent_timeout: bool = False,  # V60: True when Gemini timed out with zero output
) -> tuple[bool, "TaskResult"]:
    """
    Diagnose a failed task and retry once with enriched context.

    Steps:
      1. Use local_brain.analyze_errors() to diagnose the failure
      2. Build retry prompt: original task + error context + analysis
      3. Retry the task once with the enriched prompt
      4. Return (success, result) — caller decides whether to mark pass/fail

    Called from:
      - Parallel lane failures in _execute_dag_recursive
      - Sequential node failures (before replan)
      - Monitoring loop timeouts
    """
    from .headless_executor import TaskResult

    label = f"[{task_id}] " if task_id else ""

    # ── Fast exit on stop request — don't waste time on diagnosis/retry ──
    if state and getattr(state, 'stop_requested', False):
        logger.info("🛑  %s[Auto-Fix] Stop requested — skipping auto-fix.", label)
        from .headless_executor import TaskResult
        sk = TaskResult(prompt_used=task_description)
        sk.status = "error"
        sk.errors = ["Auto-fix skipped: stop requested"]
        return False, sk

    # ── Step 1: Build error context ──
    # V74: Compress errors to extract actionable info only.
    # Raw errors can be 50K+ chars (full Jest/Vite output).
    # Extract: error name/message, file:line, 5 lines of stack context.
    # CLI still has @. access for deeper investigation.
    compressed = _compress_errors_for_retry(failure_errors[:5])
    error_text = "\n".join(compressed)
    logger.info("🔧  %s[Auto-Fix] Building retry with error context (%d errors, %d chars compressed)", label, len(failure_errors), len(error_text))


    # ── Step 2: Build enriched retry prompt (V60: 3-way branch) ──

    if silent_timeout:
        # Gemini was spawned but returned no output and wrote no files; it likely
        # hung reading a very large context.  Don't mention errors (there are none).
        # Ask it to start immediately with a narrow, specific focus.
        retry_prompt = (
            f"PREVIOUS ATTEMPT TIMED OUT without producing any output.\n\n"
            f"ORIGINAL TASK:\n{task_description}\n\n"
            "INSTRUCTIONS:\n"
            "1. Begin coding IMMEDIATELY — do NOT re-read the entire codebase first.\n"
            "2. Use @filename syntax to read ONLY the specific files you need to change.\n"
            "3. Focus on the smallest targeted change that achieves the goal.\n"
            "4. If the task is very large, implement the most critical part now and note what remains.\n"
        )
    elif failure_errors:
        # Genuine errors returned by Gemini or the build system.
        retry_prompt = (
            f"PREVIOUS ATTEMPT FAILED. Fix the issues and complete the task.\n\n"
            f"ORIGINAL TASK:\n{task_description}\n\n"
            f"ERRORS FROM PREVIOUS ATTEMPT:\n{error_text}\n\n"
            "INSTRUCTIONS:\n"
            "1. Read the errors carefully\n"
            "2. Fix the root cause — do NOT just suppress the error\n"
            "3. Complete the original task successfully\n"
        )
    else:
        # Task exited with non-zero but produced no error messages.
        # Likely a prompt misinterpretation or incomplete write.
        retry_prompt = (
            f"PREVIOUS ATTEMPT FAILED without a specific error message.\n\n"
            f"ORIGINAL TASK:\n{task_description}\n\n"
            "INSTRUCTIONS:\n"
            "1. Use @filename syntax to re-read the relevant source files.\n"
            "2. Ensure you fully understand the existing code before making changes.\n"
            "3. Complete the task — write all required code, do not leave stubs.\n"
        )

    # ── Step 2: Retry once ──
    if state and getattr(state, 'stop_requested', False):
        logger.info("🛑  %s[Auto-Fix] Stop requested after diagnosis — skipping Gemini retry.", label)
        from .headless_executor import TaskResult
        sk = TaskResult(prompt_used=task_description)
        sk.status = "error"
        sk.errors = ["Auto-fix skipped: stop requested"]
        return False, sk

    logger.info(
        "🔧  %s[Auto-Fix→Gemini] Retry prompt (%d chars): %s",
        label, len(retry_prompt), retry_prompt,
    )
    retry_result = await executor.execute_task(retry_prompt, timeout=timeout)

    if retry_result.success or retry_result.status == "partial":
        logger.info(
            "🔧  %s[Auto-Fix] Retry SUCCEEDED (%.1fs, %d files)",
            label, retry_result.duration_s, len(retry_result.files_changed),
        )
        session_mem.record_event("auto_fix_success", f"{label}{task_description}")
        return True, retry_result
    else:
        logger.warning(
            "🔧  %s[Auto-Fix] Retry also failed: %s",
            label, retry_result.errors[:2],
        )
        session_mem.record_event("auto_fix_failed", f"{label}{retry_result.errors[:2]}")
        return False, retry_result


def _extract_tag(description: str) -> str:
    """V79: Proxy → supervisor.prompt_builder.extract_tag"""
    from .prompt_builder import extract_tag
    return extract_tag(description)


async def _decompose_user_instructions(
    instructions: list[str],
    planner,
    project_path: str,
    state=None,
) -> list[dict]:
    """
    V44: Smart instruction decomposition.

    Bundles queued user prompts and sends them to Gemini with the current
    DAG state + file tree. Gemini breaks the instructions into 5-15 atomic
    subtasks with dependencies. Fallback: returns a single task per
    instruction if Gemini fails or returns invalid JSON.

    Returns:
        List of dicts with keys: task_id, description, dependencies
    """
    if not instructions:
        return []

    # Build context: file tree + DAG state
    from pathlib import Path as _Path
    project = _Path(project_path)
    file_tree_lines: list[str] = []
    try:
        for fp in sorted(project.rglob("*")):
            if fp.is_file() and ".git" not in fp.parts and "node_modules" not in fp.parts:
                rel = fp.relative_to(project)
                file_tree_lines.append(str(rel))
                if len(file_tree_lines) >= 80:
                    file_tree_lines.append("... (truncated)")
                    break
    except Exception:
        pass
    file_tree = "\n".join(file_tree_lines) or "(no files found)"

    # DAG state context
    dag_context = ""
    if planner:
        try:
            progress = planner.get_progress()
            existing_tasks = []
            for node in planner._nodes.values():
                existing_tasks.append(
                    f"  {node.task_id} [{node.status}]: {node.description}"
                )
            dag_context = (
                f"Current DAG progress: {progress}\n"
                f"Existing tasks:\n" + "\n".join(existing_tasks[:30])
            )
        except Exception:
            dag_context = "(DAG state unavailable)"

    # Build the decomposition prompt
    bundled = "\n\n".join(
        f"### Instruction {i+1}\n{text}" for i, text in enumerate(instructions)
    )

    # Get task offset for continuous numbering
    _offset = 0
    if planner and hasattr(planner, 'get_task_offset'):
        try:
            _offset = planner.get_task_offset()
        except Exception:
            pass

    prompt = f"""You are a project decomposition expert. Break these user instructions into 5-15 atomic subtasks.

## Project File Tree
{file_tree}

## Current DAG State
{dag_context}

## User Instructions
{bundled}

## Rules
1. Create 5-15 subtasks. Each must be independently executable by a coding agent.
2. Task IDs must be t{_offset + 1}, t{_offset + 2}, etc (continuous from current offset).
3. Include dependencies as a list of task IDs that must complete first.
4. Each task description must be specific: mention files to create/modify, what to implement.
5. Do NOT duplicate work that existing DAG tasks already cover.
6. Tag each task with [FUNC], [UI/UX], or [PERF] based on category.
7. At least 3-5 tasks should focus on UI/UX visual excellence if the instructions involve frontend work.
8. At least 1-2 tasks should focus on Lighthouse performance/accessibility/SEO.
9. Priority order: biggest impact first, refinements last.

Respond ONLY with a JSON array:
```json
[
  {{"task_id": "t{_offset + 1}-FUNC", "description": "[FUNC] ...", "dependencies": []}},
  {{"task_id": "t{_offset + 2}-UIUX", "description": "[UI/UX] ...", "dependencies": ["t{_offset + 1}-FUNC"]}}
]
```
Task ID FORMAT: tX-TAG where TAG is FUNC, UIUX, or PERF.
Examples: t{_offset + 1}-FUNC, t{_offset + 2}-UIUX, t{_offset + 3}-PERF"""

    try:
        from .gemini_advisor import ask_gemini
        response = await ask_gemini(prompt, timeout=120)
        if not response:
            raise ValueError("Empty Gemini response")

        # Extract JSON from response
        import json as _json
        # Try to find JSON array in response
        _start = response.find("[")
        _end = response.rfind("]") + 1
        if _start >= 0 and _end > _start:
            tasks = _json.loads(response[_start:_end])
            if isinstance(tasks, list) and len(tasks) >= 2:
                # Record prompts for persistence
                if planner and hasattr(planner, 'record_prompt'):
                    for text in instructions:
                        planner.record_prompt(text)

                logger.info(
                    "📋  [V44] Decomposed %d instruction(s) into %d subtasks",
                    len(instructions), len(tasks),
                )
                if state:
                    state.record_activity(
                        "task",
                        f"Decomposed {len(instructions)} instruction(s) into {len(tasks)} subtasks",
                    )
                return tasks

        raise ValueError(f"Invalid JSON structure in response")

    except Exception as exc:
        logger.debug("📋  [V44] Instruction decomposition failed: %s — using single tasks", exc)
        # Fallback: one task per instruction
        fallback_tasks = []
        for i, text in enumerate(instructions):
            fallback_tasks.append({
                "task_id": f"t{_offset + i + 1}-FUNC",
                "description": f"[USER] {text}",
                "dependencies": [],
            })
        return fallback_tasks


async def _fix_serial_dependencies(
    planner,
    task_ids: list[str],
    local_brain,
    state=None,
):
    """
    V46: Detect and fix serial dependency chains in a batch of task IDs.

    A serial chain is when every task depends only on the previous one:
      t1 → t2 → t3 → t4 → ...
    This forces sequential execution even when tasks are independent.

    When detected, sends all task descriptions to Ollama to compute
    proper dependencies based on file/feature relationships, then
    rewrites the planner's dependency graph in-place.
    """
    if len(task_ids) < 3:
        return  # Too few tasks to worry about

    # ── Step 1: Detect serial chain pattern ──
    # Only consider pending tasks — no need to send completed ones to LLM
    nodes = [
        planner._nodes[tid] for tid in task_ids
        if tid in planner._nodes and planner._nodes[tid].status in ("pending", "running")
    ]
    if len(nodes) < 3:
        return

    serial_count = 0
    for i, node in enumerate(nodes):
        if i == 0:
            # First node should have no deps (or deps outside this batch)
            batch_deps = [d for d in node.dependencies if d in task_ids]
            if not batch_deps:
                serial_count += 1
        else:
            # Every other node should depend on exactly the previous one
            if node.dependencies == [nodes[i - 1].task_id]:
                serial_count += 1

    # If less than 80% of nodes follow the serial pattern, it's not a serial chain
    serial_ratio = serial_count / len(nodes)
    if serial_ratio < 0.8:
        logger.info(
            "📋  [DepFix] Batch of %d tasks is %.0f%% serial — skipping (threshold 80%%)",
            len(nodes), serial_ratio * 100,
        )
        return

    logger.info(
        "📋  [DepFix] Detected serial chain in %d tasks (%.0f%%) — requesting smart deps from Ollama …",
        len(nodes), serial_ratio * 100,
    )
    if state:
        state.record_activity(
            "system",
            f"Fixing serial dependency chain: {len(nodes)} tasks → sending to Ollama for smart deps",
        )

    # ── Step 2: Build Ollama prompt ──
    task_list = ""
    for node in nodes:
        task_list += f'- {node.task_id}: {node.description}\n'

    prompt = (
        "You are a dependency graph optimizer. These tasks currently have a serial chain "
        "(each depends on the previous one), but many are independent and can run in parallel.\n\n"
        "## Tasks\n"
        f"{task_list}\n"
        "## Rules\n"
        "1. A task should depend on another ONLY if it modifies the SAME FILE or needs "
        "output from that task.\n"
        "2. Tasks editing DIFFERENT files are independent (dependencies: []).\n"
        "3. Use task IDs as given (e.g. \"t120\", \"t121\").\n"
        "4. Return EVERY task with its corrected dependencies.\n\n"
        "Respond with ONLY a JSON array:\n"
        '[{"task_id": "t120", "dependencies": []}, {"task_id": "t121", "dependencies": ["t120"]}]\n'
        "No markdown, no explanation."
    )

    try:
        result = None

        # V46: Try Ollama first (fast, free, no quota cost) — 15s timeout
        try:
            if await local_brain.is_available():
                result = await _asyncio.wait_for(
                    local_brain.ask_json(prompt), timeout=15,
                )
                if isinstance(result, list) and result:
                    logger.info("📋  [DepFix] Got smart deps from Ollama (%d items)", len(result))
        except _asyncio.TimeoutError:
            logger.info("📋  [DepFix] Ollama timed out (15s) — trying Gemini")
        except Exception as ollama_exc:
            logger.debug("📋  [DepFix] Ollama dep-fix failed: %s — trying Gemini", ollama_exc)

        # Fallback to Gemini
        if not isinstance(result, list) or not result:
            try:
                from .gemini_advisor import ask_gemini
                raw = await ask_gemini(prompt, timeout=90)
                if raw:
                    import re as _re
                    cleaned = _re.sub(r"```json?\s*", "", raw)
                    cleaned = _re.sub(r"```\s*", "", cleaned).strip()
                    _start = cleaned.find("[")
                    _end = cleaned.rfind("]") + 1
                    if _start >= 0 and _end > _start:
                        result = json.loads(cleaned[_start:_end])
                        logger.info("📋  [DepFix] Got smart deps from Gemini (%d items)", len(result))
            except Exception as gemini_exc:
                logger.debug("📋  [DepFix] Gemini dep-fix also failed: %s", gemini_exc)

        if not isinstance(result, list) or not result:
            logger.warning("📋  [DepFix] No LLM available for dep fix — keeping serial deps.")
            return

        # ── Step 3: Apply corrected dependencies ──
        valid_ids = set(task_ids)
        changes = 0
        for item in result:
            if not isinstance(item, dict):
                continue
            tid = item.get("task_id", "")
            new_deps = item.get("dependencies", [])
            if tid not in valid_ids:
                continue
            if tid not in planner._nodes:
                continue

            # Only keep deps that exist in the planner
            resolved_deps = [d for d in new_deps if d in planner._nodes]
            node = planner._nodes[tid]
            old_deps = node.dependencies

            if resolved_deps != old_deps:
                node.dependencies = resolved_deps
                changes += 1
                logger.debug(
                    "📋  [DepFix] %s deps: %s → %s",
                    tid, old_deps, resolved_deps,
                )

        if changes > 0:
            planner._save_state()
            logger.info(
                "📋  [DepFix] Updated dependencies for %d/%d tasks — parallel execution enabled.",
                changes, len(nodes),
            )
            if state:
                state.record_activity(
                    "success",
                    f"Smart deps applied: {changes}/{len(nodes)} tasks can now run in parallel",
                )
        else:
            logger.info("📋  [DepFix] Ollama confirmed all deps are correct — no changes needed.")

    except Exception as exc:
        logger.warning("📋  [DepFix] Ollama dep-fix failed: %s — keeping original deps.", exc)


async def _audit_completed_work(
    files_changed: list[str],
    executor,
    local_brain,
    planner,
    session_mem,
    goal: str,
    effective_project: str = "",
    state=None,
    indent: str = "  ",
    phase_mgr=None,
) -> dict | None:
    """
    V41: Post-DAG audit — comprehensive code quality check that creates tasks.

    Runs after DAG completion to compare the original goal against the actual
    implementation. Identifies EVERY gap, missing feature, bug, stub, placeholder,
    and quality issue — then injects them as new DAG tasks for execution.

    IMPORTANT: This function ONLY creates tasks. It NEVER applies fixes directly.
    The tasks are injected into the DAG planner and executed in the normal pool_worker
    pipeline, ensuring full logging, preview sync, and error handling.

    Returns dict with duration_s and tasks_created, or None on skip.
    """
    import time as _time
    import json as _json
    import hashlib as _hashlib

    C = config.ANSI_CYAN
    G = config.ANSI_GREEN
    Y = config.ANSI_YELLOW
    R = config.ANSI_RESET
    B = config.ANSI_BOLD

    def _desc_fp(desc: str) -> str:
        """Stable fingerprint of a task description for cross-cycle dedup."""
        _norm = " ".join(desc.lower().split())
        return _hashlib.md5(_norm.encode()).hexdigest()[:16]

    # V56/V62: Load persistent fingerprint store + full dag_history so
    # audits never re-inject tasks that were completed in ANY prior session.
    _fp_store_path = None
    _done_fps: set[str] = set()   # fingerprints of descriptions already queued/done
    _done_descs: list[str] = []   # human-readable list for Gemini prompt
    if effective_project:
        from pathlib import Path as _Path
        _fp_store_path = _Path(effective_project) / ".ag-supervisor" / "audit_done_fingerprints.json"
        try:
            if _fp_store_path.exists():
                _fp_data = _json.loads(_fp_store_path.read_text(encoding="utf-8"))
                _done_fps = set(_fp_data.get("fingerprints", []))
                _done_descs = list(_fp_data.get("descriptions", []))  # V62: no cap — full history
                logger.info(
                    "🔍  [Audit] Loaded %d completed audit fingerprints from disk.",
                    len(_done_fps),
                )
        except Exception as _fpe:
            logger.debug("🔍  [Audit] Could not load fingerprint store: %s", _fpe)

        # V62: Augment from dag_history.jsonl — captures ALL completed tasks
        # from prior supervisor sessions, even those not in the fingerprint store.
        try:
            _dag_hist_path = _Path(effective_project) / ".ag-supervisor" / "dag_history.jsonl"
            if not _dag_hist_path.exists():
                _dag_hist_path = _Path(effective_project) / "dag_history.jsonl"
            if _dag_hist_path.exists():
                _hist_added = 0
                for _hline in _dag_hist_path.read_text(encoding="utf-8").strip().split("\n"):
                    try:
                        _hentry = _json.loads(_hline)
                        for _ndata in _hentry.get("nodes", {}).values():
                            if _ndata.get("status") == "complete":
                                _hdesc = _ndata.get("description", "")
                                if _hdesc:
                                    _hfp = _desc_fp(_hdesc)
                                    if _hfp not in _done_fps:
                                        _done_fps.add(_hfp)
                                        _done_descs.append(_hdesc[:200])
                                        _hist_added += 1
                    except Exception:
                        continue
                if _hist_added:
                    logger.info(
                        "🔍  [Audit] Augmented with %d completed tasks from dag_history.jsonl (total: %d).",
                        _hist_added, len(_done_descs),
                    )
        except Exception as _dhe:
            logger.debug("🔍  [Audit] dag_history augmentation error: %s", _dhe)

    # V54: No file count cap — paths are tiny (Gemini reads files directly via tools)
    unique_files = list(dict.fromkeys(files_changed))
    if not unique_files:
        return None

    # V56: Vision Planning — if this is a new/fresh project (no VISION.md yet),
    # ask Gemini to think about the IDEAL version of the project before auditing.
    # This prevents Gemini from only fixing what's explicitly in the brief.
    if effective_project:
        from pathlib import Path as _Path
        _vision_path = _Path(effective_project) / "VISION.md"
        # V58: Always write VISION.md on every audit run — not just first run.
        # Continuing projects may not have one yet (path issue on first run),
        # or the existing one may be stale. Refresh unconditionally.
        _vision_exists = _vision_path.exists()
        _vision_action = "refreshing" if _vision_exists else "creating"
        logger.info("🔭  [Vision] %s VISION.md for: %s", _vision_action.capitalize(), effective_project)
        if state:
            state.record_activity("system", f"🔭 Vision Planning: {_vision_action} product north-star for this project…")
            if hasattr(state, 'set_current_operation'):
                state.set_current_operation(f'🔭 Vision Planning — Gemini {_vision_action} VISION.md…')
        _vision_prompt = (
            "You are a world-class senior product architect and lead engineer.\n"
            "You are about to build a project. Read the goal below and think deeply about:\n\n"
            "1. IDEAL FEATURE SET — What are ALL the features a world-class implementation of this product should have?\n"
            "   Include features the brief mentions AND features any excellent implementation of this type of product should have\n"
            "   (e.g. error states, loading states, empty states, auth flows, responsive design, a11y, SEO, performance).\n\n"
            "2. MISSING FROM BRIEF — What critical things has the brief omitted that you'll need to make decisions about?\n"
            "   (e.g. auth strategy, data persistence, mobile breakpoints, animations, dark mode, analytics, error boundaries)\n\n"
            "3. ARCHITECTURE DECISIONS — What is the ideal tech stack and architecture? What patterns should be used?\n\n"
            "4. PRIORITY HIERARCHY — What must be built first (foundation) vs later (polish)?\n"
            "   Use tiers: Foundation → Core Features → UX Polish → Performance → Accessibility\n\n"
            "5. QUALITY BAR — What does \"done\" look like for this specific project?\n"
            "   What would make this Awwwards-worthy? What would make it production-ready?\n\n"
            f"PROJECT GOAL:\n{goal}\n\n"
            "Write a comprehensive VISION.md document with all 5 sections above.\n"
            "Be specific, opinionated, and thorough. This becomes the permanent north star for all work on this project.\n"
            "Do NOT be generic — tailor everything to this specific project type and goal."
        )
        try:
            from .gemini_advisor import ask_gemini
            _vision_result = await ask_gemini(_vision_prompt, timeout=300, use_cache=False)
            if _vision_result and _vision_result.strip():  # V58: Accept any non-empty response
                _vision_path.write_text(_vision_result, encoding="utf-8")
                logger.info("🔭  [Vision] VISION.md written to: %s (%d chars)", _vision_path, len(_vision_result))
                if state:
                    state.record_activity("system", f"🔭 VISION.md written ({len(_vision_result)} chars) — product vision established")
                # Refresh GEMINI.md so the new vision is included in task context
                try:
                    from . import bootstrap
                    bootstrap.bootstrap_workspace(effective_project, goal)
                except Exception as _bex:
                    logger.debug("[Vision] GEMINI.md refresh failed: %s", _bex)
            else:
                logger.warning("🔭  [Vision] Gemini returned empty response — VISION.md NOT written.")
        except Exception as _vision_exc:
            logger.warning("🔭  [Vision] Vision Planning pass failed (non-fatal): %s", _vision_exc)

    # V46: Bail early if shutdown requested
    if state and getattr(state, 'stop_requested', False):
        logger.info("🛑  [Audit] Skipping — shutdown in progress.")
        return None

    logger.info("🔍  [Audit] Starting post-completion audit on %d files …", len(unique_files))
    print(f"\n{indent}{B}{C}🔍 AUDIT: Scanning {len(unique_files)} changed files for remaining work …{R}")

    if state:
        state.record_activity("task", f"Audit: scanning {len(unique_files)} files for remaining tasks")
        if hasattr(state, 'set_current_operation'):
            state.set_current_operation(f'🔍 Deep scan — gathering project files ({len(unique_files)} changed)…')
        try:
            await state.broadcast_state()
        except Exception:
            pass

    start = _time.time()
    file_list = "\n".join(f"  - {f}" for f in unique_files)

    # V40 FIX: Grab the original goal to audit against
    _epic_text_str = planner._epic_text if hasattr(planner, '_epic_text') else "N/A"
    
    _combined_goal_text = f"CLI Goal: {goal}\nEpic Detail: {_epic_text_str}"

    # ── Build audit prompt — Gemini CLI loads files via @./ prefix ──────────
    # V56: --all_files was deprecated in Gemini CLI v0.11.0 (Oct 2025).
    # ask_gemini(all_files=True) now prepends '@./\n\n' to the prompt body,
    # which instructs the CLI's built-in read_many_files tool to load all
    # project files into context. No Python file-reading needed.
    from pathlib import Path
    _proj_path = Path(effective_project) if effective_project else Path(".")

    # Compact completed-task list — includes acceptance_criteria so Gemini can
    # verify each task's stated conditions are actually met in the code.
    _completed_tasks = ""
    if hasattr(planner, '_nodes') and planner._nodes:
        _done_lines = []
        for n in planner._nodes.values():
            _ac = getattr(n, 'acceptance_criteria', '') or ''
            _ac_line = f"    Acceptance criteria: {_ac[:200]}" if _ac else ""
            _done_lines.append(
                f"  - [{n.status.upper()}] {n.task_id}: {n.description}"
                + (f"\n{_ac_line}" if _ac_line else "")
            )
        _completed_tasks = "\n".join(_done_lines)

    # Read lightweight supervisor context files — these are small enough to inline
    # and give Gemini orientation on build state before it reads source files.
    _context_files_content = ""
    for _cf in [
        "PROGRESS.md", "BUILD_ISSUES.md", "CONSOLE_ISSUES.md",
        "PROJECT_STATE.md", "SUPERVISOR_MANDATE.md", "VISION.md",
        "DEEP_ANALYSIS.md",  # V59: pre-DAG deep analysis findings
    ]:
        _cfp = _proj_path / _cf
        if _cfp.exists():
            try:
                _cfc = _cfp.read_text(encoding="utf-8", errors="replace")[:5000]
                _context_files_content += f"\n### {_cf}\n{_cfc}\n"
            except Exception:
                pass

    # V58: Inject all user prompts ever submitted (goal + every instruction note).
    # This is Gemini's definitive reference for what the user actually wants —
    # more specific than the original goal and includes corrections/refinements.
    _user_ctx_block = ""
    try:
        _uprts = []
        if hasattr(planner, 'get_user_prompts'):
            _uprts = planner.get_user_prompts() or []
        elif hasattr(planner, '_user_prompts'):
            _uprts = planner._user_prompts or []
        if _uprts:
            _user_ctx_block = (
                "\n\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
                "USER CONTEXT NOTES (all prompts, instructions & corrections ever submitted):\n"
                "These are the user's own words — prioritise them over the original goal.\n"
                "\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
                + "\n".join(f"  [{i+1}] {p[:300]}" for i, p in enumerate(_uprts[-50:]))
                + "\n"
            )
            logger.info("[Audit] Injected %d user prompt(s) into audit context.", len(_uprts))
    except Exception as _upe:
        logger.debug("[Audit] User prompt injection error (non-fatal): %s", _upe)

    # Build file tree for orientation (paths only, not content — CLI loads content)
    if state and hasattr(state, 'set_current_operation'):
        state.set_current_operation('🔍 Deep scan — preparing audit prompt (CLI will read all files)…')
    _file_tree = ""
    try:
        _skip_dirs = {"node_modules", ".git", "__pycache__", ".ag-supervisor",
                      "dist", ".next", "build", ".cache", ".vite", "coverage"}
        _all_files = sorted(
            str(f.relative_to(_proj_path))
            for f in _proj_path.rglob("*")
            if f.is_file() and not any(skip in f.parts for skip in _skip_dirs)
        )
        _file_tree = "\n".join(f"  - {f}" for f in _all_files)
        logger.info("🔍  [Audit] Prepared file tree (%d files). CLI will read all via --all_files.", len(_all_files))
    except Exception as _fe:
        logger.debug("🔍  [Audit] File tree error (non-fatal): %s", _fe)

    # ── V66: Write bulky context to _AUDIT_CONTEXT.md instead of inlining ──
    # The Gemini CLI loads this via @ file reference, keeping the prompt lean
    # and avoiding the 120K cap that was truncating critical audit data.
    _audit_ctx_path = _proj_path / ".ag-supervisor" / "_AUDIT_CONTEXT.md"
    try:
        _audit_ctx_path.parent.mkdir(parents=True, exist_ok=True)
        _audit_ctx_sections = []
        if _completed_tasks:
            _audit_ctx_sections.append(
                "# COMPLETED DAG TASKS\n"
                "Verify each: check its acceptance criteria is satisfied in the actual code.\n\n"
                f"{_completed_tasks}"
            )
        if _context_files_content:
            _audit_ctx_sections.append(
                "# PROJECT CONTEXT (PROGRESS.md, BUILD_ISSUES.md, etc.)\n"
                f"{_context_files_content}"
            )
        if _file_tree:
            _audit_ctx_sections.append(
                "# FULL PROJECT FILE TREE\n"
                f"{_file_tree}"
            )
        if _already_done_block:
            _audit_ctx_sections.append(_already_done_block)
        _audit_ctx_content = "\n\n---\n\n".join(_audit_ctx_sections)
        _audit_ctx_path.write_text(_audit_ctx_content, encoding="utf-8")
        _audit_ctx_ref = f"@.ag-supervisor/_AUDIT_CONTEXT.md"
        logger.info(
            "🔍  [Audit] Wrote _AUDIT_CONTEXT.md (%d chars) — referenced via @, not inlined.",
            len(_audit_ctx_content),
        )
    except Exception as _ctx_exc:
        logger.debug("🔍  [Audit] Failed to write _AUDIT_CONTEXT.md (falling back to inline): %s", _ctx_exc)
        _audit_ctx_ref = None  # Fall back to inline


    # V54→V67: Build cross-phase scoping block — Gemini can now create tasks
    # from ANY phase where prerequisites are met, maximising concurrent work.
    # Previously restricted to only the current phase, wasting capacity when
    # later-phase tasks had no unmet dependencies.
    _phase_ctx_block = ""
    if phase_mgr and hasattr(phase_mgr, '_plan') and phase_mgr._plan:
        # Sync DAG completion → phase tasks BEFORE building context
        # so the prompt reflects accurate done/pending status.
        try:
            if planner and hasattr(phase_mgr, 'sync_completion_from_dag'):
                phase_mgr.sync_completion_from_dag(planner)
        except Exception:
            pass
        try:
            # Use cross-phase context so audit generates tasks across all phases
            if hasattr(phase_mgr, 'get_cross_phase_context_for_audit'):
                _phase_ctx_block = phase_mgr.get_cross_phase_context_for_audit()
            else:
                # Fallback to old single-phase behavior if method not available
                _ph = phase_mgr.get_current_phase()
                if _ph:
                    _all_ph = phase_mgr._plan.get("phases", [])
                    _cur    = max(1, min(phase_mgr._plan.get("current_phase", 1), len(_all_ph) if _all_ph else 1))
                    _tot    = len(_all_ph) or "?"
                    _ph_tasks     = _ph.get("tasks", [])
                    _done_tasks   = [t for t in _ph_tasks if t.get("status") == "done"]
                    _pending_tasks = [t for t in _ph_tasks if t.get("status") != "done"]
                    _ph_pending_str = "\n".join(
                        f"    - [{t.get('id','')}] {t.get('title', '')}"
                        for t in _pending_tasks
                    ) or "    (all tasks marked done)"
                    _ph_done_str = "\n".join(
                        f"    - [{t.get('id','')}] {t.get('title', '')}"
                        for t in _done_tasks[:20]
                    ) or "    (none marked done yet)"
                    _phase_ctx_block = (
                        f"\n═══════════════════════════════════════════════════════════\n"
                        f"ACTIVE PHASE (SCOPE GUARD — CRITICAL):\n"
                        f"═══════════════════════════════════════════════════════════\n"
                        f"Phase {_cur} of {_tot}: \"{_ph.get('name', '')}\"\n"
                        f"Focus: {_ph.get('focus', '')}\n"
                        f"Exit criteria: {_ph.get('exit_criteria', '')}\n\n"
                        f"PENDING phase tasks (not yet marked done in phase_state.json):\n{_ph_pending_str}\n\n"
                        f"DONE phase tasks (marked done in phase_state.json — spot-check these):\n{_ph_done_str}\n\n"
                        f"🔍 PHASE COMPLETION VERIFICATION (required):\n"
                        f"  For DONE tasks: briefly confirm the implementation exists in code. If clearly missing or broken,\n"
                        f"    treat it as a real gap and create a fix task tagged [PHASE-RECHECK].\n"
                        f"  For PENDING tasks: if the work appears already fully implemented, note it in a task description\n"
                        f"    as '[PHASE-DONE] <task-id> appears complete — verify and mark done'. Do not skip the task.\n\n"
                        f"🚨 SCOPE RULE:\n"
                        f"  - CREATE EXECUTABLE TASKS only for bugs, gaps, and issues within Phase {_cur}'s scope.\n"
                        f"  - For issues belonging to later phases: list them as INFORMATIONAL comments in task descriptions\n"
                        f"    (prefix with [FUTURE]) but do NOT create standalone tasks for future-phase work.\n"
                        f"  - The audit must still catch ALL issues everywhere — just scope the TASK OUTPUT to Phase {_cur}.\n"
                    )
        except Exception:
            pass

    # V56: Build 'ALREADY COMPLETED IN PRIOR AUDIT CYCLES' block for Gemini.
    # This is the key fix: without this, Gemini re-flags the same issues every
    # cycle because it can't distinguish 'stub detected but already queued' from
    # 'stub detected and never touched'. We give it the full list of descriptions
    # already sent so it knows NOT to create those tasks again.
    _already_done_block = ""
    if _done_descs:
        _already_done_block = (
            "\n═══════════════════════════════════════════════════════════\n"
            "ALREADY QUEUED / COMPLETED IN PRIOR AUDIT CYCLES (DO NOT RE-CREATE):\n"
            "═══════════════════════════════════════════════════════════\n"
            "The following tasks were dispatched in a previous audit cycle. The code\n"
            "changes have been applied. DO NOT create new tasks for these items.\n"
            "If you still see issues in the code, check if the fix introduced a regression\n"
            "and describe THAT specific regression instead of repeating the original task.\n\n"
            + "\n".join(f"  ✓ {d[:150]}" for d in _done_descs)
            + "\n"
        )

    # V66: If _AUDIT_CONTEXT.md was written, reference it instead of inlining
    if _audit_ctx_ref:
        _data_block = (
            f"\nRead the audit context file for completed tasks, project context, file tree,\n"
            f"and already-completed audit items:\n{_audit_ctx_ref}\n\n"
        )
    else:
        # V73: Fallback — use @file references for context files instead of inlining.
        # The CLI reads these natively. Only inline the already-done block (small).
        _fallback_refs = []
        for _cf_name in ["PROGRESS.md", "BUILD_ISSUES.md", "CONSOLE_ISSUES.md",
                         "PROJECT_STATE.md", "SUPERVISOR_MANDATE.md", "VISION.md",
                         "DEEP_ANALYSIS.md"]:
            _cfp_fb = _proj_path / _cf_name
            if _cfp_fb.exists():
                _fallback_refs.append(f"@{_cf_name}")
        _fallback_refs_str = " ".join(_fallback_refs)
        _data_block = (
            (f"\nProject context files (loaded via @reference):\n{_fallback_refs_str}\n\n"
             if _fallback_refs_str else "")
            + f"{_already_done_block}"
        )

    scan_prompt = (
        "You are a SENIOR CODE AUDITOR. Your task is a COMPREHENSIVE audit of this project.\n"
        "The Gemini CLI has loaded ALL project files into its context via @./ file expansion\n"
        "(v0.32.1+ canonical format). Read the actual loaded file contents to do your analysis.\n"
        "Do NOT assume or guess — every finding must be grounded in what you actually see.\n"
        f"{_phase_ctx_block}\n"
        "═══════════════════════════════════════════════════════════\n"
        "ORIGINAL GOAL (what was requested):\n"
        "═══════════════════════════════════════════════════════════\n"
        f"{goal}\n\n"
        f"{_user_ctx_block}"
        f"{_data_block}"
        "Review each file in your loaded context. Check for:\n"
        "  - Stubs/placeholders (TODO, pass, empty bodies, hardcoded values)\n"
        "  - Missing imports or broken cross-file references\n"
        "  - Logic errors, incorrect data flow, missing edge cases\n"
        "  - Missing wiring (component exists but never rendered/called)\n"
        "  - Dead code, unused exports, orphaned files\n"
        "  - TypeScript/lint errors visible in the source\n\n"
        "═══════════════════════════════════════════════════════════\n"
        "YOUR AUDIT MANDATE — VERIFY, DON'T ASSUME:\n"
        "═══════════════════════════════════════════════════════════\n"
        "⚠️  A task marked 'complete' may still be wrong, incomplete, or broken. Read the actual code.\n"
        "⚠️  VERIFY EACH COMPLETED TASK'S ACCEPTANCE CRITERIA against what is actually in the files.\n"
        "    If the criteria say 'tsc passes' — check the code. If they say 'X component renders' — find it.\n"
        "    Mark a task as truly done ONLY if its acceptance criteria are fully satisfied in the source.\n"
        "Compare EVERY requirement in the ORIGINAL GOAL and USER CONTEXT NOTES against what you find.\n\n"
        "CREATE TASKS IN 3 CATEGORIES (tag each with [FUNC], [UI/UX], or [PERF]):\n\n"
        "CATEGORY A — FUNCTIONALITY [FUNC]:\n"
        "  - Features MISSING entirely or implemented as stubs/placeholders\n"
        "  - Features that are BROKEN or have obvious bugs\n"
        "  - Features whose acceptance criteria are NOT fully met in the code\n"
        "  - Missing integration between systems that should work together\n"
        "  - Missing imports, broken references, undefined variables\n"
        "  - Dead code or unused imports that should be removed\n"
        "  - Missing error handling or edge cases\n\n"
        "CATEGORY B — STYLING & UI/UX [UI/UX]:\n"
        "  - Layout/spacing issues, typography hierarchy, color consistency\n"
        "  - Missing micro-interactions, hover effects, scroll animations\n"
        "  - Missing custom SVG icons (no generic icon libraries)\n"
        "  - Missing responsive breakpoints or fluid sizing\n"
        "  - Missing dark/light mode, empty/error/loading states\n"
        "  - Missing 2026 CSS: scroll-driven animations, @starting-style,\n"
        "    OKLCH colors, container queries, anchor positioning\n"
        "  - Component polish: glass-morphism, layered shadows, skeleton screens\n"
        "  - The site must look like a 2026 Awwwards Site of the Year winner\n\n"
        "CATEGORY C — LIGHTHOUSE & PERFORMANCE [PERF]:\n"
        "  - Performance: FCP<1.8s, LCP<2.5s, TBT=0, CLS=0, SI<3.4s\n"
        "  - Render-blocking resources, unused CSS/JS, code splitting\n"
        "  - Image optimization: WebP/AVIF, explicit width/height, lazy load\n"
        "  - Accessibility WCAG 2.1 AA: button names, heading order, contrast,\n"
        "    alt text, captions, keyboard nav, semantic landmarks, ARIA\n"
        "  - Best Practices: CSP, HSTS, COOP, Trusted Types, source maps\n"
        "  - SEO: meta description, valid robots.txt (not HTML!), structured data,\n"
        "    canonical URL, crawlable links\n"
        "  - HTTP/2+, cache headers, font-display: swap, preconnect origins\n\n"
        "Each task MUST be DETAILED and SPECIFIC:\n"
        "  - Name the EXACT files to create or modify\n"
        "  - Describe WHAT to implement and WHY it's needed\n"
        "  - Describe the EXPECTED BEHAVIOR after the fix\n\n"
        "PRIORITY ORDER (biggest impact first):\n"
        "  Tier 1: Critical broken functionality, missing core features, failed acceptance criteria\n"
        "  Tier 2: Major UI components, key user flows\n"
        "  Tier 3: Animations, responsive polish, secondary features\n"
        "  Tier 4: Accessibility sweep, micro-interactions, edge cases\n"
        "  Tier 5: Lighthouse metrics, SEO, caching, performance\n\n"
        "Respond with a JSON array of tasks:\n"
        '[{"id": "1", "description": "[FUNC] <detailed: what to build/fix, which files, why>", '
        '"acceptance_criteria": "<specific verifiable conditions: e.g. tsc passes, component renders, file exists>", '
        '"dependencies": []}]\n'
        "Use sequential numeric IDs: 1, 2, 3, etc.\n"
        "Set \"dependencies\" to task IDs that must finish first (e.g. [\"1\",\"2\"]), or [] if independent.\n"
        "acceptance_criteria MUST be specific and testable — not vague. Examples:\n"
        "  GOOD: 'tsc --noEmit passes, Button.tsx exported from index.ts, onClick fires'\n"
        "  BAD: 'works correctly' or 'looks good'\n"
        "Tasks editing the same file should depend on each other.\n\n"
        "⚠️  TASK COUNT MANDATE — BE EXHAUSTIVE ACROSS ALL PHASES:\n"
        "  This applies whether this is the FIRST audit or the 10th. It applies whether this is\n"
        "  a brand-new project or one you are continuing. Every DAG phase leaves gaps. Find them.\n"
        "  - For any non-trivial project with real features: aim for 50-100 tasks.\n"
        "  - Cover ALL phases with PENDING work — not just the active phase.\n"
        "    If Phase 2 has 5 pending tasks and Phase 6 has 12, you need tasks for ALL 17.\n"
        "  - Continuing projects often have MORE gaps, not fewer — early tasks are building blocks,\n"
        "    later tasks add depth, polish, edge cases, accessibility, and production-readiness.\n"
        "  - If you are creating fewer than 20 tasks, you MUST be confident the project is\n"
        "    genuinely near-complete. Add a JSON \"confidence_justification\" field as the FIRST\n"
        "    element of the array explaining exactly why:\n"
        "    e.g. {\"confidence_justification\": \"Only 8 tasks: all features working, tsc clean, a11y verified, only minor polish left\"}\n"
        "  - Do NOT stop at 15-25 tasks for any real project — that almost always means missed gaps.\n"
        "  - Every unchecked acceptance criterion = 1+ tasks.\n"
        "  - Every stub, TODO, placeholder, broken reference = 1+ tasks.\n"
        "  - Every UI/UX gap, a11y issue, PERF issue = 1+ tasks.\n"
        "  - Every pending phase task listed above without a corresponding DAG node = 1+ tasks.\n"
        "  - Every missing error state, loading state, empty state = 1+ tasks.\n"
        "  - The absolute maximum is 100 tasks per audit run.\n"
        "IMPORTANT: Respond with ONLY the JSON array. No markdown, no explanation."
    )

    tasks_created = 0
    _injected_ids = []
    try:
        logger.info(
            "🔍  [Audit→Gemini] Deep scan prompt (%d chars)",
            len(scan_prompt),
        )
        if state:
            state.record_activity("llm_prompt", "Audit: deep scan via Gemini", scan_prompt)

        # Use Gemini CLI for the deep scan (large context window)
        scan_result = None
        raw_scan = None  # Prevent UnboundLocalError if ask_gemini() raises
        _audit_failed = False
        try:
            from .gemini_advisor import ask_gemini

            # V55: Prompt size guard — only trim if the prompt is extremely large.
            # The audit prompt no longer inlines file contents (Gemini reads via tools).
            # V73: Uses the global config cap (1M chars) — Gemini has 1M+ token context.
            _MAX_AUDIT_PROMPT = config.PROMPT_SIZE_MAX_CHARS
            if len(scan_prompt) > _MAX_AUDIT_PROMPT:
                logger.warning(
                    "🔍  [Audit] Prompt still large (%d chars) — trimming to fit %dk cap",
                    len(scan_prompt), _MAX_AUDIT_PROMPT // 1000,
                )
                scan_prompt = scan_prompt[:_MAX_AUDIT_PROMPT] + "\n[... trimmed to fit context window ...]"
                logger.info("🔍  [Audit] Audit prompt capped at %d chars.", len(scan_prompt))

            # Use a long timeout — audit prompts are 4-10x larger than regular tasks
            # and require deep multi-file reasoning. 600s gives Gemini room to think.
            #
            # V55: Launch a background ticker so the user sees elapsed time while
            # Gemini is thinking. Updates every 15s via set_current_operation.
            _scan_start_ts = _time.time()
            _ticker_stop = False

            async def _scan_ticker():
                import asyncio as _aio
                _dots = ['', '.', '..', '...']
                _dot_i = 0
                while not _ticker_stop:
                    _elapsed = int(_time.time() - _scan_start_ts)
                    _min = _elapsed // 60
                    _sec = _elapsed % 60
                    _elapsed_str = f"{_min}m {_sec}s" if _min else f"{_elapsed}s"
                    _d = _dots[_dot_i % 4]
                    _dot_i += 1
                    if state and hasattr(state, 'set_current_operation'):
                        state.set_current_operation(
                            f'🔍 Deep scan running{_d} ({_elapsed_str} elapsed — Gemini reading your codebase)'
                        )
                    try:
                        await _aio.sleep(15)
                    except _aio.CancelledError:
                        break

            _ticker_task = __import__('asyncio').ensure_future(_scan_ticker())
            if state:
                state.record_activity("system", f"🔍 Deep scan started — Gemini auditing {len(_all_files if '_all_files' in dir() else unique_files)} files")

            raw_scan = await ask_gemini(
                scan_prompt,
                timeout=600,
                use_cache=False,
                all_files=True,
                # V60 FIX: cwd MUST be the project path so @./ expansion reads
                # the target project's files, not the supervisor directory.
                cwd=effective_project or None,
                model=config.GEMINI_FALLBACK_MODEL,  # V73: audits always use pro
            )

            # V73: Track budget/quota for ask_gemini calls (executor tracks
            # its own, but ask_gemini goes direct — was previously invisible).
            try:
                from .retry_policy import get_daily_budget as _adb, get_quota_probe as _aqp
                _adb().record_request()
                _aqp().record_usage()
            except Exception:
                pass

            if raw_scan:
                logger.info(
                    "🔍  [Audit←Gemini] Response (%d chars): %.500s…",
                    len(raw_scan), raw_scan,
                )
                # Stop the progress ticker now that we have a response
                _ticker_stop = True
                _ticker_task.cancel()
                _elapsed_total = int(_time.time() - _scan_start_ts)
                if state and hasattr(state, 'set_current_operation'):
                    state.set_current_operation(f'🔍 Deep scan complete ({_elapsed_total}s) — parsing tasks…')
                if state:
                    state.record_activity("system", f"🔍 Deep scan response received after {_elapsed_total}s — parsing task list")
                import re as _re
                cleaned = _re.sub(r'```json?\s*', '', raw_scan)
                cleaned = _re.sub(r'```\s*', '', cleaned).strip()

                def _extract_json_array(text: str):
                    """Three-strategy extractor resilient to trailing text / explanations."""
                    # Strategy 1: direct parse (works when response is clean JSON)
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        pass

                    # Strategy 2: bracket-counting — find the first '[' and walk
                    # forward counting open/close brackets to locate the exact end
                    # of the array. Handles Gemini appending trailing text.
                    start = text.find('[')
                    if start != -1:
                        depth, i = 0, start
                        in_str, esc = False, False
                        while i < len(text):
                            c = text[i]
                            if esc:
                                esc = False
                            elif c == '\\' and in_str:
                                esc = True
                            elif c == '"':
                                in_str = not in_str
                            elif not in_str:
                                if c == '[': depth += 1
                                elif c == ']':
                                    depth -= 1
                                    if depth == 0:
                                        try:
                                            return json.loads(text[start:i+1])
                                        except json.JSONDecodeError:
                                            break
                            i += 1

                    # Strategy 3: reconstruct from individual task objects
                    obj_pat = _re.compile(
                        r'\{\s*"(?:id|task_id|description)"\s*:.*?\}',
                        _re.DOTALL
                    )
                    hits = obj_pat.findall(text)
                    if hits:
                        try:
                            return json.loads('[' + ','.join(hits) + ']')
                        except json.JSONDecodeError:
                            pass

                    return None

                scan_result = _extract_json_array(cleaned)
                if scan_result is None:
                    logger.warning(
                        "🔍  [Audit] Could not extract JSON array from Gemini "
                        "response (%d chars) — all 3 strategies failed",
                        len(cleaned),
                    )

        except Exception as _scan_exc:
            logger.warning("🔍  [Audit] Gemini deep scan failed: %s", _scan_exc)
            _audit_failed = True

        # If the audit API call failed entirely (raw_scan never populated),
        # return None so the caller knows this was an error — not 'no tasks found'.
        # Returning None allows the outer loop to retry or continue rather than
        # treating timeout/error as a successful clean audit.
        if _audit_failed and raw_scan is None:
            logger.warning(
                "🔍  [Audit] Deep scan error: raw_scan not populated — "
                "returning None (audit error, not clean pass)"
            )
            elapsed = _time.time() - start
            logger.info("🔍  [Audit] Complete: %.1fs, scan errored (not a clean pass).", elapsed)
            if state:
                state.record_activity("system", "⚠️ Audit scan failed (Gemini timeout) — will retry")
                try:
                    await state.broadcast_state()
                except Exception:
                    pass
            return None  # None = error, 0 = genuinely no tasks

        # ── Reformat cascade ──────────────────────────────────────────────────
        # If JSON extraction failed but we have the raw Gemini text (which contains
        # the task data — just not in a clean format), pass that text to a smaller
        # reformat step rather than regenerating from scratch.
        #
        # Tier A: Ollama  — short reformat prompt (raw_scan trimmed to 4000 chars)
        # Tier B: Gemini  — if Ollama unavailable or returns garbage
        #
        # This is much cheaper than a full re-scan and preserves the task content.
        _REFORMAT_INSTRUCTION = (
            "The text below contains a list of software development tasks. "
            "Extract ALL tasks and return them as a SINGLE valid JSON array. "
            "Each task object MUST have exactly these three keys:\n"
            "  \"id\"           — the EXACT id value from the source (do NOT renumber or change it)\n"
            "  \"description\"  — the complete task text, unchanged word-for-word\n"
            "  \"dependencies\" — JSON array of id strings this task depends on "
            "(e.g. [\"1\",\"3\"]), or [] if independent. Preserve exactly as stated in source.\n\n"
            "RULES:\n"
            "- Preserve every task from the source — do NOT omit, merge, summarize, or reorder\n"
            "- Preserve ALL id values EXACTLY as they appear — never renumber\n"
            "- Preserve dependency relationships exactly as stated in the source\n"
            "- Output ONLY the JSON array — no markdown fences, no explanation, no trailing text\n"
            "- The array must start with '[' and end with ']'\n\n"
            "EXACT OUTPUT FORMAT EXAMPLE (ids come from source, not reassigned):\n"
            '[\n'
            '  {"id":"1","description":"[FUNC] Create ErrorBoundary component","dependencies":[]},\n'
            '  {"id":"2","description":"[PERF] Update BIMViewer cleanup logic","dependencies":[]},\n'
            '  {"id":"3","description":"[TEST] Add integration tests for map","dependencies":["1","2"]}\n'
            ']\n\n'
            "SOURCE TEXT:\n"
        )



        if scan_result is None and raw_scan:
            _reformat_ok = False

            # Tier A: LiteBrain (Gemini Lite → Ollama) — 1M context, no truncation
            if await local_brain.is_available():
                _lite_reformat = _REFORMAT_INSTRUCTION + raw_scan
                logger.info(
                    "🔍  [Audit] Parse failed — asking LiteBrain to reformat Gemini response (%d chars) …",
                    len(_lite_reformat),
                )
                try:
                    _lite_raw = await local_brain.ask_json(_lite_reformat)
                    if isinstance(_lite_raw, list) and _lite_raw:
                        scan_result = _lite_raw
                        _reformat_ok = True
                        logger.info(
                            "🔍  [Audit] LiteBrain reformat succeeded: %d task(s)", len(scan_result)
                        )
                    else:
                        logger.info(
                            "🔍  [Audit] LiteBrain reformat returned non-list — escalating to Gemini reformat"
                        )
                except Exception as _lite_exc:
                    logger.warning(
                        "🔍  [Audit] LiteBrain reformat failed: %s — escalating to Gemini reformat", _lite_exc
                    )

            # Tier B: Gemini — reformat using its own raw output as source
            if not _reformat_ok and scan_result is None:
                logger.info("🔍  [Audit] Gemini reformat of its own response …")
                try:
                    from .gemini_advisor import ask_gemini as _ask_gemini_reformat
                    _gemini_reformat_prompt = _REFORMAT_INSTRUCTION + raw_scan
                    _reformat_raw = await _ask_gemini_reformat(_gemini_reformat_prompt, timeout=120)
                    if _reformat_raw:
                        scan_result = _extract_json_array(_reformat_raw)
                        if scan_result is not None:
                            logger.info(
                                "🔍  [Audit] Gemini reformat succeeded: %d task(s)", len(scan_result)
                            )
                        else:
                            logger.warning(
                                "🔍  [Audit] Gemini reformat also failed — audit will be skipped this cycle"
                            )
                except Exception as _reformat_exc:
                    logger.warning("🔍  [Audit] Gemini reformat failed: %s", _reformat_exc)


        if state:
            state.record_activity("llm_response", "Audit: scan result", str(scan_result))

        if isinstance(scan_result, list) and scan_result:
            # Deduplicate against existing planner node IDs only
            existing_ids = planner.get_all_task_ids()
            # V45: Use continuous tX numbering for audit tasks
            _audit_offset = getattr(planner, '_offset', len(existing_ids))

            # V46: Map Gemini's raw numeric IDs to injected tX-TAG IDs
            # so dependencies can be resolved correctly.
            _id_map: dict[str, str] = {}
            _injected_phase_items: list[dict] = []  # for phase_manager sync

            for issue in scan_result:
                if not isinstance(issue, dict):
                    continue
                desc = issue.get("description", "")
                if not desc:
                    continue

                # [PHASE-DONE] auto-mark: Gemini detected a phase task is already
                # complete in the code. Mark it done in the phase manager instead
                # of creating a DAG task.
                if "[PHASE-DONE]" in desc and phase_mgr:
                    import re as _re_pd
                    _pd_match = _re_pd.search(r'\[PHASE-DONE\]\s*(\S+)', desc)
                    if _pd_match:
                        _pd_task_id = _pd_match.group(1)
                        try:
                            _ph = phase_mgr.get_current_phase()
                            if _ph:
                                for _pt in _ph.get("tasks", []):
                                    if _pt.get("id") == _pd_task_id and _pt.get("status") != "done":
                                        _pt["status"] = "done"
                                        _pt.setdefault("notes", []).append(
                                            "Auto-marked done by audit: code inspection confirmed implementation"
                                        )
                                        phase_mgr._save_plan()
                                        logger.info(
                                            "🔍  [Audit] PHASE-DONE: auto-marked %s as done", _pd_task_id
                                        )
                                        break
                        except Exception:
                            pass
                    continue  # Don't create a DAG task for this

                # V45: Generate continuous tX-TAG IDs instead of audit-fix-<name>
                _audit_offset += 1
                _tag = _extract_tag(desc)
                full_id = f"t{_audit_offset}-{_tag}"
                # Skip if this tX-TAG ID somehow already exists
                if full_id in existing_ids:
                    continue

                # V46: Use Gemini's declared dependencies if available.
                # Previously we always chained linearly ([_prev_audit_id]),
                # preventing parallel execution of independent audit fixes.
                # Map Gemini's numeric IDs ("1", "2") to our tX-TAG format.
                _gemini_deps = issue.get("dependencies", [])
                if _gemini_deps:
                    # Convert Gemini's raw IDs to actual injected task IDs
                    _deps = []
                    for gd in _gemini_deps:
                        gdstr = str(gd)
                        # Look up which tX-TAG ID maps to this Gemini ID
                        if gdstr in _id_map:
                            _deps.append(_id_map[gdstr])
                else:
                    _deps = []  # Independent — can run in parallel

                # V56: Fingerprint-based dedup — checks both in-process nodes
                # AND the cross-cycle persistent store so audit cycle 2, 3, N
                # don't re-inject tasks already done in cycle 1.
                _audit_desc = f"[Audit Fix] {desc}"
                _fp = _desc_fp(_audit_desc)
                if _fp in _done_fps:
                    logger.debug("🔍  [Audit] Skipping (fingerprint match, already done): %s", desc[:60])
                    continue
                # Also check current planner nodes as a belt-and-suspenders guard
                _existing_descs = {_desc_fp(n.description) for n in planner._nodes.values()
                                   if n.status in ('pending', 'running', 'complete', 'failed')}
                if _fp in _existing_descs:
                    logger.debug("🔍  [Audit] Skipping (planner match): %s", desc[:60])
                    continue

                injected = planner.inject_task(
                    task_id=full_id,
                    description=_audit_desc,
                    dependencies=_deps,
                )
                if injected:
                    tasks_created += 1
                    _injected_ids.append(full_id)
                    _injected_phase_items.append({"id": full_id, "description": _audit_desc})
                    # Add to in-memory fingerprint set so within-cycle dedup still works
                    _done_fps.add(_fp)
                    _done_descs.append(desc[:200])
                    # Map Gemini's raw numeric ID to our tX-TAG ID for dep resolution
                    _raw_id = str(issue.get("id", _audit_offset))
                    _id_map[_raw_id] = full_id
                    # Keep planner offset in sync
                    planner._offset = _audit_offset
                    logger.info(
                        "🔍  [Audit] Injected DAG task %s (deps=%s): %s",
                        full_id, _deps, desc[:60],
                    )
                    print(f"{indent}  {Y}📋 Audit task queued: {desc[:70]}{R}")

                else:
                    logger.warning("🔍  [Audit] Failed to inject %s (duplicate or cycle)", full_id)

            if tasks_created > 0:
                print(f"{indent}  {G}✓ Audit queued {tasks_created} follow-on tasks for DAG execution{R}")
                # V56: Persist fingerprint store to disk immediately so future
                # audit cycles don't re-inject these same tasks.
                if _fp_store_path is not None:
                    try:
                        _fp_store_path.parent.mkdir(parents=True, exist_ok=True)
                        _fp_store_path.write_text(
                            _json.dumps({
                                "fingerprints": list(_done_fps),
                                "descriptions": _done_descs,
                                "updated_at": _time.time(),
                            }, indent=2),
                            encoding="utf-8",
                        )
                        logger.info(
                            "🔍  [Audit] Saved %d fingerprints to disk for future dedup.",
                            len(_done_fps),
                        )
                    except Exception as _fse:
                        logger.debug("🔍  [Audit] Could not save fingerprint store: %s", _fse)
                if state:
                    state.record_activity(
                        "task",
                        f"Audit: queued {tasks_created} follow-on fix tasks into DAG",
                    )
                    # Broadcast immediately so Graph tab shows new audit nodes
                    _dag_progress["active"] = True
                    await _update_dag_progress(planner, 0, state=state)
                # V55: Update phase_state.json so Phases panel shows audit-discovered work
                if planner and hasattr(planner, '_phase_manager') and planner._phase_manager:
                    planner._phase_manager.record_audit_tasks(_injected_phase_items)
                elif phase_mgr is not None:
                    phase_mgr.record_audit_tasks(_injected_phase_items)
                session_mem.record_event(
                    "audit_tasks_injected",
                    f"{tasks_created} quality fix tasks queued by post-DAG audit",
                )
                # V46: Fix serial dependency chains — runs in background
                if _injected_ids and len(_injected_ids) >= 3:
                    async def _bg_audit_dep_fix():
                        try:
                            await _fix_serial_dependencies(
                                planner, _injected_ids, local_brain, state=state,
                            )
                            if state:
                                await _update_dag_progress(planner, 0, state=state)
                        except Exception as exc:
                            logger.debug("📋  [DepFix] Background audit dep fix error: %s", exc)
                    asyncio.create_task(_bg_audit_dep_fix())

                # V67: Phase-to-DAG gap fill (post-audit) — same logic as post-decomposition.
                # After audit tasks are injected, check if any pending phase tasks STILL
                # don't have corresponding DAG nodes and inject them.
                if phase_mgr and hasattr(phase_mgr, 'get_all_pending_phase_tasks'):
                    try:
                        _remaining_phase_tasks = phase_mgr.get_all_pending_phase_tasks()
                        _audit_gap_filled = 0
                        for _rpt in _remaining_phase_tasks:
                            _rpt_title = _rpt.get("title", "").strip()
                            if not _rpt_title:
                                continue
                            _rpt_norm = _rpt_title.lower()
                            for _pfx in ('[func]', '[ui/ux]', '[perf]', '[ui]', '[data]',
                                         '[err]', '[a11y]', '[sec]', '[qa]'):
                                _rpt_norm = _rpt_norm.replace(_pfx, '')
                            _rpt_words = [w for w in _rpt_norm.split() if len(w) > 2]
                            if not _rpt_words:
                                continue

                            _rpt_covered = False
                            for _dn in planner._nodes.values():
                                _dn_norm = _dn.description.lower()
                                _mc = sum(1 for w in _rpt_words if w in _dn_norm)
                                if _mc / max(len(_rpt_words), 1) >= 0.5:
                                    _rpt_covered = True
                                    break

                            if not _rpt_covered:
                                _rpt_phase = _rpt.get("phase_id", "?")
                                _rpt_id = _rpt.get("task_id", "")
                                _rpt_inj = planner.inject_task(
                                    task_id=f"phase-{_rpt_id}" if _rpt_id else f"phase-audit-gap-{_audit_gap_filled}",
                                    description=f"[Phase {_rpt_phase}] {_rpt_title}",
                                    dependencies=[],
                                )
                                if _rpt_inj:
                                    _audit_gap_filled += 1
                        if _audit_gap_filled > 0:
                            logger.info(
                                "📋  [GapFill] Post-audit: injected %d more uncovered phase tasks.",
                                _audit_gap_filled,
                            )
                    except Exception:
                        pass
        else:
            logger.info("🔍  [Audit] Deep scan found no additional issues.")
            if state:
                state.record_activity("success", "Audit: no additional tasks needed — code matches goal")

    except Exception as exc:
        logger.warning("🔍  [Audit] Deep scan error: %s", exc)
        # Make sure ticker is stopped on error
        try:
            _ticker_stop = True
            _ticker_task.cancel()
        except NameError:
            pass
        if state:
            state.record_activity("warning", f"Audit deep scan error: {exc}")
            if hasattr(state, 'set_current_operation'):
                state.set_current_operation("")

    duration = _time.time() - start
    logger.info(
        "🔍  [Audit] Complete: %.1fs, %d tasks created.",
        duration, tasks_created,
    )
    print(f"{indent}{G}✓ Audit complete ({duration:.1f}s, {tasks_created} tasks created){R}\n")
    # Clear operation status label now we're done
    if state and hasattr(state, 'set_current_operation'):
        state.set_current_operation("")

    return {
        "duration_s": duration,
        "tasks_created": tasks_created,
        "injected_ids": _injected_ids,
    }


async def _execute_dag_recursive(
    goal: str,
    local_brain,
    executor,
    session_mem,
    effective_project: str,
    depth: int = 0,
    max_depth: int = 3,
    state=None,
    sandbox=None,
    tools=None,
    project_path: str = "",
    phase_mgr=None,  # V54: PhaseManager instance, or None to skip phase tracking
):
    """
    Recursively decompose a complex goal into a DAG of atomic tasks
    and execute them — with parallel lanes for independent branches.

    V38.1 Improvements:
      - Parallel execution: independent DAG branches run concurrently
        via asyncio.gather() using get_parallel_batch()
      - Adaptive timeouts: each chunk gets a timeout proportional to
        its estimated complexity (via Ollama classification)
      - Cross-chunk coherence: lint check gate after file-changing chunks
      - DAG progress: updates _dag_progress dict for UI consumption

    Multi-layer decomposition:
      - depth 0: The original goal → DAG of ~5-15 chunks
      - depth 1+: If a chunk is still complex, sub-decompose it
      - max_depth: Stop recursing and execute directly

    Returns:
        TaskResult — synthetic result aggregating all chunks.
    """
    import asyncio as _asyncio
    from .temporal_planner import TemporalPlanner
    from .headless_executor import TaskResult

    global _dag_progress  # V41: Moved to function top — used by audit re-kick

    indent = "  " * (depth + 1)
    C = config.ANSI_CYAN
    G = config.ANSI_GREEN
    Y = config.ANSI_YELLOW
    R = config.ANSI_RESET
    RED = config.ANSI_RED
    B = config.ANSI_BOLD
    M = config.ANSI_MAGENTA

    # Aggregate result
    agg_result = TaskResult(prompt_used=goal)
    agg_result.status = "success"
    total_duration = 0.0
    all_files_changed = []
    chunk_errors = []

    max_workers = getattr(config, "MAX_CONCURRENT_WORKERS", 3)
    # V40: Dynamic worker count from daily budget tracker (boost / throttle)
    try:
        from .retry_policy import get_daily_budget
        max_workers = get_daily_budget().get_effective_workers()
    except Exception:
        pass

    # V51: Bail immediately if stop was already requested — don't start
    # build health checks, decomposition, or any new work.
    if state and getattr(state, 'stop_requested', False):
        logger.info("🛑  [DAG] Stop already requested — skipping execution entirely.")
        return TaskResult(status="stopped", output="Stopped before execution")

    # V76: Initialize TaskIntelligence for result recording.
    _task_intel = None
    try:
        from .task_intelligence import TaskIntelligence as _TI
        _task_intel = _TI(project_path or effective_project)
    except Exception:
        pass  # Non-fatal

    # ── Initialize planner ──
    # V41: Reuse existing planner from state if available (e.g. on resume_dag re-entry).
    # Previously a new planner was always created, discarding in-memory completed state.
    planner = getattr(state, 'planner', None) if state else None
    # V46 FIX: Check for pending/running/failed work, not just non-empty nodes.
    # V44's clear_state() preserves completed nodes, making `not _nodes`
    # always False after 1st DAG — blocking fresh decomposition on 2nd+ runs.
    _has_active_work = False
    if planner and planner._nodes:
        _has_active_work = any(
            n.status in ("pending", "running", "failed")
            for n in planner._nodes.values()
        )
    if planner is None or not _has_active_work:
        planner = TemporalPlanner.from_brain(
            local_brain if await local_brain.is_available() else None,
            effective_project,
        )

    # ── V51: Fresh Audit — user requested re-scan of current codebase ──
    # Clears old pending tasks and forces a fresh decomposition so the
    # system evaluates the code as-is (after manual edits) instead of
    # blindly resuming stale tasks from a previous session.
    if depth == 0 and state and getattr(state, 'fresh_audit', False):
        logger.info("🔍  [Fresh Audit] User requested fresh audit — discarding old DAG.")
        print(f"{indent}{C}🔍 Fresh Audit: discarding old DAG — will re-scan current codebase{R}")
        if state:
            state.record_activity("system", "Fresh Audit: discarding old DAG per user request")
        # Preserve history before clearing
        if planner._nodes:
            planner.save_history()
        planner.clear_state()
        planner = TemporalPlanner.from_brain(
            local_brain if await local_brain.is_available() else None,
            effective_project,
        )
        state.fresh_audit = False  # One-shot — don't repeat on re-entry

        # V51: Immediately update DAG progress + checkpoint so the UI
        # reflects the cancellation and any restart shows correct counts.
        await _update_dag_progress(planner, depth, state=state)
        try:
            import json as _json
            _cp_dir = Path(effective_project) / ".ag-supervisor"
            _cp_dir.mkdir(parents=True, exist_ok=True)
            _cp_data = {
                "goal": state.goal or "",
                "project_path": str(effective_project),
                "timestamp": __import__('time').time(),
                "status": "fresh_audit",
                "dag_completed": _dag_progress.get("completed", 0),
                "dag_total": _dag_progress.get("total", 0),
                "dag_pending": _dag_progress.get("pending", 0),
                "dag_failed": _dag_progress.get("failed", 0),
                "dag_cancelled": _dag_progress.get("cancelled", 0),
                "dag_nodes": _dag_progress.get("nodes", []),
            }
            (_cp_dir / "checkpoint.json").write_text(
                _json.dumps(_cp_data, indent=2), encoding="utf-8"
            )
            logger.info("🔍  [Fresh Audit] Checkpoint updated — old DAG cancelled.")
        except Exception:
            pass

    # ── Try to resume from persisted state (crash recovery) ──
    # Only load from disk if the planner has no active in-memory work.
    elif depth == 0 and not _has_active_work and planner.load_state():
        progress = planner.get_progress()
        completed = progress.get("complete", 0)
        pending = progress.get("pending", 0)
        failed = progress.get("failed", 0)
        
        if pending > 0 or failed > 0:
            # There is remaining work — resume from where we left off
            logger.info(
                "📋  [Planner] Resumed DAG from disk: %s",
                progress,
            )
            print(f"{indent}{C}📋 Resuming DAG: {completed} done, {pending} pending, {failed} failed{R}")
        elif completed > 0:
            # All tasks completed previously — run a fresh decomposition
            # to check for any remaining work not covered by the old DAG.
            logger.info(
                "📋  [Planner] Previous DAG fully completed (%d tasks). Decomposing fresh.",
                completed,
            )
            print(f"{indent}{C}📋 Previous DAG complete ({completed} tasks). Starting fresh.{R}")
            planner.clear_state()
            planner = TemporalPlanner.from_brain(
                local_brain if await local_brain.is_available() else None,
                effective_project,
            )
        else:
            # Empty state — decompose fresh
            planner.clear_state()
            planner = TemporalPlanner.from_brain(
                local_brain if await local_brain.is_available() else None,
                effective_project,
            )

    # ── Hook D: Initialize PhaseManager (depth 0, once per project) ────────
    # Must happen before decomposition so the phase context can be injected.
    if depth == 0 and phase_mgr is None:
        try:
            from .phase_manager import PhaseManager
            phase_mgr = PhaseManager(str(effective_project), executor, state)
            if not phase_mgr.is_initialized():
                await phase_mgr.initialize(goal)
        except Exception as _pm_exc:
            logger.debug("📋  [Phase] PhaseManager init skipped: %s", _pm_exc)
            phase_mgr = None

    # ── Decompose if no existing nodes with active work ──
    # V46 FIX: Check for pending/running/failed, not just any nodes.
    _needs_decompose = not any(
        n.status in ("pending", "running", "failed")
        for n in planner._nodes.values()
    ) if planner._nodes else True
    if _needs_decompose:
        # V54: Clear stale in-memory nodes immediately so the UI doesn't
        # keep rendering the previous project's graph while fresh decomposition runs.
        if depth == 0 and not planner._nodes:
            _dag_progress.clear()
            _dag_progress.update({"active": False, "nodes": [], "total": 0,
                                  "completed": 0, "pending": 0, "failed": 0,
                                  "cancelled": 0, "running": []})
            if state:
                try:
                    await state.broadcast_state()
                except Exception:
                    pass
        # V51: Don't start a long decomposition if stop was requested
        if state and getattr(state, 'stop_requested', False):
            logger.info("🛑  [Planner] Stop requested — skipping decomposition.")
            return TaskResult(status="stopped", output="Stopped before decomposition")
        logger.info("📋  [Planner] Decomposing at depth %d …", depth)
        if state:
            state.record_activity("task", f"DAG decomposition starting (depth {depth})", goal)
        # ── Hook A: prepend phase context so Gemini stays in scope ───────────
        _decompose_goal = goal
        if depth == 0 and phase_mgr:
            try:
                _phase_ctx = phase_mgr.get_phase_context_for_decomposition()
                if _phase_ctx:
                    _decompose_goal = _phase_ctx + goal
            except Exception:
                pass
        ok, msg = await planner.decompose_epic(_decompose_goal)
        if not ok:
            logger.warning("📋  [Planner] Decomposition failed: %s. Executing directly.", msg)
            # Fall back to direct execution
            return await executor.execute_task(
                goal, timeout=config.GEMINI_TIMEOUT_SECONDS,
            )

    # ── V67: Phase-to-DAG gap fill — ensure EVERY pending phase task has a DAG node ──
    # The decomposition may generate fewer DAG nodes than the phase plan lists.
    # E.g., phase plan has 19 tasks but decompose_epic() only created 5 nodes.
    # This gap-fill scans all pending phase tasks and injects any that aren't
    # already covered by an existing DAG node (matched by keyword overlap).
    if depth == 0 and phase_mgr and hasattr(phase_mgr, 'get_all_pending_phase_tasks'):
        try:
            _pending_phase_tasks = phase_mgr.get_all_pending_phase_tasks()
            if _pending_phase_tasks:
                _gap_filled = 0
                # Build a set of normalised DAG description keywords for fuzzy matching
                _dag_desc_keys: set[str] = set()
                for _dn in planner._nodes.values():
                    # Normalise: lowercase, strip tags, take significant words
                    _norm = _dn.description.lower()
                    # Strip common prefixes
                    for _pfx in ('[audit fix]', '[proactive fix]', '[func]', '[ui/ux]', '[perf]',
                                 '[ui]', '[data]', '[err]', '[a11y]', '[sec]', '[qa]'):
                        _norm = _norm.replace(_pfx, '')
                    _words = set(_norm.split())
                    _dag_desc_keys.update(_words)

                for _pt in _pending_phase_tasks:
                    _title = _pt.get("title", "").strip()
                    if not _title:
                        continue
                    # Check if this phase task is already covered by a DAG node.
                    # Match: if >= 50% of the title's significant words appear
                    # in any single DAG node's description → already covered.
                    _title_norm = _title.lower()
                    for _pfx in ('[func]', '[ui/ux]', '[perf]', '[ui]', '[data]',
                                 '[err]', '[a11y]', '[sec]', '[qa]'):
                        _title_norm = _title_norm.replace(_pfx, '')
                    _title_words = [w for w in _title_norm.split() if len(w) > 2]
                    if not _title_words:
                        continue

                    # Per-node matching: check if any SINGLE DAG node covers this task
                    _covered = False
                    for _dn in planner._nodes.values():
                        _dn_norm = _dn.description.lower()
                        _match_count = sum(1 for w in _title_words if w in _dn_norm)
                        if len(_title_words) > 0 and _match_count / len(_title_words) >= 0.5:
                            _covered = True
                            break

                    if not _covered:
                        _phase_id = _pt.get("phase_id", "?")
                        _pt_id = _pt.get("task_id", "")
                        _desc = f"[Phase {_phase_id}] {_title}"
                        _injected = planner.inject_task(
                            task_id=f"phase-{_pt_id}" if _pt_id else f"phase-gap-{_gap_filled}",
                            description=_desc,
                            dependencies=[],
                        )
                        if _injected:
                            _gap_filled += 1
                            logger.info(
                                "📋  [GapFill] Injected phase task into DAG: %s → %s",
                                _pt_id, _injected.task_id,
                            )

                if _gap_filled > 0:
                    logger.info(
                        "📋  [GapFill] Injected %d uncovered phase tasks into DAG (total nodes: %d).",
                        _gap_filled, len(planner._nodes),
                    )
                    print(f"{indent}{C}📋 Gap-fill: {_gap_filled} phase tasks added to DAG{R}")
                    if state:
                        state.record_activity(
                            "task",
                            f"Gap-fill: {_gap_filled} phase tasks injected into DAG",
                        )
        except Exception as _gf_exc:
            logger.debug("📋  [GapFill] Error (non-fatal): %s", _gf_exc)

    progress = planner.get_progress()
    total_tasks = sum(progress.values())
    logger.info(
        "📋  [Planner] DAG ready (depth=%d): %d tasks — %s",
        depth, total_tasks, progress,
    )
    print(f"{indent}{B}{C}📋 DAG: {total_tasks} atomic tasks (depth {depth}){R}")
    await _update_dag_progress(planner, depth, state=state)
    if state:
        state.record_activity("task", f"DAG ready: {total_tasks} tasks at depth {depth}")
        state.planner = planner  # V40: expose planner to monitoring loop
        # V70: Initialize per-task cancel signal set
        if not hasattr(state, '_cancel_task_ids'):
            state._cancel_task_ids = set()

    # V46: Check for serial dependency chains — runs in background
    # so workers can start immediately. The scheduling loop picks up
    # corrected deps on its next tick (within ~10s heartbeat).
    _pending_ids = [
        n.task_id for n in planner._nodes.values()
        if n.status in ("pending", "running")
    ]
    if len(_pending_ids) >= 3:
        async def _bg_dep_fix():
            try:
                await _fix_serial_dependencies(
                    planner, _pending_ids, local_brain, state=state,
                )
                await _update_dag_progress(planner, depth, state=state)
            except Exception as exc:
                logger.debug("📋  [DepFix] Background dep fix error: %s", exc)
        _asyncio.create_task(_bg_dep_fix())

    # V41: Per-file locking — workers run in parallel, only serialize on shared files
    from .workspace_transaction import get_workspace_lock
    _ws_lock = get_workspace_lock()

    class _nullctx:
        """No-op async context manager for when there are no files to lock."""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    executed_count = 0
    sem = _asyncio.Semaphore(max_workers)
    _prev_max_workers = max_workers  # Track for dynamic resize
    active_tasks: dict[str, _asyncio.Task] = {}  # task_id → asyncio.Task
    nodes_since_lint = 0
    # V57: track which priority task IDs have already triggered preemption
    # so we don't re-log (and re-cancel) on every 2-second scheduler tick
    _last_preempt_for: set[str] = set()
    # V46: Event for instant scheduling wake when a worker completes
    _worker_done_event = _asyncio.Event()

    # V41: Create git baseline so broken worker output can be reverted
    _has_checkpoint = _git_checkpoint(effective_project)

    # V59: Fire deep analysis in background — fully non-blocking.
    # Workers start immediately; analysis runs in parallel and writes
    # DEEP_ANALYSIS.md which the post-DAG audit will read as context.
    # V60: Skip on resume — if >5 nodes are already complete the architectural
    # decisions are baked in; re-running would produce stale/contradictory output.
    _already_complete = sum(
        1 for n in planner._nodes.values()
        if getattr(n, 'status', '') == 'complete'
    ) if hasattr(planner, '_nodes') else 0
    if effective_project and goal and _already_complete <= 5:
        _deep_analysis_task = _asyncio.create_task(
            _run_deep_analysis(goal, effective_project, state=state),
            name="deep-analysis",
        )
        logger.info("🔬  [DeepAnalysis] Background task launched.")
    else:
        if _already_complete > 5:
            logger.info(
                "🔬  [DeepAnalysis] Skipped (resuming session with %d complete nodes).",
                _already_complete,
            )
        _deep_analysis_task = None

    async def _pool_worker(node):
        """Execute a single node within the semaphore-limited pool."""
        nonlocal executed_count, total_duration, nodes_since_lint
        await sem.acquire()
        _sem_held = True
        try:
            # V40 FIX: Abort immediately if safe stop was requested
            # while this worker was waiting for the semaphore.
            if state and getattr(state, 'stop_requested', False):
                node.status = "pending"  # Return to pending so it's not lost
                logger.info("🛑  [Pool] Worker %s aborting — safe stop requested.", node.task_id)
                return

            # V43/V73: Quota pause gate — sleep until quota resets, then verify
            try:
                from .retry_policy import get_daily_budget, get_failover_chain
                _budget = get_daily_budget()
                _fc = get_failover_chain()

                # Check if quota is paused (uses probe-based resume timer)
                if _budget.quota_paused:
                    if state:
                        state.status = "quota_paused"
                    # V73: Verified resume loop — sleep, probe, re-sleep if still exhausted
                    while _budget._quota_paused:
                        _wait_sec = max(10, _budget._quota_resume_at - time.time()) if _budget._quota_resume_at > 0 else 60
                        _h, _m = divmod(int(_wait_sec) // 60, 60)
                        logger.warning(
                            "⏸  [Pool] Quota paused — worker %s sleeping %dh%02dm until quota resets",
                            node.task_id, _h, _m,
                        )
                        if state:
                            state.record_activity(
                                "warning",
                                f"⏸ Quota paused: {node.task_id} waiting {_h}h{_m:02d}m until quota resets",
                            )
                            await state.broadcast_state()
                        # Sleep in 30s chunks (stop-aware)
                        _slept = 0
                        while _slept < _wait_sec:
                            if state and getattr(state, 'stop_requested', False):
                                break
                            if not _budget._quota_paused:
                                break
                            _chunk = min(30, _wait_sec - _slept)
                            await asyncio.sleep(_chunk)
                            _slept += _chunk
                        # Abort if stop requested (don't clear pause prematurely)
                        if state and getattr(state, 'stop_requested', False):
                            node.status = "pending"
                            logger.info("🛑  [Pool] Stop requested during quota pause — %s returned to pending.", node.task_id)
                            if state:
                                state.status = "stopping"
                            return
                        # V73: Verify quota via /stats probe before resuming
                        _verified = await asyncio.to_thread(_budget.verified_resume_from_quota)
                        if _verified:
                            break  # Quota confirmed — proceed
                        # Not verified — loop re-sleeps with updated timer
                    if state:
                        state.status = "running"
                        await state.broadcast_state()
                    logger.info("▶  [Pool] Quota verified & resumed — %s proceeding", node.task_id)

                # V51: Check model failover chain cooldowns
                # If ALL models are exhausted, sleep until the soonest recovers.
                # This prevents the retry loop: task → quota wall → retry → quota wall.
                elif _fc.all_models_on_cooldown():
                    # V74: Requeue as pending instead of sleeping+proceeding.
                    # Sleeping here caused tasks to wake, hit the quota wall again,
                    # fail, and burn retries — a cascade of wasted failures.
                    # Returning the task to "pending" lets the scheduling loop's
                    # quota pause gate handle it cleanly.
                    _wait_sec = _fc.get_soonest_cooldown_remaining()
                    _h, _m = divmod(int(_wait_sec) // 60, 60)
                    logger.warning(
                        "⏸  [Pool] ALL models on cooldown — returning %s to pending "
                        "(cooldown %dh%02dm)",
                        node.task_id, _h, _m,
                    )
                    node.status = "pending"
                    if state:
                        state.status = "cooldown"
                        state.cooldown_remaining = _wait_sec
                        state.model_status = _fc.get_status()
                        state.record_activity(
                            "warning",
                            f"⏸ All models on cooldown: {node.task_id} returned to pending ({_h}h{_m:02d}m)",
                        )
                        await state.broadcast_state()
                    return  # Exit worker — task stays pending for re-launch after cooldown
            except Exception:
                pass

            node.status = "running"
            node.started_at = time.time()  # Watchdog: record when we marked this running
            planner._save_state()  # V41: Persist running status for crash recovery
            await _update_dag_progress(
                planner, depth,
                running=[tid for tid, t in active_tasks.items() if not t.done()],
                state=state,
            )

            # V58: Layer 3 pre-task snapshot — record line counts of source files.
            # The regression guard reads this after the task to detect content loss.
            # V75: For large repos, use FileIndex cached list instead of rglob.
            try:
                if effective_project:
                    from pathlib import Path as _PL3_pre
                    _snap: dict = {}
                    _src_exts = {'.ts', '.tsx', '.js', '.jsx', '.py', '.md', '.css', '.scss', '.html'}
                    _skip_dirs = {".ag-supervisor", "node_modules", ".git", "__pycache__",
                                  "dist", "build", ".next", ".vite", "coverage"}
                    _use_cached = False
                    try:
                        from .file_index import get_file_index
                        _fidx = get_file_index(effective_project)
                        if _fidx.is_large_repo() and _fidx._scanned:
                            _use_cached = True
                            _proj_root = _PL3_pre(effective_project)
                            for _rel in _fidx._files:
                                _sf = _proj_root / _rel
                                if _sf.suffix.lower() in _src_exts:
                                    try:
                                        _snap[str(_sf)] = len(
                                            _sf.read_text(encoding='utf-8', errors='replace').splitlines()
                                        )
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                    if not _use_cached:
                        for _sf in _PL3_pre(effective_project).rglob("*"):
                            if (_sf.is_file() and _sf.suffix.lower() in _src_exts
                                    and not _skip_dirs.intersection(_sf.parts)):
                                try:
                                    _snap[str(_sf)] = len(_sf.read_text(encoding='utf-8', errors='replace').splitlines())
                                except Exception:
                                    pass
                    node._pre_task_line_snap = _snap
            except Exception:
                node._pre_task_line_snap = {}

            progress = planner.get_progress()
            done = progress.get("complete", 0)
            logger.info(
                "📋  [Pool] Executing %s [%d/%d]: %s",
                node.task_id, done + 1, total_tasks, node.description,
            )
            print(
                f"{indent}{C}▸ [{done + 1}/{total_tasks}] {node.task_id}: "
                f"{node.description}{R}"
            )
            # V40: Timeline milestone — node started
            if state:
                state.record_activity(
                    "task",
                    f"Worker started: {node.task_id} [{done + 1}/{total_tasks}]",
                    node.description,
                )

            # V57: Q3 — Ollama pre-screen: ask local model (free) if task is already done.
            # If Ollama says YES, skip Gemini dispatch and mark as complete directly.
            # Non-fatal: if Ollama unavailable, proceeds normally to Gemini.
            _skip_task = False
            try:
                from .ollama_advisor import ask_ollama
                _ps_context = ""
                if effective_project:
                    _ps_path = __import__('pathlib').Path(effective_project) / "PROJECT_STATE.md"
                    if _ps_path.exists():
                        _ps_context = _ps_path.read_text(encoding="utf-8")
                if _ps_context:
                    _prescreen = await ask_ollama(
                        f"Based on this PROJECT_STATE.md:\n{_ps_context}\n\n"
                        f"Is this task ALREADY DONE or clearly no longer needed?\n"
                        f"Task: {node.description}\n\n"
                        f"Respond with only YES or NO.",
                        timeout=15,
                    )
                    if _prescreen and _prescreen.strip().upper().startswith("YES"):
                        logger.info(
                            "[Q3/Ollama] Pre-screen: task %s appears already done — skipping Gemini dispatch.",
                            node.task_id,
                        )
                        _skip_task = True  # mark_complete happens below in shared block
            except Exception as _ps_exc:
                logger.debug("[Q3/Ollama] Pre-screen skipped (non-fatal): %s", _ps_exc)

            if not _skip_task:
                # V57: P2 — Git safety stash before task execution.
                # Creates a stash point so we can restore if a regression is detected.
                _git_stash_name = f"supervisor-pre-{node.task_id}"
                _git_stashed = False
                if effective_project:
                    try:
                        import subprocess as _sp
                        _gs = _sp.run(
                            ["git", "-C", str(effective_project), "stash", "push",
                             "-m", _git_stash_name, "--include-untracked"],
                            capture_output=True, text=True, timeout=15,
                        )
                        if _gs.returncode == 0 and "No local changes" not in _gs.stdout:
                            _git_stashed = True
                            logger.debug("[P2/Git] Stashed before task %s", node.task_id)
                    except Exception as _git_stash_exc:
                        logger.debug("[P2/Git] Stash skipped (git unavailable): %s", _git_stash_exc)

                chunk_result = await _execute_single_chunk(
                    node=node,
                    planner=planner,
                    local_brain=local_brain,
                    executor=executor,
                    session_mem=session_mem,
                    effective_project=effective_project,
                    depth=depth,
                    max_depth=max_depth,
                    indent=indent,
                    state=state,
                    sandbox=sandbox,
                    tools=tools,
                    project_path=project_path,
                    task_intel=_task_intel,  # V77: Thread DAG-level instance
                )

                # V57: P2 — If git was stashed, decide whether to keep or pop.
                # Pop (restore) ONLY if TS regressions were detected (new breakage).
                # Otherwise drop the stash — the task's changes are good.
                if _git_stashed and effective_project:
                    _has_regs = bool(getattr(chunk_result, 'ts_regressions', []))
                    if _has_regs:
                        try:
                            import subprocess as _sp2
                            _gp = _sp2.run(
                                ["git", "-C", str(effective_project), "stash", "pop"],
                                capture_output=True, text=True, timeout=15,
                            )
                            if _gp.returncode == 0:
                                logger.warning(
                                    "[P2/Git] Restored pre-task stash after TS regression on %s",
                                    node.task_id,
                                )
                        except Exception as _gp_exc:
                            logger.debug("[P2/Git] Stash pop skipped: %s", _gp_exc)
                    else:
                        try:
                            import subprocess as _sp3
                            _sp3.run(
                                ["git", "-C", str(effective_project), "stash", "drop"],
                                capture_output=True, text=True, timeout=10,
                            )
                        except Exception:
                            pass
            else:
                # Skipped task — create a dummy successful result so pool logic proceeds
                from .headless_executor import TaskResult as _TR
                chunk_result = _TR(status="success", output="[Skipped: Ollama pre-screen determined task already done]")

            # V41: Per-file lock on shared state mutation — two workers touching
            # the same file serialize only here, not during execution.
            _changed = chunk_result.files_changed or []
            async with _ws_lock.acquire_files(_changed) if _changed else _nullctx():
                executed_count += 1
                total_duration += chunk_result.duration_s
                # Filter out paths inside directories that are never synced
                # (catches .git objects, node_modules, build artefacts, etc.
                # that Gemini occasionally appends to its changed-file list).
                _SYNC_EXCLUDED = {
                    ".git", "node_modules", "dist", "build", "out", ".next", ".nuxt",
                    "__pycache__", ".venv", "venv", ".cache", ".turbo",
                    ".vite", "coverage", "storybook-static", ".expo", ".svelte-kit", ".parcel-cache",
                    ".ag-supervisor", ".ag-brain",
                }
                _changed = [
                    f for f in _changed
                    if not any(
                        seg in _SYNC_EXCLUDED
                        for seg in f.replace("\\", "/").split("/")
                    )
                ]
                all_files_changed.extend(_changed)

            if chunk_result.success or chunk_result.status == "partial":
                # V41: Validate worker output before accepting
                # Tier 2: Shadow container validation (runtime-isolated)
                # Tier 1 fallback: Host-side syntax check (git checkpoint)
                _changed_files = chunk_result.files_changed or []
                _valid = True
                _val_errors: list[str] = []

                if _changed_files:
                    _shadow_id = None
                    try:
                        # Try Tier 2: Shadow container
                        if sandbox and sandbox.is_running:
                            _shadow_id = await sandbox.create_shadow(
                                node.task_id, effective_project
                            )
                        if _shadow_id:
                            _valid, _val_errors = await sandbox.validate_in_shadow(
                                _shadow_id, _changed_files
                            )
                            await sandbox.destroy_shadow(_shadow_id)
                            _shadow_id = None
                        elif _has_checkpoint:
                            # Tier 1 fallback: host-side syntax check
                            _valid, _val_errors = _validate_worker_files(
                                effective_project, _changed_files
                            )
                    except Exception as _ve:
                        logger.debug("🐳  [Shadow] Validation error: %s — falling back", _ve)
                        # Cleanup shadow if it was created
                        if _shadow_id:
                            try:
                                await sandbox.destroy_shadow(_shadow_id)
                            except Exception:
                                pass
                        # Tier 1 fallback
                        if _has_checkpoint:
                            _valid, _val_errors = _validate_worker_files(
                                effective_project, _changed_files
                            )

                if not _valid:
                    logger.warning(
                        "🔀  [Git] Worker %s produced invalid files: %s",
                        node.task_id, _val_errors[:3],
                    )
                    # Revert broken files to checkpoint
                    _revert_worker_files(effective_project, _changed_files)
                    if state:
                        state.record_activity(
                            "warning",
                            f"Worker {node.task_id}: reverted {len(_changed_files)} files (validation failed)",
                        )
                    # Feed validation errors into auto-fix retry
                    chunk_result.success = False
                    chunk_result.errors = _val_errors + chunk_result.errors
                    # Fall through to the failure handler below

            if chunk_result.success or chunk_result.status == "partial":
                planner.mark_complete(node.task_id)  # also calls _save_state() internally

                # V76: Record successful result in TaskIntelligence
                try:
                    if _task_intel:
                        _tag = _extract_tag(node.description)
                        _task_intel.record_result(
                            task_id=node.task_id,
                            category=_tag,
                            files_changed=chunk_result.files_changed or [],
                            success=True,
                            duration_s=chunk_result.duration_s,
                        )
                except Exception:
                    pass

                # V68: Release semaphore early — AI work is done.
                # Free the worker slot so another task can start while
                # post-task housekeeping (file sync, dep install, preview,
                # git commit) runs without blocking the pool.
                if _sem_held:
                    sem.release()
                    _sem_held = False

                # V57: Zero-change warning — task reported success but touched no files.
                # Inject a targeted retry with explicit file-write requirement.
                _zero_change = getattr(chunk_result, 'zero_change_warning', False)
                if _zero_change and effective_project:
                    _zc_desc = (
                        f"[RETRY] The previous attempt at task '{node.task_id}' reported success "
                        f"but made NO file changes. Re-implement: {node.description}\n"
                        f"You MUST write/modify at least one source file. "
                        f"If the task is already done, update PROJECT_STATE.md to document it."
                    )
                    _zc_injected = planner.inject_task(
                        task_id=f"{node.task_id}-rechk",
                        description=_zc_desc,
                        dependencies=[node.task_id],
                    )
                    if _zc_injected:
                        logger.warning("[Verify] Zero-change detected — injected re-check task for %s", node.task_id)

                # V57: Config-change liveness — if config files were touched,
                # inject a server health check task immediately.
                _touched_cfg = getattr(chunk_result, 'touched_config', False)
                if _touched_cfg and effective_project:
                    _cfg_desc = (
                        f"[HEALTH] Config files were modified by task '{node.task_id}'. "
                        f"1) Restart the dev server (or run `npm run dev` / `python manage.py runserver`). "
                        f"2) Verify the server starts without errors. "
                        f"3) Fix any startup errors before marking done. "
                        f"4) Update PROJECT_STATE.md with server status."
                    )
                    _srvchk_base = planner.get_task_offset() + 1 if planner else 1
                    _cfg_injected = planner.inject_task(
                        task_id=f"t{_srvchk_base}-SRVCHK",
                        description=(
                            f"[SRVCHK] Config files modified by {node.task_id}. "
                            f"1) Restart the dev server. "
                            f"2) Verify it starts without errors. "
                            f"3) Fix any startup errors before marking done. "
                            f"4) Update PROJECT_STATE.md with server status."
                        ),
                        dependencies=[node.task_id],
                    )
                    if _cfg_injected:
                        logger.info("[B1] Config changed — injected server liveness check task")

                # V57: Error pattern memory — record error→fix if this was a retry
                # (i.e., node had previous errors logged) that succeeded this time.
                _prev_errs = getattr(node, '_prev_errors', [])
                if _prev_errs and chunk_result.success and effective_project:
                    try:
                        from .error_memory import record_error_fix
                        for _err in _prev_errs[:3]:
                            record_error_fix(
                                effective_project,
                                error_text=_err,
                                fix_summary=f"Task {node.task_id} succeeded on retry: {node.description}",
                            )
                    except Exception as _mem_exc:
                        logger.debug("[ErrorMemory] Record skipped: %s", _mem_exc)

                # V57: O1 — Self-critique: if executor flagged this task as needing review,
                # inject a lightweight review task so Gemini can catch its own issues.
                # GUARDS: (1) executor already won't set the flag for meta-tasks, but as a
                # second-line defence, also skip if the task_id has a meta suffix.
                # (2) Only inject if the review task_id doesn't already exist in the DAG.
                _needs_review = getattr(chunk_result, 'needs_self_review', False)
                _review_ctx = getattr(chunk_result, 'self_review_context', '')
                _meta_suffixes = ("-review", "-tsfix", "-srvchk", "-health", "-lint")
                _is_meta_node = any(node.task_id.endswith(s) for s in _meta_suffixes)
                if _needs_review and _review_ctx and effective_project and not _is_meta_node:
                    try:
                        _rv_desc = (
                            f"[SELF-REVIEW] Review and fix your own work from task '{node.task_id}'.\n\n"
                            f"{_review_ctx}\n\n"
                            "Self-review checklist — check each and FIX any issues found:\n"
                            "1. Did the task implementation fully satisfy all requirements? Any stubs left?\n"
                            "2. Any TypeScript errors introduced? (check imports, types, exports)\n"
                            "3. Any missing wiring? (component created but not imported/rendered)\n"
                            "4. Any edge cases missed? (empty state, error state, loading state)\n"
                            "5. Did anything unrelated to the task accidentally break?\n\n"
                            "Fix any issues you find. Update PROJECT_STATE.md with what you checked."
                        )
                        _rv_injected = planner.inject_task(
                            task_id=f"{node.task_id}-review",
                            description=_rv_desc,
                            dependencies=[node.task_id],
                        )
                        if _rv_injected:
                            logger.info("[SelfCritique] Injected review task for %s (3+ files changed)", node.task_id)
                    except Exception as _rv_exc:
                        logger.debug("[SelfCritique] Review task injection skipped: %s", _rv_exc)


                # V56: TS Regression Guard — inject micro-fix task for regressions.
                # Accepted even with regressions (no quota waste retrying the full task).
                # A small targeted fix task is much cheaper than a full retry.
                _ts_regs = getattr(chunk_result, 'ts_regressions', [])
                if _ts_regs and effective_project:
                    try:
                        from .ts_regression_guard import build_microfix_description
                        _mf_desc = build_microfix_description(_ts_regs, parent_task_id=node.task_id)
                        _mf_id = f"{node.task_id}-tsfix"
                        _mf_injected = planner.inject_task(
                            task_id=_mf_id,
                            description=_mf_desc,
                            dependencies=[node.task_id],
                        )
                        if _mf_injected:
                            logger.warning(
                                "[TSGuard] Injected micro-fix task %s for %d regression(s).",
                                _mf_id, len(_ts_regs),
                            )
                            if state:
                                state.record_activity(
                                    "warning",
                                    f"TSGuard: {len(_ts_regs)} regression(s) from {node.task_id} — micro-fix queued",
                                )
                    except Exception as _mf_exc:
                        logger.debug("[TSGuard] Micro-fix injection error (non-fatal): %s", _mf_exc)

                # V58: Dynamic DAG expansion — process DAG_INJECT: signals from task output.
                # If the task emitted DAG_INJECT: {"tasks": [...]} the executor parsed them
                # into chunk_result.dag_injections. Inject them now into the live planner.
                _dag_injects = getattr(chunk_result, 'dag_injections', [])
                if _dag_injects:
                    try:
                        _injected = planner.inject_nodes(
                            _dag_injects,
                            parent_task_id=node.task_id,
                        )
                        if _injected:
                            logger.info(
                                "🔀  [Pool] %d child node(s) injected by %s into live DAG.",
                                len(_injected), node.task_id,
                            )
                            if state:
                                state.record_activity(
                                    "task",
                                    f"DAG expanded: {len(_injected)} sub-task(s) queued from {node.task_id}",
                                )
                            await _update_dag_progress(planner, depth, state=state)
                    except Exception as _inj_exc:
                        logger.debug("🔀  [Pool] DAG injection error (non-fatal): %s", _inj_exc)

                # ── Hook B: update living plan after each node ──────────────────
                if phase_mgr and depth == 0:
                    try:
                        import asyncio as _aio_pm
                        _aio_pm.ensure_future(phase_mgr.on_node_completed(
                            node_id=node.task_id,
                            description=node.description,
                            success=True,
                            files_changed=chunk_result.files_changed,
                            error_summary="",
                        ))
                    except Exception:
                        pass
                # V41 FIX: Broadcast DAG progress IMMEDIATELY so Graph tab updates live.
                # Previously the UI only learned about completions when the scheduling
                # loop polled — for audit re-kick tasks there is NO scheduling loop.
                await _update_dag_progress(planner, depth, state=state)
                if state and chunk_result.files_changed:
                    for f in chunk_result.files_changed:
                        state.record_change(f, "modified", node.task_id)

                # V58: Layer 1 — Register file writes + inject conflict deps
                # Serializes any pending tasks that mention the same files.
                if chunk_result.files_changed:
                    try:
                        planner.register_file_writes(node.task_id, chunk_result.files_changed)
                        _n_injected = planner.inject_file_conflict_deps(
                            node.task_id, chunk_result.files_changed
                        )
                        if _n_injected:
                            logger.info(
                                "[FileConflict] Auto-serialized %d pending task(s) that share files with %s",
                                _n_injected, node.task_id,
                            )
                    except Exception as _fc_exc:
                        logger.debug("[FileConflict] Layer 1 error (non-fatal): %s", _fc_exc)

                    # V74: Shared-file impact check — immediate type-check when
                    # a worker modifies files in shared/core directories.
                    # Prevents Worker B from committing stale code after Worker A
                    # changes a shared function signature.
                    try:
                        import os as _sf_os
                        _SHARED_DIRS = {'lib', 'utils', 'shared', 'types', 'hooks', 'common', 'core'}
                        _shared_touched = False
                        for _sf in chunk_result.files_changed:
                            _sf_str = str(_sf).replace('\\', '/')
                            _sf_parts = _sf_str.split('/')
                            # Check if any path segment is a shared directory
                            if any(p.lower() in _SHARED_DIRS for p in _sf_parts):
                                _shared_touched = True
                                break
                            # Also check if the file itself is a common shared name
                            _sf_base = _sf_os.path.basename(_sf_str).lower()
                            if _sf_base in ('types.ts', 'types.tsx', 'index.ts', 'api.ts', 'constants.ts'):
                                _shared_touched = True
                                break

                        if _shared_touched and sandbox and sandbox.is_running:
                            logger.info(
                                "🔗  [SharedFile] %s modified shared file(s) — running type-check",
                                node.task_id,
                            )
                            _tsc_result = await sandbox.exec_command(
                                "cd /workspace && npx tsc --noEmit 2>&1 | tail -30",
                                timeout=60,
                            )
                            if _tsc_result.exit_code != 0:
                                _tsc_errors = (_tsc_result.stdout or "")[-500:]
                                logger.warning(
                                    "🔗  [SharedFile] Type-check FAILED after %s: %s",
                                    node.task_id, _tsc_errors[:200],
                                )
                                # Inject high-priority repair task
                                _sfc_in_flight = any(
                                    n.task_id.startswith("shared-fix")
                                    and n.status in ("pending", "running")
                                    for n in planner._nodes.values()
                                )
                                if not _sfc_in_flight:
                                    _sfc_id = f"shared-fix-{int(__import__('time').time())}"
                                    planner.inject_task(
                                        task_id=_sfc_id,
                                        description=(
                                            "[BUILD] Type-check failure after shared file modification by "
                                            f"task {node.task_id}. TypeScript errors:\n{_tsc_errors}\n\n"
                                            "Fix ALL type errors. Check that all imports reference the "
                                            "correct types and function signatures. Run `npx tsc --noEmit` "
                                            "to verify zero errors."
                                        ),
                                        dependencies=[node.task_id],
                                        priority=95,
                                    )
                                    if state:
                                        state.record_activity(
                                            "warning",
                                            f"🔗 Shared file impact: type errors after {node.task_id} — repair task injected",
                                        )
                            else:
                                logger.debug(
                                    "🔗  [SharedFile] Type-check passed after %s", node.task_id,
                                )
                    except Exception as _sf_exc:
                        logger.debug("🔗  [SharedFile] Impact check error (non-fatal): %s", _sf_exc)

                # V58: Layer 3 — Regression guard (line-count sentinel)
                # If any file shrank by >40% and had >30 lines, auto-inject a merge task.
                try:
                    _pre_snap = getattr(node, '_pre_task_line_snap', {})
                    if _pre_snap and chunk_result.files_changed and effective_project:
                        from pathlib import Path as _PL3
                        # Generated/ephemeral files are expected to shrink when issues
                        # are resolved — exclude them from regression detection.
                        _GENERATED_FILES = {
                            'build_issues.md', 'console_issues.md', 'project_state.md',
                            'progress.md', 'vision.md', 'readme.md', 'changelog.md',
                            'dag_history.jsonl', 'epic_state.json', 'checkpoint.json',
                        }
                        _regressed = []
                        for _rf in chunk_result.files_changed:
                            try:
                                _rp = _PL3(effective_project) / _rf if not _PL3(_rf).is_absolute() else _PL3(_rf)
                                # Skip generated/ephemeral files — they legitimately shrink
                                if _rp.name.lower() in _GENERATED_FILES:
                                    continue
                                if _rp.exists() and _rp.suffix in (
                                    '.ts', '.tsx', '.js', '.jsx', '.py', '.css', '.scss', '.html'
                                ):
                                    _after_lines = len(_rp.read_text(encoding='utf-8', errors='replace').splitlines())
                                    _before_lines = _pre_snap.get(str(_rp), 0)
                                    if _before_lines > 30 and _after_lines < _before_lines * 0.60:
                                        _regressed.append((_rf, _before_lines, _after_lines))
                            except Exception:
                                pass
                        if _regressed:
                            _merge_desc = (
                                f"[MERGE RECOVERY] Task '{node.task_id}' appears to have overwritten "
                                f"content in {len(_regressed)} file(s). "
                                "Files that SHRANK significantly (possible content loss):\n"
                                + "\n".join(
                                    f"  - {rf}: {b} lines → {a} lines (lost {b-a} lines)"
                                    for rf, b, a in _regressed
                                )
                                + "\n\nFor each file listed above: READ the current file, "
                                "check PROJECT_STATE.md for what should be there, and "
                                "RESTORE any content that was accidentally deleted. "
                                "Do NOT remove features that were already implemented."
                            )
                            _mr_id = f"{node.task_id}-merge"
                            try:
                                _mr_injected = planner.inject_task(
                                    task_id=_mr_id,
                                    description=_merge_desc,
                                    dependencies=[node.task_id],
                                    priority=100,
                                )
                                if _mr_injected:
                                    logger.warning(
                                        "[L3/Regression] %d file(s) shrank >40%% — merge-recovery task %s injected",
                                        len(_regressed), _mr_id,
                                    )
                                    if state:
                                        state.record_activity(
                                            "warning",
                                            f"[L3] Content regression detected in {node.task_id} — recovery task queued",
                                        )
                            except Exception as _mr_exc:
                                logger.debug("[L3/Regression] Recovery task injection error: %s", _mr_exc)
                except Exception as _l3_exc:
                    logger.debug("[L3/Regression] Snapshot check error (non-fatal): %s", _l3_exc)

                # V58: Layer 4 — Git commit per successful task.
                # Creates an atomic checkpoint so work is never lost and recovery
                # tasks can diff against HEAD to identify what was changed.
                try:
                    if effective_project:
                        import asyncio as _l4aio
                        import subprocess as _l4sp
                        _git_result = await _l4aio.get_event_loop().run_in_executor(
                            None,
                            lambda: _l4sp.run(
                                ["git", "-C", effective_project, "add", "-A"],
                                capture_output=True, timeout=10
                            )
                        )
                        if _git_result.returncode == 0:
                            await _l4aio.get_event_loop().run_in_executor(
                                None,
                                lambda: _l4sp.run(
                                    ["git", "-C", effective_project, "commit",
                                     "-m", f"[supervisor] {node.task_id} complete",
                                     "--no-verify", "--quiet"],
                                    capture_output=True, timeout=15
                                )
                            )
                            logger.debug("[L4/Git] Committed checkpoint for %s", node.task_id)
                except Exception as _l4_exc:
                    logger.debug("[L4/Git] Commit error (non-fatal — no git repo?): %s", _l4_exc)
                if state:
                    state.record_task_complete()  # V53: Increment telemetry counter
                if state:
                    state.record_activity(
                        "success",
                        f"Chunk {node.task_id} done ({chunk_result.duration_s:.1f}s, "
                        f"{len(chunk_result.files_changed)} files)",
                    )
                session_mem.record_event(
                    "chunk_completed",
                    f"{node.task_id}: {node.description}",
                )
                print(
                    f"{indent}  {G}✓ {node.task_id} done "
                    f"({chunk_result.duration_s:.1f}s, "
                    f"{len(chunk_result.files_changed)} files){R}"
                )
                # V42 FIX: Broadcast AFTER recording changes so the Changes tab
                # updates in real-time. Previously broadcast happened before
                # record_change(), so changes were never pushed to the UI.
                if state:
                    try:
                        await state.broadcast_state()
                    except Exception:
                        pass

                # V53: Sync changed files into the sandbox immediately after
                # task completion so the dev server hot-reloads the new code.
                if chunk_result.files_changed and sandbox and sandbox.is_running:
                    try:
                        await sandbox.sync_changed_files(
                            effective_project, chunk_result.files_changed
                        )
                        if state:
                            state._last_change_ts = __import__('time').time()
                    except Exception as _sync_exc:
                        logger.debug("Post-task file sync warning: %s", _sync_exc)

                # V53: After syncing, check the dev server log for any newly
                # unresolvable imports introduced by this task and auto-install
                # them so HMR doesn't crash on the next browser refresh.
                if sandbox and sandbox.is_running and executor:
                    try:
                        _installed = await executor.resolve_missing_imports()
                        if _installed and state:
                            state.record_activity(
                                "system",
                                f"Auto-installed missing import(s): {', '.join(sorted(_installed))}",
                            )
                    except Exception as _rmi_exc:
                        logger.debug("📦  [Import Resolver] Post-task check skipped: %s", _rmi_exc)

                # V53: Auto-install missing/new dependencies after every task.
                # Trigger 1 (always): package.json changed → npm install.
                # Trigger 2 (every 3): scan imports for missing packages.
                _task_exec_count = getattr(state, '_task_exec_count', 0) + 1
                if state:
                    state._task_exec_count = _task_exec_count
                try:
                    await _auto_dep_install(
                        sandbox=sandbox,
                        executor=executor,
                        state=state,
                        changed_files=chunk_result.files_changed or [],
                        task_counter=_task_exec_count,
                    )
                except Exception as _adi_exc:
                    logger.debug("📦  [AutoDep] Skipped: %s", _adi_exc)

                # V54: Detect vite chunk corruption in task output and auto-recover.
                # "Cannot find module '.../node_modules/vite/...'" means node_modules
                # was built on a different OS/architecture (Windows host → Linux container).
                _chunk_out = " ".join(str(e) for e in (chunk_result.errors or []))
                _chunk_out += str(getattr(chunk_result, 'output', '') or '')
                if (
                    "node_modules/vite" in _chunk_out
                    and "Cannot find module" in _chunk_out
                ):
                    asyncio.ensure_future(
                        _recover_corrupt_node_modules(sandbox, state)
                    )

                # V51: Auto-reinstall deps after build-health fix tasks
                # The fix task updates package.json versions directly. We need
                # to run install_dependencies() immediately (it has network access)
                # so the updated versions are actually installed before anything else.
                if (node.task_id.startswith("build-health")
                        or node.task_id.endswith("-BUILD")
                        or node.task_id.endswith("-DEPS")) and executor:
                    logger.info("📦  [Build Health] Fix task %s done — clean reinstalling dependencies …", node.task_id)
                    if state:
                        state.record_activity("system", f"Clean reinstalling dependencies after {node.task_id}")
                    try:
                        # V51: ALWAYS nuke node_modules before reinstall after build-health.
                        # Incremental npm install leaves stale Vite internal chunks
                        # (dep-*.js) when versions change, causing "Cannot find module"
                        # runtime errors. Clean install guarantees fresh dependency tree.
                        if sandbox and sandbox.is_running:
                            await sandbox.exec_command(
                                "rm -rf node_modules package-lock.json 2>/dev/null || true",
                                timeout=15,
                            )
                            logger.info("📦  [Build Health] Nuked node_modules + lock for clean install")
                        _reinstall = await executor.install_dependencies(timeout=240)
                        if _reinstall.success:
                            logger.info("📦  [Build Health] Dependencies reinstalled successfully.")
                        else:
                            logger.warning("📦  [Build Health] Reinstall had issues: %s", _reinstall.errors[:2])
                    except Exception as _re_exc:
                        logger.debug("📦  [Build Health] Reinstall failed: %s", _re_exc)

                    # V51: Kill running dev server + purge Vite optimizer cache.
                    # The old server holds stale pre-bundled deps in memory,
                    # causing 504 "Outdated Optimize Dep" errors.
                    if sandbox and sandbox.is_running:
                        try:
                            # Kill whatever is listening on port 3000 (the dev server)
                            await sandbox.exec_command(
                                "fuser -k 3000/tcp 2>/dev/null; "
                                "pkill -f 'vite|next|nuxt|webpack' 2>/dev/null; "
                                "rm -rf node_modules/.vite .vite dist/.vite 2>/dev/null; "
                                "sleep 1",
                                timeout=10,
                            )
                            logger.info("📦  [Build Health] Dev server killed + Vite cache purged — will restart fresh")
                            if state:
                                state.record_activity("system", "Dev server killed after dep update — restarting fresh")
                        except Exception as _kill_exc:
                            logger.debug("📦  [Build Health] Dev server kill failed: %s", _kill_exc)

                # V41: Auto-redeploy after every file-changing task so the
                # preview always reflects the latest code.
                # V69/S2: Skip during safe stop — no point syncing to a sandbox
                # that's about to be destroyed (saves 30-60s per task).
                _skip_redeploy = state and getattr(state, 'stop_requested', False)
                if chunk_result.files_changed and sandbox and not _skip_redeploy:
                    try:
                        await _auto_preview_check(
                            sandbox=sandbox,
                            executor=executor,
                            tools=tools,
                            state=state,
                            project_path=project_path or effective_project,
                        )
                        logger.info(
                            "📦  [Sync] Auto-redeployed after %s (%d files changed)",
                            node.task_id, len(chunk_result.files_changed),
                        )
                        if state:
                            state.record_activity(
                                "system",
                                f"Auto-redeployed preview after {node.task_id}",
                            )
                    except Exception as _sync_exc:
                        logger.debug("📦  [Sync] Auto-redeploy failed (non-critical): %s", _sync_exc)
            else:
                # ── Failure: auto-fix first, then replan ──
                chunk_errors.extend(chunk_result.errors)

                # V40 FIX: Skip auto-fix if safe stop requested — don't
                # waste 5+ minutes retrying when the user wants to stop.
                if state and getattr(state, 'stop_requested', False):
                    planner.mark_failed(node.task_id, str(chunk_result.errors[:2]))
                    await _update_dag_progress(planner, depth, state=state)  # V41: live UI
                    logger.info("🛑  [Pool] %s failed but skipping auto-fix — safe stop active.", node.task_id)
                    if state:
                        state.record_task_error()  # V53: Telemetry
                        state.record_activity("warning", f"{node.task_id} failed — auto-fix skipped (safe stop)")
                    return

                # V41: On rate limit, IMMEDIATELY RETRY with the failover model.
                # The failover chain already switched the active model via
                # report_failure(), so re-executing goes to the next model.
                if getattr(chunk_result, '_rate_limited', False):
                    try:
                        from .retry_policy import get_failover_chain, get_quota_probe
                        _fc = get_failover_chain()

                        # V62: SMART RETRY — if the model has plenty of quota
                        # according to the probe, it's likely a transient error.
                        # Retry on the SAME model after a short delay instead of
                        # immediately failing over to a weaker model.
                        _qp = get_quota_probe()
                        _snap = _qp._snapshots.get(state.active_model or "", {})
                        _remaining = _snap.get("remaining_pct", 100)

                        if _remaining > 5:
                            # Model has quota — likely transient error, retry same model
                            logger.info(
                                "⚡  [Pool] %s rate-limited but %s has %.0f%% quota — "
                                "waiting 30s and retrying SAME model.",
                                node.task_id, state.active_model, _remaining,
                            )
                            if state:
                                state.record_activity(
                                    "warning",
                                    f"{node.task_id} rate-limited — retrying same model "
                                    f"({state.active_model} has {_remaining:.0f}% quota)",
                                )
                            # V65: Don't wait if shutdown is in progress — requeue and exit
                            if state and getattr(state, 'stop_requested', False):
                                node.status = "pending"
                                node.started_at = None
                                planner._save_state()
                                logger.info("🛑  [Pool] %s rate-limited during shutdown — requeueing as pending.", node.task_id)
                                await _update_dag_progress(planner, depth, state=state)
                                return
                            await asyncio.sleep(30)
                            retry_result = await executor.execute_task(
                                node.description,
                                timeout=config.GEMINI_TIMEOUT_SECONDS,
                            )
                            if retry_result.success or retry_result.status == "partial":
                                all_files_changed.extend(retry_result.files_changed)
                                total_duration += retry_result.duration_s
                                planner.mark_complete(node.task_id)
                                print(f"{indent}  {G}✓ {node.task_id} done via same-model retry ({retry_result.duration_s:.1f}s){R}")
                                if state:
                                    state.record_activity("success", f"{node.task_id} completed via same-model retry")
                                return
                            # Same model retry failed too — NOW try failover
                            logger.warning(
                                "⚡  [Pool] %s same-model retry also failed — trying failover.",
                                node.task_id,
                            )

                        # Model is low on quota or same-model retry failed — failover
                        # V74: Respect PRO_ONLY_CODING — don't failover to non-pro
                        _pro_only_cfg = getattr(config, "PRO_ONLY_CODING", False)
                        _next = _fc.get_active_model(pro_only=_pro_only_cfg)
                        if _next:
                            # V41 FIX (Bug 2): Update UI header immediately
                            if state:
                                state.active_model = _next
                                await state.broadcast_state()
                            logger.info(
                                "⚡  [Pool] %s rate-limited — retrying immediately with model %s",
                                node.task_id, _next,
                            )
                            if state:
                                state.record_activity("warning", f"{node.task_id} rate-limited — retrying with {_next}")
                            # Re-execute with the new model (failover chain returns it automatically)
                            retry_result = await executor.execute_task(
                                node.description,
                                timeout=config.GEMINI_TIMEOUT_SECONDS,
                            )
                            if retry_result.success or retry_result.status == "partial":
                                all_files_changed.extend(retry_result.files_changed)
                                total_duration += retry_result.duration_s
                                planner.mark_complete(node.task_id)
                                print(f"{indent}  {G}✓ {node.task_id} done via failover ({retry_result.duration_s:.1f}s){R}")
                                if state:
                                    state.record_activity("success", f"{node.task_id} completed via model failover")
                                return
                            # Failover also failed — fall through to mark failed
                            logger.warning("⚡  [Pool] %s failover also failed.", node.task_id)
                        elif _pro_only_cfg:
                            logger.warning(
                                "⏸  [Pool] %s rate-limited — no Pro model available. "
                                "PRO_ONLY_CODING active — skipping failover retry.",
                                node.task_id,
                            )
                    except Exception as _foe:
                        logger.debug("⚡  [Pool] Failover retry error: %s", _foe)
                    # If no model available or failover failed, mark failed
                    planner.mark_failed(node.task_id, "rate_limited")

                    # V43/V62: Pause entire DAG when models exhausted (429)
                    # V70: Respect quota_pause_mode (off/pro/all)
                    try:
                        from .retry_policy import get_daily_budget, get_quota_probe
                        _budget = get_daily_budget()
                        _qpm = getattr(state, 'quota_pause_mode', 'off') if state else 'off'

                        # Determine if we should pause based on mode
                        _should_pause = False
                        if _qpm == 'all':
                            # Pause when all models in the failover chain are exhausted
                            _should_pause = True
                        elif _qpm == 'pro':
                            # Pause only when a pro-bucket model is exhausted
                            # Check if the rate-limited model is in the pro bucket
                            _active_mdl = getattr(state, 'active_model', '') if state else ''
                            _pro_bucket = config.QUOTA_BUCKETS.get('pro', {}).get('models', [])
                            _should_pause = any(p in (_active_mdl or '') for p in _pro_bucket) or (
                                config.classify_model(_active_mdl) == 'pro' if _active_mdl else False
                            )
                        # 'off' = never pause, let failover handle it

                        if _should_pause:
                            _exact_cooldown = getattr(chunk_result, '_quota_cooldown_s', None)
                            if not _exact_cooldown:
                                # Try to get cooldown from the probe snapshot's resets_at
                                try:
                                    _qp_snap = get_quota_probe().get_quota_snapshot()
                                    for _m_data in _qp_snap.values():
                                        if isinstance(_m_data, dict) and _m_data.get("remaining_pct", 100) == 0:
                                            _resets_at = _m_data.get("resets_at", 0)
                                            if _resets_at > time.time():
                                                _exact_cooldown = int(_resets_at - time.time())
                                                break
                                except Exception:
                                    pass
                            if _exact_cooldown and _exact_cooldown > 0:
                                _budget.pause_for_quota(cooldown_seconds=_exact_cooldown)
                                _h, _m = _exact_cooldown // 3600, (_exact_cooldown % 3600) // 60
                                logger.warning("\u23f8  [Quota] Models exhausted (mode=%s) - pausing DAG for %dh%dm (probe reset)", _qpm, _h, _m)
                                if state:
                                    state.record_activity("warning", f"Quota pause ({_qpm}): pausing for {_h}h{_m}m (probe timer)")
                                    await state.broadcast_state()
                            else:
                                _budget.pause_for_quota()
                                logger.warning("\u23f8  [Quota] Models exhausted (mode=%s) - pausing DAG until midnight PT", _qpm)
                                if state:
                                    state.record_activity("warning", f"Quota pause ({_qpm}): pausing until midnight PT reset")
                                    await state.broadcast_state()
                        else:
                            logger.info("⚡  [Quota] Models exhausted but quota_pause_mode=%s — skipping pause.", _qpm)
                    except Exception:
                        pass
                    await _update_dag_progress(planner, depth, state=state)  # V41: live UI
                    if state:
                        state.record_activity("warning", f"{node.task_id} rate-limited — all models exhausted")
                    return

                # V65: Safe-stop guard — if shutdown was requested while this
                # task was running, don't waste quota on a retry. Reset to
                # pending so the task can be resumed in the next session.
                if state and getattr(state, 'stop_requested', False):
                    node.status = "pending"
                    node.started_at = None
                    planner._save_state()
                    logger.info(
                        "🛑  [Pool] %s failed during shutdown — requeueing as pending for next session.",
                        node.task_id,
                    )
                    print(f"{indent}  {Y}⏸ {node.task_id} failed during shutdown — requeued for next session{R}")
                    if state:
                        state.record_activity(
                            "warning",
                            f"Stop requested: {node.task_id} requeued as pending (will resume next session)",
                        )
                    await _update_dag_progress(planner, depth, state=state)
                    return

                logger.warning(
                    "📋  [Pool] Node %s failed: %s. Auto-fixing …",
                    node.task_id, chunk_result.errors[:2],
                )
                print(f"{indent}  {RED}✗ {node.task_id} failed — auto-fixing …{R}")
                # V40: Timeline milestone — auto-fix attempt
                if state:
                    state.record_activity(
                        "warning",
                        f"Auto-fix: attempting {node.task_id}",
                        str(chunk_result.errors[:1]),
                    )

                # V41: Auto-fix runs without global lock — parallel with other workers
                # V60: Pass timeout=1.5× the original task timeout.
                # A retry prompt is larger (original task + error context) and
                # deserves more runway. Using the same timeout burns the retry
                # on tasks that were already near the limit.
                _orig_timeout = getattr(chunk_result, 'duration_s', 300)
                _retry_timeout = max(450, int(_orig_timeout * 1.5))
                fix_ok, fix_result = await _diagnose_and_retry(
                    task_description=node.description,
                    failure_errors=chunk_result.errors,
                    executor=executor,
                    local_brain=local_brain,
                    session_mem=session_mem,
                    timeout=_retry_timeout,
                    task_id=node.task_id,
                    state=state,
                    silent_timeout=getattr(chunk_result, 'silent_timeout', False),  # V60
                )
                if fix_ok:
                    all_files_changed.extend(fix_result.files_changed)
                    total_duration += fix_result.duration_s
                    planner.mark_complete(node.task_id)
                    await _update_dag_progress(planner, depth, state=state)  # V41: live UI
                    print(
                        f"{indent}  {G}✓ {node.task_id} auto-fixed!"
                        f" ({fix_result.duration_s:.1f}s){R}"
                    )
                    # V40: Timeline milestone — auto-fix success
                    if state:
                        state.record_activity(
                            "success",
                            f"Auto-fix succeeded: {node.task_id} ({fix_result.duration_s:.1f}s)",
                        )
                else:
                    planner.mark_failed(node.task_id, str(chunk_result.errors[:2]))
                    await _update_dag_progress(planner, depth, state=state)  # V41: live UI
                    print(f"{indent}  {RED}✗ {node.task_id} failed (auto-fix unsuccessful){R}")
                    # V40: Timeline milestone — auto-fix failure
                    if state:
                        state.record_activity(
                            "error",
                            f"Auto-fix failed: {node.task_id}",
                            str(chunk_result.errors[:1]),
                        )

                    # V76: Record failed result in TaskIntelligence
                    try:
                        if _task_intel:
                            _tag = _extract_tag(node.description)
                            _task_intel.record_result(
                                task_id=node.task_id,
                                category=_tag,
                                files_changed=chunk_result.files_changed or [],
                                success=False,
                                errors=chunk_result.errors[:5],
                                duration_s=chunk_result.duration_s,
                            )
                    except Exception:
                        pass

                    # Try replanning for this node
                    try:
                        replan_ok, replan_msg = await planner.replan(
                            node.task_id,
                            lesson=str(chunk_result.errors[:2]),
                        )
                        if replan_ok:
                            logger.info("📋  [Pool] Replanned %s: %s", node.task_id, replan_msg)
                            print(f"{indent}  {Y}🔄 Replanned: {replan_msg}{R}")
                            session_mem.record_event("dag_replanned", replan_msg)
                            # V40: Timeline milestone — replan
                            if state:
                                state.record_activity("task", f"Replanned {node.task_id}: {replan_msg}")
                    except Exception as exc:
                        logger.debug("📋  [Pool] Replan skipped: %s", exc)

                # V68: Release semaphore after failure handling completes.
                if _sem_held:
                    sem.release()
                    _sem_held = False

        finally:
            # V68: Safety net — ensure semaphore is always released.
            if _sem_held:
                sem.release()
                _sem_held = False

        # V68: Post-semaphore cleanup — runs regardless of success/failure.
        nodes_since_lint += 1
        # V46: Signal scheduling loop to wake immediately
        _worker_done_event.set()
        await _update_dag_progress(planner, depth, state=state)

    # ── V58: Safety-net finalizer — wraps the entire _pool_worker inner body ──
    # If any unexpected exception escapes all inner handlers, this guarantees
    # the node is marked failed (not stuck as "running" forever).
    _pool_worker_orig = _pool_worker
    async def _pool_worker(node):  # noqa: F811  (intentional re-wrap)
        try:
            await _pool_worker_orig(node)
        except _asyncio.CancelledError:
            # V70: Per-task cancel — user cancelled this task from the UI
            logger.info("⛔  [Pool] Worker %s cancelled by user.", node.task_id)
            try:
                node.status = "cancelled"
                node.started_at = None
                planner._save_state()
                _worker_done_event.set()
                await _update_dag_progress(planner, depth, state=state)
                if state:
                    state.record_activity("user", f"Task cancelled: {node.task_id}")
            except Exception:
                pass
        except Exception as _pw_exc:
            logger.error(
                "🔥  [Pool] Unhandled exception in worker for %s — marking failed to prevent stuck-Running: %s",
                node.task_id, _pw_exc,
            )
            try:
                planner.mark_failed(node.task_id, f"Worker crash: {_pw_exc}")
                _worker_done_event.set()
                await _update_dag_progress(planner, depth, state=state)
            except Exception:
                pass

    # ── V51: Inject boot-time build issues into DAG before scheduling ──
    # The boot _build_health_check() runs before planner exists, so it only
    # creates BUILD_ISSUES.md. Now that planner is ready, inject a fix task
    # if the file has active issues.
    # V53: Also include CONSOLE_ISSUES.md (runtime errors after server start)
    # in the same fix task so the CLI gets the complete picture at once.
    try:
        _boot_issues_path    = Path(effective_project) / "BUILD_ISSUES.md"
        _console_issues_path = Path(effective_project) / "CONSOLE_ISSUES.md"

        _build_content   = _boot_issues_path.read_text(encoding="utf-8") if _boot_issues_path.exists() else ""
        _console_content = _console_issues_path.read_text(encoding="utf-8") if _console_issues_path.exists() else ""

        _has_build_issues = ("## ❌" in _build_content or "## ⚠" in _build_content) \
                            and "## ✅ All Clear" not in _build_content
        _has_console_issues = "## ❌" in _console_content and "## ✅ All Clear" not in _console_content

        if (_has_build_issues or _has_console_issues) and not (state and getattr(state, 'stop_requested', False)):
            # V73: Use @file references instead of inlining issue content.
            # The Gemini CLI worker reads these files natively — no need to
            # extract lines and paste them into the prompt.
            _issue_refs = []
            if _has_build_issues:
                _issue_refs.append("@BUILD_ISSUES.md")
            if _has_console_issues:
                _issue_refs.append("@CONSOLE_ISSUES.md")
            _issue_refs_str = " ".join(_issue_refs)

            _fix_desc = (
                f"{_issue_refs_str}\n\n"
                "[BUILD] Read BUILD_ISSUES.md and CONSOLE_ISSUES.md "
                "in the project root and fix ALL issues listed under ❌ and ⚠ sections.\n\n"
                "FOR OUTDATED DEPENDENCIES (Major Version Bumps): These are "
                "breaking changes that npm update cannot auto-resolve. For each "
                "major bump, check the package's changelog/migration guide for "
                "API changes, update any imports or config accordingly, then "
                "bump the version in package.json. After updating, delete "
                "package-lock.json so the next install picks up the new versions. "
                "(Non-breaking patch/minor updates were already auto-applied.)\n\n"
                "FOR BUILD ISSUES: Fix TypeScript errors, Vite config problems, "
                "missing modules, and ESM/CJS mismatches directly in the source.\n\n"
                "FOR CONSOLE/RUNTIME ISSUES: Fix runtime errors (ReferenceError, "
                "TypeError, uncaught exceptions, failed module loads) so the app "
                "renders without errors in the browser.\n\n"
                "After fixing each issue, update the respective file to move it "
                "to the ✅ Resolved section. Once ALL issues are resolved, "
                "delete both files — a clean project has no issues files."
            )

            _boot_offset = planner.get_task_offset() + 1 if planner else 1
            _injected = planner.inject_task(
                task_id=f"t{_boot_offset}-BUILD",
                description="[BUILD] " + _fix_desc,
                dependencies=[],
                priority=90,
            )
            if _injected:
                _files_injected = []
                if _has_build_issues:
                    _files_injected.append("BUILD_ISSUES.md")
                if _has_console_issues:
                    _files_injected.append("CONSOLE_ISSUES.md")
                logger.info(
                    "🔍  [Health] Injected boot fix task (priority=90) → %s",
                    " + ".join(_files_injected),
                )
                if state:
                    state.record_activity("task",
                        f"Boot health fix injected: {' + '.join(_files_injected)}")
    except Exception as exc:
        logger.debug("🔍  [Build Health] Boot injection skipped: %s", exc)

    # ── V54: Inject dep-upgrade build-health task if npm outdated finds stale packages ──
    # Runs npm outdated --json (non-blocking, fast) to detect deprecated/outdated deps.
    # If any major or deprecated packages found, injects a high-priority task to upgrade them.
    try:
        _pkg_json = Path(effective_project) / "package.json"
        if _pkg_json.exists() and not (state and getattr(state, 'stop_requested', False)):
            # Already have a DAG injection? Don't double-inject on resume
            _deps_already_queued = any(
                (n.task_id == "build-health-deps" or n.task_id.endswith("-DEPS"))
                and n.status in ("pending", "running", "complete", "failed", "skipped")
                for n in planner._nodes.values()
            )
            # V54: Also skip if we already ran a dep upgrade in this session.
            # Without this, every build-health-check invocation (coherence gate, boot, etc.)
            # would each spawn a separate upgrade task, causing 2-3 back-to-back upgrades.
            _dep_upgrade_session_done = getattr(state, '_dep_upgrade_done', False) if state else False
            if not _deps_already_queued and not _dep_upgrade_session_done:
                import asyncio as _aio_dep
                import subprocess as _sp_dep
                try:
                    _outdated_proc = await _aio_dep.wait_for(
                        _aio_dep.create_subprocess_exec(
                            "npm", "outdated", "--json",
                            cwd=str(effective_project),
                            stdout=_sp_dep.PIPE,
                            stderr=_sp_dep.DEVNULL,
                        ),
                        timeout=15,
                    )
                    _outdated_stdout, _ = await _aio_dep.wait_for(
                        _outdated_proc.communicate(), timeout=15
                    )
                    _outdated_raw = _outdated_stdout.decode("utf-8", errors="replace").strip()
                except Exception:
                    _outdated_raw = ""

                # npm outdated --json exits non-zero when outdated packages exist,
                # but still writes valid JSON. Parse it regardless of exit code.
                _outdated_pkgs: dict = {}
                if _outdated_raw:
                    try:
                        import json as _json_dep
                        _outdated_pkgs = _json_dep.loads(_outdated_raw)
                    except Exception:
                        _outdated_pkgs = {}

                # Separate major bumps (breaking) from minor/patch (safe auto-update)
                _major_bumps = []
                _minor_bumps = []
                for _pkg_name, _pkg_info in _outdated_pkgs.items():
                    if not isinstance(_pkg_info, dict):
                        continue
                    _cur  = str(_pkg_info.get("current", "0")).lstrip("^~>=")
                    _want = str(_pkg_info.get("wanted",  "0")).lstrip("^~>=")
                    _late = str(_pkg_info.get("latest",  "0")).lstrip("^~>=")
                    # Major version bump = first digit differs
                    _cur_major  = _cur.split(".")[0]  if _cur  else "0"
                    _late_major = _late.split(".")[0] if _late else "0"
                    if _cur_major != _late_major:
                        _major_bumps.append(f"{_pkg_name}: {_cur} → {_late}")
                    else:
                        _minor_bumps.append(f"{_pkg_name}: {_cur} → {_late}")

                if _outdated_pkgs:
                    _dep_list_str = ""
                    if _major_bumps:
                        _dep_list_str += f"MAJOR version bumps (check for breaking changes):\n"
                        _dep_list_str += "\n".join(f"  - {p}" for p in _major_bumps[:15]) + "\n\n"
                    if _minor_bumps:
                        _dep_list_str += f"Minor/patch updates (safe to auto-upgrade):\n"
                        _dep_list_str += "\n".join(f"  - {p}" for p in _minor_bumps[:20]) + "\n"
                    if len(_outdated_pkgs) > 35:
                        _dep_list_str += f"\n... and {len(_outdated_pkgs) - 35} more.\n"

                    _major_list_for_prompt = "\n".join(f"  - {p}" for p in _major_bumps[:15]) if _major_bumps else "  (none)"
                    _dep_upgrade_desc = (
                        "[FUNC] Upgrade all outdated npm dependencies to their latest stable "
                        "versions AND migrate all source code to use the new APIs.\n\n"
                        f"Found {len(_outdated_pkgs)} outdated package(s):\n{_dep_list_str}\n"
                        "STEPS - execute ALL of these, do not stop early:\n\n"
                        "STEP 1 - Upgrade package.json:\n"
                        "  Run `cd /workspace && npx npm-check-updates@latest -u`\n"
                        "  This rewrites package.json with latest versions for ALL packages.\n\n"
                        "STEP 2 - Clean install:\n"
                        "  Run `rm -f package-lock.json && npm install --no-audit --no-fund`\n\n"
                        "STEP 3 - Research breaking changes for MAJOR version bumps:\n"
                        f"  These packages had a major version change:\n{_major_list_for_prompt}\n"
                        "  For each major bump: use your web browsing / file reading capability\n"
                        "  to look up the package's official CHANGELOG or migration guide\n"
                        "  (e.g. 'eslint 9 migration guide', 'vite 6 migration guide').\n"
                        "  Identify renamed exports, changed APIs, removed options, new config format.\n\n"
                        "STEP 4 - Update ALL source code references:\n"
                        "  Search the project source files for usage of the old APIs.\n"
                        "  Update imports, config files (eslint.config.js, vite.config.ts, etc.),\n"
                        "  and any call sites that reference renamed or removed APIs. Examples:\n"
                        "  - eslint@9 uses flat config (eslint.config.js), not .eslintrc.*\n"
                        "  - Vite@6 changed some plugin APIs and config options\n"
                        "  - React@19 removed legacy APIs (ReactDOM.render -> createRoot)\n"
                        "  Do a full grep of the source directory for each deprecated pattern\n"
                        "  and fix every occurrence.\n\n"
                        "STEP 5 - Verify the build:\n"
                        "  Run `npm run build`. If it fails, fix any TypeScript errors or\n"
                        "  config issues introduced by the version bumps, then rebuild.\n\n"
                        "STEP 6 - Security audit:\n"
                        "  Run `npm audit fix` to address remaining advisories.\n\n"
                        "CRITICAL: Do NOT skip Steps 3-4. Upgrading package.json without\n"
                        "migrating source code leaves the project broken. Every major bump\n"
                        "MUST have its source code reviewed and updated."
                    )

                    _deps_off = planner.get_task_offset() + 1 if planner else 1
                    _dep_injected = planner.inject_task(
                        task_id=f"t{_deps_off}-DEPS",
                        description=_dep_upgrade_desc,
                        dependencies=[],
                        priority=85,
                    )
                    if _dep_injected:
                        logger.info(
                            "📦  [Deps] Injected dep-upgrade task: %d outdated packages "
                            "(%d major, %d minor/patch)",
                            len(_outdated_pkgs), len(_major_bumps), len(_minor_bumps),
                        )
                        if state:
                            state.record_activity(
                                "task",
                                f"Dep upgrade: {len(_outdated_pkgs)} outdated packages detected "
                                f"({len(_major_bumps)} major, {len(_minor_bumps)} minor/patch)",
                            )
                            # V54: Mark upgrade as queued for this session so the coherence-gate
                            # build-health-check doesn't inject a second upgrade task.
                            state._dep_upgrade_done = True
    except Exception as _dep_exc:
        logger.debug("📦  [Deps] Outdated check skipped: %s", _dep_exc)

    # ── Main scheduling loop — feed workers as nodes become unblocked ──
    import time as _time
    _last_retry_sweep = _time.monotonic()
    while True:
        # V40: Safe stop check — drain workers, block new launches
        if state and getattr(state, 'stop_requested', False):
            # V69/S1: Propagate stop flag to executor bridge module
            try:
                from . import _supervisor_state_ref
                _supervisor_state_ref.stop_requested = True
            except Exception:
                pass

            running_tasks = {tid: t for tid, t in active_tasks.items() if not t.done()}
            n_running = len(running_tasks)

            # V46: Track worker count for the stop API/UI
            state._active_worker_count = n_running

            # V46: Force stop — cancel everything immediately
            if getattr(state, 'force_stop', False):
                logger.info("🛑  [Pool] FORCE STOP — cancelling %d active workers.", n_running)
                print(f"{indent}{config.ANSI_YELLOW}🛑 Force stop — cancelling {n_running} worker(s) immediately.{config.ANSI_RESET}")
                for tid, t in running_tasks.items():
                    t.cancel()
                    logger.info("🛑  [Pool] Cancelled worker: %s", tid)
                # Brief wait for cancellations to propagate
                if running_tasks:
                    await _asyncio.sleep(0.5)
                if state:
                    state.record_activity("system", f"Force stop complete: {n_running} worker(s) cancelled")
                    # Save DAG state before exit
                    try:
                        planner.save_epic_state()
                        planner.write_progress_file()
                    except Exception:
                        pass
                    await state.broadcast_state()
                logger.info("🛑  [Pool] Force stop complete.")
                print(f"{indent}{config.ANSI_GREEN}✅ Force stop complete.{config.ANSI_RESET}")
                break

            # Graceful drain — wait for in-flight workers
            logger.info("🛑  [Pool] Safe stop requested — draining %d active workers.", n_running)
            print(f"{indent}{config.ANSI_YELLOW}🛑 Safe stop — draining {n_running} active worker(s).{config.ANSI_RESET}")
            if state:
                state.record_activity(
                    "system",
                    f"Safe stop: draining {n_running} active worker(s) — no new tasks will launch",
                )
                await state.broadcast_state()

            # Wait for all in-flight workers to finish
            if running_tasks:
                try:
                    await _asyncio.gather(*running_tasks.values(), return_exceptions=True)
                except Exception as exc:
                    logger.warning("🛑  [Pool] Drain exception: %s", exc)

            # V46: Save DAG state in parallel with broadcast
            if state:
                state.record_activity("system", "Safe stop: all workers drained — exiting cleanly")
                try:
                    planner.save_epic_state()
                    planner.write_progress_file()
                except Exception:
                    pass
                await state.broadcast_state()
            logger.info("🛑  [Pool] All workers drained. Exiting scheduling loop.")
            print(f"{indent}{config.ANSI_GREEN}✅ All workers drained — safe stop complete.{config.ANSI_RESET}")
            break
        # V46: Drain instruction queue IN the scheduling loop — not just in
        # the monitoring loop.  During a DAG execution (which can last hours),
        # the monitoring loop is blocked.  Without this, user prompts submitted
        # mid-DAG sit in state.queue untouched until the entire DAG finishes.
        if state and hasattr(state, 'queue'):
            while True:
                _ui = state.queue.pop_nowait()
                if not _ui:
                    break
                _ui_text = _ui.text.strip()
                if not _ui_text:
                    continue
                # Inject as high-priority DAG task so get_parallel_batch picks it up next
                _instr_ctr = getattr(state, '_instr_counter', 0) + 1
                state._instr_counter = _instr_ctr
                _ui_task_id = f"user-instr-{_instr_ctr}"
                _injected = planner.inject_task(
                    task_id=_ui_task_id,
                    description=f"[User Instruction] {_ui_text}",
                    dependencies=[],
                    priority=100,
                )
                if _injected:
                    logger.info(
                        "📬  [Pool] Injected user prompt as DAG task %s (priority=100)",
                        _ui_task_id,
                    )
                    if state:
                        state.record_activity(
                            "instruction",
                            f"User prompt injected into active DAG: {_ui_text[:80]}",
                        )
                    await _update_dag_progress(planner, depth, state=state)
                    await state.broadcast_state()
                    # V53: Wake the scheduling loop immediately so the priority
                    # task picks up the first available worker slot, not after
                    # the 2-second heartbeat.
                    _worker_done_event.set()

        # V46: Auto-retry sweep — re-queue failed tasks every 30s
        # even while workers are active. Previously only ran when all idle.

        # V73: Total silence guard — skip ALL processing when quota is paused.
        # Stop check and instruction queue drain still run (above) so the user
        # can stop the system or queue instructions for when pause ends.
        try:
            from .retry_policy import get_daily_budget as _get_silence_budget
            _silence_budget = _get_silence_budget()
            if _silence_budget.quota_paused:
                if state and state.status != "quota_paused":
                    state.status = "quota_paused"
                    await state.broadcast_state()
                # Sleep 30s and re-check (stop-aware)
                for _ in range(6):  # 6 x 5s = 30s total
                    if state and getattr(state, 'stop_requested', False):
                        break
                    if not _silence_budget._quota_paused:
                        break
                    await _asyncio.sleep(5)
                continue  # Skip ALL node processing, launches, retry sweeps
        except Exception:
            pass

        _now_mono = _time.monotonic()
        if _now_mono - _last_retry_sweep > 30:
            _last_retry_sweep = _now_mono
            retriable = planner.get_failed_retriable()
            for failed_node in retriable:
                if planner.mark_retry(failed_node.task_id):
                    logger.info("🔄  [AutoRetry] Re-queued %s (retry %d/%d)",
                        failed_node.task_id, failed_node.retry_count, failed_node.max_retries)
                    if state:
                        state.record_activity("task",
                            f"Auto-retry: {failed_node.task_id} (attempt {failed_node.retry_count})")

        # V46: Purge completed tasks from active_tasks so their IDs don't
        # block slots. Previously done tasks stayed in active_tasks forever,
        # preventing get_parallel_batch from filling empty worker slots.
        for _tid in [tid for tid, t in active_tasks.items() if t.done()]:
            del active_tasks[_tid]

        # V70: Per-task cancel bridge — drain cancel signals from state
        if state and hasattr(state, '_cancel_task_ids') and state._cancel_task_ids:
            for _cid in list(state._cancel_task_ids):
                if _cid in active_tasks and not active_tasks[_cid].done():
                    active_tasks[_cid].cancel()
                    logger.info("⛔  [Pool] User cancelled task: %s", _cid)
                    print(f"{indent}  {Y}⛔ User cancelled: {_cid}{R}")
                    if state:
                        state.record_activity("user", f"Cancelled running task: {_cid}")
                state._cancel_task_ids.discard(_cid)

        # Find all unblocked nodes not already running.
        # V51: 2× pre-fill keeps all max_workers slots busy — tasks are already
        # queued at the semaphore when a running slot opens, preventing gaps.
        _pipeline_depth = max_workers * 2
        unblocked = planner.get_parallel_batch(max_workers=_pipeline_depth)
        new_nodes = [n for n in unblocked if n.task_id not in active_tasks]

        # V53: Priority preemption — if a high-priority task (≥90) is ready,
        # cancel any regular tasks that are queued at the semaphore but not yet
        # running (node.status still 'pending'). Then rebuild new_nodes with the
        # priority task FIRST so it gets the first available worker slot.
        #
        # GUARD: only trigger if the priority task has NO asyncio Task yet.
        # If it's already in active_tasks (waiting at the semaphore), preemption
        # already happened — re-triggering every loop tick causes infinite log spam.
        _priority_in_new  = [n for n in new_nodes
                             if getattr(n, 'priority', 0) >= 90
                             and n.task_id not in active_tasks]  # ← not yet launched
        _priority_waiting = []
        # _priority_waiting is intentionally NOT checked against active_tasks nodes
        # because once a task is in active_tasks it is already queued/running.
        # (The old code checked active_tasks for pending-status tasks, which matched
        # tasks blocked at the semaphore — causing infinite re-preemption.)
        _priority_ready = _priority_in_new or _priority_waiting

        if _priority_ready:
            # Only cancel regular tasks that are queued but NOT yet in active_tasks
            # (i.e., they're in new_nodes but haven't been create_task'd yet).
            # Never cancel tasks already in active_tasks — they hold semaphore or are running.
            _preempted_nodes = [n for n in new_nodes
                                if getattr(n, 'priority', 0) < 90
                                and n.task_id not in active_tasks]
            # Only log once per unique priority task — avoid infinite log spam
            _prio_id = _priority_ready[0].task_id
            if _preempted_nodes and _prio_id not in _last_preempt_for:
                _last_preempt_for.add(_prio_id)
                logger.info(
                    "📬  [Pool] Priority preemption: held back %d regular task(s) "
                    "for priority task %s",
                    len(_preempted_nodes), _prio_id,
                )
            elif _prio_id in _last_preempt_for and not _preempted_nodes:
                # Priority task is now dispatching — clear the guard for next time
                _last_preempt_for.discard(_prio_id)
            # Rebuild new_nodes: priority tasks first, then regular
            _priority_nodes = [n for n in new_nodes if getattr(n, 'priority', 0) >= 90]
            _regular_nodes  = [n for n in new_nodes if getattr(n, 'priority', 0) < 90]
            new_nodes = (_priority_nodes + _regular_nodes)[:_pipeline_depth]


        # V46: Manual mode gate — only allow user-injected tasks (priority > 0)
        # to launch. Regular DAG tasks (priority == 0) wait until auto mode.
        # The scheduling loop stays alive so user prompts are still processed.
        if state and getattr(state, 'execution_mode', 'auto') == 'manual':
            _user_tasks = [n for n in new_nodes if getattr(n, 'priority', 0) > 0]
            if new_nodes and not _user_tasks:
                # Regular tasks waiting — don't exit, just sleep and re-check
                running_tasks = {tid: t for tid, t in active_tasks.items() if not t.done()}
                if not running_tasks:
                    state.status = "paused"
                    await state.broadcast_state()
                await asyncio.sleep(2)
                continue
            new_nodes = _user_tasks

        # V43/V73: Scheduling loop quota guard — verified resume
        try:
            from .retry_policy import get_daily_budget as _get_budget
            _sched_budget = _get_budget()
            if _sched_budget.quota_paused:
                if state:
                    state.status = "quota_paused"
                # V73: Verified resume loop
                while _sched_budget._quota_paused:
                    _sched_wait = max(10, _sched_budget._quota_resume_at - time.time()) if _sched_budget._quota_resume_at > 0 else 60
                    logger.warning("⏸  [Pool] Quota paused — scheduling loop sleeping %.0fs", _sched_wait)
                    if state:
                        state.record_activity("warning", f"Quota paused: scheduler sleeping {_sched_wait:.0f}s")
                        await state.broadcast_state()
                    # Sleep in 30s chunks (stop-aware + manual resume aware)
                    _sch_slept = 0
                    while _sch_slept < _sched_wait:
                        if state and getattr(state, 'stop_requested', False):
                            break
                        if not _sched_budget._quota_paused:
                            break
                        _sch_chunk = min(30, _sched_wait - _sch_slept)
                        await _asyncio.sleep(_sch_chunk)
                        _sch_slept += _sch_chunk
                    if state and getattr(state, 'stop_requested', False):
                        break
                    # V73: Verify quota via /stats probe before resuming
                    _verified = await _asyncio.to_thread(_sched_budget.verified_resume_from_quota)
                    if _verified:
                        break  # Quota confirmed — proceed
                if state and not getattr(state, 'stop_requested', False):
                    state.status = "running"
                    await state.broadcast_state()
                continue
        except Exception:
            pass

        # V40 FIX: Skip launches if Gemini API is on cooldown.
        # Without this, workers launch against a dead API, waste retries,
        # and exhaust their max_retries quota without doing real work.
        if new_nodes:
            try:
                from .retry_policy import get_failover_chain
                _fc = get_failover_chain()
                if _fc.all_models_on_cooldown():
                    _wait = min(60, _fc.get_soonest_cooldown_remaining())
                    logger.warning(
                        "⚡  [Pool] ALL models on cooldown — pausing worker launches for %.0fs",
                        _wait,
                    )
                    if state:
                        state.record_activity(
                            "warning",
                            f"API cooldown: pausing {len(new_nodes)} worker launches for {_wait:.0f}s",
                        )
                    await _asyncio.sleep(max(5.0, _wait))
                    new_nodes = []  # Don't launch — will re-check next loop iteration
            except Exception:
                pass

        # Launch new workers up to semaphore limit
        for node in new_nodes:
            task = _asyncio.create_task(_pool_worker(node))
            active_tasks[node.task_id] = task

        # Broadcast queued status immediately after submitting new workers.
        # Nodes in active_tasks that are still status='pending' (waiting at the semaphore)
        # are passed as queued_ids so the UI shows them as 'Queued', not 'Pending'.
        if new_nodes:
            _queued_ids = {
                tid for tid, t in active_tasks.items()
                if not t.done() and tid in planner._nodes
                and planner._nodes[tid].status == "pending"
            }
            if _queued_ids:
                await _update_dag_progress(planner, depth, state=state, queued_ids=_queued_ids)

        # If nothing is running and nothing can be launched, check for retries
        running_tasks = {tid: t for tid, t in active_tasks.items() if not t.done()}
        if not running_tasks and not new_nodes:
            # V40: Re-queue failed tasks before giving up
            retriable = planner.get_failed_retriable()
            if retriable:
                for failed_node in retriable:
                    if planner.mark_retry(failed_node.task_id):
                        logger.info(
                            "🔄  [Pool] Re-queued failed task %s for retry",
                            failed_node.task_id,
                        )
                        if state:
                            state.record_activity(
                                "task",
                                f"Re-queuing {failed_node.task_id} (retry {failed_node.retry_count}/{failed_node.max_retries})",
                            )
                await _update_dag_progress(planner, depth, state=state)
                continue  # Re-check for newly unblocked nodes

            # V45 FIX: Re-check for pending tasks one final time before exiting.
            # Tasks may have been re-queued inside _pool_worker (via replan/mark_retry)
            # AFTER the scheduling loop's get_parallel_batch() call.
            _recheck = planner.get_parallel_batch(max_workers=max_workers)
            _recheck_new = [n for n in _recheck if n.task_id not in active_tasks]
            if _recheck_new:
                logger.info(
                    "🔄  [Pool] Found %d pending tasks on re-check — continuing.",
                    len(_recheck_new),
                )
                continue  # New pending tasks found — re-enter the scheduling loop

            break  # Truly nothing left — DAG is done

        # Wait for any one task to complete, then loop to fill idle slots
        # V45: Add a heartbeat sentinel — ensures broadcast_state() fires every
        # ~10s even during long Gemini CLI calls (which block 3+ minutes).
        # Without this, the UI stalls because no one calls broadcast_state()
        # while the scheduling loop is stuck in asyncio.wait().
        if running_tasks:
            # V46: Reduced heartbeat from 10s → 2s for faster task pickup.
            # Also waits on _worker_done_event for instant wake.
            async def _heartbeat():
                await _asyncio.sleep(2)
            async def _event_waiter():
                await _worker_done_event.wait()
                _worker_done_event.clear()
            _hb = _asyncio.create_task(_heartbeat())
            _ew = _asyncio.create_task(_event_waiter())
            all_waitable = set(running_tasks.values()) | {_hb, _ew}
            done_set, _ = await _asyncio.wait(
                all_waitable,
                return_when=_asyncio.FIRST_COMPLETED,
            )
            # Cancel unused sentinels
            if not _hb.done():
                _hb.cancel()
            if not _ew.done():
                _ew.cancel()
            if _hb in done_set:
                # Heartbeat fired — broadcast progress and re-loop
                done_set.discard(_hb)
                await _update_dag_progress(
                    planner, depth,
                    running=[tid for tid, t in running_tasks.items() if not t.done()],
                    state=state,
                )
            done_set.discard(_ew)  # Event waiter is a sentinel, not a real task
            # Process exceptions from completed tasks.
            # CRITICAL: if a worker raises an unhandled exception, we must mark
            # its DAG node as 'failed' — otherwise the node stays stuck in
            # 'running' state forever and blocks all downstream tasks.
            _task_to_id = {v: k for k, v in active_tasks.items()}
            for t in done_set:
                _exc = None
                try:
                    _exc = t.exception()
                except Exception:
                    pass
                if _exc:
                    _crashed_id = _task_to_id.get(t)
                    logger.error(
                        "📋  [Pool] Worker raised: %s%s",
                        _exc,
                        f" (node={_crashed_id})" if _crashed_id else "",
                    )
                    if _crashed_id and _crashed_id in planner._nodes:
                        _crashed_node = planner._nodes[_crashed_id]
                        if _crashed_node.status == "running":
                            planner.mark_failed(_crashed_id, errors=[str(_exc)])
                            logger.warning(
                                "📋  [Pool] Marked node %s as failed after worker crash.",
                                _crashed_id,
                            )
                            if state:
                                state.record_activity(
                                    "warning",
                                    f"Worker crash: {_crashed_id} → failed ({type(_exc).__name__}: {str(_exc)})",
                                )
                            await _update_dag_progress(planner, depth, state=state)
        else:
            # Edge case: waiting for new nodes to become unblocked
            await _asyncio.sleep(1)

        # Refresh max_workers from budget tracker and dynamically resize semaphore
        try:
            _new_w = get_daily_budget().get_effective_workers()
            if _new_w != _prev_max_workers:
                _delta = _new_w - _prev_max_workers
                # Adjust the semaphore capacity by _delta
                # Positive delta → release extra slots → more concurrent workers
                # Negative delta → reduce available slots (in-progress tasks finish naturally)
                if _delta > 0:
                    for _ in range(_delta):
                        sem.release()
                elif _delta < 0:
                    # Reduce: decrease internal value (can go negative → blocks new acquisitions)
                    sem._value = max(0, sem._value + _delta)
                logger.info(
                    "🔧  [Pool] Worker count changed: %d → %d (sem adjusted by %+d)",
                    _prev_max_workers, _new_w, _delta,
                )
                _prev_max_workers = _new_w
                max_workers = _new_w
                if state:
                    state.record_activity("system", f"Worker count adjusted: {_new_w} concurrent workers")
            else:
                max_workers = _new_w
        except Exception:
            pass

        # ── Periodic coherence gate (every 5 completed nodes) ──
        if nodes_since_lint >= 5 and all_files_changed:
            nodes_since_lint = 0
            logger.info("🔍  [Coherence] Running lint check after %d nodes …", executed_count)
            try:
                lint_result = await executor.execute_task(
                    "Run a quick lint check on recently changed files. "
                    "Fix any syntax errors or import issues you find. "
                    "Do NOT add new features — only fix broken syntax.",
                    timeout=120,
                    preferred_tier="flash",  # Syntax-only — Flash is sufficient
                )
                if lint_result.files_changed:
                    all_files_changed.extend(lint_result.files_changed)
                    total_duration += lint_result.duration_s
                    logger.info(
                        "🔍  [Coherence] Lint pass fixed %d files.",
                        len(lint_result.files_changed),
                    )
            except Exception as exc:
                logger.debug("🔍  [Coherence] Lint check skipped: %s", exc)

            # V51: Run build health check after coherence gate (skip if stopping)
            if not (state and getattr(state, 'stop_requested', False)):
                try:
                    await _build_health_check(
                        executor, sandbox, state,
                        str(effective_project) if effective_project else project_path,
                        planner=planner,
                    )
                except Exception as exc:
                    logger.debug("🔍  [Build Health] Post-coherence check skipped: %s", exc)

            # V74: Architectural drift detection — catch duplicate exports/files
            # Workers operating in parallel can create duplicate utility files or
            # redundant exports. Detect this early and inject a consolidation task.
            if not (state and getattr(state, 'stop_requested', False)):
                try:
                    if sandbox and sandbox.is_running:
                        _drift_result = await sandbox.exec_command(
                            # Find all exported names across .ts/.tsx/.js/.jsx files,
                            # extract "export {function|class|const} NAME" patterns,
                            # then find duplicates across different files.
                            "cd /workspace && "
                            "grep -rn --include='*.ts' --include='*.tsx' --include='*.js' --include='*.jsx' "
                            "  -E 'export (function|class|const|let|var) ' src/ 2>/dev/null | "
                            "sed 's/.*export \\(function\\|class\\|const\\|let\\|var\\) //' | "
                            "sed 's/[(<:{[:space:]].*//' | "
                            "sort | uniq -d",
                            timeout=10,
                        )
                        _dup_names = [n.strip() for n in (_drift_result.stdout or "").splitlines() if n.strip()]
                        if _dup_names and planner:
                            # Check if a drift-fix task is already pending
                            _drift_in_flight = any(
                                n.task_id.startswith("drift-fix")
                                and n.status in ("pending", "running")
                                for n in planner._nodes.values()
                            )
                            if not _drift_in_flight:
                                _dup_preview = ", ".join(_dup_names[:10])
                                logger.warning(
                                    "🏗️  [Drift] Duplicate exported names detected: %s",
                                    _dup_preview,
                                )
                                from .temporal_planner import TaskNode as _DTN
                                _drift_id = f"drift-fix-{int(__import__('time').time())}"
                                planner.inject_task(
                                    task_id=_drift_id,
                                    description=(
                                        "[BUILD] Architectural drift detected: the following names are "
                                        f"exported from MULTIPLE files: {_dup_preview}. "
                                        "For each duplicate, consolidate to a single canonical location. "
                                        "Update all import statements across the codebase to use the "
                                        "canonical export. Remove the redundant duplicate. "
                                        "Run `npx tsc --noEmit` to verify no import breakage."
                                    ),
                                    dependencies=[],
                                    priority=85,
                                )
                                if state:
                                    state.record_activity(
                                        "warning",
                                        f"🏗️ Drift: {len(_dup_names)} duplicate export(s) detected — fix task injected",
                                    )
                except Exception as _drift_exc:
                    logger.debug("🏗️  [Drift] Scan skipped: %s", _drift_exc)

    # Wait for any remaining active tasks to drain
    remaining = [t for t in active_tasks.values() if not t.done()]
    if remaining:
        await _asyncio.gather(*remaining, return_exceptions=True)

    await _update_dag_progress(planner, depth, state=state)

    # ── Hook C: Assess phase completion and advance if done ─────────────────
    # Runs once after the full DAG drains, at depth 0 only.
    if depth == 0 and phase_mgr and not (state and getattr(state, 'stop_requested', False)):
        try:
            _phase_advanced = await phase_mgr.check_and_advance_phase(planner, executor)
            if _phase_advanced:
                # New phase is active — clear the old DAG so next cycle
                # decomposes fresh tasks for the new phase.
                logger.info("📋  [Phase] Phase advanced — clearing DAG for next phase.")
                planner.save_history()
                planner.clear_state()
                if state:
                    state.record_activity("system", "Phase advanced — next phase plan ready. Starting new DAG.")
        except Exception as _hmc_exc:
            logger.debug("📋  [Phase] Hook C error (non-fatal): %s", _hmc_exc)


    # ── Finalize ──
    if planner.is_epic_complete():
        logger.info(
            "📋  [Planner] DAG complete (depth=%d): %d chunks, %.1fs total, %d files changed.",
            depth, executed_count, total_duration, len(all_files_changed),
        )
        print(
            f"{indent}{B}{G}✅ All {executed_count} chunks complete "
            f"({total_duration:.1f}s, {len(all_files_changed)} files){R}"
        )

        # V40: Post-completion audit — check code quality and create fix tasks
        # V41 FIX: Do NOT call clear_state() before audit — the audit needs the
        # completed nodes in memory for context and deduplication.
        # V42: Continuous audit loop — keep auditing until user stops it or
        # an audit finds nothing to fix.
        if depth == 0 and all_files_changed:
            _audit_cycle = 0
            _audit_scan_failures = 0  # V55: persistent failure counter, independent of cycle reset
            # V72: No hard cap — the audit loop runs until it naturally
            # terminates (audit finds 0 issues, user stops, or all tasks
            # in a cycle fail). Projects vary widely in scope.
            while True:
                # V65: Bail immediately if stop is requested — audit tasks are saved to the
                # DAG and will resume as pending in the next session.
                if state and getattr(state, 'stop_requested', False):
                    logger.info("🛑  [Audit] Stop requested — exiting audit loop.")
                    if state:
                        state.record_activity("system", "Audit loop exited — stop requested")
                    break

                # V42: Check if audit loop is still enabled
                if state and not getattr(state, 'audit_loop_enabled', True):
                    logger.info("⏸  [Audit] Audit loop stopped by user.")
                    if state:
                        state.record_activity("system", "Audit loop stopped by user")
                    break

                # V46: Don't run audit cycles in manual mode — wait for auto
                if state and getattr(state, 'execution_mode', 'auto') == 'manual':
                    logger.info("⏸  [Audit] Skipping audit — manual mode active.")
                    state.record_activity("system", "Audit paused: manual mode active")
                    await asyncio.sleep(3)
                    continue

                # V42: Check safe stop
                if state and getattr(state, 'stop_requested', False):
                    logger.info("🛑  [Audit] Skipping audit — safe stop active.")
                    break

                _audit_cycle += 1
                if state:
                    state.audit_cycle = _audit_cycle

                if _audit_cycle > 1:
                    logger.info(
                        "🔄  [Audit] Starting audit cycle %d …", _audit_cycle
                    )
                    print(f"{indent}{B}{C}🔄 Audit cycle {_audit_cycle} — scanning for improvements …{R}")
                    if state:
                        state.record_activity("task", f"Starting audit cycle {_audit_cycle}")
                        await state.broadcast_state()

                    # V46/V62/V73: Skip audit when quota is paused or models exhausted.
                    try:
                        from .retry_policy import get_failover_chain, get_daily_budget as _get_aud_budget
                        _aud_budget = _get_aud_budget()
                        if _aud_budget.quota_paused:
                            if state:
                                state.status = "quota_paused"
                            # V73: Verified resume loop
                            while _aud_budget._quota_paused:
                                _wait_s = max(10, _aud_budget._quota_resume_at - time.time()) if _aud_budget._quota_resume_at > 0 else 60
                                _h, _m = divmod(int(_wait_s) // 60, 60)
                                logger.warning(
                                    "⏸  [Audit] Quota paused — sleeping %dh%02dm until quota resets",
                                    _h, _m,
                                )
                                if state:
                                    state.record_activity("warning", f"⏸ Audit paused: waiting {_h}h{_m:02d}m for quota reset")
                                    await state.broadcast_state()
                                # Sleep in 30s chunks (stop-aware)
                                _aud_slept = 0
                                while _aud_slept < _wait_s:
                                    if state and getattr(state, 'stop_requested', False):
                                        break
                                    if not _aud_budget._quota_paused:
                                        break
                                    _aud_chunk = min(30, _wait_s - _aud_slept)
                                    await asyncio.sleep(_aud_chunk)
                                    _aud_slept += _aud_chunk
                                if state and getattr(state, 'stop_requested', False):
                                    break
                                # V73: Verify quota via /stats probe before resuming
                                _verified = await asyncio.to_thread(_aud_budget.verified_resume_from_quota)
                                if _verified:
                                    break  # Quota confirmed — proceed
                            if state and not getattr(state, 'stop_requested', False):
                                state.status = "running"
                                await state.broadcast_state()
                            continue

                        _fc = get_failover_chain()
                        if _fc.get_active_model() is None:
                            logger.info("⏸  [Audit] All models on cooldown — skipping audit cycle.")
                            if state:
                                state.record_activity("system", "Audit skipped: all API models on cooldown")
                            # V65: Don't waste 30s if stop is requested
                            if state and getattr(state, 'stop_requested', False):
                                logger.info("🛑  [Audit] Stop requested — skipping cooldown wait.")
                                break
                            await asyncio.sleep(30)  # Wait before retrying
                            continue
                    except Exception:
                        pass

                    # Brief pause between cycles to avoid API hammering
                    await asyncio.sleep(5)

                # V57: O2 — Vision refresh every 3rd audit cycle
                # Re-runs vision planning in "update" mode so VISION.md stays
                # current as the project evolves beyond the original brief.
                # V62: Skip if quota is paused — vision refresh is low-priority.
                _vision_quota_ok = True
                try:
                    from .retry_policy import get_daily_budget as _gvb
                    if _gvb().quota_paused:
                        _vision_quota_ok = False
                except Exception:
                    pass
                if _audit_cycle > 0 and _audit_cycle % 3 == 0 and effective_project and _vision_quota_ok:
                    try:
                        from pathlib import Path as _PV
                        _vp = _PV(effective_project) / "VISION.md"
                        if _vp.exists():
                            # V73: Use @file reference — the CLI loads VISION.md natively.
                            # all_files=True is also set on the ask_gemini call below,
                            # so this was being loaded twice before.
                            _vrefresh_prompt = (
                                "@VISION.md\n\n"
                                "You are updating a project VISION.md after significant work has been done.\n\n"
                                f"ORIGINAL GOAL:\n{goal}\n\n"
                                "The current VISION.md has been loaded into your context above.\n\n"
                                "Read the current project files and:\n"
                                "1. Update the feature list to reflect what's now built vs what remains\n"
                                "2. Add any NEW goals or features that have emerged from the implementation\n"
                                "3. Update the priority hierarchy based on current progress\n"
                                "4. Add a dated ## Update section at the bottom with key changes\n\n"
                                "Write the complete updated VISION.md. Keep all original sections, update them in place."
                            )
                            from .gemini_advisor import ask_gemini
                            _vr = await ask_gemini(_vrefresh_prompt, timeout=180, use_cache=False, all_files=True, model=config.GEMINI_FALLBACK_MODEL)
                            # V73: Track budget/quota for vision refresh Gemini call
                            try:
                                from .retry_policy import get_daily_budget as _vrdb, get_quota_probe as _vrqp
                                _vrdb().record_request()
                                _vrqp().record_usage()
                            except Exception:
                                pass
                            if _vr and len(_vr) > 300:
                                _vp.write_text(_vr, encoding="utf-8")
                                logger.info("🔭  [Vision] Refreshed VISION.md at audit cycle %d", _audit_cycle)
                                if state:
                                    state.record_activity("system", f"🔭 VISION.md refreshed at cycle {_audit_cycle}")
                    except Exception as _vr_exc:
                        logger.debug("[Vision] Refresh skipped: %s", _vr_exc)

                audit_result = await _audit_completed_work(
                    files_changed=all_files_changed,
                    executor=executor,
                    local_brain=local_brain,
                    planner=planner,
                    session_mem=session_mem,
                    goal=goal,
                    effective_project=effective_project,
                    state=state,
                    indent=indent,
                    phase_mgr=phase_mgr,
                )
                if audit_result is None:
                    # V55: Track scan failures with a separate counter (not _audit_cycle,
                    # which resets). After 5 failures, break to avoid infinite loop.
                    _audit_scan_failures += 1
                    _MAX_SCAN_FAILURES = 5
                    logger.warning(
                        "🔍  [Audit] Cycle %d scan failed (%d/%d) — %s.",
                        _audit_cycle,
                        _audit_scan_failures,
                        _MAX_SCAN_FAILURES,
                        "retrying after delay" if _audit_scan_failures < _MAX_SCAN_FAILURES else "giving up",
                    )
                    print(f"{indent}{Y}⚠️  Audit scan failed ({_audit_scan_failures}/{_MAX_SCAN_FAILURES}) — retrying …{R}")
                    if state:
                        state.record_activity("system", f"Audit scan failed ({_audit_scan_failures}/{_MAX_SCAN_FAILURES}) — will retry")
                    if _audit_scan_failures >= _MAX_SCAN_FAILURES:
                        logger.warning(
                            "🔍  [Audit] Max scan failures (%d) reached — aborting audit loop.",
                            _MAX_SCAN_FAILURES,
                        )
                        break  # Stop infinite retry on deterministic errors
                    # Reset cycle counter so retry gets a fresh attempt, then cool down
                    _audit_cycle = max(0, _audit_cycle - 1)
                    # V65: Don't sleep if stop is requested
                    if state and getattr(state, 'stop_requested', False):
                        logger.info("🛑  [Audit] Stop requested — aborting audit retry cooldown.")
                        break
                    await asyncio.sleep(30)  # brief cooldown before retry
                    continue  # DO NOT break — a timeout is not a clean pass

                total_duration += audit_result.get("duration_s", 0)

                # V41: If audit injected follow-on DAG tasks, re-kick the worker pool
                # so they execute through the standard DAG pipeline (visible in Graph
                # tab, tracked, retried, and resumable across sessions).
                _audit_tasks = audit_result.get("tasks_created", 0)
                if _audit_tasks == 0:
                    logger.info("✅  [Audit] Cycle %d found no code issues.", _audit_cycle)
                    print(f"{indent}{G}✅ Audit cycle {_audit_cycle}: no code issues found{R}")
                    if state:
                        state.record_activity("success", f"Audit cycle {_audit_cycle}: no code issues found")

                    # ── V73: Final Completion Gate ──────────────────────────────────
                    # Before declaring project complete, verify that ALL tasks in ALL
                    # phases are done and run a final Gemini verification audit against
                    # every phase's exit criteria. If anything is missing, inject the
                    # pending work as DAG tasks and loop back to execution.
                    _project_truly_done = False

                    if phase_mgr and hasattr(phase_mgr, 'is_project_complete'):
                        # Step 1: Final sync of DAG completion → phase tasks
                        try:
                            if hasattr(phase_mgr, 'sync_completion_from_dag'):
                                phase_mgr.sync_completion_from_dag(planner)
                        except Exception:
                            pass

                        # Step 2: Check if all phases have all tasks done
                        if phase_mgr.is_project_complete():
                            logger.info("🏁  [Final Gate] All phase tasks marked done — running final verification audit …")
                            if state:
                                state.record_activity("task", "🏁 Final completion gate: verifying all phase exit criteria …")
                                if hasattr(state, 'set_current_operation'):
                                    state.set_current_operation('🏁 Final completion verification — Gemini checking all exit criteria …')
                                await state.broadcast_state()

                            # Step 3: Final Gemini verification — comprehensive hand-off audit
                            try:
                                from .gemini_advisor import ask_gemini as _fca_ask
                                _phases_summary = phase_mgr.get_all_phases_summary_for_verification()

                                # ── Gather all available project context ──────────────────
                                _fca_context_sections = []
                                _fca_proj = Path(effective_project)

                                # Context notes (user-defined hard constraints)
                                try:
                                    _fca_cn = _fca_proj / ".ag-supervisor" / "context_notes.json"
                                    if _fca_cn.exists():
                                        _fca_notes = json.loads(_fca_cn.read_text(encoding="utf-8"))
                                        if _fca_notes:
                                            _fca_context_sections.append(
                                                "PERSISTENT CONTEXT NOTES (USER-DEFINED HARD CONSTRAINTS):\n"
                                                + "\n".join(f"  {i+1}. {n['text']}" for i, n in enumerate(_fca_notes))
                                            )
                                except Exception:
                                    pass

                                # VISION.md — the north-star product vision
                                try:
                                    _fca_vision = _fca_proj / "VISION.md"
                                    if _fca_vision.exists():
                                        _fca_context_sections.append(
                                            "PRODUCT VISION (read the full file for details):\n"
                                            f"@VISION.md"
                                        )
                                except Exception:
                                    pass

                                # Research document
                                try:
                                    for _rn in ("research.md", "RESEARCH.md"):
                                        _fca_research = _fca_proj / _rn
                                        if _fca_research.exists():
                                            _fca_context_sections.append(
                                                "DOMAIN RESEARCH (read the full file for details):\n"
                                                f"@{_rn}"
                                            )
                                            break
                                except Exception:
                                    pass

                                # SUPERVISOR_MANDATE.md — mission + constraints
                                try:
                                    _fca_mandate = _fca_proj / "SUPERVISOR_MANDATE.md"
                                    if _fca_mandate.exists():
                                        _fca_context_sections.append(
                                            "PROJECT MANDATE (read the full file for details):\n"
                                            "@SUPERVISOR_MANDATE.md"
                                        )
                                except Exception:
                                    pass

                                # User prompts / instructions history
                                try:
                                    _fca_uprts = []
                                    if hasattr(planner, '_user_prompts') and planner._user_prompts:
                                        _fca_uprts = planner._user_prompts
                                    elif hasattr(planner, 'get_all_user_prompts'):
                                        _fca_uprts = planner.get_all_user_prompts() or []
                                    if _fca_uprts:
                                        _fca_context_sections.append(
                                            "USER INSTRUCTIONS & CORRECTIONS (all prompts submitted during this project):\n"
                                            + "\n".join(f"  [{i+1}] {p[:300]}" for i, p in enumerate(_fca_uprts[-50:]))
                                        )
                                except Exception:
                                    pass

                                _fca_extra_ctx = ""
                                if _fca_context_sections:
                                    _fca_extra_ctx = "\n\n".join(_fca_context_sections) + "\n\n"

                                _final_verify_prompt = (
                                    "You are a WORLD-CLASS SENIOR TECHNICAL REVIEWER performing FINAL PROJECT SIGN-OFF.\n"
                                    "This is the LAST gate before the finished product is handed back to the user.\n"
                                    "Your job is to ensure this is the BEST POSSIBLE version of the project — not just\n"
                                    "functional, but polished, complete, and truly excellent.\n\n"
                                    f"ORIGINAL GOAL:\n{goal}\n\n"
                                    f"{_fca_extra_ctx}"
                                    f"ALL PROJECT PHASES AND EXIT CRITERIA:\n{_phases_summary}\n\n"
                                    "═══════════════════════════════════════════════════════════\n"
                                    "FINAL HAND-OFF AUDIT — COMPREHENSIVE VERIFICATION\n"
                                    "═══════════════════════════════════════════════════════════\n\n"
                                    "Read ALL project source files. This audit goes BEYOND bug-checking.\n"
                                    "You are the final approval gate. Check ALL of the following:\n\n"
                                    "1. PHASE EXIT CRITERIA — For EACH phase, verify its exit criteria\n"
                                    "   against the actual code. Every criterion must be fully met.\n\n"
                                    "2. GOAL COMPLETENESS — Compare the original goal (and every detail\n"
                                    "   in the context notes, research, vision, and user instructions above)\n"
                                    "   against what was actually built. Flag ANYTHING mentioned or implied\n"
                                    "   in those sources that isn't implemented.\n\n"
                                    "3. MISSING FEATURES — Are there features that SHOULD exist for a\n"
                                    "   world-class version of this project but weren't planned in any phase?\n"
                                    "   (e.g. error states, loading states, empty states, edge cases,\n"
                                    "   accessibility, responsive design, animations, dark mode,\n"
                                    "   keyboard navigation, SEO, performance optimisations)\n\n"
                                    "4. CODE QUALITY — Stubs, placeholders, TODO comments, broken imports,\n"
                                    "   dead code, missing wiring (components that exist but aren't rendered).\n\n"
                                    "5. POLISH & UX — Does the project feel finished and premium?\n"
                                    "   Missing micro-interactions, inconsistent styling, rough edges,\n"
                                    "   missing transitions, unpolished layouts.\n\n"
                                    "6. ROBUSTNESS — Error handling, validation, edge cases, security.\n"
                                    "   Would any normal user interaction cause a crash or bad experience?\n\n"
                                    "7. PHASE GAPS — Were there things that SHOULD have been in the phase\n"
                                    "   plan but were never added as tasks? Any blind spots in the plan?\n\n"
                                    "OUTPUT: A JSON array of any remaining tasks.\n"
                                    "  If the project is GENUINELY ready for hand-off: [{\"tasks\": []}]\n"
                                    "  If ANY work remains: [{\"id\": \"final-1\", \"description\": \"[FUNC/UI/PERF] ...\", "
                                    "\"acceptance_criteria\": \"...\", \"dependencies\": []}, ...]\n\n"
                                    "Maximum 100 tasks. Each must be specific, actionable, and name exact files.\n"
                                    "IMPORTANT: Respond with ONLY the JSON array. No markdown, no explanation.\n\n"
                                    "BE STRICT AND THOROUGH: This is the LAST check. If you approve this,\n"
                                    "the user gets the product AS-IS. Anything you miss will ship incomplete.\n"
                                    "If you would NOT be proud to hand this product to a paying client,\n"
                                    "list EVERY issue that needs fixing."
                                )

                                logger.info(
                                    "🏁  [Final Gate→Gemini] Verification prompt (%d chars)",
                                    len(_final_verify_prompt),
                                )
                                if state:
                                    state.record_activity("llm_prompt", "Final completion verification", _final_verify_prompt)

                                _final_raw = await _fca_ask(
                                    _final_verify_prompt,
                                    timeout=600,
                                    use_cache=False,
                                    all_files=True,
                                    cwd=effective_project or None,
                                )

                                # V73: Track budget/quota for final verification Gemini call
                                try:
                                    from .retry_policy import get_daily_budget as _fvdb, get_quota_probe as _fvqp
                                    _fvdb().record_request()
                                    _fvqp().record_usage()
                                except Exception:
                                    pass

                                if state and hasattr(state, 'set_current_operation'):
                                    state.set_current_operation('')

                                _final_tasks = []
                                if _final_raw:
                                    logger.info(
                                        "🏁  [Final Gate←Gemini] Response (%d chars): %.500s…",
                                        len(_final_raw), _final_raw,
                                    )
                                    import re as _fca_re
                                    _fca_cleaned = _fca_re.sub(r"```json?\s*", "", _final_raw)
                                    _fca_cleaned = _fca_re.sub(r"```\s*", "", _fca_cleaned).strip()
                                    try:
                                        _fca_parsed = json.loads(_fca_cleaned)
                                        if isinstance(_fca_parsed, dict):
                                            _final_tasks = _fca_parsed.get("tasks", [])
                                        elif isinstance(_fca_parsed, list):
                                            # Filter out confidence_justification entries
                                            _final_tasks = [
                                                t for t in _fca_parsed
                                                if isinstance(t, dict) and "description" in t
                                            ]
                                    except json.JSONDecodeError:
                                        logger.warning("🏁  [Final Gate] Could not parse final verification JSON.")

                                if _final_tasks:
                                    # Final verification found issues — inject as DAG tasks
                                    _fca_injected = 0
                                    for _ft in _final_tasks[:50]:
                                        _ft_id = _ft.get("id", f"final-{_fca_injected + 1}")
                                        _ft_desc = _ft.get("description", "")
                                        _ft_deps = _ft.get("dependencies", [])
                                        if _ft_desc and planner.inject_task(_ft_id, _ft_desc, _ft_deps):
                                            _fca_injected += 1

                                    if _fca_injected > 0:
                                        logger.info(
                                            "🏁  [Final Gate] Verification found %d remaining issue(s) — injecting into DAG.",
                                            _fca_injected,
                                        )
                                        print(f"{indent}{Y}🏁 Final verification: {_fca_injected} issue(s) found — continuing …{R}")
                                        if state:
                                            state.record_activity(
                                                "warning",
                                                f"🏁 Final gate: {_fca_injected} issue(s) found — re-entering execution",
                                            )
                                        # Record these as phase tasks too
                                        if phase_mgr and hasattr(phase_mgr, 'record_audit_tasks'):
                                            phase_mgr.record_audit_tasks([
                                                {"id": _ft.get("id", f"final-{i+1}"), "description": _ft.get("description", "")}
                                                for i, _ft in enumerate(_final_tasks[:50])
                                                if _ft.get("description")
                                            ])
                                        continue  # Loop back to execute + re-audit
                                    else:
                                        _project_truly_done = True
                                else:
                                    # Gemini returned EMPTY response — likely model exhaustion,
                                    # quota pause, or timeout. Do NOT treat as "project complete".
                                    # Let the audit loop retry after quota/model recovery.
                                    logger.warning(
                                        "🏁  [Final Gate] Gemini returned empty response — "
                                        "NOT declaring complete (possible model exhaustion)."
                                    )
                                    if state:
                                        state.record_activity(
                                            "warning",
                                            "🏁 Final verification: empty Gemini response — will retry after cooldown",
                                        )
                                    # Don't set _project_truly_done — fall through to the
                                    # check below which won't break, then loop continues

                            except Exception as _fca_exc:
                                # Model exhaustion, quota errors, timeouts, etc.
                                # Do NOT declare complete — retry after recovery.
                                logger.warning(
                                    "🏁  [Final Gate] Verification error: %s — NOT declaring complete, will retry.",
                                    _fca_exc,
                                )
                                if state:
                                    state.record_activity(
                                        "warning",
                                        f"🏁 Final verification failed: {str(_fca_exc)[:100]} — will retry",
                                    )

                        else:
                            # Step 2b: Phases have pending tasks — inject them into the DAG
                            _incomplete = phase_mgr.get_incomplete_phases()
                            _total_pending = sum(p["pending_count"] for p in _incomplete)
                            logger.info(
                                "🏁  [Final Gate] %d phase(s) still have %d pending task(s) — injecting into DAG.",
                                len(_incomplete), _total_pending,
                            )
                            print(f"{indent}{Y}🏁 Final gate: {_total_pending} phase task(s) still pending across {len(_incomplete)} phase(s) — continuing …{R}")
                            if state:
                                state.record_activity(
                                    "warning",
                                    f"🏁 Final gate: {_total_pending} pending task(s) in {len(_incomplete)} phase(s) — injecting",
                                )

                            _phase_injected = 0
                            for _ip in _incomplete:
                                for _pt_title in _ip["pending_tasks"][:20]:
                                    _pt_id = f"phase{_ip['phase_id']}-final-{_phase_injected + 1}"
                                    _pt_desc = (
                                        f"[PHASE {_ip['phase_id']}] {_pt_title}\n"
                                        f"Phase: {_ip['phase_name']}\n"
                                        f"Exit Criteria: {_ip['exit_criteria']}"
                                    )
                                    if planner.inject_task(_pt_id, _pt_desc, []):
                                        _phase_injected += 1

                            if _phase_injected > 0:
                                logger.info(
                                    "🏁  [Final Gate] Injected %d pending phase task(s) into DAG.",
                                    _phase_injected,
                                )
                                if state:
                                    state.record_activity(
                                        "task",
                                        f"🏁 Injected {_phase_injected} incomplete phase task(s) into DAG for execution",
                                    )
                                continue  # Loop back to execute + re-audit
                            else:
                                # All pending tasks were duplicates — treat as complete
                                logger.info("🏁  [Final Gate] All pending phase tasks already in DAG — proceeding to verification.")
                                _project_truly_done = True

                    else:
                        # No phase manager — simple projects skip phase verification
                        _project_truly_done = True

                    if _project_truly_done:
                        if state:
                            # V55: Mark session as naturally complete so the UI shows the completion
                            # banner without auto-navigating to the shutdown screen. Preview stays live.
                            state.session_complete = True
                            state.status = "complete"
                            await state.broadcast_state()
                            logger.info("🎉  [Session] Project complete — all phases verified. Preview staying live. Press Stop to shut down.")
                        break

                if not planner.has_active_dag():
                    break

                logger.info(
                    "📋  [Audit→DAG] Re-kicking worker pool for %d audit follow-on tasks.",
                    _audit_tasks,
                )
                print(f"{indent}{B}{C}📋 Executing {_audit_tasks} audit follow-on tasks via DAG pool …{R}")
                if state:
                    state.record_activity(
                        "task",
                        f"Re-entering DAG pool for {_audit_tasks} audit follow-on tasks",
                    )

                _dag_progress["active"] = True
                await _update_dag_progress(planner, depth, state=state)

                # Re-enter the scheduling loop for the remaining audit nodes
                _audit_batch_nodes = planner.get_parallel_batch(max_workers=max_workers)
                for _ab_node in _audit_batch_nodes:
                    if _ab_node.task_id not in active_tasks:
                        _ab_task = _asyncio.create_task(_pool_worker(_ab_node))
                        active_tasks[_ab_node.task_id] = _ab_task

                # Drain all audit workers
                _audit_remaining = [t for t in active_tasks.values() if not t.done()]
                if _audit_remaining:
                    await _asyncio.gather(*_audit_remaining, return_exceptions=True)

                # Check for any more unblocked nodes (chain reactions)
                while True:
                    _more = planner.get_parallel_batch(max_workers=max_workers)
                    _new_more = [n for n in _more if n.task_id not in active_tasks]
                    if not _new_more:
                        # Also try re-queuing failed tasks
                        _retriable = planner.get_failed_retriable()
                        if _retriable:
                            for _fn in _retriable:
                                planner.mark_retry(_fn.task_id)
                            continue
                        break
                    for _n in _new_more:
                        _t = _asyncio.create_task(_pool_worker(_n))
                        active_tasks[_n.task_id] = _t
                    _wait_set = [t for t in active_tasks.values() if not t.done()]
                    if _wait_set:
                        await _asyncio.gather(*_wait_set, return_exceptions=True)

                await _update_dag_progress(planner, depth, state=state)

                # Collect results from audit tasks
                _cycle_successes = 0
                _cycle_failures = 0
                for _aid in audit_result.get("injected_ids", []):
                    if _aid in planner._nodes:
                        _a_node = planner._nodes[_aid]
                        if _a_node.status == "complete" and hasattr(_a_node, 'result'):
                            executed_count += 1
                            _cycle_successes += 1
                        elif _a_node.status in ("failed", "error"):
                            _cycle_failures += 1

                # V51: If ALL tasks in this cycle failed, stop auditing.
                # Re-auditing will just generate the same tasks again.
                if _cycle_failures > 0 and _cycle_successes == 0:
                    logger.warning(
                        "🛑  [Audit] All %d tasks in cycle %d failed — stopping audit loop to prevent spin.",
                        _cycle_failures, _audit_cycle,
                    )
                    if state:
                        state.record_activity(
                            "warning",
                            f"Audit cycle {_audit_cycle}: all {_cycle_failures} tasks failed — loop stopped",
                        )
                    break

                # Track files changed by this audit cycle for the next iteration
                _cycle_files = []
                for _aid in audit_result.get("injected_ids", []):
                    if _aid in planner._nodes:
                        _n = planner._nodes[_aid]
                        if hasattr(_n, 'result') and _n.result:
                            _cycle_files.extend(getattr(_n.result, 'files_changed', []) or [])
                if _cycle_files:
                    all_files_changed.extend(_cycle_files)

                logger.info("📋  [Audit→DAG] Follow-on DAG pool complete (cycle %d).", _audit_cycle)
                print(f"{indent}{G}✅ Audit cycle {_audit_cycle} complete{R}")
                if state:
                    state.record_activity("success", f"Audit cycle {_audit_cycle} complete")
    elif agg_result.status != "error":
        # Some tasks may have been skipped
        agg_result.status = "partial"
        agg_result.errors = chunk_errors

    agg_result.duration_s = total_duration
    agg_result.files_changed = all_files_changed

    # Preserve DAG state so Tasks tab stays populated and checkpoint has data
    if depth == 0:
        # V41 FIX: Clear state AFTER audit + re-kick (not before).
        # Only clear when everything is truly done.
        if planner.is_epic_complete():
            # ── V57: Write final checkpoint BEFORE clearing state ──────────────
            # clear_state() wipes the node list from disk. checkpoint.json is
            # only saved on safe-stop (line ~5228), so a natural DAG completion
            # left the project picker showing stale data from the previous session.
            # Write the final counts here so the picker is always accurate.
            try:
                import json as _ck_json
                _ck_progress = planner.get_progress()
                _ck_dir = Path(effective_project) / ".ag-supervisor"
                _ck_dir.mkdir(parents=True, exist_ok=True)
                _ck_data = {
                    "goal": goal if "goal" in dir() else "",
                    "project_path": str(effective_project),
                    "timestamp": time.time(),
                    "status": "complete",
                    "dag_active": False,
                    "dag_completed": _ck_progress.get("complete", 0),
                    "dag_total": len(planner._nodes),
                    "dag_pending": _ck_progress.get("pending", 0),
                    "dag_failed": _ck_progress.get("failed", 0),
                    "dag_cancelled": _ck_progress.get("cancelled", 0),
                    "dag_nodes": [
                        {"id": n.task_id, "status": n.status,
                         "description": n.description}
                        for n in planner._nodes.values()
                    ],
                    "files_changed": all_files_changed[:50],
                    "tasks_completed": executed_count,
                    "error_count": len(chunk_errors),
                }
                (_ck_dir / "checkpoint.json").write_text(
                    _ck_json.dumps(_ck_data, indent=2), encoding="utf-8"
                )
                logger.info(
                    "💾  [Checkpoint] Final state saved: %d/%d tasks complete.",
                    _ck_data["dag_completed"], _ck_data["dag_total"],
                )
            except Exception as _ck_exc:
                logger.warning("💾  [Checkpoint] Could not save final checkpoint: %s", _ck_exc)

            planner.clear_state()
        # Always keep nodes visible — just mark DAG as no longer running
        _dag_progress["active"] = False

    # V76: Persist TaskIntelligence data collected during this DAG execution.
    try:
        if _task_intel:
            _task_intel.save()
            logger.info("📊  [TaskIntel] Saved task intelligence data.")
    except Exception as _ti_exc:
        logger.debug("📊  [TaskIntel] Save error (non-fatal): %s", _ti_exc)

    return agg_result


# ─────────────────────────────────────────────────────────────
# V73: Early stop cleanup — called when safe stop is requested during boot
# ─────────────────────────────────────────────────────────────

async def _stop_early_cleanup(state, sandbox, effective_project) -> None:
    """V73: Clean exit during boot — release lockfile, destroy sandbox, broadcast."""
    logger.info("🛑  Safe stop during boot — cleaning up.")
    state.status = "stopped"
    state.record_activity("system", "Safe stop requested during startup — aborting boot.")
    try:
        _remove_lockfile(str(effective_project))
    except Exception:
        pass
    if sandbox and getattr(sandbox, 'container_id', None):
        try:
            sandbox.copy_out(str(effective_project))
            await sandbox.destroy()
        except Exception as _sd_exc:
            logger.debug("🛑  Boot cleanup: sandbox teardown error: %s", _sd_exc)
    try:
        await state.broadcast_state()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Main loop — V8 Headless Sandbox Mode
# ─────────────────────────────────────────────────────────────

async def run(goal: str, project_path: str | None = None, dry_run: bool = False, existing_state=None) -> None:
    """
    Main supervisor loop — V8 Headless Sandbox.

    Architecture:
        1. Bootstrap workspace (mandate files, PROJECT_STATE.md)
        2. Create Docker sandbox with project workspace mounted
        3. Initialize dual-brain: Ollama (local) + Gemini CLI (cloud)
        4. Execute the goal via HeadlessExecutor
        5. Monitor loop: gather context → decide action → execute
        6. Auto-recovery on failure

    Args:
        existing_state: If provided (e.g. from launcher.py), reuses this
            SupervisorState and does NOT start a duplicate API server.
    """
    if project_path:
        config.set_project_path(project_path)

    G = config.ANSI_GREEN
    C = config.ANSI_CYAN
    B = config.ANSI_BOLD
    R = config.ANSI_RESET

    print(f"\n{B}{G}{'='*60}{R}")
    print(f"{B}{G}  🌐 SUPERVISOR AI {config.SUPERVISOR_VERSION_LABEL} — COMMAND CENTRE{R}")
    print(f"{B}{G}{'='*60}{R}")
    print(f"{C}  Goal: {goal}{R}")
    print(f"{C}  Project: {project_path or 'N/A'}{R}")
    print(f"{C}  Dry-run: {dry_run}{R}")
    print(f"{C}  Mode: Host Intelligence + Docker Sandbox + Command Centre{R}")
    print(f"{B}{G}{'='*60}{R}\n")

    # ── Ensure file logging is active ──
    # When launched via launcher.py → run(), the main() function is never
    # called, so the FileHandler setup at the bottom of this file is skipped.
    # We add it here to guarantee supervisor.log captures ALL session logs.
    root_logger = logging.getLogger()
    has_file_handler = any(
        isinstance(h, logging.FileHandler) for h in root_logger.handlers
    )
    if not has_file_handler:
        import shutil as _shutil
        _log_mode = "w"
        try:
            if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > 0:
                log_age_s = time.time() - _LOG_FILE.stat().st_mtime
                if log_age_s < 120:
                    _log_mode = "a"  # Recent session — append
                else:
                    bak_path = _LOG_FILE.with_suffix(".log.bak")
                    _shutil.copy2(str(_LOG_FILE), str(bak_path))
        except Exception:
            pass
        try:
            _fh = logging.FileHandler(str(_LOG_FILE), encoding="utf-8", mode=_log_mode)
            _fh.setLevel(logging.DEBUG)
            _fh.setFormatter(logging.Formatter(
                "%(asctime)s │ %(levelname)-7s │ %(name)-20s │ %(message)s",
                datefmt="%H:%M:%S",
            ))
            root_logger.addHandler(_fh)
            logger.debug("📝  FileHandler attached to supervisor.log (mode=%s)", _log_mode)
        except Exception as exc:
            logger.warning("Could not set up file logging: %s", exc)

    logger.info("=" * 60)
    logger.info("🚀  Supervisor AI %s Command Centre starting", config.SUPERVISOR_VERSION_LABEL)
    logger.info("   Goal: %s", goal)
    logger.info("   Project: %s", project_path or "N/A")
    logger.info("   Dry-run: %s", dry_run)
    logger.info("=" * 60)

    if dry_run:
        logger.info("[DRY-RUN] Skipping sandbox creation.")
        logger.info("[DRY-RUN] Would execute goal: %s", goal)
        print(f"  {G}[DRY-RUN] All systems nominal. Exiting.{R}")
        return

    # ── Persist session state for auto-resume after reboot ──
    _save_session_state(goal, project_path)

    # ── Workspace Bootstrap (Mandate + Agents) ──
    if project_path:
        bootstrap.bootstrap_workspace(project_path, goal)

    # V62+: Persistent PTY quota probe — spawns the Gemini CLI in a pseudo-
    # terminal at startup (background thread, ~25s). The PTY stays alive so
    # follow-up /stats calls after each task complete in ~2s.
    #
    # V73: If launcher.py already started the probe when the Command Centre
    # opened, reuse that thread instead of spawning a duplicate.
    import threading

    _early_thread = getattr(existing_state, '_early_probe_thread', None) if existing_state else None

    if _early_thread is not None:
        # Launcher already started the probe — reuse it
        _probe_thread = _early_thread
        if _probe_thread.is_alive():
            logger.info("📊  [Launch] Reusing early PTY probe from Command Centre (still running).")
        else:
            logger.info("📊  [Launch] Early PTY probe from Command Centre already completed — PTY is warm.")
    else:
        # Direct CLI invocation (no launcher) — start probe now
        def _bg_launch_probe():
            try:
                from .retry_policy import get_quota_probe
                _qp = get_quota_probe()
                # Apply auto-resets for any models past their window
                for _m in list(_qp._snapshots.keys()):
                    _qp._auto_reset_if_due(_m)
                # Spawn the persistent PTY and run first /stats probe
                _count = _qp.run_stats_probe()
                if _count:
                    logger.info("📊  [Launch] PTY probe: %d models loaded from CLI /stats.", _count)
                else:
                    _loaded = len(_qp._snapshots)
                    if _loaded:
                        logger.info(
                            "📊  [Launch] PTY probe returned no data — %d model(s) from memory. "
                            "Call-count estimation active.", _loaded,
                        )
                    else:
                        logger.info("📊  [Launch] No quota data — starting fresh. Estimation active.")
            except Exception as _lp_exc:
                logger.debug("📊  [Launch] Quota probe failed: %s", _lp_exc)

        _probe_thread = threading.Thread(target=_bg_launch_probe, daemon=True, name="launch-pty-probe")
        _probe_thread.start()
        logger.info("📊  [Launch] Background PTY probe started (async, non-blocking).")

    # ── Initialize sandbox layer ──
    sandbox = SandboxManager()
    local_brain = OllamaLocalBrain()
    api_task = None  # Will hold the API server background task

    # ── Resolve effective project path early (used by state + sandbox) ──
    effective_project = project_path or os.getcwd()

    # V41: Release any stale preview ports from a previous session
    _release_stale_preview(effective_project)

    # ── SupervisorState: reuse from launcher or create fresh ──
    from .api_server import SupervisorState, start_api_server

    if existing_state is not None:
        # Launched from the UI — reuse the existing state + API server
        state = existing_state
        state.goal = goal
        state.project_path = effective_project
    else:
        # Direct CLI invocation — create fresh state + start API server
        state = SupervisorState(goal=goal, project_path=effective_project)

    # V55: Register Gemini status callback so attempt/retry/rate-limit
    # events surface in the UI operation label + activity feed.
    def _gemini_ui_callback(event: str, msg: str) -> None:
        try:
            if hasattr(state, 'set_current_operation'):
                state.set_current_operation(msg)
            if event in ('retry', 'ratelimit') and hasattr(state, 'record_activity'):
                state.record_activity("warning", msg)
            elif event == 'attempt' and hasattr(state, 'record_activity'):
                # Only log attempt 2+ to avoid spamming the feed on normal calls
                if 'attempt 1/' not in msg:
                    state.record_activity("system", msg)
        except Exception:
            pass
    set_gemini_status_callback(_gemini_ui_callback)

    try:
        # ── Docker prerequisite check ──
        # ── Docker prerequisite check + auto-recovery ──
        # V73: Gate 1 — stop before Docker verify
        if state.stop_requested:
            await _stop_early_cleanup(state, None, effective_project)
            return

        print(f"  {C}🐳 Verifying Docker …{R}")
        logger.info("🐳  Verifying Docker prerequisites …")
        
        while True:
            try:
                await sandbox.verify_docker()
                break
            except Exception as e:
                # If we're on Windows and Docker is failing, offer to kill hung background processes
                if sys.platform == "win32" and ("daemon is not running" in str(e) or "timeout" in str(e).lower()):
                    print(f"\n  {R}⚠️  Docker connection failed: {e}{R}")
                    print(f"  {Y}Docker Desktop background processes (wsl.exe, com.docker.*) might be hung.{R}")
                    
                    if sys.stdin.isatty():
                        ans = input(f"  {C}Forcefully close hanging Docker processes and retry? [Y/n]: {R}").strip().lower()
                    else:
                        logger.info("Docker verify failed in headless mode. Auto-killing hung processes.")
                        ans = "y"
                        
                    if ans in ("", "y", "yes"):
                        print(f"  {Y}🔪 Terminating hung Docker processes...{R}")
                        logger.info("Docker verify failed. User requested forceful cleanup of WSL/Docker processes.")
                        import subprocess as _sp
                        
                        # 1. First attempt native WSL shutdown (this is the cleanest way to kill all wsl.exe instances)
                        _sp.run(["wsl", "--shutdown"], capture_output=True)
                        
                        # 2. Kill known Docker Desktop processes and any surviving wsl.exe instances
                        targets = [
                            "com.docker.build.exe", 
                            "com.docker.backend.exe", 
                            "com.docker.proxy.exe", 
                            "wsl.exe", 
                            "vpnkit.exe"
                        ]
                        for target in targets:
                            # Use /T to kill the entire process tree (children included), preventing zombies
                            _sp.run(["taskkill", "/F", "/T", "/IM", target], capture_output=True)
                            
                        # Try restarting the docker service if running natively
                        # (This might fail if no admin rights, but that's fine, the taskkills usually do the job)
                        _sp.run(["sc.exe", "stop", "com.docker.service"], capture_output=True)
                        time.sleep(2)
                        _sp.run(["sc.exe", "start", "com.docker.service"], capture_output=True)
                        
                        # V41 FIX: To actually bring the engine back online, we must launch the Desktop GUI
                        # Start it silently in the background
                        _start_cmd = "Start-Process -FilePath 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe' -WindowStyle Hidden"
                        _sp.run(["powershell", "-Command", _start_cmd], capture_output=True)
                        
                        print(f"  {G}⏳ Waiting 15 seconds for Docker Desktop Engine to recover...{R}")
                        time.sleep(15)
                        continue
                
                # If they say no, or not on Windows, or it's a different error, raise it
                raise e
                
        print(f"  {G}✅ Docker verified.{R}")

        # V45: Clean up orphaned volumes from previous crashed sessions
        try:
            await sandbox.cleanup_stale_volumes()
        except Exception as _vcx:
            logger.debug("Volume cleanup skipped: %s", _vcx)

        # ── Start the V44 Command Centre API server (only if not already running) ──
        if existing_state is None:
            api_task = asyncio.create_task(start_api_server(state))
        state.status = "initializing"

        # ── Create the sandbox container ──
        # ── V54: Reset DAG progress the instant a new project loads ──────────
        # Without this, the Graph and sidebar show stale nodes from the
        # previously-opened project until the new DAG is decomposed.
        global _dag_progress  # noqa: PLW0603
        _dag_progress.clear()
        _dag_progress.update({"active": False, "nodes": [], "total": 0, "completed": 0,
                              "pending": 0, "failed": 0, "cancelled": 0, "running": []})
        state.project_path = effective_project
        # Broadcast the empty state immediately so already-connected UI clients
        # see a clean slate before the new project's DAG appears.
        try:
            await state.broadcast_state()
        except Exception:
            pass

        # Mount mode: copy for isolation (default)
        # The user can override via SANDBOX_MOUNT_MODE env var
        mount_mode = os.getenv("SANDBOX_MOUNT_MODE", "copy")
        if _lockfile_exists(effective_project):
            logger.info("🔐  Lockfile found — session continuing.")
        else:
            _create_lockfile(effective_project)

        # V73: Gate 2 — stop before sandbox creation (~90s)
        if state.stop_requested:
            await _stop_early_cleanup(state, None, effective_project)
            return

        print(f"  {C}📦 Creating sandbox container (mount={mount_mode}) …{R}")
        logger.info("📦  [Boot] Creating sandbox container (mount=%s) …", mount_mode)

        sandbox_info = await sandbox.create(
            effective_project,
            mount_mode=mount_mode,
        )

        state.container_id = sandbox_info.container_id
        state.container_health = "running"
        state.mount_mode = mount_mode
        state.preview_port = sandbox_info.host_preview_port or sandbox_info.preview_port
        state.engine_running = True

        print(f"  {G}✅ Sandbox ready: {sandbox_info.container_id} ({sandbox_info.image}){R}")
        logger.info(
            "✅  Sandbox created: id=%s, image=%s, mount=%s",
            sandbox_info.container_id, sandbox_info.image, sandbox_info.mount_mode,
        )

        # ── Initialize tool server + executor ──
        logger.info("🔧  [Boot] Initializing ToolServer + HeadlessExecutor …")
        tools = ToolServer(sandbox)
        executor = HeadlessExecutor(tools, sandbox)
        state._executor = executor  # V61: exposed for Ollama model resolver in api_server
        logger.info("🔧  [Boot] Tool server and executor ready.")

        # ── V46: Parallel boot — run independent I/O tasks concurrently ──
        # Ollama check, dep install, and preview are all I/O-bound with no
        # cross-dependencies. Session memory, scheduler, and failover chain
        # are instant (CPU-only) and run inline.

        # --- Kick off all I/O tasks in parallel ---

        async def _litebrain_boot():
            """V67: LiteBrain is always available (Gemini Lite is cloud-based).
            Ollama availability is checked lazily on first fallback."""
            ok = await local_brain.is_available()  # Always True
            logger.info("🧠  [Boot] Lite AI brain ready (Gemini Lite primary, Ollama fallback).")
            return ok

        async def _dep_boot():
            """Install project dependencies."""
            print(f"  {C}📥 Installing dependencies …{R}")
            result = await executor.install_dependencies()
            if result.success:
                print(f"  {G}✅ Dependencies installed.{R}")
                # V61: If a fresh install was done, signal the preview layer
                # to restart the dev server so Vite re-optimizes with the
                # new node_modules. Prevents 504 Outdated Optimize Dep.
                if getattr(executor, '_fresh_npm_install', False):
                    executor._fresh_npm_install = False
                    logger.info("📥  [Boot] Fresh npm install — signalling preview restart "
                                "for clean Vite cache.")
                    state._preview_retry_needed = True
            else:
                logger.warning("📥  Dependency install had issues: %s", result.errors[:2])
            return result

        logger.info("🚀  [Boot] Starting parallel init: Ollama + deps …")
        print(f"  {C}📥 Installing dependencies …{R}")
        state.record_activity("system", "Parallel boot: Ollama + deps")

        # V61: Run Ollama and dep install in parallel, but do NOT start the
        # preview in parallel with them.  The preview requires node_modules to
        # exist inside the container — if we race dep install, the preview check
        # always loses (node_modules is never ready) and defers until the first
        # task completes, which can be >10 minutes away.
        # Fix: await _dep_task first, then immediately kick off the preview.
        # This costs ≈0ms of extra wait time since dep install is the bottleneck.
        _ollama_task = asyncio.create_task(_litebrain_boot())
        _dep_task = asyncio.create_task(_dep_boot())

        # V46: Early dep fix — runs in parallel with dep install.
        # Only needs planner state (JSON on disk) + LiteBrain. No sandbox needed.
        # V51: Skip when fresh_audit — old DAG will be discarded anyway.
        async def _depfix_boot():
            """Load planner state from disk and fix serial deps ASAP."""
            try:
                if state and getattr(state, 'fresh_audit', False):
                    logger.info("📋  [Boot] Skipping early dep fix — fresh audit will discard old DAG.")
                    return
                from .temporal_planner import TemporalPlanner
                _early_planner = TemporalPlanner.from_brain(None, str(effective_project))
                if not _early_planner.load_state():
                    return  # No persisted DAG — nothing to fix
                _pids = [
                    n.task_id for n in _early_planner._nodes.values()
                    if n.status in ("pending", "running")
                ]
                if len(_pids) < 3:
                    return
                # Wait for LiteBrain check result (it's already running in parallel)
                _brain_ready = await _ollama_task
                if not _brain_ready:
                    return  # LiteBrain offline — will try later in _execute_dag_recursive
                await _fix_serial_dependencies(
                    _early_planner, _pids, local_brain, state=state,
                )
                logger.info("📋  [Boot] Early dep fix complete — corrected state saved to disk.")
            except Exception as exc:
                logger.debug("📋  [Boot] Early dep fix error: %s", exc)
        _depfix_task = asyncio.create_task(_depfix_boot())

        # --- Sync init runs instantly while I/O tasks are in flight ---
        from .session_memory import SessionMemory
        session_mem = SessionMemory(project_path)
        session_mem.set_goal(goal)

        from . import retry_policy
        from .scheduler import create_default_scheduler
        retry_policy.init()
        logger.info("⏰  [Boot] Scheduler + retry policy initialized.")
        scheduler = create_default_scheduler()

        from .retry_policy import get_failover_chain
        _chain = get_failover_chain()
        state.active_model = _chain.get_active_model() or config.GEMINI_FALLBACK_MODEL

        # V52: Set model status + cooldown at boot so UI shows immediately
        state.model_status = _chain.get_status()
        _boot_cooldown = _chain.seconds_until_any_available()
        if _boot_cooldown > 0:
            state.cooldown_remaining = _boot_cooldown
            state.status = "cooldown"
            logger.info("⏰  [Boot] Model cooldown active: %.0fs remaining", _boot_cooldown)

        # V73: Gate 3 — stop before dep install await
        if state.stop_requested:
            _ollama_task.cancel()
            _dep_task.cancel()
            await _stop_early_cleanup(state, sandbox, effective_project)
            return

        # V61: Gather Ollama + dep install together so whichever finishes last
        # is the gate — a slow/hung Ollama check can't delay the preview start
        # beyond what the dep install itself takes.
        ollama_ok, _ = await asyncio.gather(_ollama_task, _dep_task)
        state.ollama_online = ollama_ok
        if ollama_ok:
            state.ollama_model = local_brain.model  # V61: expose for api_server resolver
            asyncio.create_task(local_brain.warm_up())
            print(f"  {G}🧠 Ollama local brain: ONLINE (model: {local_brain.model}){R}")
        else:
            state.ollama_model = None
            print(f"  {config.ANSI_YELLOW}🧠 Ollama local brain: OFFLINE (Gemini-only mode){R}")

        # V61: Mark deps as installed so the monitoring loop knows not to
        # defer preview any further.
        import time as _time_boot
        state._deps_installed_at = _time_boot.time()

        # V51: Build health check — validates deps, config, exports
        # Runs in background after dep install, before preview starts
        asyncio.create_task(_build_health_check(
            executor, sandbox, state, str(effective_project)
        ))

        # V61: Start preview NOW — after dep install, so node_modules is ready.
        # Give it up to 45s (was 30s, but first-time installs may be slow to
        # bind the port).
        # V68: Fire-and-forget preview — don't block boot for up to 45s.
        # The monitoring loop will pick up preview status on its next tick.
        print(f"  {C}🖥️  Starting preview (background) …{R}")
        state.record_activity("system", "Starting preview after dep install (background)")

        async def _boot_preview_bg():
            try:
                await asyncio.wait_for(
                    _auto_preview_check(sandbox, executor, tools, state, str(effective_project)),
                    timeout=45,
                )
                if state.preview_running:
                    logger.info("🖥️  [Boot] Preview live on port %s", state.preview_port)
                    state.record_activity("success", f"Preview started on port {state.preview_port}")
            except asyncio.TimeoutError:
                logger.info("🖥️  [Boot] Preview timed out (45s) — monitoring loop will retry.")
                state._preview_retry_needed = True
            except Exception as _preview_exc:
                logger.debug("🖥️  [Boot] Early preview failed: %s", _preview_exc)
                state._preview_retry_needed = True

        asyncio.create_task(_boot_preview_bg())

        # V73: Gate 4 — stop before dep fix await
        if state.stop_requested:
            _depfix_task.cancel()
            await _stop_early_cleanup(state, sandbox, effective_project)
            return

        # Await early dep fix (non-blocking — already ran in parallel)
        try:
            await asyncio.wait_for(_depfix_task, timeout=15)
        except asyncio.TimeoutError:
            logger.debug("📋  [Boot] Early dep fix timed out — will recheck later.")
        except Exception as _df_exc:
            logger.debug("📋  [Boot] Early dep fix error: %s", _df_exc)

        await state.broadcast_state()


        # ── First injection: execute the main goal ──
        state.status = "executing"
        print(f"\n  {B}{C}💉 EXECUTING GOAL VIA HOST INTELLIGENCE{R}")
        logger.info("💉  Executing goal: %s", goal)

        # ── Pre-flight startup audit ──────────────────────────────────────
        # Run before ANY DAG resume or planning to catch fatal config issues
        # early and surface them clearly in the activity log / UI.
        _pf_issues: list[str] = []
        _pf_warnings: list[str] = []

        # 1. Goal sanity
        if not goal or not goal.strip():
            _pf_issues.append("No project directive (goal) set — engine has nothing to do")
        elif len(goal.strip()) < 20:
            _pf_warnings.append(f"Project directive is very short ({len(goal)} chars) — may produce poor results")

        # 2. Project path exists and is a directory
        _pf_proj = Path(effective_project)
        if not _pf_proj.exists():
            _pf_issues.append(f"Project path does not exist: {effective_project}")
        elif not _pf_proj.is_dir():
            _pf_issues.append(f"Project path is not a directory: {effective_project}")
        else:
            # 3. SUPERVISOR_MANDATE.md readable
            _mandate_path = _pf_proj / "SUPERVISOR_MANDATE.md"
            if not _mandate_path.exists():
                _pf_warnings.append("SUPERVISOR_MANDATE.md not found — goal will be written now during bootstrap")
            else:
                try:
                    _mandate_text = _mandate_path.read_text(encoding="utf-8")
                    if "YOUR MISSION" not in _mandate_text:
                        _pf_warnings.append("SUPERVISOR_MANDATE.md exists but missing ## YOUR MISSION section")
                except Exception as _pf_mex:
                    _pf_issues.append(f"SUPERVISOR_MANDATE.md unreadable: {_pf_mex}")

            # 4. epic_state.json valid JSON if present
            _state_path = _pf_proj / ".ag-supervisor" / "epic_state.json"
            if _state_path.exists():
                try:
                    import json as _pf_json
                    _pf_state = _pf_json.loads(_state_path.read_text(encoding="utf-8"))
                    _pf_nodes = _pf_state.get("nodes", {})
                    _pf_running = [
                        tid for tid, n in _pf_nodes.items()
                        if n.get("status") == "running"
                    ]
                    if _pf_running:
                        _pf_warnings.append(
                            f"DAG has {len(_pf_running)} task(s) left in 'running' state from last session "
                            f"({', '.join(_pf_running[:3])}{'…' if len(_pf_running) > 3 else ''}) — resetting to pending"
                        )
                        # ── Watchdog: actually reset them now, before planner loads ──
                        # Without this they stay stuck as 'Running' in the UI forever.
                        try:
                            for _stuck_id, _stuck_node in _pf_nodes.items():
                                if _stuck_node.get("status") == "running":
                                    _stuck_node["status"] = "pending"
                                    _stuck_node.pop("started_at", None)
                            _pf_state["nodes"] = _pf_nodes
                            _state_path.write_text(
                                _pf_json.dumps(_pf_state, indent=2), encoding="utf-8"
                            )
                            logger.info(
                                "📋  [Pre-flight] Watchdog: reset %d orphaned 'running' node(s) → pending in epic_state.json",
                                len(_pf_running),
                            )
                        except Exception as _pf_reset_exc:
                            logger.warning(
                                "📋  [Pre-flight] Watchdog reset failed (non-fatal): %s", _pf_reset_exc
                            )
                except Exception as _pf_sex:
                    _pf_issues.append(f"epic_state.json is corrupt (invalid JSON): {_pf_sex} — DAG will be rebuilt")

            # 5. package.json / pyproject.toml / requirements.txt present?
            _has_pkg = any(
                (_pf_proj / f).exists()
                for f in ("package.json", "pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "go.mod")
            )
            if not _has_pkg:
                _pf_warnings.append("No package manager file found (package.json / requirements.txt / pyproject.toml) — may be greenfield or non-standard project")

        # Surface all findings
        for _issue in _pf_issues:
            logger.error("🔴  [Pre-flight] ISSUE: %s", _issue)
            state.record_activity("error", f"[Pre-flight] {_issue}")
        for _warn in _pf_warnings:
            logger.warning("🟡  [Pre-flight] WARN: %s", _warn)
            state.record_activity("system", f"[Pre-flight] {_warn}")

        if _pf_issues:
            logger.warning(
                "🔴  [Pre-flight] %d issue(s) detected at startup. Engine will attempt to proceed — "
                "check the activity log for details.",
                len(_pf_issues),
            )
            print(f"  {config.ANSI_RED}⚠️  Pre-flight: {len(_pf_issues)} issue(s). Check activity log.{R}")
        elif _pf_warnings:
            logger.info("🟡  [Pre-flight] %d warning(s) — proceeding.", len(_pf_warnings))
            print(f"  {config.ANSI_YELLOW}⚠️  Pre-flight: {len(_pf_warnings)} warning(s). Check activity log.{R}")
        else:
            logger.info("✅  [Pre-flight] All checks passed. Engine is ready.")
            print(f"  {G}✅ Pre-flight checks passed.{R}")

        # ── V54: Static health scan — runs in background, writes to BUILD_ISSUES.md ──
        # Launched as a task so it doesn't block engine startup. Results are picked
        # up by the build-health-boot injector once the DAG planner is ready.
        asyncio.create_task(
            _static_health_scan(str(effective_project), state=state),
            name="static-health-scan",
        )
        # ─────────────────────────────────────────────────────────────────

        # V42 FIX: Check for saved DAG state BEFORE creating the boot planner.
        # The boot planner's inject_task + mark_complete both call _save_state(),
        # which OVERWRITES the previous session's epic_state.json (containing
        # multiple pending tasks) with just the single "goal-init" node.
        from .temporal_planner import TemporalPlanner, TaskNode
        _saved_state_path = Path(effective_project) / ".ag-supervisor" / "epic_state.json"
        _has_resumable_dag = False

        # V73: Fresh audit — preserve COMPLETED task history but remove active work.
        # Previously this deleted epic_state.json entirely, losing hundreds of
        # completed tasks from PROGRESS.md. Now we rewrite the file keeping only
        # completed/cancelled/skipped nodes so the full history carries forward.
        if state and getattr(state, 'fresh_audit', False):
            if _saved_state_path.exists():
                try:
                    _fa_data = json.loads(_saved_state_path.read_text(encoding="utf-8"))
                    _fa_nodes = _fa_data.get("nodes", {})
                    _fa_before = len(_fa_nodes)
                    # Keep only COMPLETED nodes — cancelled/skipped are abandoned work
                    _fa_preserved = {
                        tid: ndata for tid, ndata in _fa_nodes.items()
                        if ndata.get("status") == "complete"
                    }
                    _fa_data["nodes"] = _fa_preserved
                    # Reset replan count for fresh start
                    _fa_data["replan_count"] = 0
                    _saved_state_path.write_text(
                        json.dumps(_fa_data, indent=2), encoding="utf-8"
                    )
                    logger.info(
                        "🔍  [Fresh Audit] Preserved %d completed nodes, removed %d active — history intact.",
                        len(_fa_preserved), _fa_before - len(_fa_preserved),
                    )
                    state.record_activity(
                        "system",
                        f"Fresh audit: preserved {len(_fa_preserved)} completed tasks, cleared active work"
                    )
                except Exception as _fa_exc:
                    # Fallback: if parsing fails, delete and start clean
                    logger.warning(
                        "🔍  [Fresh Audit] Could not preserve history: %s — deleting state", _fa_exc
                    )
                    try:
                        _saved_state_path.unlink()
                    except Exception:
                        pass
            state.fresh_audit = False
            logger.info("🔍  [Fresh Audit] fresh_audit flag consumed — will run full boot + audit.")
        else:
            if _saved_state_path.exists():
                try:
                    import json as _json_check
                    _saved = _json_check.loads(_saved_state_path.read_text(encoding="utf-8"))
                    _saved_nodes = _saved.get("nodes", {})
                    # Check for genuinely pending/failed tasks (not just boot "goal-init")
                    _real_tasks = {
                        tid: n for tid, n in _saved_nodes.items()
                        if tid != "goal-init"
                    }
                    _pending = sum(
                        1 for n in _real_tasks.values()
                        if n.get("status") in ("pending", "failed", "running")
                    )
                    if _pending > 0 and len(_real_tasks) > 0:
                        _has_resumable_dag = True
                        logger.info(
                            "📋  [Resume] Found saved DAG with %d real tasks (%d pending). "
                            "Skipping boot planner — resuming directly.",
                            len(_real_tasks), _pending,
                        )
                except Exception as _chk_exc:
                    logger.debug("📋  [Resume] Could not check saved state: %s", _chk_exc)

        # ── V73: Startup Quota Gate ─────────────────────────────────────
        # Wait for the background PTY probe to finish (it's been running
        # ~60-90s in parallel with all the prep above), then check if quota
        # should be paused based on the user's quota_pause_mode setting.
        # Only uses LIVE /stats data — if probe returned nothing, we start
        # optimistically and let the existing 429 → pause flow handle it.
        if state.quota_pause_mode != 'off':
            # 1. Wait for probe thread (it's been running since line 6836)
            # V73: Gate 5 — skip probe join if stopping
            if _probe_thread.is_alive() and not state.stop_requested:
                logger.info("⏳  [Boot] Waiting for PTY probe to complete before checking quota …")
                state.record_activity("system", "Waiting for quota probe to complete …")
                _probe_thread.join(timeout=30)
                if _probe_thread.is_alive():
                    logger.warning("⏳  [Boot] PTY probe still running after 30s — proceeding optimistically.")
            elif state.stop_requested:
                logger.info("⏳  [Boot] Skipping probe join — stop requested.")

            # 2. Evaluate live probe data
            try:
                from .retry_policy import get_daily_budget as _get_boot_budget, get_quota_probe as _get_boot_probe
                _boot_budget = _get_boot_budget()
                _boot_qp = _get_boot_probe()
                _boot_snap = _boot_qp.get_quota_snapshot()

                # Only proceed with pause check if we have LIVE data from the probe
                # V73 FIX: _boot_snap is {"enabled":..., "models":{...}, ...}
                # Must iterate .get('models', {}) — NOT top-level keys.
                _boot_models = _boot_snap.get('models', {})
                _live_models = {
                    m: d for m, d in _boot_models.items()
                    if isinstance(d, dict) and not d.get('stale', True)
                }
                logger.info(
                    "📊  [Boot] Quota gate: %d total models, %d with live probe data.",
                    len(_boot_models), len(_live_models),
                )

                if _live_models:
                    # Determine which models to check based on quota_pause_mode
                    _qpm = state.quota_pause_mode
                    from . import config as _boot_cfg
                    _buckets = getattr(_boot_cfg, 'QUOTA_BUCKETS', {})

                    if _qpm == 'pro':
                        _pro_bucket_models = set()
                        for bname, bdata in _buckets.items():
                            if 'pro' in bname.lower():
                                _pro_bucket_models.update(bdata.get('models', []))
                        _check_models = {
                            m: d for m, d in _live_models.items()
                            if m in _pro_bucket_models
                        }
                    else:  # 'all'
                        _check_models = _live_models

                    if _check_models:
                        # All checked models must be at 0% to trigger pause
                        _all_exhausted = all(
                            d.get('remaining_pct', 100) <= 0
                            for d in _check_models.values()
                        )

                        if _all_exhausted and not _boot_budget._quota_paused:
                            # Find the nearest reset time from live data
                            _reset_times = [
                                d.get('resets_at', 0)
                                for d in _check_models.values()
                                if d.get('resets_at', 0) > time.time()
                            ]
                            if _reset_times:
                                _cooldown = min(_reset_times) - time.time() + 60  # 60s buffer
                            else:
                                _cooldown = None  # Use default (midnight PT)

                            logger.warning(
                                "⏸  [Boot] Quota exhausted at startup (%s mode). "
                                "Pausing before execution.",
                                _qpm,
                            )
                            state.record_activity(
                                "warning",
                                f"Quota exhausted at startup ({_qpm} mode) — "
                                "waiting for quota to reset before starting tasks",
                            )
                            _boot_budget.pause_for_quota(_cooldown)

                            # Enter verified-resume wait loop
                            state.status = "quota_paused"
                            await state.broadcast_state()

                            while _boot_budget._quota_paused:
                                _bw = max(10, _boot_budget._quota_resume_at - time.time()) if _boot_budget._quota_resume_at > 0 else 60
                                _bh, _bm = divmod(int(_bw) // 60, 60)
                                logger.warning(
                                    "⏸  [Boot] Quota paused — sleeping %dh%02dm until quota resets",
                                    _bh, _bm,
                                )
                                # Sleep in 30s chunks
                                _boot_slept = 0
                                while _boot_slept < _bw:
                                    if state and getattr(state, 'stop_requested', False):
                                        break
                                    if not _boot_budget._quota_paused:
                                        break
                                    _bc = min(30, _bw - _boot_slept)
                                    await asyncio.sleep(_bc)
                                    _boot_slept += _bc
                                if state and getattr(state, 'stop_requested', False):
                                    logger.info("🛑  [Boot] Stop requested during quota pause — aborting.")
                                    break
                                # Verified resume
                                _bv = await asyncio.to_thread(_boot_budget.verified_resume_from_quota)
                                if _bv:
                                    break

                            if not getattr(state, 'stop_requested', False):
                                state.status = "executing"
                                await state.broadcast_state()
                                logger.info("▶  [Boot] Quota verified — proceeding with execution.")
                        elif not _all_exhausted:
                            logger.info(
                                "✅  [Boot] Quota check passed (%s mode) — quota available.",
                                _qpm,
                            )
                else:
                    logger.info(
                        "📊  [Boot] No live probe data — skipping startup quota gate (optimistic start)."
                    )
            except Exception as _boot_q_exc:
                logger.debug("📊  [Boot] Startup quota check failed (non-fatal): %s", _boot_q_exc)

        # V73: Gate 6 — stop before DAG resume or planner
        if state.stop_requested:
            await _stop_early_cleanup(state, sandbox, effective_project)
            return

        if _has_resumable_dag:
            # Skip boot planner entirely — go straight to DAG resume
            state.status = "executing"
            state.record_activity("system", f"Resuming saved DAG for: {goal}")
            await state.broadcast_state()

            main_result = await _execute_dag_recursive(
                goal=goal,
                local_brain=local_brain,
                executor=executor,
                session_mem=session_mem,
                effective_project=effective_project,
                depth=0,
                max_depth=3,
                state=state,
                sandbox=sandbox,
                tools=tools,
                project_path=str(effective_project),
            )
            # Skip the rest of the goal execution — resume handled everything
            state.status = "monitoring"
            await state.broadcast_state()
            return main_result

        # ── No resumable DAG — proceed with normal boot ──
        _boot_planner = TemporalPlanner.from_brain(
            local_brain if await local_brain.is_available() else None,
            str(effective_project),
        )
        # V54: Boot planner is EPHEMERAL — its 3 display nodes (goal-init,
        # phase-plan, dag-decompose) must NEVER write to epic_state.json.
        # If they did, _execute_dag_recursive would load them (all complete),
        # skip decompose_epic(), and the real 50-95 atomic tasks would never
        # be generated.
        _boot_planner.ephemeral = True
        # Also prevent the load_state() call inside from_brain() above from
        # pre-loading any stale completed nodes into the boot planner.
        _boot_planner._nodes = {}
        state.planner = _boot_planner

        # ── Boot DAG: 3 nodes representing what actually happens during boot ──
        # These are synthetic display nodes (never sent to Gemini) that give the
        # Graph tab a meaningful view of the boot sequence.

        # Node 1: Goal receipt + complexity analysis
        _genesis = _boot_planner.inject_task(
            task_id="goal-init",
            description=(
                f"[Goal Analysis] Analysing project goal and determining build complexity.\n\n"
                f"Goal: {goal}"
            ),
            dependencies=[],
            priority=0,
        )
        if _genesis:
            _genesis.status = "running"
            _genesis.started_at = time.time()

        # Node 2: Phase planning — Gemini analyses codebase and determines phases
        _phase_node = _boot_planner.inject_task(
            task_id="phase-plan",
            description=(
                "[Phase Planning] Gemini is analysing the codebase and determining project phases.\n"
                "  - Scanning existing files to assess what is built vs what is missing\n"
                "  - Determining the optimal number of phases for this specific project\n"
                "  - Writing phase_state.json and project_plan.md to .ag-supervisor/"
            ),
            dependencies=["goal-init"],
            priority=0,
        )

        # Node 3: DAG decomposition — breaking Phase 1 into atomic tasks
        _dag_node = _boot_planner.inject_task(
            task_id="dag-decompose",
            description=(
                "[DAG Decomposition] Gemini is decomposing the active phase into atomic DAG tasks:\n"
                "  - Generating 50-95 atomic tasks across FUNC / UI-UX / PERF categories\n"
                "  - Resolving dependencies and building the execution graph\n"
                "  - Scheduling parallel lanes for independent branches"
            ),
            dependencies=["phase-plan"],
            priority=0,
        )

        # Drain pre-flight instructions from queue into tracked DAG nodes (visibility)
        # Then re-push them so the monitoring loop still executes them.
        _preflight_instrs = []
        while state.queue.size > 0:
            _pi = state.queue.pop_nowait()
            if _pi:
                _preflight_instrs.append(_pi)
        for _idx, _pi_obj in enumerate(_preflight_instrs):
            _pi_node = _boot_planner.inject_task(
                task_id=f"pre-instr-{_idx+1}",
                description=f"[Custom Instruction] {_pi_obj.text}",  # Full text — no truncation
                dependencies=["goal-init"],
                priority=1,
            )
            if _pi_node:
                logger.info("📋  [Boot] Queued pre-flight instruction as DAG node: %s", _pi_obj.text[:60])
            # Re-push so the monitoring loop picks it up for execution
            await state.queue.push(_pi_obj.text, source=_pi_obj.source)

        # Broadcast initial DAG state so Graph tab lights up
        _dag_progress["active"] = True
        await _update_dag_progress(_boot_planner, 0, state=state)
        state.record_activity("system", f"Goal received: {goal}")
        if _preflight_instrs:
            state.record_activity("system", f"{len(_preflight_instrs)} custom instruction(s) queued as DAG tasks")
        await state.broadcast_state()

        # V42 FIX: Smarter complexity heuristic using semantic signals, not just
        # character count. A 1420-char goal describing a full multi-page website
        # was being classified as SIMPLE and executed in one monolithic shot.
        _char_threshold = getattr(config, "COMPLEX_TASK_CHAR_THRESHOLD", 2000)
        _goal_lower = goal.lower()

        # Signal 1: Raw length (long goals are almost always complex)
        _is_long = len(goal) > _char_threshold

        # Signal 2: Multi-component keywords (pages, sections, features, etc.)
        _component_keywords = [
            "page", "section", "component", "feature", "module",
            "navigation", "footer", "header", "sidebar", "navbar",
            "form", "booking", "contact", "about", "dashboard",
            "package", "pricing", "plan", "tier",
            "testimonial", "portfolio", "gallery", "blog",
            "responsive", "mobile", "animation", "dark mode",
            "authentication", "login", "signup", "api",
        ]
        _keyword_hits = sum(1 for kw in _component_keywords if kw in _goal_lower)

        # Signal 3: Enumeration markers (lists of things to build)
        _enum_markers = [
            "it includes", "it details", "it showcases",
            "key points", "key features", "the site",
            "there is a", "there are",
            "1.", "2.", "3.",
            " - ", "section outlining",
        ]
        _enum_hits = sum(1 for em in _enum_markers if em in _goal_lower)

        # Decision: COMPLEX if long enough OR has multiple semantic signals
        is_complex = _is_long or (len(goal) > 800 and (_keyword_hits >= 3 or _enum_hits >= 2))

        logger.info(
            "🧠  [Task] Complexity heuristic: %d chars, %d component keywords, "
            "%d enum markers → %s",
            len(goal), _keyword_hits, _enum_hits,
            "COMPLEX (DAG)" if is_complex else "SIMPLE (Linear)",
        )
        
        task_class = {
            "complexity": "complex" if is_complex else "simple",
            "needs_gemini": True  # All supervised tasks route to Gemini by default
        }

        # ── Step nodes through completion as real work happens ────────────────

        # Node 1 complete: goal received, complexity determined
        if _genesis:
            _boot_planner.mark_complete("goal-init")
            if _phase_node:
                _phase_node.status = "running"
                _phase_node.started_at = time.time()
            await _update_dag_progress(_boot_planner, 0, state=state)

        # Node 2 — Phase planning: run PhaseManager.initialize() here explicitly
        # so it completes before the boot planner is cleared and the real DAG starts.
        _phase_mgr_boot = None
        try:
            from .phase_manager import PhaseManager as _PM
            _phase_mgr_boot = _PM(str(effective_project), executor, state)
            if not _phase_mgr_boot.is_initialized():
                await _phase_mgr_boot.initialize(goal)
        except Exception as _pm_boot_exc:
            logger.debug("📋  [Boot] PhaseManager init skipped: %s", _pm_boot_exc)
            _phase_mgr_boot = None

        if _phase_node:
            _boot_planner.mark_complete("phase-plan")
            if _dag_node:
                _dag_node.status = "running"
                _dag_node.started_at = time.time()
            await _update_dag_progress(_boot_planner, 0, state=state)

        # V42 FIX: Only clear the boot planner's IN-MEMORY state.
        # Do NOT call clear_state() — it deletes epic_state.json from disk,
        # destroying the previous session's persisted DAG before
        # _execute_dag_recursive gets a chance to load it via load_state().
        state.planner = None  # Prevent reuse of boot planner in DAG execution
        _boot_planner._nodes = {}
        _boot_planner._replan_count = 0

        # V73: Gate 7 — stop before DAG execution
        if state.stop_requested:
            logger.info("🛑  [DAG] Stop requested — skipping execution entirely.")
            await _stop_early_cleanup(state, sandbox, effective_project)
            return

        # V75: Ensure planner is defined for both code paths (used in checkpoint at L8334)
        planner = None

        if is_complex:
            # ── DAG Decomposition Pipeline ──

            print(f"  {C}📋 Complex task detected — decomposing into atomic chunks …{R}")
            logger.info("📋  [Planner] Task is complex (%d chars). Initiating DAG decomposition.",
                         len(goal))

            main_result = await _execute_dag_recursive(
                goal=goal,
                local_brain=local_brain,
                executor=executor,
                session_mem=session_mem,
                effective_project=effective_project,
                depth=0,
                max_depth=3,
                state=state,
                sandbox=sandbox,
                tools=tools,
                project_path=str(effective_project),
                phase_mgr=_phase_mgr_boot,  # Already initialized — Hook D skips re-init
            )
        else:
            # ── Simple task: execute directly ──
            logger.info("📋  [Planner] Simple task — executing directly.")
            state.record_activity("task", f"Executing goal directly: {goal}")
            logger.info(
                "💉  [Goal→Gemini] Simple task prompt (%d chars): %s",
                len(goal), goal,
            )
            state.record_activity("llm_prompt", "Gemini: simple goal execution", goal)
            main_result = await executor.execute_task(
                goal,
                timeout=config.GEMINI_TIMEOUT_SECONDS,
            )

        if main_result.success:
            print(f"  {G}✅ Goal executed successfully! ({main_result.duration_s:.1f}s){R}")
            state.record_activity("success", f"Goal completed ({main_result.duration_s:.1f}s)")
            if main_result.files_changed:
                # V42 FIX: Also update state.files_changed so proactive
                # audits (which check this list) can trigger after simple tasks.
                state.files_changed = list(set(
                    state.files_changed + main_result.files_changed
                ))
                for f in main_result.files_changed:
                    state.record_change(f, "created", "goal_execution")
            session_mem.record_event("goal_executed", "success")
            _recovery_engine.record_success()
        elif main_result.status == "partial":
            print(f"  {config.ANSI_YELLOW}⚠️  Goal executed with warnings ({main_result.duration_s:.1f}s){R}")
            state.record_activity("warning", f"Goal partial ({main_result.duration_s:.1f}s)", str(main_result.errors[:2]))
            if main_result.files_changed:
                state.files_changed = list(set(
                    state.files_changed + main_result.files_changed
                ))
                for f in main_result.files_changed:
                    state.record_change(f, "created", "goal_execution")
            session_mem.record_event("goal_executed", "partial")
        else:
            print(f"  {config.ANSI_RED}❌ Goal execution failed: {main_result.errors[:2]}{R}")
            state.record_activity("error", f"Goal failed", str(main_result.errors[:2]))
            session_mem.record_event("goal_failed", str(main_result.errors[:2]))

            # V40 FIX: Retry the goal execution up to 2 times before giving up.
            # Without this, one API timeout/error permanently stalls the system.
            for _retry_attempt in range(1, 3):
                # V41 FIX: Check safe stop BEFORE spending time on retries
                if state.stop_requested:
                    logger.info("🛑  [Goal] Safe stop requested — skipping retry %d.", _retry_attempt)
                    break

                logger.info("🔄  [Goal] Retrying goal execution (attempt %d/2) after 30s cooldown …", _retry_attempt)
                state.record_activity("system", f"Retrying goal (attempt {_retry_attempt}/2) after initial failure")
                await state.broadcast_state()

                # Interruptible cooldown — check stop every 5s instead of sleeping 30s straight
                for _cool_tick in range(6):
                    if state.stop_requested:
                        break
                    await asyncio.sleep(5)
                if state.stop_requested:
                    logger.info("🛑  [Goal] Safe stop requested during cooldown — aborting retries.")
                    break

                # Re-attempt via DAG decomposition (reset planner)
                try:
                    from .temporal_planner import TemporalPlanner
                    retry_planner = TemporalPlanner.from_brain(
                        local_brain if await local_brain.is_available() else None,
                        str(effective_project),
                    )
                    ok, msg = await retry_planner.decompose_epic(goal)
                    if ok:
                        logger.info("🔄  [Goal] DAG decomposition succeeded on retry %d!", _retry_attempt)
                        state.planner = retry_planner
                        main_result = await _execute_dag_recursive(
                            goal=goal,
                            local_brain=local_brain,
                            executor=executor,
                            session_mem=session_mem,
                            effective_project=str(effective_project),
                            depth=0,
                            max_depth=3,
                            state=state,
                            sandbox=sandbox,
                            tools=tools,
                            project_path=str(effective_project),
                        )
                        if main_result.success or main_result.status == "partial":
                            state.record_activity("success", f"Goal succeeded on retry {_retry_attempt}")
                            break
                    else:
                        logger.warning("🔄  [Goal] DAG retry %d also failed: %s", _retry_attempt, msg)
                except Exception as retry_exc:
                    logger.warning("🔄  [Goal] Retry %d exception: %s", _retry_attempt, retry_exc)

        # Auto-preview: sync files and start dev server if buildable
        if state.stop_requested:
            logger.info("🛑  [Goal] Safe stop requested — skipping auto-preview.")
        else:
            await _auto_preview_check(
                sandbox, executor, tools, state, str(effective_project)
            )

        # ── Monitoring loop ──
        state.status = "monitoring"
        state.record_activity("system", "Entering monitoring loop")
        print(f"\n  {G}✅ Entering {config.SUPERVISOR_VERSION_LABEL} monitoring loop …{R}\n")
        logger.info("✅  Entering monitoring loop (poll every %.1fs)",
                     config.POLL_INTERVAL_SECONDS)

        consecutive_errors = 0
        loop_count = 0
        _monitoring_task_failures = 0  # V40: Circuit breaker for monitoring loop tasks
        _MONITORING_BACKOFF_THRESHOLD = 5   # Start backoff after this many consecutive failures
        _MONITORING_STOP_THRESHOLD = 15     # Stop retrying after this many
        _consecutive_waits = 0             # V40: Idle backoff counter
        _consecutive_run_tests = 0         # V40: Anti-spin guard for run_tests
        _audit_reset_time = time.time()    # V40: 24h reset for daily audit cap

        while True:
            # V40: Idle backoff — increase sleep when nothing to do
            if _consecutive_waits >= 3:
                _idle_sleep = min(300, config.POLL_INTERVAL_SECONDS * (2 ** min(_consecutive_waits - 2, 5)))
            else:
                _idle_sleep = config.POLL_INTERVAL_SECONDS
            # V55: Interruptible sleep — poll in 1s ticks so urgent requests
            # (restart_dev_server_requested, stop_requested) are acted on immediately
            # instead of waiting up to 300s during idle backoff.
            _sleep_remaining = _idle_sleep
            while _sleep_remaining > 0:
                await asyncio.sleep(min(1.0, _sleep_remaining))
                _sleep_remaining -= 1.0
                # Break early if there's urgent work to do
                if (getattr(state, 'restart_dev_server_requested', '')
                        or getattr(state, 'stop_requested', False)
                        or getattr(state, '_preview_retry_needed', False)
                        or (hasattr(state, 'queue') and state.queue.size > 0)):
                    break
            loop_count += 1
            state.loop_count = loop_count

            # V61: Retry preview if it failed/timed-out at boot.
            # Runs once immediately on the first monitoring tick after boot
            # so the user sees the preview as soon as possible without waiting
            # for the first task to complete (which can be minutes away).
            if getattr(state, '_preview_retry_needed', False) and not state.preview_running:
                state._preview_retry_needed = False
                # V73: Skip if already stopping
                if state.stop_requested:
                    logger.info("🖥️  [Monitor] Skipping preview retry — stop requested.")
                else:
                    logger.info("🖥️  [Monitor] Retrying preview start (boot attempt timed out) …")
                    state.record_activity("system", "🖥️ Auto-retrying preview startup …")
                    try:
                        await asyncio.wait_for(
                            _auto_preview_check(sandbox, executor, tools, state, str(effective_project)),
                            timeout=60,
                        )
                        if state.preview_running:
                            logger.info("🖥️  [Monitor] Preview retry succeeded — port %s", state.preview_port)
                            state.record_activity("success", f"Preview started on port {state.preview_port}")
                        else:
                            logger.info("🖥️  [Monitor] Preview retry: not buildable yet")
                    except asyncio.TimeoutError:
                        logger.warning("🖥️  [Monitor] Preview retry timed out (60s) — will try again after first task")
                    except Exception as _pr_exc:
                        logger.debug("🖥️  [Monitor] Preview retry failed: %s", _pr_exc)

            _restart_mode = getattr(state, 'restart_dev_server_requested', '')
            if _restart_mode and sandbox:
                state.restart_dev_server_requested = ''
                logger.info("🖥️  [Dev Server] Manual %s triggered via UI …", _restart_mode)
                state.record_activity("system", f"Dev server {_restart_mode} starting …")
                await state.broadcast_state()
                try:
                    # Get preview port
                    _preview_port = 3000
                    _port_file = Path(effective_project) / ".ag-supervisor" / "_preview_port.json"
                    if _port_file.exists():
                        import json as _pjson
                        _pdata = _pjson.loads(_port_file.read_text(encoding="utf-8"))
                        _preview_port = _pdata.get("container_port", 3000)

                    # Kill existing server
                    await sandbox.exec_command(
                        f"fuser -k {_preview_port}/tcp 2>/dev/null || true", timeout=5,
                    )

                    if _restart_mode == "reinstall":
                        if hasattr(sandbox, 'grant_network'):
                            await sandbox.grant_network()
                        # V55: Smart reinstall tiering — avoid full nuclear wipe when possible.
                        # Check if node_modules exists but vite binary is missing (most common case).
                        # If so, prefer-offline restores binaries from npm cache in ~5s instead of ~40s.
                        try:
                            _nm_check = await sandbox.exec_command(
                                "test -d /workspace/node_modules && echo 'EXISTS' || echo 'MISSING'",
                                timeout=5,
                            )
                            _nm_exists = "EXISTS" in (_nm_check.stdout or "")
                        except Exception:
                            _nm_exists = False

                        if _nm_exists:
                            # node_modules dir is present — likely just lost binaries or stale caches.
                            # V61 (generalized): If the error is "Cannot find module" pointing at an
                            # INTERNAL file of an installed package (vite/dist/..., rollup/dist/...,
                            # esbuild/lib/..., @swc/core/index.js, etc.) npm --prefer-offline will
                            # skip reinstalling it because the package directory exists.
                            # Fix: parse the error line to extract the package name and surgically
                            # wipe just those packages so npm is forced to fully reinstall them.
                            import re as _re_nm
                            _surgical_pkgs: list[str] = []

                            # V75 FIX: Read dev server log — _dsl_txt was undefined here
                            try:
                                _dsl_reinstall = await sandbox.exec_command(
                                    "cat /workspace/.ag-supervisor/dev_server.log 2>/dev/null | tail -50",
                                    timeout=5,
                                )
                                _dsl_txt = _dsl_reinstall.stdout or ""
                            except Exception:
                                _dsl_txt = ""

                            # Match any "Cannot find module '/workspace/node_modules/PKG/internal/path'"
                            # Covers: scoped (@scope/pkg) and unscoped (vite, rollup, esbuild…)
                            _nm_internal_re = _re_nm.compile(
                                r"Cannot find module ['\"]?(?:/workspace)?/workspace/node_modules/"
                                r"(@[^/]+/[^/]+|[^/]+)"   # captures @scope/pkg OR pkg
                                r"/[^'\"]*(?:dist|lib|chunks|build|cjs|esm|umd|internal)[^'\"]*['\"]?",
                                _re_nm.IGNORECASE,
                            )
                            for _line in _dsl_txt.splitlines():
                                _m = _nm_internal_re.search(_line)
                                if _m:
                                    _pkg = _m.group(1)  # e.g. "vite", "@vitejs/plugin-react"
                                    if _pkg not in _surgical_pkgs:
                                        _surgical_pkgs.append(_pkg)

                            # Also match the simpler "imported from .../node_modules/PKG/anything"
                            _import_re = _re_nm.compile(
                                r"imported from /workspace/node_modules/(@[^/]+/[^/]+|[^/]+)/",
                                _re_nm.IGNORECASE,
                            )
                            for _line in _dsl_txt.splitlines():
                                _m = _import_re.search(_line)
                                if _m:
                                    _pkg = _m.group(1)
                                    if _pkg not in _surgical_pkgs:
                                        _surgical_pkgs.append(_pkg)

                            if _surgical_pkgs:
                                _wipe_dirs = " ".join(
                                    f"/workspace/node_modules/{p}" for p in _surgical_pkgs
                                )
                                logger.info(
                                    "🖥️  [Dev Server] Corrupt package internals detected — surgical wipe: %s",
                                    ", ".join(_surgical_pkgs),
                                )
                                state.record_activity(
                                    "system",
                                    f"🔧 Surgical rm of corrupt package(s): {', '.join(_surgical_pkgs)} — forcing npm reinstall …",
                                )
                                await sandbox.exec_command(
                                    f"rm -rf {_wipe_dirs} /workspace/node_modules/.vite"
                                    f" /workspace/node_modules/.cache /workspace/.vite 2>/dev/null; true",
                                    timeout=15,
                                )

                            # Use prefer-offline to restore from npm cache (fast path, ~5-10s).
                            logger.info("🖥️  [Dev Server] Reinstall: node_modules present — fast restore (prefer-offline) …")
                            state.record_activity("system", "🧹 Fast reinstall: restoring binaries from npm cache …")
                            _fast_inst = await sandbox.exec_command(
                                "rm -rf /workspace/.vite /workspace/node_modules/.vite"
                                " /workspace/node_modules/.cache 2>/dev/null; "
                                "npm install --prefer-offline --no-audit --no-fund 2>&1 | tail -10",
                                timeout=120,
                            )
                            if _fast_inst.exit_code != 0:
                                # Fast path failed — fall through to nuclear
                                logger.warning("🖥️  [Dev Server] Fast restore failed (exit %d) — falling back to nuclear wipe", _fast_inst.exit_code)
                                _nm_exists = False  # force nuclear on next branch
                            else:
                                logger.info("🖥️  [Dev Server] Fast restore succeeded")

                        if not _nm_exists:
                            # node_modules missing or fast restore failed — full nuclear wipe.
                            logger.info("🖥️  [Dev Server] Reinstall: nuclear wipe (no node_modules or fast restore failed) …")
                            state.record_activity("system", "🧹 Nuclear reinstall: wiping node_modules + Vite caches …")
                            await sandbox.exec_command(
                                # Wipe node_modules AND all Vite optimizer caches.
                                # NOTE: dist is NOT wiped — it doesn't cause vite binary errors
                                # and takes time to rebuild unnecessarily.
                                "rm -rf node_modules .vite node_modules/.vite "
                                "node_modules/.cache package-lock.json 2>/dev/null; "
                                "npm install --no-audit --no-fund 2>&1 | tail -10",
                                timeout=300,
                            )

                    # Restart dev server
                    _srv_result = await executor.start_dev_server()
                    if _srv_result.status == "success" or getattr(_srv_result, 'exit_code', 1) == 0:
                        state.dev_server_error = ''
                        state.record_activity("system", f"Dev server {_restart_mode} succeeded ✅")
                    else:
                        _err_msg = '; '.join(_srv_result.errors[:2]) if _srv_result.errors else 'Unknown error'
                        state.dev_server_error = _err_msg
                        state.record_activity("system", f"Dev server {_restart_mode} failed: {_err_msg}")

                    # Auto-detect vite chunk corruption in dev server output and
                    # self-heal with a follow-up clean reinstall if not already doing one.
                    _srv_out = str(getattr(_srv_result, 'output', '') or '') + '; '.join(_srv_result.errors or [])
                    if (
                        "node_modules/vite" in _srv_out
                        and "Cannot find module" in _srv_out
                        and _restart_mode != "reinstall"  # don't loop
                    ):
                        logger.warning("🖥️  [Dev Server] Vite chunk corruption detected in dev server — auto-triggering clean reinstall …")
                        state.restart_dev_server_requested = "reinstall"
                        state.record_activity("system", "⚠️ Vite chunk error detected — queuing clean reinstall …")

                except Exception as _exc:
                    state.dev_server_error = str(_exc)
                    state.record_activity("system", f"Dev server {_restart_mode} failed: {_exc}")
                    logger.warning("🖥️  [Dev Server] Manual %s failed: %s", _restart_mode, _exc)

                # V52: Re-run build health check after restart to detect remaining issues
                try:
                    await _build_health_check(
                        executor=executor,
                        sandbox=sandbox,
                        project_path=str(effective_project),
                        state=state,
                    )
                except Exception as _bh_exc:
                    logger.debug("Build health check after restart failed: %s", _bh_exc)

                await state.broadcast_state()

            # V38.1: Safe stop check
            if state.stop_requested:
                logger.info("🛑  Safe stop requested. Saving checkpoint and exiting.")
                print(f"\n  {config.ANSI_YELLOW}🛑 Safe stop requested — shutting down gracefully.{R}")

                # Save per-project checkpoint for resume
                import json as _json
                checkpoint_dir = Path(effective_project) / ".ag-supervisor"
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
                checkpoint = {
                    "goal": goal,
                    "project_path": str(effective_project),
                    "timestamp": time.time(),
                    "loop_count": loop_count,
                    "status": "stopped",
                    "dag_active": _dag_progress.get("active", False),
                    "dag_completed": _dag_progress.get("completed", 0),
                    "dag_total": _dag_progress.get("total", 0),
                    "dag_pending": _dag_progress.get("pending", 0),
                    "dag_failed": _dag_progress.get("failed", 0),
                    "dag_cancelled": _dag_progress.get("cancelled", 0),
                    "dag_nodes": _dag_progress.get("nodes", []),
                    "files_changed": state.files_changed[:50],
                    "tasks_completed": state.tasks_completed,
                    "error_count": state.error_count,
                }
                checkpoint_path = checkpoint_dir / "checkpoint.json"
                checkpoint_path.write_text(
                    _json.dumps(checkpoint, indent=2), encoding="utf-8"
                )
                # V53: Also persist full planner node statuses so 'complete' tasks
                # survive a safe stop and don't appear as 'pending' on next resume.
                try:
                    if planner:
                        planner._save_state()
                except Exception:
                    pass
                logger.info(
                    "🛑  Checkpoint saved: %s (DAG %d/%d complete)",
                    checkpoint_path,
                    checkpoint.get("dag_completed", 0),
                    checkpoint.get("dag_total", 0),
                )
                print(f"  {G}💾 Checkpoint saved → {checkpoint_path}{R}")

                state.status = "stopped"
                await state.broadcast_state()
                break

            try:
                # ── Check instruction queue (user commands from UI) ──
                # V40: Drain ALL queued instructions in one tick (prevents pile-up)
                _instr_processed = False
                _instr_counter = getattr(state, '_instr_counter', 0)
                while True:
                    user_instruction = state.queue.pop_nowait()
                    if not user_instruction:
                        break

                    _instr_processed = True

                    # V40: Input validation
                    instr_text = user_instruction.text.strip()
                    if not instr_text:
                        logger.warning("📬  Empty instruction skipped.")
                        continue
                    # V45: Use config limit (200K default). Gemini has 1M+ context.
                    _max_instr = getattr(config, "PROMPT_SIZE_MAX_CHARS", 200_000)
                    if len(instr_text) > _max_instr:
                        instr_text = instr_text[:_max_instr]
                        logger.warning("📬  Instruction truncated to %d chars.", _max_instr)

                    # V40: Rate limit check — don't burn quota on cooldown
                    from .retry_policy import get_failover_chain as _get_chain
                    _fc = _get_chain()
                    if _fc.all_models_on_cooldown():
                        _wait = min(60, _fc.get_soonest_cooldown_remaining())
                        logger.warning(
                            "📬  All models on cooldown — deferring instruction %.0fs",
                            _wait,
                        )
                        state.record_activity(
                            "warning",
                            f"Instruction deferred: all models on cooldown ({_wait:.0f}s)",
                        )
                        # Re-queue for next tick
                        await state.queue.push(instr_text, source=user_instruction.source)
                        # V55: Interruptible defer — break early on stop or restart
                        _defer_remaining = max(5.0, _wait)
                        while _defer_remaining > 0:
                            await asyncio.sleep(min(1.0, _defer_remaining))
                            _defer_remaining -= 1.0
                            if getattr(state, 'stop_requested', False) or getattr(state, 'restart_dev_server_requested', ''):
                                break
                        break

                    logger.info("📬  User instruction: %s", instr_text)
                    state.last_action = f"user_instruction: {instr_text}"

                    # ── V40: DAG-aware injection ──
                    # If a DAG is active AND the worker pool is still running,
                    # inject as a proper task node so it's tracked, retried, and
                    # serialized with other DAG work.
                    # V40 FIX: Also check _dag_progress["active"]. When the DAG
                    # pool exits (goal partial/complete), has_active_dag() can
                    # still return True (tasks remain in planner), but the worker
                    # pool is gone. Injecting into that dead DAG creates dead
                    # letters that never execute.
                    _active_planner = getattr(state, 'planner', None)
                    _pool_running = _dag_progress.get("active", False)
                    if _active_planner and _active_planner.has_active_dag() and _pool_running:
                        # V44: Decompose instruction into subtasks via Gemini
                        _subtasks = await _decompose_user_instructions(
                            [instr_text],
                            _active_planner,
                            str(effective_project),
                            state=state,
                        )
                        _injected_any = False
                        for _sub in _subtasks:
                            _instr_counter += 1
                            _task_id = _sub.get("task_id", f"user-instr-{_instr_counter}")
                            _sub_deps = _sub.get("dependencies", [])
                            injected = _active_planner.inject_task(
                                task_id=_task_id,
                                description=f"[User Instruction] {_sub.get('description', instr_text)}",
                                dependencies=_sub_deps,
                                priority=100,  # V44: High priority — execute before regular tasks
                            )
                            if injected:
                                _injected_any = True
                                session_mem.record_event(
                                    "user_instruction_injected",
                                    f"{_task_id}: {_sub.get('description', '')[:80]}",
                                )
                                logger.info(
                                    "📬  Instruction subtask injected as DAG task %s.",
                                    _task_id,
                                )
                        if _injected_any:
                            # V41: Broadcast DAG progress immediately so Graph tab updates
                            _pool_depth = _dag_progress.get("depth", 0)
                            await _update_dag_progress(
                                _active_planner, _pool_depth, state=state,
                            )
                            state.record_activity(
                                "instruction",
                                f"Decomposed instruction into {len(_subtasks)} subtask(s): {instr_text}",
                            )
                            await state.broadcast_state()
                        else:
                            # All inject_task calls failed — fall through to direct execution
                            logger.warning("📬  DAG injection failed for all subtasks — executing directly.")
                            state.record_activity("warning", f"DAG injection failed, executing directly: {instr_text}")
                            # Continue to Path B below
                            _active_planner = None  # Force direct execution
                            _pool_running = False

                    # ── Path B: Direct execution (no active DAG, pool not running, or injection failed) ──
                    if not (_active_planner and _active_planner.has_active_dag() and _pool_running):
                        state.status = "executing"
                        state.record_activity("instruction", f"Executing user instruction: {instr_text}")

                        # V41: Create a visible DAG node so the instruction appears in the Graph tab,
                        # even when no DAG pool is running.  Reuse the existing planner if one exists,
                        # otherwise spin up a lightweight planner just for visibility.
                        from .temporal_planner import TemporalPlanner, TaskNode
                        _vis_planner = getattr(state, 'planner', None)
                        if _vis_planner is None:
                            _vis_planner = TemporalPlanner.from_brain(
                                local_brain if await local_brain.is_available() else None,
                                str(effective_project),
                            )
                            state.planner = _vis_planner

                        _instr_counter += 1
                        _vis_task_id = f"user-instr-{_instr_counter}"
                        _vis_node = _vis_planner.inject_task(
                            task_id=_vis_task_id,
                            description=f"[User Instruction] {instr_text}",
                            dependencies=[],
                            priority=1,
                        )
                        if _vis_node:
                            _vis_node.status = "running"
                            _vis_planner._save_state()
                        # Broadcast immediately so the Graph tab shows the task
                        _vis_depth = _dag_progress.get("depth", 0)
                        await _update_dag_progress(
                            _vis_planner, _vis_depth,
                            running=[_vis_task_id],
                            state=state,
                        )
                        await state.broadcast_state()

                        # V40: Serialize through sandbox lock
                        from .workspace_transaction import get_workspace_lock as _get_ws_lock
                        _instr_lock = _get_ws_lock()
                        async with _instr_lock.acquire_sandbox():
                            logger.info(
                                "📬  [Instruction→Gemini] User instruction prompt (%d chars): %s",
                                len(instr_text), instr_text,
                            )
                            state.record_activity("llm_prompt", "Gemini: user instruction", instr_text)
                            instr_result = await executor.execute_task(
                                instr_text, timeout=config.GEMINI_TIMEOUT_SECONDS,
                            )

                        state.last_task_status = instr_result.status
                        state.last_task_duration = instr_result.duration_s
                        state.tasks_completed += 1
                        if instr_result.files_changed:
                            state.files_changed = list(set(state.files_changed + instr_result.files_changed))
                            for f in instr_result.files_changed:
                                state.record_change(f, "modified", "user_instruction")
                            logger.info(
                                "📄  Files changed (%d): %s",
                                len(instr_result.files_changed),
                                ", ".join(instr_result.files_changed[:30]),
                            )

                        if instr_result.status == "error":
                            state.error_count += 1
                            state.record_activity("error", f"Instruction failed: {instr_text}", str(instr_result.errors[:2]))

                            # V41: Mark DAG node as failed so Graph tab shows ✕
                            if _vis_node:
                                _vis_planner.mark_failed(_vis_task_id, str(instr_result.errors[:1]))
                            await _update_dag_progress(_vis_planner, _vis_depth, state=state)

                            # V40: Auto-retry failed instructions
                            logger.info("📬  Instruction failed — auto-retrying …")
                            async with _instr_lock.acquire_sandbox():
                                fix_ok, fix_result = await _diagnose_and_retry(
                                    task_description=instr_text,
                                    failure_errors=instr_result.errors,
                                    executor=executor,
                                    local_brain=local_brain,
                                    session_mem=session_mem,
                                    task_id="user_instruction",
                                )
                            if fix_ok:
                                state.record_activity("success", f"Instruction auto-fixed ({fix_result.duration_s:.1f}s): {instr_text}")
                                if fix_result.files_changed:
                                    state.files_changed = list(set(state.files_changed + fix_result.files_changed))
                                    for f in fix_result.files_changed:
                                        state.record_change(f, "fixed", "instruction_retry")
                                # V41: Mark as complete after successful retry
                                if _vis_node:
                                    _vis_planner.mark_complete(_vis_task_id)
                                await _update_dag_progress(_vis_planner, _vis_depth, state=state)
                            else:
                                logger.warning("📬  Instruction auto-fix also failed.")
                                # V40 FIX: Ensure PROJECT_STATE.md reflects unrecoverable manual instruction errors
                                try:
                                    _ps = Path(effective_project) / "PROJECT_STATE.md"
                                    if _ps.exists():
                                        _txt = _ps.read_text(encoding="utf-8")
                                        _err_log = f"\n- ❌ **Failed Instruction:** {instr_text}... \n  - Error: {instr_result.errors[:1]}\n"
                                        if "## Current Blockers" in _txt:
                                            _txt = _txt.replace("## Current Blockers", f"## Current Blockers\n{_err_log}")
                                        else:
                                            _txt += f"\n\n## Current Blockers\n{_err_log}"
                                        _ps.write_text(_txt, encoding="utf-8")
                                except Exception:
                                    pass
                        else:
                            state.record_activity("success", f"Instruction completed ({instr_result.duration_s:.1f}s): {instr_text}")
                            # V41: Mark DAG node as complete so Graph tab shows ✓
                            if _vis_node:
                                _vis_planner.mark_complete(_vis_task_id)
                            await _update_dag_progress(_vis_planner, _vis_depth, state=state)

                        state.status = "monitoring"
                        await state.broadcast_state()

                        session_mem.record_event("user_instruction", instr_text)

                        # Auto-preview: sync and check after user instruction
                        if instr_result.files_changed:
                            await _auto_preview_check(
                                sandbox, executor, tools, state, str(effective_project)
                            )

                state._instr_counter = _instr_counter  # Persist counter

                if _instr_processed:
                    continue  # Re-poll immediately after draining instructions
                # ── Scheduler tick ──
                try:
                    sched_results = await scheduler.tick()
                    if sched_results:
                        for sr in sched_results:
                            logger.info("⏰  %s", sr)
                except Exception as sched_exc:
                    logger.debug("⏰  Scheduler tick error: %s", sched_exc)

                # ── Rate limit cooldown guard ──
                from .retry_policy import get_failover_chain
                _failover = get_failover_chain()
                if _failover.all_models_on_cooldown():
                    _wait_secs = _failover.get_soonest_cooldown_remaining()
                    _h, _m = divmod(int(_wait_secs) // 60, 60)
                    logger.warning(
                        "⏸  ALL models on cooldown — pausing %dh%02dm until next model recovers",
                        _h, _m,
                    )
                    if state:
                        state.status = "cooldown"
                        state.cooldown_remaining = _wait_secs
                        state.record_activity(
                            "warning",
                            f"⏸ All models exhausted — pausing {_h}h{_m:02d}m",
                        )
                        await state.broadcast_state()
                    # Sleep the full cooldown in 60s chunks (stop-aware)
                    # JS countdown handles the timer client-side — no need to broadcast each tick
                    _slept = 0
                    while _slept < _wait_secs:
                        if getattr(state, 'stop_requested', False) or getattr(state, 'restart_dev_server_requested', ''):
                            break
                        _chunk = min(60, _wait_secs - _slept)
                        await asyncio.sleep(max(5.0, _chunk))
                        _slept += _chunk
                    # Reset cooldown state
                    if state:
                        state.cooldown_remaining = 0
                        state.status = "executing"
                        await state.broadcast_state()
                    continue

                # ── Gather structured context ──
                ctx = await executor.gather_context()
                session_mem.record_context_gather()

                # ── V41: Deterministic action routing (no LLM needed) ──
                # The structured context already tells us exactly what to do.
                # Ollama's 76s response adds no value over these instant checks.
                action = "wait"
                reason = "Everything looks healthy"

                if ctx.diagnostics_errors > 0:
                    action = "fix_errors"
                    reason = f"{ctx.diagnostics_errors} diagnostic errors detected"
                elif not ctx.dev_server_running and ctx.workspace_files:
                    action = "start_server"
                    reason = "Dev server not running"
                else:
                    # Check for pending DAG work
                    _active_planner = getattr(state, 'planner', None)
                    if _active_planner and _active_planner.has_active_dag():
                        _pending = _active_planner.get_parallel_batch()
                        if _pending:
                            action = "resume_dag"
                            reason = f"{len(_pending)} pending DAG tasks"

                    # V54: Fallback — check _dag_pending_count when state.planner
                    # is None (cleared after boot). If there are pending tasks but
                    # the DAG loop hasn't picked them up yet, nudge it.
                    if action == "wait":
                        _pending_dag = getattr(state, '_dag_pending_count', 0)
                        if _pending_dag > 0:
                            action = "resume_dag"
                            reason = f"{_pending_dag} pending DAG tasks (state._dag_pending_count)"

                    # Check for feature epic
                    if action == "wait":
                        _feature_path = Path(effective_project) / "FEATURE_EPIC.md"
                        if _feature_path.exists():
                            try:
                                _ft = _feature_path.read_text(encoding="utf-8")[:2000]
                                if _ft.strip():
                                    action = "execute_task"
                                    reason = f"Build feature from FEATURE_EPIC.md: {_ft[:200]}"
                            except Exception:
                                pass

                logger.info("🧠  [Monitor] Action decided (heuristic): %s — %s", action, reason)
                state.record_activity("system", f"Decision: {action}", reason)

                # V41 FIX: Check for pending/failed DAG tasks on EITHER idle wait
                # OR repeated run_tests (anti-spin scenario). Previously this only
                # fired on wait with 3+ consecutive waits — but Ollama returning
                # run_tests reset the wait counter, so it never triggered.
                _should_check_dag = (
                    (action == "wait" and _consecutive_waits >= 3) or
                    (action == "run_tests" and _consecutive_run_tests >= 2)
                )
                if _should_check_dag:
                    _active_planner = getattr(state, 'planner', None)
                    if _active_planner and _active_planner.has_active_dag():
                        # First, re-queue any failed-but-retriable tasks
                        _retriable = _active_planner.get_failed_retriable()
                        for _fn in _retriable:
                            _active_planner.mark_retry(_fn.task_id, force=True)
                            logger.info("🔄  [DAG Resume] Re-queued failed task %s for retry", _fn.task_id)

                        _pending_batch = _active_planner.get_parallel_batch()
                        if _pending_batch:
                            action = "resume_dag"
                            reason = f"[DAG Resume] {len(_pending_batch)} pending tasks detected: {', '.join(n.task_id for n in _pending_batch[:5])}"
                            logger.info("🔄  [DAG Resume] Overriding '%s' — DAG has %d pending tasks", action, len(_pending_batch))
                            state.record_activity("system", f"DAG resume: {len(_pending_batch)} pending tasks")
                            _consecutive_run_tests = 0
                            _consecutive_waits = 0
                    elif _active_planner:
                        # Planner exists but no active tasks — check for failed ones to resurrect
                        _all_failed = [
                            n for n in _active_planner._nodes.values()
                            if n.status == "failed"
                        ]
                        if _all_failed:
                            _resurrect_node = _all_failed[0]
                            if _active_planner.mark_retry(_resurrect_node.task_id, force=True):
                                action = "resume_dag"
                                reason = f"[Heuristic] Auto-resurrecting failed DAG task: {_resurrect_node.task_id}"
                                logger.info("🔄  [Heuristic] Resurrecting failed DAG task %s", _resurrect_node.task_id)
                                state.record_activity("system", f"Auto-resurrecting failed task: {_resurrect_node.task_id}")

                # ── Act on decision ──
                # V40: Reset idle backoff on any real action
                if action != "wait":
                    _consecutive_waits = 0
                    state._idle_ticks = 0  # Also reset proactive audit idle counter
                    if action != "run_tests":
                        _consecutive_run_tests = 0  # V40: Reset anti-spin counter on real work

                # V40: Update model pill every tick (immediate feedback)
                try:
                    _active = get_failover_chain().get_active_model()
                    if _active and _active != state.active_model:
                        state.active_model = _active
                        await state.broadcast_state()
                except Exception:
                    pass

                if action == "fix_errors" and ctx.diagnostic_details:
                    logger.info("🔧  Fixing %d errors …", ctx.diagnostics_errors)
                    state.record_activity("fix", f"Fixing {ctx.diagnostics_errors} diagnostic errors", str(ctx.diagnostic_details[:3]))
                    print(f"  {config.ANSI_YELLOW}🔧 Fixing {ctx.diagnostics_errors} errors …{R}")

                    # Use local brain for quick error analysis
                    analysis = None
                    if ollama_ok:
                        logger.info(
                            "🔧  [Fix→Ollama] Analyzing %d diagnostic errors …",
                            len(ctx.diagnostic_details),
                        )
                        analysis = await local_brain.analyze_errors(ctx.diagnostic_details)
                        if analysis:
                            logger.info(
                                "🔧  [Fix←Ollama] Error analysis (%d chars): %.500s…",
                                len(analysis), analysis,
                            )

                    error_prompt = (
                        f"Fix the following diagnostic errors in the project:\n\n"
                        f"{json.dumps(ctx.diagnostic_details[:10], indent=2)}"
                    )
                    if analysis:
                        error_prompt += f"\n\nQuick analysis from local LLM:\n{analysis}"

                    logger.info(
                        "🔧  [Fix→Gemini] Error fix prompt (%d chars): %s",
                        len(error_prompt), error_prompt,
                    )
                    state.record_activity("llm_prompt", "Gemini: fix diagnostic errors", error_prompt)
                    fix_result = await executor.execute_task(error_prompt, timeout=config.GEMINI_TIMEOUT_SECONDS)
                    if fix_result.success:
                        print(f"  {G}✅ Errors fixed! ({fix_result.duration_s:.1f}s){R}")
                        state.record_activity("success", f"Fixed {ctx.diagnostics_errors} errors ({fix_result.duration_s:.1f}s)")
                        if fix_result.files_changed:
                            for f in fix_result.files_changed:
                                state.record_change(f, "fixed", "error_fix")
                        session_mem.record_event("errors_fixed", str(ctx.diagnostics_errors))
                    else:
                        logger.warning("🔧  Error fix attempt failed — auto-retrying …")
                        fix2_ok, fix2_result = await _diagnose_and_retry(
                            task_description=error_prompt,
                            failure_errors=fix_result.errors,
                            executor=executor,
                            local_brain=local_brain,
                            session_mem=session_mem,
                            task_id="monitoring_fix",
                            state=state,
                        )
                        if fix2_ok:
                            print(f"  {G}✅ Errors auto-fixed on retry! ({fix2_result.duration_s:.1f}s){R}")
                        else:
                            logger.warning("🔧  Auto-fix also failed: %s", fix2_result.errors[:2])
                            # V40 FIX: Log unrecoverable heuristic fix errors to PROJECT_STATE.md
                            try:
                                _ps = Path(effective_project) / "PROJECT_STATE.md"
                                if _ps.exists():
                                    _txt = _ps.read_text(encoding="utf-8")
                                    _err_log = f"\n- ❌ **Failed Heuristic Fix:** Tried to fix {ctx.diagnostics_errors} errors but failed.\n  - Error: {fix2_result.errors[:1]}\n"
                                    if "## Current Blockers" in _txt:
                                        _txt = _txt.replace("## Current Blockers", f"## Current Blockers\n{_err_log}")
                                    else:
                                        _txt += f"\n\n## Current Blockers\n{_err_log}"
                                    _ps.write_text(_txt, encoding="utf-8")
                            except Exception:
                                pass

                elif action == "run_tests":
                    _consecutive_run_tests += 1
                    
                    # V41 FIX: Anti-spin guard — if Ollama keeps recommending "run_tests"
                    # 3+ times in a row with no progress, check for DAG work first.
                    # Only fall back to raw goal execution if no DAG tasks are pending.
                    if _consecutive_run_tests >= 3:
                        logger.warning("🔄  [Anti-Spin] run_tests recommended %d times consecutively with no progress.", _consecutive_run_tests)
                        _consecutive_run_tests = 0

                        # V41: Prefer DAG resume over blind goal execution
                        _active_planner = getattr(state, 'planner', None)
                        _has_dag_work = False
                        if _active_planner:
                            # Re-queue failed tasks
                            for _fn in _active_planner.get_failed_retriable():
                                _active_planner.mark_retry(_fn.task_id, force=True)
                            if _active_planner.has_active_dag():
                                _has_dag_work = True

                        if _has_dag_work:
                            # Resume DAG instead of blind goal
                            logger.info("🔄  [Anti-Spin→DAG] Resuming DAG execution instead of raw goal")
                            state.record_activity("task", "Anti-spin: resuming DAG execution")
                            _dag_progress["active"] = True
                            await _update_dag_progress(_active_planner, 0, state=state)
                            _spin_result = await _execute_dag_recursive(
                                goal=goal,
                                local_brain=local_brain,
                                executor=executor,
                                session_mem=session_mem,
                                effective_project=str(effective_project),
                                depth=0,
                                max_depth=2,
                                state=state,
                            )
                            state.tasks_completed += 1
                            if _spin_result.files_changed:
                                state.files_changed = list(set(state.files_changed + _spin_result.files_changed))
                                for f in _spin_result.files_changed:
                                    state.record_change(f, "modified", "dag_resume")
                            if _spin_result.success or _spin_result.status == "partial":
                                state.record_activity("success", f"DAG resume completed ({_spin_result.duration_s:.1f}s)")
                            else:
                                state.record_activity("error", f"DAG resume failed")
                                _monitoring_task_failures += 1
                        else:
                            # No DAG work — fall back to raw goal execution
                            state.record_activity("task", "Anti-spin override: executing goal")
                            _spin_result = await executor.execute_task(
                                goal, timeout=config.GEMINI_TIMEOUT_SECONDS,
                            )
                            state.tasks_completed += 1
                            if _spin_result.files_changed:
                                state.files_changed = list(set(state.files_changed + _spin_result.files_changed))
                                for f in _spin_result.files_changed:
                                    state.record_change(f, "modified", "anti_spin_goal")
                            if _spin_result.success:
                                state.record_activity("success", f"Anti-spin goal execution succeeded ({_spin_result.duration_s:.1f}s)")
                            else:
                                state.record_activity("error", f"Anti-spin goal execution failed")
                                _monitoring_task_failures += 1
                    else:
                        if state.stop_requested:
                            logger.info("🛑  Safe stop requested — skipping tests.")
                            break
                        
                        logger.info("🧪  Running tests …")
                        print(f"  {C}🧪 Running tests …{R}")
                        test_result = await executor.run_tests()
                        if test_result.success:
                            print(f"  {G}✅ Tests passed! ({test_result.duration_s:.1f}s){R}")
                        else:
                            print(f"  {config.ANSI_YELLOW}⚠️  Tests had issues{R}")
                        session_mem.record_event("tests_run", test_result.status)

                        # V41: After tests pass, check if DAG has pending work
                        _active_planner = getattr(state, 'planner', None)
                        if _active_planner and _active_planner.has_active_dag():
                            _pending = _active_planner.get_parallel_batch()
                            if _pending:
                                logger.info("📋  [Monitor] DAG has %d pending tasks — accelerating resume", len(_pending))
                                _consecutive_run_tests = 3  # Trigger DAG resume on next tick

                elif action == "start_server":
                    logger.info("🖥️  Starting dev server …")
                    print(f"  {C}🖥️  Starting dev server …{R}")
                    await executor.start_dev_server()
                    session_mem.record_event("dev_server_started", "")

                elif action == "execute_task":
                    # V40: Circuit breaker — skip execution if too many consecutive failures
                    if _monitoring_task_failures >= _MONITORING_STOP_THRESHOLD:
                        _backoff_sleep = min(300, 30 * (2 ** (_monitoring_task_failures - _MONITORING_STOP_THRESHOLD)))
                        logger.warning(
                            "🛑  [Circuit Breaker] %d consecutive task failures — "
                            "API likely unavailable. Sleeping %ds.",
                            _monitoring_task_failures, _backoff_sleep,
                        )
                        state.record_activity(
                            "warning",
                            f"API unavailable: {_monitoring_task_failures} consecutive failures, "
                            f"sleeping {_backoff_sleep}s",
                        )
                        state.status = "cooldown"
                        await state.broadcast_state()
                        # V55: Interruptible circuit-breaker sleep — break on stop/restart
                        _backoff_remaining = _backoff_sleep
                        while _backoff_remaining > 0:
                            await asyncio.sleep(min(1.0, _backoff_remaining))
                            _backoff_remaining -= 1.0
                            if getattr(state, 'stop_requested', False) or getattr(state, 'restart_dev_server_requested', ''):
                                break
                        # V40 FIX: Fully reset after sleeping — allow full
                        # retry budget. Without this, overnight API outages
                        # permanently lock out task execution.
                        _monitoring_task_failures = 0
                        state.status = "monitoring"
                        logger.info("🔄  Circuit breaker reset — resuming task execution.")
                        continue

                    if state.stop_requested:
                        logger.info("🛑  Safe stop requested — skipping task execution.")
                        break

                    logger.info("📋  Executing additional task: %s", reason)
                    state.record_activity("task", f"Executing: {reason}")
                    # The local brain identified something to do
                    task_result = await executor.execute_task(reason, timeout=config.GEMINI_TIMEOUT_SECONDS)
                    state.last_task_status = task_result.status
                    state.last_task_duration = task_result.duration_s
                    state.tasks_completed += 1
                    if task_result.files_changed:
                        state.files_changed = list(set(state.files_changed + task_result.files_changed))
                        for f in task_result.files_changed:
                            state.record_change(f, "modified", reason)
                        logger.info(
                            "📄  Files changed (%d): %s",
                            len(task_result.files_changed),
                            ", ".join(task_result.files_changed[:30]),
                        )
                    if task_result.status == "error":
                        state.error_count += 1
                        _monitoring_task_failures += 1
                        state.record_activity("error", f"Task failed: {reason}", str(task_result.errors[:2]))

                        # V40: Exponential backoff after threshold
                        if _monitoring_task_failures >= _MONITORING_BACKOFF_THRESHOLD:
                            _backoff_secs = min(300, 30 * (2 ** (_monitoring_task_failures - _MONITORING_BACKOFF_THRESHOLD)))
                            logger.warning(
                                "⚡  [Circuit Breaker] %d consecutive failures — backing off %ds",
                                _monitoring_task_failures, _backoff_secs,
                            )
                            state.record_activity(
                                "warning",
                                f"Consecutive failures: {_monitoring_task_failures}, backing off {_backoff_secs}s",
                            )
                            await asyncio.sleep(_backoff_secs)
                    else:
                        _monitoring_task_failures = 0  # Reset on success
                        state.record_activity("success", f"Task completed ({task_result.duration_s:.1f}s): {reason}")
                    session_mem.record_event("task_executed", task_result.status)
                    await state.broadcast_state()

                elif action == "resume_dag":
                    # V41: Resume DAG execution with pending/re-queued tasks
                    if state.stop_requested:
                        logger.info("🛑  Safe stop requested — skipping DAG resume.")
                        break

                    logger.info("📋  [DAG Resume] Re-entering DAG execution: %s", reason)
                    state.status = "executing"
                    state.record_activity("task", f"Resuming DAG: {reason}")
                    await state.broadcast_state()

                    _dag_progress["active"] = True
                    _active_planner = getattr(state, 'planner', None)
                    if _active_planner:
                        await _update_dag_progress(_active_planner, 0, state=state)

                    try:
                        dag_result = await _execute_dag_recursive(
                            goal=goal,
                            local_brain=local_brain,
                            executor=executor,
                            session_mem=session_mem,
                            effective_project=str(effective_project),
                            depth=0,
                            max_depth=2,
                            state=state,
                            sandbox=sandbox,
                            tools=tools,
                            project_path=str(effective_project),
                        )
                        state.tasks_completed += 1
                        if dag_result.files_changed:
                            state.files_changed = list(set(state.files_changed + dag_result.files_changed))
                            for f in dag_result.files_changed:
                                state.record_change(f, "modified", "dag_resume")
                        if dag_result.success or dag_result.status == "partial":
                            state.record_activity("success", f"DAG resume completed ({dag_result.duration_s:.1f}s, {len(dag_result.files_changed)} files)")
                            _monitoring_task_failures = 0
                        else:
                            state.record_activity("error", f"DAG resume failed: {dag_result.errors[:2]}")
                            _monitoring_task_failures += 1
                    except Exception as dag_exc:
                        logger.error("📋  [DAG Resume] Exception: %s", dag_exc)
                        state.record_activity("error", f"DAG resume exception: {dag_exc}")
                        _monitoring_task_failures += 1

                    state.status = "monitoring"
                    await state.broadcast_state()
                    session_mem.record_event("dag_resumed", reason)

                elif action == "escalate":
                    logger.warning("🚨  Local brain requesting escalation: %s", reason)
                    _play_alert()
                    session_mem.record_event("escalation", reason)

                else:
                    # wait or unknown — proactive audit when idle long enough
                    _consecutive_waits += 1  # V40: Idle backoff counter
                    _idle_ticks = getattr(state, '_idle_ticks', 0) + 1
                    state._idle_ticks = _idle_ticks
                    _daily_audits = getattr(state, '_daily_audits', 0)

                    # V40 FIX: Reset daily audit cap every 24 hours
                    if time.time() - _audit_reset_time > 86400:
                        _audit_reset_time = time.time()
                        state._daily_audits = 0
                        _daily_audits = 0
                        logger.info("🔄  Daily audit cap reset (24h elapsed).")

                    # V42: Post-task gap analysis — runs ONCE after:
                    #   a) All DAG tasks complete (complex task), OR
                    #   b) Simple task has been executed and monitoring is idle
                    # Asks Gemini to compare current code against original goal
                    # and identify new work (missing features, bugs, improvements).
                    _gap_done = getattr(state, '_gap_analysis_done', False)
                    _active_planner = getattr(state, 'planner', None)
                    _dag_complete = (
                        _active_planner
                        and _active_planner._nodes
                        and _active_planner.is_epic_complete()
                    )
                    # V42 FIX: For simple tasks, state.planner is None.
                    # Trigger gap analysis when we have files_changed (proof
                    # work was done) but no planner — meaning a simple task ran.
                    _simple_task_done = (
                        not _active_planner
                        and state.files_changed
                    )
                    if (not _gap_done
                            and (_dag_complete or _simple_task_done)
                            and _idle_ticks >= 2
                            and not state.stop_requested):
                        state._gap_analysis_done = True
                        logger.info("🔍  [Gap Analysis] DAG complete — asking Gemini to identify remaining work …")
                        state.record_activity("task", "Gap analysis: scanning for missing work")
                        state.status = "analyzing"
                        await state.broadcast_state()

                        try:
                            # V42 FIX: Guard planner access for simple tasks
                            _epic_text = ""
                            _completed_summary = ""
                            if _active_planner:
                                _epic_text = getattr(_active_planner, '_epic_text', "") or ""
                                _completed_summary = _active_planner.get_completed_summary()
                            else:
                                # Simple task — summarise from files_changed
                                _completed_summary = (
                                    f"Simple task executed directly. "
                                    f"Files changed: {', '.join(state.files_changed[:20])}"
                                )

                            # V42: Scan workspace for actual files to give Gemini
                            # full context — critical for pre-existing projects
                            # or when DAG summary is thin.
                            _workspace_listing = ""
                            try:
                                _proj_path = Path(effective_project)
                                _skip = {'.git', 'node_modules', '__pycache__', '.ag-supervisor',
                                         '.next', 'dist', 'build', '.venv', 'venv', '.supervisor_lock'}
                                _found = []
                                for _fp in sorted(_proj_path.rglob("*")):
                                    if any(part in _skip for part in _fp.parts):
                                        continue
                                    if _fp.is_file():
                                        _rel = _fp.relative_to(_proj_path)
                                        _found.append(str(_rel))
                                        if len(_found) >= 100:
                                            break
                                if _found:
                                    _workspace_listing = "\n".join(_found)
                                    logger.info(
                                        "🔍  [Gap] Workspace scan: %d files found for context.",
                                        len(_found),
                                    )
                            except Exception as _ws_exc:
                                logger.debug("Gap analysis workspace scan failed: %s", _ws_exc)

                            gap_prompt = (
                                "You are a senior technical reviewer. A development task has been completed. "
                                "Compare the ORIGINAL GOAL against what was ACTUALLY DONE and identify ANY "
                                "remaining gaps, missing features, bugs, or improvements needed.\n\n"
                                f"ORIGINAL GOAL:\n{goal}\n\n"
                            )
                            if _epic_text:
                                gap_prompt += f"EPIC DETAILS:\n{_epic_text}\n\n"
                            gap_prompt += f"COMPLETED WORK:\n{_completed_summary}\n\n"
                            if _workspace_listing:
                                gap_prompt += f"CURRENT PROJECT FILES:\n{_workspace_listing}\n\n"
                            gap_prompt += (
                                "Review the project files carefully. Output a JSON array of new tasks needed:\n"
                                '{"tasks": [{"task_id": "gap-1", "description": "...", "dependencies": []}, ...]}\n\n'
                                "RULES:\n"
                                "1. Only list GENUINELY MISSING work — not cosmetic preferences\n"
                                "2. Each task must be small and atomic (1-3 files max)\n"
                                "3. Maximum 50 tasks\n"
                                "4. If everything looks complete, return {\"tasks\": []}\n"
                                "5. Output strict JSON only — no markdown, no explanation"
                            )

                            logger.info(
                                "🔍  [Gap→Gemini] Gap analysis prompt (%d chars): %.500s…",
                                len(gap_prompt), gap_prompt,
                            )
                            state.record_activity("llm_prompt", "Gemini: gap analysis", gap_prompt)

                            gap_response = await ask_gemini(gap_prompt, timeout=180, model=config.GEMINI_FALLBACK_MODEL)

                            if gap_response:
                                logger.info(
                                    "🔍  [Gap←Gemini] Response (%d chars): %.500s…",
                                    len(gap_response), gap_response,
                                )
                                import re as _re
                                _cleaned = _re.sub(r"```json?\s*", "", gap_response)
                                _cleaned = _re.sub(r"```\s*", "", _cleaned).strip()

                                try:
                                    _gap_data = json.loads(_cleaned)
                                    _gap_tasks = _gap_data.get("tasks", [])
                                except json.JSONDecodeError:
                                    _gap_tasks = []
                                    logger.warning("🔍  [Gap] Could not parse JSON response.")

                                if _gap_tasks:
                                    if _active_planner:
                                        # DAG mode: inject tasks into planner
                                        _injected = 0
                                        for gt in _gap_tasks[:10]:
                                            _tid = gt.get("task_id", f"gap-{_injected+1}")
                                            _desc = gt.get("description", "")
                                            _deps = gt.get("dependencies", [])
                                            if _desc and _active_planner.inject_task(_tid, _desc, _deps):
                                                _injected += 1

                                        if _injected > 0:
                                            logger.info(
                                                "🔍  [Gap Analysis] Injected %d new tasks into DAG.",
                                                _injected,
                                            )
                                            state.record_activity(
                                                "task",
                                                f"Gap analysis: {_injected} new tasks identified and queued",
                                            )
                                            state._gap_analysis_done = False
                                            _consecutive_waits = 0
                                            _dag_progress["active"] = True
                                            await _update_dag_progress(_active_planner, 0, state=state)
                                        else:
                                            logger.info("🔍  [Gap Analysis] No new work identified — goal appears complete.")
                                            state.record_activity("success", "Gap analysis: goal fully satisfied ✓")
                                    else:
                                        # V42: No active planner — create one and
                                        # inject tasks so they execute through the
                                        # normal DAG pipeline (Graph tab, Changes
                                        # tab, history, etc. all work properly).
                                        from .temporal_planner import TemporalPlanner
                                        _gap_planner = TemporalPlanner.from_brain(
                                            None, str(effective_project)
                                        )
                                        _gap_planner._epic_text = goal

                                        _injected = 0
                                        for gt in _gap_tasks[:10]:
                                            _tid = gt.get("task_id", f"gap-{_injected+1}")
                                            _desc = gt.get("description", "")
                                            _deps = gt.get("dependencies", [])
                                            if _desc and _gap_planner.inject_task(_tid, _desc, _deps):
                                                _injected += 1

                                        if _injected > 0:
                                            state.planner = _gap_planner
                                            logger.info(
                                                "🔍  [Gap Analysis] Created DAG with %d tasks (simple→DAG promotion).",
                                                _injected,
                                            )
                                            state.record_activity(
                                                "task",
                                                f"Gap analysis: {_injected} new tasks queued in fresh DAG",
                                            )
                                            state._gap_analysis_done = False
                                            _consecutive_waits = 0
                                            _dag_progress["active"] = True
                                            await _update_dag_progress(_gap_planner, 0, state=state)
                                            # Next tick: heuristic sees pending
                                            # planner work → triggers resume_dag
                                        else:
                                            logger.info("🔍  [Gap Analysis] No valid tasks to inject.")
                                            state.record_activity("success", "Gap analysis: goal fully satisfied ✓")
                                else:
                                    logger.info("🔍  [Gap Analysis] Gemini confirmed: no remaining gaps.")
                                    state.record_activity("success", "Gap analysis: goal fully satisfied ✓")
                            else:
                                logger.warning("🔍  [Gap Analysis] No response from Gemini.")

                        except Exception as gap_exc:
                            logger.error("🔍  [Gap Analysis] Failed: %s", gap_exc)
                            state.record_activity("error", f"Gap analysis failed: {gap_exc}")

                        state.status = "monitoring"
                        await state.broadcast_state()

                    # V40: Every ~5min of idle (30 ticks × 10s), run proactive audit
                    # Max 10 audits/day to conserve budget
                    if (_idle_ticks >= 30
                            and state.files_changed
                            and _daily_audits < 10):
                        state._idle_ticks = 0
                        state._daily_audits = _daily_audits + 1
                        logger.info(
                            "🔍  [Proactive] Idle for %d ticks — running project audit #%d …",
                            _idle_ticks, _daily_audits + 1,
                        )
                        state.status = "auditing"
                        state.record_activity(
                            "task",
                            f"Proactive audit #{_daily_audits + 1}: scanning project for issues",
                        )
                        await state.broadcast_state()

                        if state.stop_requested:
                            logger.info("🛑  Safe stop requested — skipping proactive audit.")
                            break

                        try:
                            # V74: Build project health context for targeted auditing (§5.3)
                            _health_lines = []
                            if hasattr(state, 'dag_complete_count'):
                                _health_lines.append(f"Tasks completed this session: {getattr(state, 'dag_complete_count', 0)}")
                            if state.files_changed:
                                _health_lines.append(f"Files changed: {len(state.files_changed)}")
                            if state.preview_running and state.preview_port:
                                _health_lines.append(f"Dev server: running on port {state.preview_port}")
                            else:
                                _health_lines.append("Dev server: NOT running")
                            _health_lines.append(f"Supervisor status: {state.status}")
                            _health_section = "\n".join(_health_lines)

                            audit_prompt = (
                                "You are a meticulous code auditor. Do a FULL PROJECT AUDIT.\n"
                                f"\nCURRENT PROJECT HEALTH:\n{_health_section}\n\n"
                                "Scan all source files for:\n"
                                "1. Missing imports, undefined references, broken requires\n"
                                "2. Broken file paths (images, SVGs, assets, config files)\n"
                                "3. Functions called with wrong arguments or missing params\n"
                                "4. Inconsistencies between files (renamed vars, changed APIs)\n"
                                "5. Missing error handling, uncaught exceptions\n"
                                "6. Dead code, unused imports, orphaned functions\n"
                                "7. Missing package dependencies\n"
                                "8. Any build or runtime errors waiting to happen\n\n"
                                "Fix ALL real issues you find. Be thorough but surgical — "
                                "do NOT refactor working code or change functionality."
                            )
                            logger.info(
                                "🔍  [Proactive→Gemini] Audit prompt (%d chars): %s",
                                len(audit_prompt), audit_prompt,
                            )
                            state.record_activity("llm_prompt", "Gemini: proactive audit", audit_prompt)
                            audit_result = await executor.execute_task(
                                audit_prompt,
                                timeout=min(config.GEMINI_TIMEOUT_SECONDS, 300),
                            )
                            if audit_result.files_changed:
                                for f in audit_result.files_changed:
                                    state.record_change(f, "fixed", "proactive_audit")
                                state.files_changed = list(set(
                                    state.files_changed + audit_result.files_changed
                                ))
                                state.record_activity(
                                    "success",
                                    f"Proactive audit fixed {len(audit_result.files_changed)} files",
                                )
                                logger.info(
                                    "🔍  [Proactive] Fixed %d files.",
                                    len(audit_result.files_changed),
                                )
                                print(f"  {G}✓ Proactive audit fixed "
                                      f"{len(audit_result.files_changed)} files{R}")
                            else:
                                state.record_activity(
                                    "success", "Proactive audit: project looks clean ✓",
                                )
                                logger.info("🔍  [Proactive] No issues found.")
                                print(f"  {G}✓ Proactive audit: all clean{R}")

                        except Exception as audit_exc:
                            logger.debug("🔍  [Proactive] Audit error: %s", audit_exc)

                        state.status = "monitoring"
                        await state.broadcast_state()

                # ── Health tracking ──
                consecutive_errors = 0
                _recovery_engine.record_success()

                # ── Periodic status log + live checkpoint ──
                if loop_count % 6 == 0:  # Every ~60s at 10s poll
                    logger.info(
                        "📊  Status: files=%d, git=%s, errors=%d, dev_server=%s",
                        len(ctx.workspace_files), ctx.git_branch,
                        ctx.diagnostics_errors, ctx.dev_server_running,
                    )
                    await state.broadcast_state()
                    # Write a live checkpoint so the project shows as resumable
                    # even if never safe-stopped.
                    _save_session_state(
                        goal, str(effective_project), dag_progress=_dag_progress
                    )
                    # ── Stuck-task watchdog ──
                    # If a task has been in 'running' state for > 15 minutes
                    # with no LLM calls (e.g. pool worker crashed silently),
                    # reset it to 'pending' so the pool retries it.  This
                    # directly fixes the t53-blocking-everything scenario.
                    _STUCK_TASK_TIMEOUT = 15 * 60  # 15 minutes
                    _now = time.time()
                    _watchdog_planner = getattr(state, 'planner', None)
                    if _watchdog_planner and hasattr(_watchdog_planner, '_nodes'):
                        _reset_count = 0
                        for _wn in list(_watchdog_planner._nodes.values()):
                            if _wn.status != "running":
                                continue
                            _age = _now - _wn.started_at if _wn.started_at else _STUCK_TASK_TIMEOUT + 1
                            if _age > _STUCK_TASK_TIMEOUT:
                                logger.warning(
                                    "🕵️  [Watchdog] Task %s stuck in 'running' for %.0fm — resetting to pending",
                                    _wn.task_id, _age / 60,
                                )
                                _wn.status = "pending"
                                _wn.started_at = 0.0
                                _reset_count += 1
                        if _reset_count:
                            _watchdog_planner._save_state()
                            state.record_activity(
                                "system",
                                f"Watchdog: reset {_reset_count} stuck task(s) to pending",
                            )
                            logger.info(
                                "🕵️  [Watchdog] Reset %d stuck task(s). DAG should resume on next tick.",
                                _reset_count,
                            )

                # ── Auto-preview check every ~30s ──
                if loop_count % 3 == 0:
                    await _auto_preview_check(
                        sandbox, executor, tools, state, str(effective_project)
                    )

                    # ── V60: Vite dev-log compile error scanner ──────────────────
                    # Tails /tmp/dev-server.log for Vite/Babel/TS errors (JSX parse,
                    # TypeScript type errors) and injects a priority-92 BUILD fix task.
                    _vite_planner = getattr(state, 'planner', None)
                    if state.preview_running and _vite_planner:
                        await _scan_vite_dev_log(sandbox, _vite_planner, state)

                    # ── V53: Console error capture & categorised DAG fix task ──
                    if state.preview_running and _error_collector_started:
                        _err_result = await _capture_console_errors(sandbox)
                        _console_errs = _err_result.get("formatted", [])
                        _cat_counts = _err_result.get("categories", {})
                        _raw_errs = _err_result.get("raw", [])

                        # V55: LOCAL self-heal for infrastructure browser errors.
                        # These never need Gemini — they are pure sandbox/server state issues.
                        # Each pattern has its own targeted fix + activity pill label.
                        # Throttled per pattern to avoid restart loops.

                        def _ce_set_op(label: str) -> None:
                            if hasattr(state, 'set_current_operation'):
                                state.set_current_operation(label)

                        def _src(e): return e.get("source", "") or e.get("url", "")
                        def _msg(e): return e.get("message", "") or ""

                        # Pattern A: Vite dep cache stale (504 Outdated Optimize Dep)
                        _vite_stale = any(
                            "vite/deps" in _src(e)
                            or "Outdated Optimize Dep" in _msg(e)
                            or (e.get("status") == 504 and ".vite" in _src(e))
                            for e in _raw_errs
                        )
                        # Pattern B: Dev server down — ERR_CONNECTION_REFUSED or ERR_EMPTY_RESPONSE
                        _server_down = any(
                            ("ERR_CONNECTION_REFUSED" in _msg(e) or "ERR_EMPTY_RESPONSE" in _msg(e))
                            and "localhost" in _src(e)
                            for e in _raw_errs
                        )
                        # Pattern C: Failed to fetch stale dynamic chunk (.vite URL → clear cache)
                        _stale_chunk = any(
                            "Failed to fetch dynamically imported module" in _msg(e)
                            and (".vite" in _src(e) or "node_modules" in _src(e))
                            for e in _raw_errs
                        )

                        _ts = time.time()
                        _healed = False

                        if _vite_stale or _stale_chunk:
                            _cd = getattr(state, '_vite_cache_clear_ts', 0)
                            if _ts - _cd > 120:
                                state._vite_cache_clear_ts = _ts
                                _reason = "504 Outdated Optimize Dep" if _vite_stale else "stale dynamic chunk"
                                logger.warning("🖥️  [Console] %s → clearing Vite cache + restart…", _reason)
                                state.record_activity("system", f"⚠️ Vite cache stale ({_reason}) — clearing + restarting")
                                _ce_set_op('🧹 Clearing stale Vite cache…')
                                try:
                                    await sandbox.exec_command(
                                        "rm -rf /workspace/node_modules/.vite /workspace/.vite "
                                        "/workspace/node_modules/.cache 2>/dev/null; true", timeout=10,
                                    )
                                    await sandbox.exec_command("pkill -f 'vite' 2>/dev/null || true", timeout=5)
                                    await __import__('asyncio').sleep(2)
                                    await executor.start_dev_server()
                                    state.record_activity("system", "Vite cache cleared + server restarted ✅")
                                except Exception as _e:
                                    logger.debug("🖥️  [Console] Vite cache clear failed: %s", _e)
                                finally:
                                    _ce_set_op("")
                                _healed = True

                        elif _server_down:
                            _cd = getattr(state, '_server_restart_ts', 0)
                            if _ts - _cd > 60:
                                state._server_restart_ts = _ts
                                logger.warning("🖥️  [Console] ERR_CONNECTION_REFUSED — dev server is down, restarting…")
                                state.record_activity("system", "⚠️ Dev server unreachable — restarting")
                                _ce_set_op('🔄 Restarting dev server…')
                                try:
                                    await sandbox.exec_command("pkill -f 'vite|next|webpack' 2>/dev/null || true", timeout=5)
                                    await __import__('asyncio').sleep(1)
                                    await executor.start_dev_server()
                                    state.record_activity("system", "Dev server restarted ✅")
                                except Exception as _e:
                                    logger.debug("🖥️  [Console] Server restart failed: %s", _e)
                                finally:
                                    _ce_set_op("")
                                _healed = True

                        # Strip locally-handled errors so they don't also go to Gemini
                        if _healed:
                            _console_errs = [
                                e for e in _console_errs
                                if not any(pat in e for pat in (
                                    "vite/deps", "Outdated Optimize Dep",
                                    "ERR_CONNECTION_REFUSED", "ERR_EMPTY_RESPONSE",
                                    "Failed to fetch dynamically imported module",
                                ))
                            ]

                        _ce_planner = getattr(state, 'planner', None)
                        if _console_errs and _ce_planner:
                            # ── Guard: don't stack fix tasks ──────────────────────
                            # Check 1: Is any console-fix task already pending/running?
                            _ce_in_flight = any(
                                (n.task_id.startswith("console-fix") or
                                 n.task_id.endswith("-CONSOLE")) and
                                n.status in ("pending", "running")
                                for n in _ce_planner._nodes.values()
                            )
                            # Check 2: Time-based cooldown — wait 120s after the last
                            # injection before queuing another, so the fix can run and
                            # be tested before we re-evaluate console errors.
                            _ce_last_ts = getattr(state, '_console_fix_last_ts', 0)
                            _ce_cooldown_ok = (time.time() - _ce_last_ts) > 120

                            if _ce_in_flight:
                                logger.debug(
                                    "🖥️  [Error Capture] console-fix already in-flight — skipping"
                                )
                            elif not _ce_cooldown_ok:
                                logger.debug(
                                    "🖥️  [Error Capture] console-fix cooldown active (%.0fs remaining) — skipping",
                                    120 - (time.time() - _ce_last_ts),
                                )
                            else:
                                logger.info(
                                    "🖥️  [Error Capture] %d browser error(s) — injecting fix task",
                                    len(_console_errs),
                                )

                                # Build a categorised description with specific fix hints
                                _err_summary = "\n".join(_console_errs[:10])  # cap at 10
                                _hint_blocks = []
                                for _cat, _count in sorted(_cat_counts.items(), key=lambda x: -x[1]):
                                    _hint = _CATEGORY_FIX_HINTS.get(
                                        _cat, _CATEGORY_FIX_HINTS["uncategorised"]
                                    )
                                    _hint_blocks.append(f"• [{_cat} × {_count}] {_hint}")

                                _fix_desc = (
                                    "[CONSOLE] Live preview has browser errors that "
                                    "must be fixed. DO NOT restart the dev server — fix the root cause "
                                    "in the source files.\n\n"
                                    "ERRORS DETECTED:\n"
                                    f"{_err_summary}\n\n"
                                    "REQUIRED FIXES (apply ALL that are relevant):\n"
                                    + "\n".join(_hint_blocks)
                                )

                                _ce_counter = getattr(state, '_console_fix_counter', 0) + 1
                                state._console_fix_counter = _ce_counter
                                _ce_offset = _ce_planner.get_task_offset() + 1 if _ce_planner else _ce_counter
                                _ce_task_id = f"t{_ce_offset}-CONSOLE"

                                _injected = _ce_planner.inject_task(
                                    task_id=_ce_task_id,
                                    description=_fix_desc,
                                    dependencies=[],
                                    priority=85,  # Below build-health (90), above normal tasks (0)
                                )
                                if _injected:
                                    state._console_fix_last_ts = time.time()
                                    logger.info(
                                        "🖥️  [Error Capture] Injected fix task %s (priority=85)",
                                        _ce_task_id,
                                    )
                                    if state:
                                        state.record_activity(
                                            "warning",
                                            f"Browser errors → fix task {_ce_task_id}: {list(_cat_counts.keys())[:3]}",
                                        )
                                    await state.broadcast_state()

            except SandboxError as exc:
                consecutive_errors += 1
                logger.error("🐳  Sandbox error (#%d): %s", consecutive_errors, exc)
                session_mem.record_event("sandbox_error", str(exc))

                if consecutive_errors >= 3:
                    strategy = _recovery_engine.recover(str(exc))
                    logger.info("🔄  Recovery strategy: %s", strategy)

                    if strategy == "RESTART_SANDBOX":
                        logger.info("🔄  Restarting sandbox …")
                        try:
                            await sandbox.destroy()
                        except Exception:
                            pass
                        sandbox_info = await sandbox.create(
                            effective_project, mount_mode=mount_mode,
                        )
                        tools = ToolServer(sandbox)
                        executor = HeadlessExecutor(tools, sandbox)
                        logger.info("🔄  Sandbox restarted: %s", sandbox_info.container_id)

                    elif strategy == "SWITCH_MOUNT":
                        new_mode = "copy" if mount_mode == "bind" else "bind"
                        logger.info("🔄  Switching mount mode: %s → %s", mount_mode, new_mode)
                        sandbox_info = await sandbox.switch_mount_mode(new_mode)
                        mount_mode = new_mode
                        tools = ToolServer(sandbox)
                        executor = HeadlessExecutor(tools, sandbox)

                    elif strategy == "REBUILD_IMAGE":
                        logger.info("🔄  Force-rebuilding sandbox image …")
                        await sandbox.build_image(force=True)
                        await sandbox.destroy()
                        sandbox_info = await sandbox.create(
                            effective_project, mount_mode=mount_mode,
                        )
                        tools = ToolServer(sandbox)
                        executor = HeadlessExecutor(tools, sandbox)

                    consecutive_errors = 0

            except Exception as exc:
                consecutive_errors += 1
                tb_str = traceback.format_exc()
                logger.error("💥  Unexpected error (#%d): %s\n%s", consecutive_errors, exc, tb_str)

                if consecutive_errors >= 5:
                    strategy = _recovery_engine.recover(str(exc))
                    logger.info("🔄  Recovery strategy: %s", strategy)
                    consecutive_errors = 0

    except DockerNotAvailableError as exc:
        logger.critical("🐳  Docker is not available: %s", exc)
        print(f"\n  {config.ANSI_RED}🐳 Docker is not available: {exc}{R}")
        print(f"  {config.ANSI_YELLOW}Install Docker Desktop: https://docs.docker.com/get-docker/{R}")
        _play_alert()

    except KeyboardInterrupt:
        logger.info("🛑  Supervisor interrupted by user.")
        print(f"\n  {config.ANSI_YELLOW}🛑 Interrupted. Cleaning up …{R}")

    except Exception as exc:
        tb_str = traceback.format_exc()
        logger.critical("💀  FATAL ERROR: %s\n%s", exc, tb_str)
        print(f"\n  {config.ANSI_RED}💀 FATAL: {exc}{R}")

        # Ask Gemini if this is transient or a code bug
        try:
            triage = await _triage_fatal_error(tb_str)
            if triage:
                logger.info("🩺  Gemini triage: %s", triage[:200])
        except Exception:
            pass

    finally:
        # ── Shutdown Sequence (V54: fully parallelized) ───────────────
        logger.info("🧹  Cleaning up …")
        state.status = "stopped"
        print(f"  {C}🧹 Cleaning up …{R}")

        # ── 0. Immediately clear lockfile + preview port ───────────────
        # V55: On natural completion (session_complete=True), keep the preview
        # port alive so the user can inspect the result. Only clear it on
        # explicit Stop (stop_requested=True) or on a fatal error.
        _is_natural_complete = getattr(state, 'session_complete', False) and not getattr(state, 'stop_requested', False)
        if project_path:
            if not _is_natural_complete:
                _clear_preview_port(project_path)
            _remove_lockfile(project_path)
            # V59: Clear DEEP_ANALYSIS.md — it's session-scoped (stale analysis
            # from a previous goal would mislead audits in the next session).
            try:
                _da_cleanup = Path(project_path) / ".ag-supervisor" / "DEEP_ANALYSIS.md"
                if _da_cleanup.exists():
                    _da_cleanup.unlink()
                    logger.debug("🔬  [DeepAnalysis] Cleared DEEP_ANALYSIS.md on shutdown.")
            except Exception:
                pass

        # ── 1. Save DAG state NOW — before copy_workspace_out() ────────
        # copy_workspace_out() can overwrite the host's epic_state.json
        # with a stale container copy. Saving here means the in-memory
        # state is written to the HOST path before any copy happens,
        # so copy-out can only write an older file (which we then overwrite
        # again below to be safe). No more re-save workaround needed.
        try:
            _planner = getattr(state, "planner", None)
            if _planner and hasattr(_planner, "_save_state") and _planner._nodes:
                _planner._save_state(force=True)
                logger.info("💾  [Shutdown] DAG state saved (%d nodes).", len(_planner._nodes))
        except Exception as _pse:
            logger.debug("💾  [Shutdown] Could not save planner state: %s", _pse)

        # V60: Force-save phase state so Phases tab resumes correctly on restart.
        try:
            _pm = getattr(state, "phase_mgr", None)
            if _pm and hasattr(_pm, "force_save"):
                _pm.force_save()
                logger.info("💾  [Shutdown] Phase state force-saved.")
        except Exception as _pmse:
            logger.debug("💾  [Shutdown] Could not save phase state: %s", _pmse)


        # ── 2. Cancel API server with a tight timeout ──────────────────
        # Without a timeout, await api_task can hang if Uvicorn is stuck.
        if api_task and not api_task.done():
            api_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(api_task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # ── 3. Kill backend/worker processes in the sandbox ───────────
        # Do this BEFORE destroy() so they terminate cleanly rather than
        # being SIGKILL'd with the container — avoids corrupt DB/lock files.
        try:
            _kill_ports = list(range(4000, 4000 + len(getattr(executor, "_backends", []))))
            _kill_ports += list(range(4001, 4010))  # workers on adjacent ports
            if _kill_ports and sandbox and sandbox.is_running:
                _fuser_cmds = " ".join(f"{p}/tcp" for p in _kill_ports[:12])
                await sandbox.exec_command(
                    f"fuser -k {_fuser_cmds} 2>/dev/null; pkill -f 'celery worker' 2>/dev/null || true",
                    timeout=5,
                )
        except Exception:
            pass

        # ── 4. Shadow container cleanup — non-blocking ─────────────────
        async def _cleanup_shadows() -> None:
            try:
                _is_win = platform.system() == "Windows"
                if _is_win:
                    _proc = await asyncio.create_subprocess_exec(
                        "powershell", "-Command",
                        "docker ps -a -q --filter 'name=shadow-' | ForEach-Object { docker rm -f $_ }",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                else:
                    _proc = await asyncio.create_subprocess_shell(
                        "docker ps -a -q --filter 'name=shadow-' | xargs -r docker rm -f",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                await asyncio.wait_for(_proc.wait(), timeout=15)
            except Exception:
                pass

        # ── 5. Run all slow cleanup in parallel ────────────────────────
        # copy_workspace_out + destroy + shadow cleanup + volume cleanup
        # + close Ollama all happen concurrently — total time = longest one.
        async def _do_copy_out() -> None:
            try:
                await sandbox.copy_workspace_out()
                # Re-save state after copy-out in case container copy was newer
                try:
                    _pl = getattr(state, "planner", None)
                    if _pl and hasattr(_pl, "_save_state") and _pl._nodes:
                        _pl._save_state(force=True)
                except Exception:
                    pass
            except Exception:
                pass

        async def _do_destroy() -> None:
            try:
                await sandbox.destroy()
            except Exception:
                pass

        async def _do_volumes() -> None:
            try:
                _vol_removed = await sandbox.cleanup_stale_volumes()
                if _vol_removed:
                    logger.info("🗑️  [Shutdown] Cleaned up %d orphaned volume(s)", _vol_removed)
            except Exception:
                pass

        async def _do_close_brain() -> None:
            try:
                await local_brain.close()
            except Exception:
                pass

        # V75: Explicitly close the persistent PTY process
        async def _do_close_pty() -> None:
            try:
                from .retry_policy import get_quota_probe
                get_quota_probe()._pty_close()
            except Exception:
                pass

        await asyncio.gather(
            _do_copy_out(),
            _do_destroy(),
            _cleanup_shadows(),
            _do_volumes(),
            _do_close_brain(),
            _do_close_pty(),
            return_exceptions=True,
        )

        logger.info("🏁  Supervisor shut down cleanly.")


# ─────────────────────────────────────────────────────────────
# Session State Persistence
# ─────────────────────────────────────────────────────────────

def _save_session_state(
    goal: str,
    project_path: str | None,
    *,
    dag_progress: dict | None = None,
) -> None:
    """Save current session state for auto-resume after reboot.

    Also writes a ``checkpoint.json`` into the project's ``.ag-supervisor/``
    directory so that new/running/crashed projects surface the resume UI
    even without a safe-stop checkpoint.
    """
    state = {
        "goal": goal,
        "project_path": project_path,
        "timestamp": time.time(),
    }
    try:
        with open(_SESSION_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        logger.info("💾  Session state saved for auto-resume.")
    except Exception as exc:
        logger.warning("Could not save session state: %s", exc)

    if project_path:
        try:
            proj_state_dir = Path(project_path) / ".ag-supervisor"
            proj_state_dir.mkdir(parents=True, exist_ok=True)

            # ── session_state.json (goal + timestamp only) ──
            proj_session = proj_state_dir / "session_state.json"
            with open(proj_session, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)

            # ── checkpoint.json (richer — includes DAG progress) ──
            # Written on every periodic save so projects are resumable
            # even if they were never safe-stopped.
            cp = {
                "goal": goal,
                "project_path": str(project_path),
                "timestamp": state["timestamp"],
                "status": "running",
                "dag_active": bool(dag_progress and dag_progress.get("active")),
                "dag_completed": int(dag_progress.get("completed", 0)) if dag_progress else 0,
                "dag_total": int(dag_progress.get("total", 0)) if dag_progress else 0,
                "dag_pending": int(dag_progress.get("pending", 0)) if dag_progress else 0,
                "dag_failed": int(dag_progress.get("failed", 0)) if dag_progress else 0,
                "dag_cancelled": int(dag_progress.get("cancelled", 0)) if dag_progress else 0,
                "dag_nodes": list(dag_progress.get("nodes", [])) if dag_progress else [],
            }
            checkpoint_path = proj_state_dir / "checkpoint.json"
            # Only overwrite if we have richer data OR no checkpoint exists yet
            if not checkpoint_path.exists() or dag_progress is not None:
                with open(checkpoint_path, "w", encoding="utf-8") as f:
                    json.dump(cp, f, indent=2)
        except Exception as exc:
            logger.debug("Could not save project-local session/checkpoint: %s", exc)


def _load_session_state() -> tuple[str, str | None] | None:
    """Load saved session state. Returns (goal, project_path) or None."""
    if not _SESSION_STATE_PATH.exists():
        return None
    try:
        with open(_SESSION_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        age = time.time() - state.get("timestamp", 0)
        if age > 86400:
            return None
        goal = state.get("goal")
        project_path = state.get("project_path")
        if not goal:
            return None
        return (goal, project_path)
    except Exception:
        return None


def _clear_session_state() -> None:
    """Clear the session state file."""
    try:
        if _SESSION_STATE_PATH.exists():
            _SESSION_STATE_PATH.unlink()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Interactive goal & project selection
# ─────────────────────────────────────────────────────────────

def _interactive_goal() -> tuple[str, str | None]:
    """Present an interactive menu when no --goal is supplied."""
    print("\n  What would you like to do?\n")
    print("  [1] 🆕  Build something new")
    print("  [2] 🔄  Continue from an existing workspace")
    print()

    choice = ""
    while choice not in ("1", "2"):
        choice = input("  Enter choice (1 or 2): ").strip()

    if choice == "1":
        project_name = input("\n  📁 Project name: ").strip()
        if not project_name:
            print("  [ERROR] No project name provided. Exiting.")
            sys.exit(1)

        project_dir = EXPERIMENTS_DIR / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        print(f"  ✅ Created: {project_dir}")

        goal = input("\n  🎯 Enter your goal:\n  > ").strip()
        if not goal:
            print("  [ERROR] No goal provided. Exiting.")
            sys.exit(1)

        goal = f"[Workspace: {project_dir}] {goal}"
        return goal, str(project_dir)

    else:
        if not EXPERIMENTS_DIR.is_dir():
            print(f"  [ERROR] Experiments directory not found: {EXPERIMENTS_DIR}")
            sys.exit(1)

        workspaces = sorted([d.name for d in EXPERIMENTS_DIR.iterdir() if d.is_dir()])
        if not workspaces:
            print("  [ERROR] No workspace folders found.")
            sys.exit(1)

        print(f"\n  Found {len(workspaces)} workspaces:\n")
        for i, ws in enumerate(workspaces, 1):
            print(f"    [{i:2d}] {ws}")
        print()

        ws_idx = 0
        while ws_idx < 1 or ws_idx > len(workspaces):
            try:
                ws_idx = int(input("  Enter workspace number: ").strip())
            except ValueError:
                continue

        chosen_ws = workspaces[ws_idx - 1]
        chosen_path = EXPERIMENTS_DIR / chosen_ws
        print(f"  ✅ Selected: {chosen_path}")

        goal = input(f"\n  🎯 What should I do in '{chosen_ws}'?\n  > ").strip()
        if not goal:
            print("  [ERROR] No goal provided. Exiting.")
            sys.exit(1)

        goal = f"[Workspace: {chosen_path}] {goal}"
        return goal, str(chosen_path)



# ─────────────────────────────────────────────────────────────
# V42: Proactive Audit Engine
# ─────────────────────────────────────────────────────────────

async def _run_proactive_audit(
    executor,
    local_brain,
    session_mem,
    planner,
    state,
    effective_project: str,
    goal: str,
    sandbox=None,
    tools=None,
) -> dict | None:
    """
    V42: Proactive audit — runs during idle monitoring to find
    and create tasks for quality improvements.

    Triggered when:
      - No DAG work is pending
      - No user instructions queued
      - System has been idle for 3+ monitoring ticks

    Returns dict with tasks_created count, or None on skip.
    """
    import time as _time
    from pathlib import Path

    C = config.ANSI_CYAN
    G = config.ANSI_GREEN
    Y = config.ANSI_YELLOW
    R = config.ANSI_RESET
    B = config.ANSI_BOLD

    start = _time.time()
    _proj_path = Path(effective_project)

    # V62: Quota pause guard — skip proactive audit entirely when paused.
    try:
        from .retry_policy import get_daily_budget as _get_pro_budget
        _pro_budget = _get_pro_budget()
        if _pro_budget.quota_paused:
            logger.info("⏸  [Proactive] Quota paused — skipping proactive audit.")
            if state:
                state.record_activity("system", "Proactive audit skipped: quota paused")
            return None
    except Exception:
        pass

    # Gather project file tree
    _file_tree = ""
    try:
        _all_files = sorted(
            str(f.relative_to(_proj_path))
            for f in _proj_path.rglob("*")
            if f.is_file()
            and "node_modules" not in str(f)
            and ".git" not in str(f)
            and "__pycache__" not in str(f)
        )
        _file_tree = "\n".join(f"  - {f}" for f in _all_files[:100])
    except Exception:
        pass

    # Read changed files from state
    _changed = getattr(state, "files_changed", [])[:30]
    _file_list = "\n".join(f"  - {f}" for f in _changed)

    # Read file contents for context
    _file_contents = ""
    for f in _changed[:15]:
        _full = _proj_path / f
        if _full.exists() and _full.is_file():
            try:
                _txt = _full.read_text(encoding="utf-8")[:6000]
                _file_contents += f"\n--- {f} ---\n{_txt}\n"
            except Exception:
                pass
    # V67: Build cross-phase context from disk if available.
    # Previously restricted to current phase only; now includes ALL phases
    # so the proactive audit can generate tasks from any phase where
    # dependencies are met, maximising concurrent work output.
    _proactive_phase_ctx = ""
    try:
        _phase_file = _proj_path / ".ag-supervisor" / "phase_state.json"
        if _phase_file.exists():
            _ph_data = json.loads(_phase_file.read_text(encoding="utf-8"))
            _cur_idx = _ph_data.get("current_phase", 1)
            _phases = _ph_data.get("phases", [])

            if _phases:
                _total = len(_phases)
                _ctx_parts: list[str] = []
                _ctx_parts.append(
                    f"\nALL PROJECT PHASES (CROSS-PHASE SCOPE — MAXIMIZE WORK):\n"
                    f"Active phase: {_cur_idx} of {_total}\n\n"
                )

                for _p in _phases:
                    _p_id = _p.get("id", "?")
                    _p_status = _p.get("status", "pending")
                    _p_tasks = _p.get("tasks", [])
                    _p_pending = [t for t in _p_tasks if t.get("status") != "done"]
                    _p_done = [t for t in _p_tasks if t.get("status") == "done"]
                    _marker = "✅" if _p_status == "completed" else ("▶" if _p_id == _cur_idx else "⏳")

                    _ctx_parts.append(
                        f"{_marker} Phase {_p_id}: \"{_p.get('name','')}\"\n"
                        f"   Focus: {_p.get('focus','')}\n"
                        f"   Status: {_p_status} ({len(_p_done)} done, {len(_p_pending)} pending)\n"
                    )
                    for t in _p_pending[:15]:
                        _ctx_parts.append(f"     - [PENDING] {t.get('title','')}\n")
                    if len(_p_pending) > 15:
                        _ctx_parts.append(f"     ... and {len(_p_pending) - 15} more pending tasks\n")
                    if _p_done:
                        _ctx_parts.append(f"     ({len(_p_done)} tasks already done)\n")
                    _ctx_parts.append("\n")

                _ctx_parts.append(
                    f"CROSS-PHASE SCOPE — MAXIMIZE TASK OUTPUT: Create tasks from ANY phase "
                    f"that has PENDING work. This includes EARLIER phases (2, 3, 4, etc.) even "
                    f"if the active phase is {_cur_idx}. A PARTIAL phase has incomplete work -- "
                    f"generate tasks for it. Do NOT skip any phase. "
                    f"If an unchecked task is already implemented, create a task: "
                    f"'[PHASE-DONE] <task-id> appears complete — verify and mark done'.\n\n"
                )
                _proactive_phase_ctx = "".join(_ctx_parts)
    except Exception:
        pass

    scan_prompt = (
        "You are a PROACTIVE CODE AUDITOR. The project is idle — scan for improvements.\n\n"
        f"{_proactive_phase_ctx}"
        "ORIGINAL GOAL:\n"
        f"{goal}\n\n"
        "FILE TREE:\n"
        f"{_file_tree or '(no files)'}\n\n"
        "CHANGED FILES:\n"
        f"{_file_list or '(none)'}\n\n"
        "FILE CONTENTS:\n"
        f"{_file_contents or '(none)'}\n\n"
        "LOOK FOR:\n"
        "- Missing features from the original goal\n"
        "- Bugs, broken imports, undefined variables\n"
        "- Stubs or placeholder implementations\n"
        "- Missing error handling\n"
        "- Accessibility issues\n"
        "- Performance improvements\n"
        "\n"
        "\U0001f3a8 UI/UX VISUAL EXCELLENCE (MINIMUM 5 FIX TASKS):\n"
        "- Missing or generic SVG icons (should be custom, brand-matched)\n"
        "- No micro-interactions (hover, scroll-reveal, button feedback)\n"
        "- Poor layout rhythm (no whitespace system, no asymmetric sections)\n"
        "- Generic typography (browser defaults instead of premium fonts)\n"
        "- Missing component states (empty, error, loading, disabled)\n"
        "- No page transitions or skeleton loading screens\n"
        "- Flat shadows or no depth system (needs layered box-shadow)\n"
        "- Missing glassmorphism, gradients, or modern visual effects\n"
        "- Site does not WOW at first glance (2026 Awwwards quality required)\n\n"
        "CONTENT & PAGE FLOW (if user-facing pages exist):\n"
        "- Missing or placeholder text content\n"
        "- Broken navigation or dead links\n"
        "- Missing responsive layout at mobile/tablet breakpoints\n"
        "- Missing meta tags, SEO, or Open Graph tags\n\n"
        "Respond with a JSON array of tasks:\n"
        '[{"id": "1", "description": "<detailed description>"}]\n'
        "Use sequential numeric IDs: 1, 2, 3, etc.\n"
        "If nothing to improve: respond []\n"
        "IMPORTANT: Respond with ONLY the JSON array."
    )

    logger.info("\U0001f50d  [Proactive] Audit prompt (%d chars)", len(scan_prompt))
    if state:
        state.record_activity("task", "Proactive audit: scanning for improvements")
        state.record_activity("llm_prompt", "Proactive audit scan", scan_prompt)

    tasks_created = 0
    try:
        from .gemini_advisor import ask_gemini
        raw = await ask_gemini(scan_prompt, timeout=180, cwd=effective_project or None, model=config.GEMINI_FALLBACK_MODEL)

        if raw:
            logger.info("\U0001f50d  [Proactive] Response (%d chars): %.500s", len(raw), raw)
            import re as _re
            cleaned = _re.sub(r"```json?\s*", "", raw)
            cleaned = _re.sub(r"```\s*", "", cleaned).strip()
            try:
                result = json.loads(cleaned)
            except json.JSONDecodeError:
                _match = _re.search(r'\[.*\]', cleaned, _re.DOTALL)
                result = json.loads(_match.group()) if _match else None

            if isinstance(result, list) and result:
                from .temporal_planner import TemporalPlanner
                if planner is None:
                    planner = TemporalPlanner.from_brain(
                        local_brain if await local_brain.is_available() else None,
                        effective_project,
                    )
                    state.planner = planner

                # ── Fingerprint dedup (cross-cycle) ──
                import hashlib as _hashlib
                def _desc_fp(d: str) -> str:
                    return _hashlib.md5(" ".join(d.lower().split()).encode()).hexdigest()[:16]

                _fp_store_path = Path(effective_project) / ".ag-supervisor" / "audit_done_fingerprints.json"
                _done_fps: set[str] = set()
                _done_descs: list[str] = []
                try:
                    if _fp_store_path.exists():
                        _fp_data = json.loads(_fp_store_path.read_text(encoding="utf-8"))
                        _done_fps = set(_fp_data.get("fingerprints", []))
                        _done_descs = list(_fp_data.get("descriptions", []))  # V62: no cap
                except Exception:
                    pass

                # V62: Augment from dag_history.jsonl — full cross-session history
                try:
                    _dag_hist_path = Path(effective_project) / ".ag-supervisor" / "dag_history.jsonl"
                    if not _dag_hist_path.exists():
                        _dag_hist_path = Path(effective_project) / "dag_history.jsonl"
                    if _dag_hist_path.exists():
                        for _hline in _dag_hist_path.read_text(encoding="utf-8").strip().split("\n"):
                            try:
                                _hentry = json.loads(_hline)
                                for _ndata in _hentry.get("nodes", {}).values():
                                    if _ndata.get("status") == "complete":
                                        _hdesc = _ndata.get("description", "")
                                        if _hdesc:
                                            _hfp = _desc_fp(_hdesc)
                                            if _hfp not in _done_fps:
                                                _done_fps.add(_hfp)
                                                _done_descs.append(_hdesc[:200])
                            except Exception:
                                continue
                except Exception:
                    pass

                existing_ids = planner.get_all_task_ids()
                # V45: Use continuous tX numbering
                _pro_offset = getattr(planner, '_offset', len(existing_ids))
                for issue in result:
                    if not isinstance(issue, dict):
                        continue
                    desc = issue.get("description", "")
                    if not desc:
                        continue

                    # [PHASE-DONE] auto-mark — same as post-DAG audit
                    if "[PHASE-DONE]" in desc:
                        import re as _re_pd
                        _pd_match = _re_pd.search(r'\[PHASE-DONE\]\s*(\S+)', desc)
                        if _pd_match:
                            _pd_task_id = _pd_match.group(1)
                            try:
                                _phase_file = _proj_path / ".ag-supervisor" / "phase_state.json"
                                if _phase_file.exists():
                                    _ph_data = json.loads(_phase_file.read_text(encoding="utf-8"))
                                    _cur_ph = next(
                                        (p for p in _ph_data.get("phases", [])
                                         if p.get("id") == _ph_data.get("current_phase", 1)),
                                        None
                                    )
                                    if _cur_ph:
                                        for _pt in _cur_ph.get("tasks", []):
                                            if _pt.get("id") == _pd_task_id and _pt.get("status") != "done":
                                                _pt["status"] = "done"
                                                _pt.setdefault("notes", []).append(
                                                    "Auto-marked done by proactive audit"
                                                )
                                                _phase_file.write_text(
                                                    json.dumps(_ph_data, indent=2, ensure_ascii=False),
                                                    encoding="utf-8",
                                                )
                                                logger.info(
                                                    "🔍  [Proactive] PHASE-DONE: auto-marked %s", _pd_task_id
                                                )
                                                break
                            except Exception:
                                pass
                        continue

                    # Fingerprint dedup — skip tasks already done in prior cycles
                    _pro_desc = f"[Proactive Fix] {desc}"
                    _fp = _desc_fp(_pro_desc)
                    if _fp in _done_fps:
                        logger.debug("🔍  [Proactive] Skipping (fingerprint match): %s", desc[:60])
                        continue
                    # Also check planner nodes
                    _existing_fps = {_desc_fp(n.description) for n in planner._nodes.values()
                                     if n.status in ('pending', 'running', 'complete', 'failed')}
                    if _fp in _existing_fps:
                        logger.debug("🔍  [Proactive] Skipping (planner match): %s", desc[:60])
                        continue

                    _pro_offset += 1
                    _tag = _extract_tag(desc)
                    full_id = f"t{_pro_offset}-{_tag}"
                    if full_id in existing_ids:
                        continue
                    injected = planner.inject_task(
                        task_id=full_id,
                        description=_pro_desc,
                        dependencies=[],
                    )
                    if injected:
                        tasks_created += 1
                        planner._offset = _pro_offset
                        _done_fps.add(_fp)
                        _done_descs.append(desc[:200])
                        logger.info("🔍  [Proactive] Injected: %s", full_id)

                # Persist fingerprints for future cycles
                if tasks_created:
                    try:
                        _fp_store_path.parent.mkdir(parents=True, exist_ok=True)
                        _fp_store_path.write_text(
                            json.dumps({
                                "fingerprints": list(_done_fps),
                                "descriptions": _done_descs,
                                "updated_at": _time.time(),
                            }, indent=2),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass

                if tasks_created:
                    if state:
                        state.record_activity(
                            "success",
                            f"Proactive audit queued {tasks_created} improvement tasks",
                        )

                # V67: Phase-to-DAG gap fill (proactive audit) — ensure every
                # pending phase task has a DAG node after proactive injection.
                try:
                    _phase_file_gf = _proj_path / ".ag-supervisor" / "phase_state.json"
                    if _phase_file_gf.exists() and planner:
                        _gf_data = json.loads(_phase_file_gf.read_text(encoding="utf-8"))
                        _gf_phases = _gf_data.get("phases", [])
                        _pro_gap_filled = 0
                        for _gf_ph in _gf_phases:
                            if _gf_ph.get("status") == "completed":
                                continue
                            for _gf_t in _gf_ph.get("tasks", []):
                                if _gf_t.get("status") == "done":
                                    continue
                                _gf_title = _gf_t.get("title", "").strip()
                                if not _gf_title:
                                    continue
                                _gf_norm = _gf_title.lower()
                                for _pfx in ('[func]', '[ui/ux]', '[perf]', '[ui]', '[data]',
                                             '[err]', '[a11y]', '[sec]', '[qa]'):
                                    _gf_norm = _gf_norm.replace(_pfx, '')
                                _gf_words = [w for w in _gf_norm.split() if len(w) > 2]
                                if not _gf_words:
                                    continue
                                _gf_covered = False
                                for _dn in planner._nodes.values():
                                    _dn_norm = _dn.description.lower()
                                    _mc = sum(1 for w in _gf_words if w in _dn_norm)
                                    if _mc / max(len(_gf_words), 1) >= 0.5:
                                        _gf_covered = True
                                        break
                                if not _gf_covered:
                                    _gf_ph_id = _gf_ph.get("id", "?")
                                    _gf_t_id = _gf_t.get("id", "")
                                    _gf_inj = planner.inject_task(
                                        task_id=f"phase-{_gf_t_id}" if _gf_t_id else f"phase-pro-gap-{_pro_gap_filled}",
                                        description=f"[Phase {_gf_ph_id}] {_gf_title}",
                                        dependencies=[],
                                    )
                                    if _gf_inj:
                                        _pro_gap_filled += 1
                        if _pro_gap_filled > 0:
                            logger.info(
                                "📋  [GapFill] Proactive: injected %d uncovered phase tasks.",
                                _pro_gap_filled,
                            )
                            tasks_created += _pro_gap_filled
                except Exception:
                    pass

    except Exception as exc:
        logger.warning("\U0001f50d  [Proactive] Audit error: %s", exc)

    duration = _time.time() - start
    logger.info("\U0001f50d  [Proactive] Complete: %.1fs, %d tasks", duration, tasks_created)
    return {"duration_s": duration, "tasks_created": tasks_created}


# ─────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Supervisor AI {config.SUPERVISOR_VERSION_LABEL} — Command Centre",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python -m supervisor --goal "Build a React dashboard" -p ./myproject\n'
            '  python -m supervisor --goal "Fix failing tests" --dry-run\n'
            '  python -m supervisor  (interactive mode)\n'
            '\n'
            'The V44 Command Centre UI is available at http://localhost:8420\n'
        ),
    )
    parser.add_argument("--goal", "-g", default=None, help="The goal to achieve.")
    parser.add_argument("--project-path", "-p", default=None, help="Path to the project folder.")
    parser.add_argument("--dry-run", action="store_true", help="Run without connecting.")
    parser.add_argument(
        "--log-level", default=config.LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    args = parser.parse_args()

    # ── Logging setup: console + file ──
    log_level = getattr(logging, args.log_level)
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    fmt = logging.Formatter(config.LOG_FORMAT, datefmt=config.LOG_DATE_FORMAT)

    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(log_level)
        console.setFormatter(fmt)
        root_logger.addHandler(console)

    # Rotate/append log file
    _log_mode = "w"
    try:
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > 0:
            log_age_s = time.time() - _LOG_FILE.stat().st_mtime
            if log_age_s < 120:
                _log_mode = "a"  # Auto-restart, preserve context
            else:
                bak_path = _LOG_FILE.with_suffix(".log.bak")
                import shutil
                shutil.copy2(str(_LOG_FILE), str(bak_path))
    except Exception:
        pass

    try:
        file_handler = logging.FileHandler(str(_LOG_FILE), encoding="utf-8", mode=_log_mode)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root_logger.addHandler(file_handler)
    except Exception as exc:
        logger.warning("Could not set up file logging: %s", exc)

    # ── Resolve goal + project path ──
    goal = args.goal
    project_path = args.project_path

    if not goal:
        saved = _load_session_state()
        if saved:
            saved_goal, saved_project = saved
            G = config.ANSI_GREEN
            Y = config.ANSI_YELLOW
            C = config.ANSI_CYAN
            B = config.ANSI_BOLD
            R = config.ANSI_RESET

            # V41: Check if SUPERVISOR_MANDATE.md was manually updated
            # since the session was saved — if so, reload the goal from it.
            _mandate_updated = False
            if saved_project:
                _mandate_path = Path(saved_project) / config.MANDATE_FILENAME
                if _mandate_path.exists():
                    _mandate_mtime = _mandate_path.stat().st_mtime
                    _session_ts = 0
                    try:
                        with open(_SESSION_STATE_PATH, "r", encoding="utf-8") as _f:
                            _session_ts = json.load(_f).get("timestamp", 0)
                    except Exception:
                        pass
                    if _mandate_mtime > _session_ts:
                        # Mandate was edited after session was saved — extract the goal
                        try:
                            _mandate_text = _mandate_path.read_text(encoding="utf-8")
                            # Extract the "YOUR MISSION" section
                            if "## YOUR MISSION" in _mandate_text:
                                _mission_start = _mandate_text.index("## YOUR MISSION") + len("## YOUR MISSION")
                                _mission_end = _mandate_text.index("---", _mission_start) if "---" in _mandate_text[_mission_start:] else len(_mandate_text)
                                _mission_end = _mission_start + _mandate_text[_mission_start:].index("---") if "---" in _mandate_text[_mission_start:] else len(_mandate_text)
                                _new_goal = _mandate_text[_mission_start:_mission_end].strip()
                                if _new_goal and _new_goal != saved_goal:
                                    logger.info("📋  [Goal] Mandate updated since last session — reloading goal.")
                                    saved_goal = _new_goal
                                    _mandate_updated = True
                                    _save_session_state(saved_goal, saved_project)
                        except Exception as _me:
                            logger.debug("📋  [Goal] Mandate reload failed: %s", _me)

            logger.info("🔄  SAVED SESSION FOUND: goal='%s', project='%s'", saved_goal, saved_project or 'N/A')
            print(f"\n  {B}{Y}╔═══════════════════════════════════════════════════════╗{R}")
            print(f"  {B}{Y}║  🔄 SAVED SESSION FOUND                              ║{R}")
            print(f"  {B}{Y}╚═══════════════════════════════════════════════════════╝{R}")
            if _mandate_updated:
                print(f"  {G}  ✨ Directive updated! New goal loaded from SUPERVISOR_MANDATE.md{R}")
            print(f"  {C}  Goal:    {saved_goal}{R}")
            print(f"  {C}  Project: {saved_project or 'N/A'}{R}")
            print()
            print(f"  {B}{G}  Press ENTER or wait 5s to CONTINUE this session.{R}")
            print(f"  {B}{Y}  Press E to EDIT the goal before continuing.{R}")
            print(f"  {B}{Y}  Press N to CANCEL and choose a different project.{R}")
            print()

            cancelled = False
            edit_goal = False
            try:
                if platform.system() == "Windows":
                    import msvcrt
                    for remaining in range(5, 0, -1):
                        print(f"\r  ⏳ Auto-continuing in {remaining}s … ", end="", flush=True)
                        deadline = time.time() + 1.0
                        while time.time() < deadline:
                            if msvcrt.kbhit():
                                key = msvcrt.getch()
                                if key in (b'n', b'N'):
                                    cancelled = True
                                    break
                                elif key in (b'e', b'E'):
                                    edit_goal = True
                                    break
                                elif key in (b'\r', b'\n'):
                                    remaining = 0
                                    break
                            time.sleep(0.05)
                        if cancelled or edit_goal or remaining == 0:
                            break
                    print()
                else:
                    import select
                    print("  ⏳ Auto-continuing in 5s …", flush=True)
                    ready, _, _ = select.select([sys.stdin], [], [], 5.0)
                    if ready:
                        user_input = sys.stdin.readline().strip().upper()
                        if user_input == "N":
                            cancelled = True
                        elif user_input == "E":
                            edit_goal = True
            except Exception:
                pass

            if cancelled:
                print(f"\n  {Y}⛔ Session cancelled. Opening interactive menu …{R}\n")
                _clear_session_state()
            elif edit_goal:
                print(f"\n  {C}✏️  Edit your goal (press Enter to keep current):{R}")
                new_goal = input(f"  > ").strip()
                if new_goal:
                    saved_goal = new_goal
                    _save_session_state(saved_goal, saved_project)
                    print(f"  {G}✅ Goal updated and saved.{R}")
                else:
                    print(f"  {G}✅ Keeping current goal.{R}")
                goal = saved_goal
                project_path = saved_project
            else:
                print(f"  {G}✅ Continuing saved session.{R}")
                logger.info("✅  Continuing saved session.")
                goal = saved_goal
                project_path = saved_project

    if not goal:
        goal, project_path = _interactive_goal()

    # ── Run the async loop ──
    asyncio.run(run(goal, project_path, args.dry_run))


if __name__ == "__main__":
    main()
