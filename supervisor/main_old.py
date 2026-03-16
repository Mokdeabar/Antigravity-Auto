"""
main.py — The Orchestrator (V44 Command Centre).

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


def _save_preview_port(project_path: str, host_port: int, container_port: int = 3000) -> None:
    """Save the active preview port mapping to disk for crash recovery."""
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


def _validate_worker_files(
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


def _revert_worker_files(project_path: str, files: list[str]) -> bool:
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


async def _update_dag_progress(planner, depth: int, running: list[str] | None = None, state=None):
    """Update the global DAG progress dict for UI consumption and broadcast."""
    global _dag_progress
    nodes_list = []
    for n in planner._nodes.values():
        nodes_list.append({
            "id": n.task_id,
            "desc": n.description,
            "status": n.status,
            "deps": n.dependencies,
            "priority": getattr(n, "priority", 0),
        })

    progress = planner.get_progress()
    _dag_progress = {
        "active": True,
        "depth": depth,
        "total": sum(progress.values()),
        "completed": progress.get("complete", 0),
        "running": running or [],
        "pending": progress.get("pending", 0),
        "failed": progress.get("failed", 0),
        "nodes": nodes_list,
    }
    # V40: Broadcast to UI immediately so progress is live
    if state:
        try:
            await state.broadcast_state()
        except Exception:
            pass


async def _compute_chunk_timeout(local_brain, description: str) -> int:
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
    completed_summary = planner.get_completed_summary()
    focused_prompt = node.description
    if completed_summary:
        focused_prompt = (
            f"{completed_summary}\n\n"
            f"NOW DO THIS NEXT STEP:\n{node.description}"
        )

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
    )

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

        # Sync files from host to sandbox (copy mode only, no-op for bind)
        await sandbox.sync_files_to_sandbox(project_path)

        # Check if dev server is already running
        from .tool_server import DevServerResult
        server = await tools.dev_server_check()
        if server.running:
            state.preview_running = True
            # Resolve actual host port
            host_port = await sandbox.resolve_host_port(
                sandbox.active_sandbox.preview_port
            )
            if host_port:
                state.preview_port = host_port
                _save_preview_port(project_path, host_port, sandbox.active_sandbox.preview_port)
            elif sandbox.active_sandbox.host_preview_port:
                state.preview_port = sandbox.active_sandbox.host_preview_port
            await state.broadcast_state()
            return

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
            logger.info("🖥️  [Auto-Preview] Dev server did not start (may not be buildable yet)")

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


async def _inject_error_hook(sandbox) -> None:
    """
    Inject the error capture hook script into all HTML files in /workspace.
    Uses sed to insert a <script> tag before </head>.
    """
    try:
        # Copy the hook script into the workspace
        hook_path = Path(__file__).parent / "console_error_hook.js"
        if not hook_path.exists():
            logger.debug("🖥️  [Error Capture] Hook script not found: %s", hook_path)
            return
        await sandbox.copy_file_in(str(hook_path), "/workspace/_supervisor_error_hook.js")

        # Inject script tag into all HTML files (idempotent — checks if already injected)
        inject_cmd = (
            "find /workspace -maxdepth 3 -name '*.html' -exec "
            "grep -L '_supervisor_error_hook' {} \\; | "
            "xargs -r sed -i "
            "'s|</head>|<script src=\"/_supervisor_error_hook.js\"></script>\\n</head>|'"
        )
        await sandbox.exec_command(inject_cmd, timeout=10)
        logger.info("🖥️  [Error Capture] Hook injected into HTML files")
    except Exception as exc:
        logger.debug("🖥️  [Error Capture] Failed to inject hook: %s", exc)


async def _capture_console_errors(sandbox) -> list[str]:
    """
    Read collected console errors from the sandbox. Returns a list of
    formatted error strings suitable for _diagnose_and_retry().
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
            return []

        # Clear after reading
        await sandbox.exec_command("echo '[]' > /tmp/console_errors.json", timeout=3)

        # Format errors for auto-fix
        formatted = []
        for err in errors_raw:
            msg = err.get("message", "")
            err_type = err.get("type", "unknown")
            source = err.get("source", "")
            line = err.get("line", 0)
            stack = err.get("stack", "")

            parts = [f"[{err_type}] {msg}"]
            if source:
                parts.append(f"  at {source}:{line}")
            if stack:
                parts.append(f"  {stack[:200]}")
            formatted.append("\n".join(parts))

        if formatted:
            logger.warning(
                "🖥️  [Error Capture] Captured %d console error(s) from preview",
                len(formatted),
            )
        return formatted

    except Exception as exc:
        logger.debug("🖥️  [Error Capture] Failed to read errors: %s", exc)
        return []


