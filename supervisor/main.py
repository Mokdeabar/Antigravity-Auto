"""
main.py — The Orchestrator (V35 Command Centre).

Entry point for the Supervisor AI. V35 adds the Command Centre — a thin-client
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
from typing import Optional

# ── Ensure Windows System32 is in PATH ──
if sys.platform == "win32":
    _sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if _sys32.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = f"{_sys32}{os.pathsep}{os.environ.get('PATH', '')}"

from . import config
from . import bootstrap
from .self_evolver import self_evolve
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


async def _triage_fatal_error(tb_str: str) -> Optional[str]:
    """Ask Gemini if a fatal error is transient or a code bug."""
    prompt = (
        "You are a DevOps engineer analyzing a supervisor crash.\n\n"
        f"Traceback:\n```\n{tb_str[:2000]}\n```\n\n"
        "Is this transient (retry) or a code bug (needs fix)? Reply concisely."
    )
    try:
        return await ask_gemini(prompt, timeout=180)
    except Exception:
        return None


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
    print(f"{B}{G}  🌐 SUPERVISOR AI V35 — COMMAND CENTRE{R}")
    print(f"{B}{G}{'='*60}{R}")
    print(f"{C}  Goal: {goal[:100]}{'…' if len(goal) > 100 else ''}{R}")
    print(f"{C}  Project: {project_path or 'N/A'}{R}")
    print(f"{C}  Dry-run: {dry_run}{R}")
    print(f"{C}  Mode: Host Intelligence + Docker Sandbox + Command Centre{R}")
    print(f"{B}{G}{'='*60}{R}\n")

    logger.info("=" * 60)
    logger.info("🚀  Supervisor AI V35 Command Centre starting")
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
        print(f"  {C}🐳 Verifying Docker …{R}")
        logger.info("🐳  Verifying Docker prerequisites …")
        await sandbox.verify_docker()
        print(f"  {G}✅ Docker verified.{R}")

        # ── Start the V35 Command Centre API server (only if not already running) ──
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
        logger.info("📦  Creating sandbox container …")

        sandbox_info = await sandbox.create(
            effective_project,
            mount_mode=mount_mode,
        )

        state.container_id = sandbox_info.container_id
        state.container_health = "running"
        state.mount_mode = mount_mode
        state.preview_port = sandbox_info.preview_port
        state.engine_running = True

        print(f"  {G}✅ Sandbox ready: {sandbox_info.container_id} ({sandbox_info.image}){R}")
        logger.info(
            "✅  Sandbox created: id=%s, image=%s, mount=%s",
            sandbox_info.container_id, sandbox_info.image, sandbox_info.mount_mode,
        )

        # ── Initialize tool server + executor ──
        tools = ToolServer(sandbox)
        executor = HeadlessExecutor(tools, sandbox)

        # ── Check Ollama availability ──
        ollama_ok = await local_brain.is_available()
        state.ollama_online = ollama_ok
        if ollama_ok:
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

        # ── Initialize session memory + council ──
        from .session_memory import SessionMemory
        session_mem = SessionMemory(project_path)
        session_mem.set_goal(goal)

        # ── Initialize scheduler ──
        from . import retry_policy
        from .scheduler import create_default_scheduler
        retry_policy.init()
        scheduler = create_default_scheduler()
        logger.info("🔄  V12 systems initialized: RetryPolicy + CronScheduler")

        # ── First injection: execute the main goal ──
        state.status = "executing"
        print(f"\n  {B}{C}💉 EXECUTING GOAL VIA HOST INTELLIGENCE{R}")
        logger.info("💉  Executing goal: %s", goal[:100])

        # Ask local brain to classify the task (if available)
        task_class = await local_brain.classify_task(goal)
        logger.info("🧠  Task classification: %s", task_class)

        # Execute the main task
        main_result = await executor.execute_task(
            goal,
            timeout=getattr(config, "SANDBOX_TIMEOUT_S", 600),
        )

        if main_result.success:
            print(f"  {G}✅ Goal executed successfully! ({main_result.duration_s:.1f}s){R}")
            session_mem.record_event("goal_executed", "success")
            _recovery_engine.record_success()
        elif main_result.status == "partial":
            print(f"  {config.ANSI_YELLOW}⚠️  Goal executed with warnings ({main_result.duration_s:.1f}s){R}")
            session_mem.record_event("goal_executed", "partial")
        else:
            print(f"  {config.ANSI_RED}❌ Goal execution failed: {main_result.errors[:2]}{R}")
            session_mem.record_event("goal_failed", str(main_result.errors[:2]))

        # ── Monitoring loop ──
        state.status = "monitoring"
        print(f"\n  {G}✅ Entering V35 monitoring loop …{R}\n")
        logger.info("✅  Entering monitoring loop (poll every %.1fs)",
                     config.POLL_INTERVAL_SECONDS)

        consecutive_errors = 0
        loop_count = 0

        while True:
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
            loop_count += 1
            state.loop_count = loop_count

            try:
                # ── Check instruction queue (user commands from UI) ──
                user_instruction = state.queue.pop_nowait()
                if user_instruction:
                    logger.info("📬  User instruction: %s", user_instruction.text[:100])
                    state.last_action = f"user_instruction: {user_instruction.text[:60]}"
                    state.status = "executing"
                    await state.broadcast_state()

                    instr_result = await executor.execute_task(
                        user_instruction.text, timeout=300,
                    )
                    state.last_task_status = instr_result.status
                    state.last_task_duration = instr_result.duration_s
                    state.status = "monitoring"
                    await state.broadcast_state()

                    session_mem.record_event("user_instruction", user_instruction.text[:80])
                    continue  # Re-poll immediately
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

                # ── Local brain: decide next action ──
                context_summary = executor.format_context_for_prompt(ctx)

                if ollama_ok:
                    decision = await local_brain.decide_action(context_summary)
                    action = decision.get("action", "wait")
                    reason = decision.get("reason", "")
                    logger.info("🧠  Ollama decision: %s — %s", action, reason)
                else:
                    # Without local brain, use simple heuristics
                    if ctx.diagnostics_errors > 0:
                        action = "fix_errors"
                        reason = f"{ctx.diagnostics_errors} diagnostic errors"
                    elif not ctx.dev_server_running and ctx.workspace_files:
                        action = "start_server"
                        reason = "Dev server not running"
                    else:
                        action = "wait"
                        reason = "Everything looks healthy"

                # ── Act on decision ──
                if action == "fix_errors" and ctx.diagnostic_details:
                    logger.info("🔧  Fixing %d errors …", ctx.diagnostics_errors)
                    print(f"  {config.ANSI_YELLOW}🔧 Fixing {ctx.diagnostics_errors} errors …{R}")

                    # Use local brain for quick error analysis
                    analysis = None
                    if ollama_ok:
                        analysis = await local_brain.analyze_errors(ctx.diagnostic_details)

                    error_prompt = (
                        f"Fix the following diagnostic errors in the project:\n\n"
                        f"{json.dumps(ctx.diagnostic_details[:10], indent=2)}"
                    )
                    if analysis:
                        error_prompt += f"\n\nQuick analysis from local LLM:\n{analysis}"

                    fix_result = await executor.execute_task(error_prompt, timeout=300)
                    if fix_result.success:
                        print(f"  {G}✅ Errors fixed! ({fix_result.duration_s:.1f}s){R}")
                        session_mem.record_event("errors_fixed", str(ctx.diagnostics_errors))
                    else:
                        logger.warning("🔧  Error fix attempt failed: %s", fix_result.errors[:2])

                elif action == "run_tests":
                    logger.info("🧪  Running tests …")
                    print(f"  {C}🧪 Running tests …{R}")
                    test_result = await executor.run_tests()
                    if test_result.success:
                        print(f"  {G}✅ Tests passed! ({test_result.duration_s:.1f}s){R}")
                    else:
                        print(f"  {config.ANSI_YELLOW}⚠️  Tests had issues{R}")
                    session_mem.record_event("tests_run", test_result.status)

                elif action == "start_server":
                    logger.info("🖥️  Starting dev server …")
                    print(f"  {C}🖥️  Starting dev server …{R}")
                    await executor.start_dev_server()
                    session_mem.record_event("dev_server_started", "")

                elif action == "execute_task":
                    logger.info("📋  Executing additional task: %s", reason[:80])
                    # The local brain identified something to do
                    task_result = await executor.execute_task(reason, timeout=300)
                    session_mem.record_event("task_executed", task_result.status)

                elif action == "escalate":
                    logger.warning("🚨  Local brain requesting escalation: %s", reason)
                    _play_alert()
                    session_mem.record_event("escalation", reason)

                else:
                    # wait or unknown — do nothing this tick
                    pass

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
                    # Update preview state for the UI
                    if ctx.dev_server_running:
                        state.preview_running = True
                        state.preview_port = ctx.dev_server_port if hasattr(ctx, 'dev_server_port') else 3000
                    else:
                        state.preview_running = False
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

        # Destroy sandbox
        try:
            await sandbox.destroy()
        except Exception:
            pass

        # Close Ollama session
        try:
            await local_brain.close()
        except Exception:
            pass

        # Remove lockfile
        if project_path:
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
        description="Supervisor AI V35 — Command Centre",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python -m supervisor --goal "Build a React dashboard" -p ./myproject\n'
            '  python -m supervisor --goal "Fix failing tests" --dry-run\n'
            '  python -m supervisor  (interactive mode)\n'
            '\n'
            'The V35 Command Centre UI is available at http://localhost:8420\n'
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

            logger.info("🔄  SAVED SESSION FOUND: goal='%s', project='%s'", saved_goal[:70], saved_project or 'N/A')
            print(f"\n  {B}{Y}╔═══════════════════════════════════════════════════════╗{R}")
            print(f"  {B}{Y}║  🔄 SAVED SESSION FOUND                              ║{R}")
            print(f"  {B}{Y}╚═══════════════════════════════════════════════════════╝{R}")
            print(f"  {C}  Goal:    {saved_goal[:70]}{'…' if len(saved_goal) > 70 else ''}{R}")
            print(f"  {C}  Project: {saved_project or 'N/A'}{R}")
            print()
            print(f"  {B}{G}  Press ENTER or wait 5s to CONTINUE this session.{R}")
            print(f"  {B}{Y}  Press N to CANCEL and choose a different project.{R}")
            print()

            cancelled = False
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
                                elif key in (b'\r', b'\n'):
                                    remaining = 0
                                    break
                            time.sleep(0.05)
                        if cancelled or remaining == 0:
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
            except Exception:
                pass

            if cancelled:
                print(f"\n  {Y}⛔ Session cancelled. Opening interactive menu …{R}\n")
                _clear_session_state()
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