async def _diagnose_and_retry(
    task_description: str,
    failure_errors: list[str],
    executor,
    local_brain,
    session_mem,
    timeout: int = 300,
    task_id: str = "",
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

    # ── Step 1: Diagnose ──
    error_text = "\n".join(str(e)[:300] for e in failure_errors[:5])
    analysis = None
    logger.info(
        "🔧  %s[Auto-Fix→Ollama] Diagnosing errors for: %.200s…",
        label, task_description,
    )
    try:
        analysis = await local_brain.analyze_errors(
            [{"error": e[:500]} for e in failure_errors[:5]],
            context=task_description[:500],
        )
    except Exception:
        pass  # Ollama may be offline — proceed with raw errors

    if analysis:
        logger.info(
            "🔧  %s[Auto-Fix←Ollama] Diagnosis (%d chars): %s",
            label, len(analysis), analysis[:500],
        )
    else:
        logger.info("🔧  %s[Auto-Fix] Retrying with error context (no diagnosis available)", label)

    # ── Step 2: Build enriched retry prompt ──
    retry_prompt = (
        f"PREVIOUS ATTEMPT FAILED. Fix the issues and complete the task.\n\n"
        f"ORIGINAL TASK:\n{task_description}\n\n"
        f"ERRORS FROM PREVIOUS ATTEMPT:\n{error_text}\n\n"
    )
    if analysis:
        retry_prompt += f"ROOT CAUSE ANALYSIS:\n{analysis}\n\n"
    retry_prompt += (
        "INSTRUCTIONS:\n"
        "1. Read the errors carefully\n"
        "2. Fix the root cause — do NOT just suppress the error\n"
        "3. Complete the original task successfully\n"
    )

    # ── Step 3: Retry once ──
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
        session_mem.record_event("auto_fix_success", f"{label}{task_description[:80]}")
        return True, retry_result
    else:
        logger.warning(
            "🔧  %s[Auto-Fix] Retry also failed: %s",
            label, retry_result.errors[:2],
        )
        session_mem.record_event("auto_fix_failed", f"{label}{retry_result.errors[:2]}")
        return False, retry_result


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

    C = config.ANSI_CYAN
    G = config.ANSI_GREEN
    Y = config.ANSI_YELLOW
    R = config.ANSI_RESET
    B = config.ANSI_BOLD

    # Limit audit scope to avoid oversized prompts
    unique_files = list(dict.fromkeys(files_changed))[:30]
    if not unique_files:
        return None

    logger.info("🔍  [Audit] Starting post-completion audit on %d files …", len(unique_files))
    print(f"\n{indent}{B}{C}🔍 AUDIT: Scanning {len(unique_files)} changed files for remaining work …{R}")

    if state:
        state.record_activity("task", f"Audit: scanning {len(unique_files)} files for remaining tasks")
        try:
            await state.broadcast_state()
        except Exception:
            pass

    start = _time.time()
    file_list = "\n".join(f"  - {f}" for f in unique_files)

    # V40 FIX: Grab the original goal to audit against
    _epic_text_str = planner._epic_text if hasattr(planner, '_epic_text') else "N/A"
    
    _combined_goal_text = f"CLI Goal: {goal}\nEpic Detail: {_epic_text_str}"

    # ── Task Creation Scan — find ALL remaining work and create DAG tasks ──
    # V41: Audits ONLY create tasks, never fix directly. Uses Gemini CLI
    # (1M+ context) for comprehensive scanning.
    tasks_created = 0
    _injected_ids = []
    try:
        from pathlib import Path
        _proj_path = Path(effective_project) if effective_project else Path(".")

        # ── Gather full project state for comprehensive audit ──

        # 1. PROJECT_STATE.md (if exists)
        _project_state_md = ""
        _ps_path = _proj_path / "PROJECT_STATE.md"
        if _ps_path.exists():
            try:
                _project_state_md = _ps_path.read_text(encoding="utf-8")[:5000]
            except Exception:
                pass

        # 2. Full file tree (all source files)
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

        # 3. Completed DAG tasks (what was already done)
        _completed_tasks = ""
        if hasattr(planner, '_nodes') and planner._nodes:
            _done = [
                f"  - [{n.status.upper()}] {n.task_id}: {n.description[:120]}"
                for n in planner._nodes.values()
            ]
            _completed_tasks = "\n".join(_done)

        # 3.5 DAG run history (all previous runs from dag_history.jsonl)
        _dag_history_text = "(no prior DAG runs)"
        try:
            _hist_path = _proj_path / "dag_history.jsonl"
            if not _hist_path.exists():
                _hist_path = _proj_path / ".supervisor" / "dag_history.jsonl"
            if _hist_path.exists():
                _lines = _hist_path.read_text(encoding="utf-8").strip().split("\n")
                _hist_entries = []
                for _line in _lines[-3:]:  # Last 3 runs
                    try:
                        _entry = json.loads(_line)
                        _summary = _entry.get("summary", {})
                        _nodes = _entry.get("nodes", {})
                        _task_descs = [
                            f"    [{v['status'].upper()}] {v['task_id']}: {v['description'][:80]}"
                            for v in _nodes.values()
                        ]
                        _hist_entries.append(
                            f"  Run ({_summary.get('total', '?')} tasks, "
                            f"{_summary.get('complete', '?')} complete, "
                            f"{_summary.get('failed', '?')} failed):\n"
                            + "\n".join(_task_descs)
                        )
                    except Exception:
                        continue
                if _hist_entries:
                    _dag_history_text = "\n\n".join(_hist_entries)
        except Exception:
            pass

        # 4. AST-sliced context of changed files (V41: precision > volume)
        # Instead of dumping 20 files × 8000 chars = 160K of raw source,
        # extract only the function/class bodies matching the goal keywords.
        _file_contents = ""
        try:
            from .workspace_indexer import WorkspaceMap
            _wm = WorkspaceMap(str(_proj_path))
            _wm.scan_workspace()
            _file_contents = _wm.extract_relevant_bodies(
                _combined_goal_text, unique_files, max_total_chars=50000
            )
        except Exception as _ast_exc:
            logger.debug("🔍  [Audit] AST slicing failed, falling back to raw: %s", _ast_exc)
            # Fallback: head-truncated raw source (old behavior)
            for f in unique_files[:20]:
                _full_path = _proj_path / f
                if _full_path.exists() and _full_path.is_file():
                    try:
                        _txt = _full_path.read_text(encoding="utf-8")[:8000]
                        _file_contents += f"\n--- {f} ---\n{_txt}\n"
                    except Exception:
                        pass

        scan_prompt = (
            "You are a SENIOR CODE AUDITOR performing a comprehensive post-completion audit.\n"
            "Your job is to compare the ORIGINAL GOAL against the ACTUAL implementation and find "
            "EVERY gap, missing feature, bug, and improvement needed.\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "ORIGINAL GOAL (what was requested):\n"
            "═══════════════════════════════════════════════════════════\n"
            f"{goal}\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "PROJECT STATE (current status):\n"
            "═══════════════════════════════════════════════════════════\n"
            f"{_project_state_md or '(no PROJECT_STATE.md found)'}\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "FILE TREE (all files in project):\n"
            "═══════════════════════════════════════════════════════════\n"
            f"{_file_tree or '(no files found)'}\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "COMPLETED DAG TASKS (what was already done):\n"
            "═══════════════════════════════════════════════════════════\n"
            f"{_completed_tasks or '(no tasks completed)'}\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "DAG RUN HISTORY (all previous runs):\n"
            "═══════════════════════════════════════════════════════════\n"
            f"{_dag_history_text}\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "CHANGED FILE CONTENTS (full source code):\n"
            "═══════════════════════════════════════════════════════════\n"
            f"{_file_contents}\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "YOUR AUDIT MANDATE — VERIFY, DON'T ASSUME:\n"
            "═══════════════════════════════════════════════════════════\n"
            "⚠️  DO NOT assume tasks were done correctly just because they are\n"
            "marked 'complete'. A task can be marked complete but the actual\n"
            "implementation may be:\n"
            "  - A STUB (empty function body, placeholder code, TODO comments)\n"
            "  - INCOMPLETE (partially implemented, missing edge cases)\n"
            "  - INCORRECT (wrong logic, wrong file, wrong approach)\n"
            "  - HARDCODED (values that should be dynamic)\n\n"
            "YOUR PROCESS:\n"
            "1. Read EVERY line of the original goal carefully\n"
            "2. For EACH requirement in the goal, find the ACTUAL code that implements it\n"
            "3. Read the code and verify it ACTUALLY works — don't just check if a file exists\n"
            "4. Check function bodies are real implementations, not stubs or placeholders\n"
            "5. Verify cross-file references (imports, function calls) are correct\n"
            "6. Check that the UI/UX matches what was requested (not just 'a button exists')\n"
            "7. Verify game mechanics, physics, collision, etc. are actual implementations\n\n"
            "CREATE DETAILED TASKS FOR:\n"
            "  - Features that are MISSING entirely\n"
            "  - Features that exist as STUBS or PLACEHOLDERS\n"
            "  - Features that are BROKEN or have obvious bugs\n"
            "  - Quality issues that make the implementation BELOW what was requested\n"
            "  - Missing integration between systems that should work together\n"
            "  - Missing imports, broken references, undefined variables\n"
            "  - Dead code or unused imports that should be removed\n"
            "  - Missing error handling or edge cases\n\n"
            "Each task description must be DETAILED and SPECIFIC:\n"
            "  - Name the EXACT files to create or modify\n"
            "  - Describe WHAT functions/classes to implement\n"
            "  - Explain WHY it's needed (what's currently wrong or missing)\n"
            "  - Describe the EXPECTED BEHAVIOR after the fix\n\n"
            "Respond with a JSON array of tasks:\n"
            '[{"id": "fix-<short-name>", "description": "<detailed: what to build/fix, which files, why, what the code currently does wrong>"}]\n'
            "If EVERY requirement is PERFECTLY and FULLY implemented: respond []\n"
            "Create ALL tasks needed — do not limit the count. Prioritize: stubs/missing > broken > quality.\n"
            "IMPORTANT: Respond with ONLY the JSON array. No markdown, no explanation."
        )
        logger.info(
            "🔍  [Audit→Gemini] Deep scan prompt (%d chars)",
            len(scan_prompt),
        )
        if state:
            state.record_activity("llm_prompt", "Audit: deep scan via Gemini", scan_prompt[:2000])

        # Use Gemini CLI for the deep scan (large context window)
        scan_result = None
        try:
            from .gemini_advisor import ask_gemini
            raw_scan = await ask_gemini(
                scan_prompt,
                timeout=180,
            )
            if raw_scan:
                logger.info(
                    "🔍  [Audit←Gemini] Response (%d chars): %.500s…",
                    len(raw_scan), raw_scan,
                )
                # Parse JSON from response
                import re as _re
                cleaned = _re.sub(r"```json?\s*", "", raw_scan)
                cleaned = _re.sub(r"```\s*", "", cleaned).strip()
                try:
                    scan_result = json.loads(cleaned)
                except json.JSONDecodeError:
                    # Try to extract JSON array from mixed text
                    _match = _re.search(r'\[.*\]', cleaned, _re.DOTALL)
                    if _match:
                        try:
                            scan_result = json.loads(_match.group())
                        except json.JSONDecodeError:
                            logger.warning("🔍  [Audit] Could not parse JSON from Gemini response")
        except Exception as gemini_exc:
            logger.warning("🔍  [Audit] Gemini deep scan failed: %s", gemini_exc)

        # Fallback: try Ollama if Gemini failed
        if scan_result is None and await local_brain.is_available():
            logger.info("🔍  [Audit] Falling back to Ollama for deep scan …")
            # Drastically reduce prompt to fit 8K context
            _short_prompt = (
                f"Review changed files against goal: {goal[:200]}\n"
                f"Files: {', '.join(unique_files[:5])}\n"
                "List missing features or bugs as JSON tasks:\n"
                '[{"id": "fix-name", "description": "what to fix"}]\n'
                "If nothing missing, respond: []"
            )
            scan_result = await local_brain.ask_json(_short_prompt)

        if state:
            state.record_activity("llm_response", "Audit: scan result", str(scan_result)[:2000])

        if isinstance(scan_result, list) and scan_result:
            # Deduplicate against existing planner node IDs only
            existing_ids = planner.get_all_task_ids()

            for issue in scan_result:
                if not isinstance(issue, dict):
                    continue
                task_id = issue.get("id", "")
                desc = issue.get("description", "")
                if not task_id or not desc:
                    continue

                full_id = f"audit-{task_id}"
                if full_id in existing_ids:
                    continue

                injected = planner.inject_task(
                    task_id=full_id,
                    description=f"[Audit Fix] {desc}",
                    dependencies=[],
                )
                if injected:
                    tasks_created += 1
                    _injected_ids.append(full_id)
                    logger.info(
                        "🔍  [Audit] Injected DAG task %s: %s",
                        full_id, desc[:60],
                    )
                    print(f"{indent}  {Y}📋 Audit task queued: {desc[:70]}{R}")
                else:
                    logger.warning("🔍  [Audit] Failed to inject %s (duplicate or cycle)", full_id)

            if tasks_created > 0:
                print(f"{indent}  {G}✓ Audit queued {tasks_created} follow-on tasks for DAG execution{R}")
                if state:
                    state.record_activity(
                        "task",
                        f"Audit: queued {tasks_created} follow-on fix tasks into DAG",
                    )
                    # Broadcast immediately so Graph tab shows new audit nodes
                    _dag_progress["active"] = True
                    await _update_dag_progress(planner, 0, state=state)
                session_mem.record_event(
                    "audit_tasks_injected",
                    f"{tasks_created} quality fix tasks queued by post-DAG audit",
                )
        else:
            logger.info("🔍  [Audit] Deep scan found no additional issues.")
            if state:
                state.record_activity("success", "Audit: no additional tasks needed — code matches goal")

    except Exception as exc:
        logger.warning("🔍  [Audit] Deep scan error: %s", exc)
        if state:
            state.record_activity("warning", f"Audit deep scan error: {exc}")

    duration = _time.time() - start
    logger.info(
        "🔍  [Audit] Complete: %.1fs, %d tasks created.",
        duration, tasks_created,
    )
    print(f"{indent}{G}✓ Audit complete ({duration:.1f}s, {tasks_created} tasks created){R}\n")

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
    agg_result = TaskResult(prompt_used=goal[:200])
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

    # ── Initialize planner ──
    # V41: Reuse existing planner from state if available (e.g. on resume_dag re-entry).
    # Previously a new planner was always created, discarding in-memory completed state.
    planner = getattr(state, 'planner', None) if state else None
    if planner is None or not planner._nodes:
        planner = TemporalPlanner.from_brain(
            local_brain if await local_brain.is_available() else None,
            effective_project,
        )

    # ── Try to resume from persisted state (crash recovery) ──
    # Only load from disk if the planner has no in-memory nodes.
    if depth == 0 and not planner._nodes and planner.load_state():
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

    # ── Decompose if no existing nodes ──
    if not planner._nodes:
        logger.info("📋  [Planner] Decomposing at depth %d …", depth)
        if state:
            state.record_activity("task", f"DAG decomposition starting (depth {depth})", goal[:80])
        ok, msg = await planner.decompose_epic(goal)
        if not ok:
            logger.warning("📋  [Planner] Decomposition failed: %s. Executing directly.", msg)
            # Fall back to direct execution
            return await executor.execute_task(
                goal, timeout=config.GEMINI_TIMEOUT_SECONDS,
            )

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

    # V41: Per-file locking — workers run in parallel, only serialize on shared files
    from .workspace_transaction import get_workspace_lock
    _ws_lock = get_workspace_lock()

    class _nullctx:
        """No-op async context manager for when there are no files to lock."""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    executed_count = 0
    sem = _asyncio.Semaphore(max_workers)
    active_tasks: dict[str, _asyncio.Task] = {}  # task_id → asyncio.Task
    nodes_since_lint = 0

    # V41: Create git baseline so broken worker output can be reverted
    _has_checkpoint = _git_checkpoint(effective_project)

    async def _pool_worker(node):
        """Execute a single node within the semaphore-limited pool."""
        nonlocal executed_count, total_duration, nodes_since_lint
        async with sem:
            # V40 FIX: Abort immediately if safe stop was requested
            # while this worker was waiting for the semaphore.
            if state and getattr(state, 'stop_requested', False):
                node.status = "pending"  # Return to pending so it's not lost
                logger.info("🛑  [Pool] Worker %s aborting — safe stop requested.", node.task_id)
                return

            node.status = "running"
            planner._save_state()  # V41: Persist running status for crash recovery
            await _update_dag_progress(
                planner, depth,
                running=[tid for tid, t in active_tasks.items() if not t.done()],
                state=state,
            )

            progress = planner.get_progress()
            done = progress.get("complete", 0)
            logger.info(
                "📋  [Pool] Executing %s [%d/%d]: %s",
                node.task_id, done + 1, total_tasks, node.description[:80],
            )
            print(
                f"{indent}{C}▸ [{done + 1}/{total_tasks}] {node.task_id}: "
                f"{node.description[:60]}…{R}"
            )
            # V40: Timeline milestone — node started
            if state:
                state.record_activity(
                    "task",
                    f"Worker started: {node.task_id} [{done + 1}/{total_tasks}]",
                    node.description[:80],
                )

            # V41: True parallel execution — workers run Gemini CLI concurrently.
            # Docker handles concurrent docker cp/exec to different destination paths.
            # Per-file locking (below) protects shared state mutation only.
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
            )

            # V41: Per-file lock on shared state mutation — two workers touching
            # the same file serialize only here, not during execution.
            _changed = chunk_result.files_changed or []
            async with _ws_lock.acquire_files(_changed) if _changed else _nullctx():
                executed_count += 1
                total_duration += chunk_result.duration_s
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
                planner.mark_complete(node.task_id)
                # V41 FIX: Broadcast DAG progress IMMEDIATELY so Graph tab updates live.
                # Previously the UI only learned about completions when the scheduling
                # loop polled — for audit re-kick tasks there is NO scheduling loop.
                await _update_dag_progress(planner, depth, state=state)
                if state and chunk_result.files_changed:
                    for f in chunk_result.files_changed:
                        state.record_change(f, "modified", node.task_id)
                if state:
                    state.record_activity(
                        "success",
                        f"Chunk {node.task_id} done ({chunk_result.duration_s:.1f}s, "
                        f"{len(chunk_result.files_changed)} files)",
                    )
                session_mem.record_event(
                    "chunk_completed",
                    f"{node.task_id}: {node.description[:80]}",
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

                # V41: Auto-redeploy after every file-changing task so the
                # preview always reflects the latest code.
                if chunk_result.files_changed and sandbox:
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
                        state.record_activity("warning", f"{node.task_id} failed — auto-fix skipped (safe stop)")
                    return

                # V41: On rate limit, IMMEDIATELY RETRY with the failover model.
                # The failover chain already switched the active model via
                # report_failure(), so re-executing goes to the next model.
                if getattr(chunk_result, '_rate_limited', False):
                    try:
                        from .retry_policy import get_failover_chain
                        _fc = get_failover_chain()
                        _next = _fc.get_active_model()
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
                    except Exception as _foe:
                        logger.debug("⚡  [Pool] Failover retry error: %s", _foe)
                    # If no model available or failover failed, mark failed
                    planner.mark_failed(node.task_id, "rate_limited")
                    await _update_dag_progress(planner, depth, state=state)  # V41: live UI
                    if state:
                        state.record_activity("warning", f"{node.task_id} rate-limited — all models exhausted")
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
                        str(chunk_result.errors[:1])[:120],
                    )

                # V41: Auto-fix runs without global lock — parallel with other workers
                fix_ok, fix_result = await _diagnose_and_retry(
                    task_description=node.description,
                    failure_errors=chunk_result.errors,
                    executor=executor,
                    local_brain=local_brain,
                    session_mem=session_mem,
                    task_id=node.task_id,
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
                            str(chunk_result.errors[:1])[:120],
                        )

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
                                state.record_activity("task", f"Replanned {node.task_id}: {replan_msg[:60]}")
                    except Exception as exc:
                        logger.debug("📋  [Pool] Replan skipped: %s", exc)

            nodes_since_lint += 1
            await _update_dag_progress(planner, depth, state=state)

    # ── Main scheduling loop — feed workers as nodes become unblocked ──
    while True:
        # V40: Safe stop check — drain workers, block new launches
        if state and getattr(state, 'stop_requested', False):
            running_tasks = {tid: t for tid, t in active_tasks.items() if not t.done()}
            n_running = len(running_tasks)
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

            if state:
                state.record_activity("system", "Safe stop: all workers drained — exiting cleanly")
                await state.broadcast_state()
            logger.info("🛑  [Pool] All workers drained. Exiting scheduling loop.")
            print(f"{indent}{config.ANSI_GREEN}✅ All workers drained — safe stop complete.{config.ANSI_RESET}")
            break

        # Find all unblocked nodes not already running
        unblocked = planner.get_parallel_batch(max_workers=max_workers * 2)
        new_nodes = [n for n in unblocked if n.task_id not in active_tasks]

        # V40: Block new launches if stop is pending (belt-and-suspenders)
        if state and getattr(state, 'stop_requested', False):
            new_nodes = []

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
                        state.record_activity(
                            "task",
                            f"Re-queuing {failed_node.task_id} (retry {failed_node.retry_count}/{failed_node.max_retries})",
                        )
                await _update_dag_progress(planner, depth, state=state)
                continue  # Re-check for newly unblocked nodes
            break  # No retries left — DAG is truly done

        # Wait for any one task to complete, then loop to fill idle slots
        if running_tasks:
            done_set, _ = await _asyncio.wait(
                running_tasks.values(),
                return_when=_asyncio.FIRST_COMPLETED,
            )
            # Process exceptions from completed tasks
            for t in done_set:
                if t.exception():
                    logger.error("📋  [Pool] Worker raised: %s", t.exception())
        else:
            # Edge case: waiting for new nodes to become unblocked
            await _asyncio.sleep(1)

        # Refresh max_workers from budget tracker (boost may have activated/expired)
        try:
            max_workers = get_daily_budget().get_effective_workers()
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

    # Wait for any remaining active tasks to drain
    remaining = [t for t in active_tasks.values() if not t.done()]
    if remaining:
        await _asyncio.gather(*remaining, return_exceptions=True)

    await _update_dag_progress(planner, depth, state=state)

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
            while True:
                # V42: Check if audit loop is still enabled
                if state and not getattr(state, 'audit_loop_enabled', True):
                    logger.info("⏸  [Audit] Audit loop stopped by user.")
                    if state:
                        state.record_activity("system", "Audit loop stopped by user")
                    break

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
                    # Brief pause between cycles to avoid API hammering
                    await _asyncio.sleep(5)

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
                )
                if not audit_result:
                    break

                total_duration += audit_result.get("duration_s", 0)

                # V41: If audit injected follow-on DAG tasks, re-kick the worker pool
                # so they execute through the standard DAG pipeline (visible in Graph
                # tab, tracked, retried, and resumable across sessions).
                _audit_tasks = audit_result.get("tasks_created", 0)
                if _audit_tasks == 0:
                    logger.info("✅  [Audit] Cycle %d found no issues — audit loop complete.", _audit_cycle)
                    print(f"{indent}{G}✅ Audit cycle {_audit_cycle}: no issues found — done{R}")
                    if state:
                        state.record_activity("success", f"Audit cycle {_audit_cycle}: no issues found")
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
                for _aid in audit_result.get("injected_ids", []):
                    if _aid in planner._nodes:
                        _a_node = planner._nodes[_aid]
                        if _a_node.status == "complete" and hasattr(_a_node, 'result'):
                            executed_count += 1

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
            planner.clear_state()
        # Always keep nodes visible — just mark DAG as no longer running
        _dag_progress["active"] = False

    return agg_result


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
    print(f"{B}{G}  🌐 SUPERVISOR AI V44 — COMMAND CENTRE{R}")
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
    logger.info("🚀  Supervisor AI V44 Command Centre starting")
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

    try:
        # ── Docker prerequisite check ──
        # ── Docker prerequisite check + auto-recovery ──
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

        # ── Start the V44 Command Centre API server (only if not already running) ──
        if existing_state is None:
            api_task = asyncio.create_task(start_api_server(state))
        state.status = "initializing"

        # ── Create the sandbox container ──
        state.project_path = effective_project

        # Mount mode: copy for isolation (default)
        # The user can override via SANDBOX_MOUNT_MODE env var
        mount_mode = os.getenv("SANDBOX_MOUNT_MODE", "copy")
        if _lockfile_exists(effective_project):
            logger.info("🔐  Lockfile found — session continuing.")
        else:
            _create_lockfile(effective_project)

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
        logger.info("🔧  [Boot] Tool server and executor ready.")

        # ── Check Ollama availability ──
        # V41: Retry loop — Ollama's Windows service may still be starting
        # when the supervisor boots. Give it up to 6 seconds (3 × 2s) to come online.
        logger.info("🧠  [Boot] Checking Ollama availability …")
        ollama_ok = False
        for _ollama_attempt in range(3):
            ollama_ok = await local_brain.is_available()
            if ollama_ok:
                break
            if _ollama_attempt < 2:
                logger.info("🧠  [Boot] Ollama not responding (attempt %d/3) — retrying in 2s …", _ollama_attempt + 1)
                await asyncio.sleep(2)
                # Reset cache so next is_available() actually pings again
                local_brain._available = None
                local_brain._available_checked_at = 0.0

        state.ollama_online = ollama_ok
        if ollama_ok:
            # Pre-warm model into GPU/RAM in the background (non-blocking)
            asyncio.create_task(local_brain.warm_up())
            print(f"  {G}🧠 Ollama local brain: ONLINE (model: {local_brain.model}){R}")
        else:
            print(f"  {config.ANSI_YELLOW}🧠 Ollama local brain: OFFLINE (Gemini-only mode){R}")

        # ── Install project dependencies ──
        print(f"  {C}📥 Installing dependencies …{R}")
        dep_result = await executor.install_dependencies()
        if dep_result.success:
            print(f"  {G}✅ Dependencies installed.{R}")
        else:
            logger.warning("📥  Dependency install had issues: %s", dep_result.errors[:2])

        # ── Auto-preview: check if project is buildable at boot ──
        logger.info("🖥️  [Boot] Checking for buildable preview …")
        asyncio.create_task(_auto_preview_check(
            sandbox, executor, tools, state, str(effective_project)
        ))

        # ── Initialize session memory + council ──
        from .session_memory import SessionMemory
        session_mem = SessionMemory(project_path)
        session_mem.set_goal(goal)

        # ── Initialize scheduler ──
        from . import retry_policy
        from .scheduler import create_default_scheduler
        retry_policy.init()
        logger.info("⏰  [Boot] Initializing scheduler + background jobs …")
        scheduler = create_default_scheduler()
        logger.info("🔄  V12 systems initialized: RetryPolicy + CronScheduler")

        # V40: Set active model from failover chain
        from .retry_policy import get_failover_chain
        _chain = get_failover_chain()
        state.active_model = _chain.get_active_model() or config.GEMINI_FALLBACK_MODEL

        # ── First injection: execute the main goal ──
        state.status = "executing"
        print(f"\n  {B}{C}💉 EXECUTING GOAL VIA HOST INTELLIGENCE{R}")
        logger.info("💉  Executing goal: %s", goal)

        # V42 FIX: Check for saved DAG state BEFORE creating the boot planner.
        # The boot planner's inject_task + mark_complete both call _save_state(),
        # which OVERWRITES the previous session's epic_state.json (containing
        # multiple pending tasks) with just the single "goal-init" node.
        from .temporal_planner import TemporalPlanner, TaskNode
        _saved_state_path = Path(effective_project) / ".ag-supervisor" / "epic_state.json"
        _has_resumable_dag = False
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

        if _has_resumable_dag:
            # Skip boot planner entirely — go straight to DAG resume
            state.status = "executing"
            state.record_activity("system", f"Resuming saved DAG for: {goal[:80]}")
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
        global _dag_progress
        _boot_planner = TemporalPlanner.from_brain(
            local_brain if await local_brain.is_available() else None,
            str(effective_project),
        )
        state.planner = _boot_planner

        # Genesis node: represents the goal itself
        _genesis = _boot_planner.inject_task(
            task_id="goal-init",
            description=f"[Goal] {goal[:200]}",
            dependencies=[],
            priority=0,
        )
        if _genesis:
            _genesis.status = "running"

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
                description=f"[Custom Instruction] {_pi_obj.text[:200]}",
                dependencies=["goal-init"],  # Execute after goal decomposition
                priority=1,
            )
            if _pi_node:
                logger.info("📋  [Boot] Queued pre-flight instruction as DAG node: %s", _pi_obj.text[:60])
            # Re-push so the monitoring loop picks it up for execution
            await state.queue.push(_pi_obj.text, source=_pi_obj.source)

        # Broadcast initial DAG state so Graph tab lights up
        _dag_progress["active"] = True
        await _update_dag_progress(_boot_planner, 0, state=state)
        state.record_activity("system", f"Goal received: {goal[:80]}")
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

        # Mark genesis node as complete (goal has been received and routed)
        if _genesis:
            _boot_planner.mark_complete("goal-init")
            await _update_dag_progress(_boot_planner, 0, state=state)

        # V42 FIX: Only clear the boot planner's IN-MEMORY state.
        # Do NOT call clear_state() — it deletes epic_state.json from disk,
        # destroying the previous session's persisted DAG before
        # _execute_dag_recursive gets a chance to load it via load_state().
        state.planner = None  # Prevent reuse of boot planner in DAG execution
        _boot_planner._nodes = {}
        _boot_planner._replan_count = 0

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
            )
        else:
            # ── Simple task: execute directly ──
            logger.info("📋  [Planner] Simple task — executing directly.")
            state.record_activity("task", f"Executing goal directly: {goal[:80]}")
            logger.info(
                "💉  [Goal→Gemini] Simple task prompt (%d chars): %s",
                len(goal), goal,
            )
            state.record_activity("llm_prompt", "Gemini: simple goal execution", goal[:2000])
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
        print(f"\n  {G}✅ Entering V44 monitoring loop …{R}\n")
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
            await asyncio.sleep(_idle_sleep)
            loop_count += 1
            state.loop_count = loop_count

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
                    "dag_nodes": _dag_progress.get("nodes", []),
                    "files_changed": state.files_changed[:50],
                    "tasks_completed": state.tasks_completed,
                    "error_count": state.error_count,
                }
                checkpoint_path = checkpoint_dir / "checkpoint.json"
                checkpoint_path.write_text(
                    _json.dumps(checkpoint, indent=2), encoding="utf-8"
                )
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
                    if len(instr_text) > getattr(config, "PROMPT_SIZE_MAX_CHARS", 15000):
                        instr_text = instr_text[:15000]
                        logger.warning("📬  Instruction truncated to %d chars.", 15000)

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
                        await asyncio.sleep(max(5.0, _wait))
                        break

                    logger.info("📬  User instruction: %s", instr_text[:100])
                    state.last_action = f"user_instruction: {instr_text[:80]}"

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
                        _instr_counter += 1
                        _task_id = f"user-instr-{_instr_counter}"
                        injected = _active_planner.inject_task(
                            task_id=_task_id,
                            description=f"[User Instruction] {instr_text}",
                            dependencies=[],  # No deps — run ASAP
                            priority=1,       # V41: Execute before regular tasks
                        )
                        if injected:
                            # V41: Broadcast DAG progress immediately so Graph tab updates
                            _pool_depth = _dag_progress.get("depth", 0)
                            await _update_dag_progress(
                                _active_planner, _pool_depth, state=state,
                            )
                            state.record_activity(
                                "instruction",
                                f"Injected as DAG task {_task_id}: {instr_text[:60]}",
                            )
                            await state.broadcast_state()
                            session_mem.record_event(
                                "user_instruction_injected",
                                f"{_task_id}: {instr_text[:80]}",
                            )
                            logger.info(
                                "📬  Instruction injected as DAG task %s — will execute in worker pool.",
                                _task_id,
                            )
                        else:
                            # inject_task failed (duplicate?) — fall through to direct execution
                            logger.warning("📬  DAG injection failed for %s — executing directly.", _task_id)
                            state.record_activity("warning", f"DAG injection failed, executing directly: {instr_text[:60]}")
                            # Continue to Path B below
                            _active_planner = None  # Force direct execution
                            _pool_running = False

                    # ── Path B: Direct execution (no active DAG, pool not running, or injection failed) ──
                    if not (_active_planner and _active_planner.has_active_dag() and _pool_running):
                        state.status = "executing"
                        state.record_activity("instruction", f"Executing user instruction: {instr_text[:80]}")

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
                            state.record_activity("llm_prompt", "Gemini: user instruction", instr_text[:2000])
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
                            state.record_activity("error", f"Instruction failed: {instr_text[:60]}", str(instr_result.errors[:2]))

                            # V41: Mark DAG node as failed so Graph tab shows ✕
                            if _vis_node:
                                _vis_planner.mark_failed(_vis_task_id, str(instr_result.errors[:1])[:200])
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
                                state.record_activity("success", f"Instruction auto-fixed ({fix_result.duration_s:.1f}s): {instr_text[:60]}")
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
                                        _err_log = f"\n- ❌ **Failed Instruction:** {instr_text[:100]}... \n  - Error: {instr_result.errors[:1]}\n"
                                        if "## Current Blockers" in _txt:
                                            _txt = _txt.replace("## Current Blockers", f"## Current Blockers\n{_err_log}")
                                        else:
                                            _txt += f"\n\n## Current Blockers\n{_err_log}"
                                        _ps.write_text(_txt, encoding="utf-8")
                                except Exception:
                                    pass
                        else:
                            state.record_activity("success", f"Instruction completed ({instr_result.duration_s:.1f}s): {instr_text[:60]}")
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
                    _wait_secs = min(60, _failover.get_soonest_cooldown_remaining())
                    logger.warning(
                        "⚡  ALL models on cooldown — sleeping %.0fs", _wait_secs,
                    )
                    await asyncio.sleep(max(5.0, _wait_secs))
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
                state.record_activity("system", f"Decision: {action}", reason[:200])

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
                    state.record_activity("llm_prompt", "Gemini: fix diagnostic errors", error_prompt[:2000])
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
                                goal[:3000], timeout=config.GEMINI_TIMEOUT_SECONDS,
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
                        await asyncio.sleep(_backoff_sleep)
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

                    logger.info("📋  Executing additional task: %s", reason[:80])
                    state.record_activity("task", f"Executing: {reason[:80]}")
                    # The local brain identified something to do
                    task_result = await executor.execute_task(reason, timeout=config.GEMINI_TIMEOUT_SECONDS)
                    state.last_task_status = task_result.status
                    state.last_task_duration = task_result.duration_s
                    state.tasks_completed += 1
                    if task_result.files_changed:
                        state.files_changed = list(set(state.files_changed + task_result.files_changed))
                        for f in task_result.files_changed:
                            state.record_change(f, "modified", reason[:40])
                        logger.info(
                            "📄  Files changed (%d): %s",
                            len(task_result.files_changed),
                            ", ".join(task_result.files_changed[:30]),
                        )
                    if task_result.status == "error":
                        state.error_count += 1
                        _monitoring_task_failures += 1
                        state.record_activity("error", f"Task failed: {reason[:60]}", str(task_result.errors[:2]))

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
                        state.record_activity("success", f"Task completed ({task_result.duration_s:.1f}s): {reason[:60]}")
                    session_mem.record_event("task_executed", task_result.status)
                    await state.broadcast_state()

                elif action == "resume_dag":
                    # V41: Resume DAG execution with pending/re-queued tasks
                    if state.stop_requested:
                        logger.info("🛑  Safe stop requested — skipping DAG resume.")
                        break

                    logger.info("📋  [DAG Resume] Re-entering DAG execution: %s", reason[:80])
                    state.status = "executing"
                    state.record_activity("task", f"Resuming DAG: {reason[:80]}")
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
                    session_mem.record_event("dag_resumed", reason[:80])

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
                                f"ORIGINAL GOAL:\n{goal[:2000]}\n\n"
                            )
                            if _epic_text:
                                gap_prompt += f"EPIC DETAILS:\n{_epic_text[:2000]}\n\n"
                            gap_prompt += f"COMPLETED WORK:\n{_completed_summary[:2000]}\n\n"
                            if _workspace_listing:
                                gap_prompt += f"CURRENT PROJECT FILES:\n{_workspace_listing[:3000]}\n\n"
                            gap_prompt += (
                                "Review the project files carefully. Output a JSON array of new tasks needed:\n"
                                '{"tasks": [{"task_id": "gap-1", "description": "...", "dependencies": []}, ...]}\n\n'
                                "RULES:\n"
                                "1. Only list GENUINELY MISSING work — not cosmetic preferences\n"
                                "2. Each task must be small and atomic (1-3 files max)\n"
                                "3. Maximum 10 tasks\n"
                                "4. If everything looks complete, return {\"tasks\": []}\n"
                                "5. Output strict JSON only — no markdown, no explanation"
                            )

                            logger.info(
                                "🔍  [Gap→Gemini] Gap analysis prompt (%d chars): %.500s…",
                                len(gap_prompt), gap_prompt,
                            )
                            state.record_activity("llm_prompt", "Gemini: gap analysis", gap_prompt[:2000])

                            gap_response = await ask_gemini(gap_prompt, timeout=180)

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
                                        _gap_planner._epic_text = goal[:2000]

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
                            audit_prompt = (
                                "You are a meticulous code auditor. Do a FULL PROJECT AUDIT.\n"
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
                            state.record_activity("llm_prompt", "Gemini: proactive audit", audit_prompt[:2000])
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

                # ── Periodic status log ──
                if loop_count % 6 == 0:  # Every ~60s at 10s poll
                    logger.info(
                        "📊  Status: files=%d, git=%s, errors=%d, dev_server=%s",
                        len(ctx.workspace_files), ctx.git_branch,
                        ctx.diagnostics_errors, ctx.dev_server_running,
                    )
                    await state.broadcast_state()

                # ── Auto-preview check every ~30s ──
                if loop_count % 3 == 0:
                    await _auto_preview_check(
                        sandbox, executor, tools, state, str(effective_project)
                    )

                    # ── V44: Console error capture & auto-fix ──
                    if state.preview_running and _error_collector_started:
                        console_errors = await _capture_console_errors(sandbox)
                        if console_errors:
                            logger.info(
                                "🖥️  [Error Capture] %d browser error(s) detected — triggering auto-fix",
                                len(console_errors),
                            )
                            fix_ok, fix_result = await _diagnose_and_retry(
                                task_description=(
                                    "Fix browser console errors in the live preview. "
                                    "These errors appeared in the browser when rendering the project."
                                ),
                                failure_errors=console_errors,
                                executor=executor,
                                local_brain=local_brain,
                                session_mem=session_mem,
                                task_id="console-fix",
                            )
                            if fix_ok:
                                state.tasks_completed += 1
                                if fix_result.files_changed:
                                    state.files_changed.extend(fix_result.files_changed)
                                    logger.info(
                                        "📄  Console fix changed %d file(s): %s",
                                        len(fix_result.files_changed),
                                        ", ".join(fix_result.files_changed[:10]),
                                    )
                                await state.broadcast_state()

            except SandboxError as exc:
                consecutive_errors += 1
                logger.error("🐳  Sandbox error (#%d): %s", consecutive_errors, exc)
                session_mem.record_event("sandbox_error", str(exc)[:200])

                if consecutive_errors >= 3:
                    strategy = _recovery_engine.recover(str(exc)[:200])
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
                    strategy = _recovery_engine.recover(str(exc)[:200])
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
        # ── Cleanup ──
        logger.info("🧹  Cleaning up …")
        state.status = "stopped"
        print(f"  {C}🧹 Cleaning up …{R}")

        # Stop API server
        if api_task and not api_task.done():
            api_task.cancel()
            try:
                await api_task
            except asyncio.CancelledError:
                pass

        # Copy workspace out if in copy mode
        try:
            await sandbox.copy_workspace_out()
        except Exception:
            pass

        # V41 FIX (Bug 1): The container has a STALE copy of epic_state.json
        # from boot time. copy_workspace_out() just overwrote the host's
        # up-to-date file. Re-save the planner's in-memory state to restore it.
        try:
            _planner = getattr(state, 'planner', None)
            if _planner and hasattr(_planner, '_save_state') and _planner._nodes:
                _planner._save_state()
                logger.info("💾  [Shutdown] Re-saved DAG state after copy-out (%d nodes).", len(_planner._nodes))
        except Exception as _pse:
            logger.debug("💾  [Shutdown] Could not re-save planner state: %s", _pse)

        # Destroy sandbox and any orphaned shadow containers
        try:
            await sandbox.destroy()
            
            # V41 EGRESS FIX: Mass cleanup of any orphaned shadow containers
            # generated during parallel execution before a crash.
            _sp_cmd = "docker ps -a -q --filter 'name=shadow-' | xargs -r docker rm -f"
            if platform.system() == "Windows":
                # PowerShell equivalent for xargs
                _sp_cmd = "docker ps -a -q --filter 'name=shadow-' | ForEach-Object { docker rm -f $_ }"
                import subprocess as _sp
                _sp.run(["powershell", "-Command", _sp_cmd], capture_output=True)
            else:
                import subprocess as _sp
                _sp.run(_sp_cmd, shell=True, capture_output=True)
        except Exception:
            pass

        # Close Ollama session
        try:
            await local_brain.close()
        except Exception:
            pass

        # Remove lockfile and preview port
        if project_path:
            _clear_preview_port(project_path)
            _remove_lockfile(project_path)

        logger.info("Supervisor shut down.")


# ─────────────────────────────────────────────────────────────
# Session State Persistence
# ─────────────────────────────────────────────────────────────

def _save_session_state(goal: str, project_path: str | None) -> None:
    """Save current session state for auto-resume after reboot."""
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

    # V41 FIX: Also save to the project-local session state file so
    # list_projects() can find the goal from the project directory itself.
    if project_path:
        try:
            proj_state_dir = Path(project_path) / ".ag-supervisor"
            proj_state_dir.mkdir(parents=True, exist_ok=True)
            proj_session = proj_state_dir / "session_state.json"
            with open(proj_session, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as exc:
            logger.debug("Could not save project-local session state: %s", exc)


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
# CLI entry point
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Supervisor AI V44 — Command Centre",
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
            print(f"  {C}  Goal:    {saved_goal[:120]}{R}")
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
