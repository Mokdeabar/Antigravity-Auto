"""
scheduler.py — OpenClaw-Inspired Cron Scheduler.

A lightweight, persistent job scheduler that runs inside the
supervisor's main monitoring loop.

Features:
  - Persistent job store in _cron_jobs.json (survives restarts)
  - Job types: one_shot (run once), interval (every N seconds)
  - Built-in job actions: health_check, screenshot_audit,
    context_compact, evolution_check
  - tick() called from main loop — checks and runs due jobs
  - Glass Brain output for job execution
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from . import config

logger = logging.getLogger("supervisor.scheduler")


# ─────────────────────────────────────────────────────────────
# Job Definition
# ─────────────────────────────────────────────────────────────

class CronJob:
    """A single scheduled job."""

    def __init__(
        self,
        name: str,
        action: str,
        interval_seconds: float,
        one_shot: bool = False,
        enabled: bool = True,
    ):
        self.name = name
        self.action = action            # Action key (mapped to callable)
        self.interval_seconds = interval_seconds
        self.one_shot = one_shot
        self.enabled = enabled
        self.last_run: float = 0.0
        self.run_count: int = 0
        self.last_result: str = ""
        self.created_at: float = time.time()

    def is_due(self) -> bool:
        """Return True if this job should run now."""
        if not self.enabled:
            return False
        if self.one_shot and self.run_count > 0:
            return False
        return (time.time() - self.last_run) >= self.interval_seconds

    def mark_run(self, result: str = "") -> None:
        """Mark the job as just run."""
        self.last_run = time.time()
        self.run_count += 1
        self.last_result = result[:200]
        if self.one_shot:
            self.enabled = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "action": self.action,
            "interval_seconds": self.interval_seconds,
            "one_shot": self.one_shot,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "run_count": self.run_count,
            "last_result": self.last_result,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CronJob":
        job = cls(
            name=data["name"],
            action=data["action"],
            interval_seconds=data.get("interval_seconds", 600),
            one_shot=data.get("one_shot", False),
            enabled=data.get("enabled", True),
        )
        job.last_run = data.get("last_run", 0.0)
        job.run_count = data.get("run_count", 0)
        job.last_result = data.get("last_result", "")
        job.created_at = data.get("created_at", time.time())
        return job


# ─────────────────────────────────────────────────────────────
# Cron Scheduler
# ─────────────────────────────────────────────────────────────

class CronScheduler:
    """
    Persistent cron-like scheduler for the supervisor.

    Usage:
        scheduler = CronScheduler()
        scheduler.add_job("health_check", "health_check", interval_seconds=300)
        scheduler.add_job("compact", "context_compact", interval_seconds=1800)

        # In main loop:
        async for result in scheduler.tick(context_getter):
            print(result)
    """

    def __init__(
        self,
        state_path: Optional[Path] = None,
        actions: Optional[dict[str, Callable]] = None,
    ):
        from . import config
        self._state_path = state_path or (config.get_state_dir() / "_cron_jobs.json")
        self._jobs: dict[str, CronJob] = {}
        self._actions: dict[str, Callable] = actions or {}
        self._load_state()

    # ────────────────────────────────────────────────
    # Job Management
    # ────────────────────────────────────────────────

    def add_job(
        self,
        name: str,
        action: str,
        interval_seconds: float,
        one_shot: bool = False,
    ) -> None:
        """Add or update a scheduled job."""
        if name in self._jobs:
            # Update existing job
            job = self._jobs[name]
            job.action = action
            job.interval_seconds = interval_seconds
            job.one_shot = one_shot
            job.enabled = True
        else:
            self._jobs[name] = CronJob(
                name=name,
                action=action,
                interval_seconds=interval_seconds,
                one_shot=one_shot,
            )
        self._save_state()
        logger.info(
            "⏰  Job registered: %s → %s every %ds%s",
            name, action, interval_seconds,
            " (one-shot)" if one_shot else "",
        )

    def remove_job(self, name: str) -> bool:
        """Remove a job by name. Returns True if found and removed."""
        if name in self._jobs:
            del self._jobs[name]
            self._save_state()
            logger.info("⏰  Job removed: %s", name)
            return True
        return False

    def register_action(self, key: str, handler: Callable) -> None:
        """Register an action handler."""
        self._actions[key] = handler

    def list_jobs(self) -> list[dict]:
        """Return all jobs as dicts."""
        return [j.to_dict() for j in self._jobs.values()]

    # ────────────────────────────────────────────────
    # Tick — called from main loop
    # ────────────────────────────────────────────────

    async def tick(self) -> list[str]:
        """
        Check all jobs and run any that are due.

        Returns list of result strings from executed jobs.
        Should be called once per main loop iteration.
        """
        results: list[str] = []
        ran_any = False

        for job in list(self._jobs.values()):
            if not job.is_due():
                continue

            handler = self._actions.get(job.action)
            if handler is None:
                logger.warning("⏰  No handler for action: %s", job.action)
                job.mark_run(result=f"ERROR: no handler for '{job.action}'")
                continue

            M = config.ANSI_MAGENTA
            R = config.ANSI_RESET
            logger.info("⏰  Running job: %s → %s", job.name, job.action)
            print(f"  {M}⏰ Scheduler: running '{job.name}'{R}")

            try:
                # Support both async and sync handlers
                import asyncio
                if asyncio.iscoroutinefunction(handler):
                    result = await handler()
                else:
                    result = handler()

                result_str = str(result) if result else "OK"
                job.mark_run(result=result_str)
                results.append(f"{job.name}: {result_str}")
                ran_any = True

                logger.info(
                    "⏰  Job '%s' completed (run #%d): %s",
                    job.name, job.run_count, result_str[:100],
                )

            except Exception as exc:
                error_msg = f"ERROR: {exc}"
                job.mark_run(result=error_msg)
                results.append(f"{job.name}: {error_msg}")
                logger.warning(
                    "⏰  Job '%s' failed: %s", job.name, exc,
                )

        if ran_any:
            self._save_state()

        return results

    # ────────────────────────────────────────────────
    # Status
    # ────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return scheduler status."""
        now = time.time()
        return {
            "total_jobs": len(self._jobs),
            "enabled_jobs": sum(1 for j in self._jobs.values() if j.enabled),
            "jobs": {
                name: {
                    "action": job.action,
                    "enabled": job.enabled,
                    "interval": job.interval_seconds,
                    "due_in": max(0, job.interval_seconds - (now - job.last_run)),
                    "run_count": job.run_count,
                    "last_result": job.last_result,
                }
                for name, job in self._jobs.items()
            },
        }

    # ────────────────────────────────────────────────
    # Persistence
    # ────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load jobs from disk."""
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                for job_data in data.get("jobs", []):
                    job = CronJob.from_dict(job_data)
                    self._jobs[job.name] = job
                logger.info(
                    "⏰  Loaded %d scheduled jobs from disk", len(self._jobs),
                )
        except Exception as exc:
            logger.debug("Could not load scheduler state: %s", exc)

    def _save_state(self) -> None:
        """Save jobs to disk."""
        try:
            data = {
                "jobs": [j.to_dict() for j in self._jobs.values()],
                "saved_at": time.time(),
            }
            self._state_path.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Could not save scheduler state: %s", exc)

    def __repr__(self) -> str:
        return (
            f"CronScheduler(jobs={len(self._jobs)}, "
            f"enabled={sum(1 for j in self._jobs.values() if j.enabled)})"
        )


# ─────────────────────────────────────────────────────────────
# Built-in Job Actions
# ─────────────────────────────────────────────────────────────

def create_default_scheduler() -> CronScheduler:
    """
    Create a scheduler with default built-in jobs.

    Default jobs:
      - context_compact: Compact session history every 30 minutes
      - budget_report: Log context budget every 15 minutes
      - failover_check: Log model failover status every 10 minutes
      - rate_limit_report: Log rate limit stats every 20 minutes
      - self_improvement: Ask Gemini for improvement suggestions every 60 minutes
    """
    scheduler = CronScheduler()

    # Register built-in actions
    scheduler.register_action("context_compact", _action_context_compact)
    scheduler.register_action("budget_report", _action_budget_report)
    scheduler.register_action("failover_check", _action_failover_check)
    scheduler.register_action("rate_limit_report", _action_rate_limit_report)
    scheduler.register_action("self_improvement", _action_self_improvement)
    scheduler.register_action("workspace_index", _action_workspace_index)
    scheduler.register_action("telemetry_hud_update", _action_telemetry_hud_update)
    scheduler.register_action("metacognitive_review", _action_metacognitive_review)
    scheduler.register_action("memory_consolidation", _action_memory_consolidation)

    # Add default jobs (only if not already loaded from disk)
    if "telemetry_hud_update" not in scheduler._jobs:
        scheduler.add_job(
            "telemetry_hud_update", "telemetry_hud_update",
            interval_seconds=10,  # 10 seconds (Live Refresh)
        )
    if "workspace_index" not in scheduler._jobs:
        scheduler.add_job(
            "workspace_index", "workspace_index",
            interval_seconds=300,  # 5 minutes
        )
    if "context_compact" not in scheduler._jobs:
        scheduler.add_job(
            "context_compact", "context_compact",
            interval_seconds=1800,  # 30 minutes
        )
    if "budget_report" not in scheduler._jobs:
        scheduler.add_job(
            "budget_report", "budget_report",
            interval_seconds=900,  # 15 minutes
        )
    if "failover_check" not in scheduler._jobs:
        scheduler.add_job(
            "failover_check", "failover_check",
            interval_seconds=600,  # 10 minutes
        )
    if "rate_limit_report" not in scheduler._jobs:
        scheduler.add_job(
            "rate_limit_report", "rate_limit_report",
            interval_seconds=1200,  # 20 minutes
        )
    if "self_improvement" not in scheduler._jobs:
        scheduler.add_job(
            "self_improvement", "self_improvement",
            interval_seconds=config.SELF_IMPROVEMENT_INTERVAL_S,  # 60 minutes
        )
    if "metacognitive_review" not in scheduler._jobs:
        scheduler.add_job(
            "metacognitive_review", "metacognitive_review",
            interval_seconds=3600,  # 60 minutes
        )
    if "memory_consolidation" not in scheduler._jobs:
        scheduler.add_job(
            "memory_consolidation", "memory_consolidation",
            interval_seconds=7200,  # 2 hours
        )

    # V26: Hotfix watcher and telemetry polling
    scheduler.register_action("hotfix_watcher", _action_hotfix_watcher)
    scheduler.register_action("telemetry_poll", _action_telemetry_poll)
    if "hotfix_watcher" not in scheduler._jobs:
        scheduler.add_job(
            "hotfix_watcher", "hotfix_watcher",
            interval_seconds=30,  # Check every 30 seconds
        )
    if "telemetry_poll" not in scheduler._jobs:
        scheduler.add_job(
            "telemetry_poll", "telemetry_poll",
            interval_seconds=300,  # Poll production logs every 5 minutes
        )

    # V27: Growth engine experiment evaluation and watcher
    scheduler.register_action("experiment_evaluator", _action_experiment_evaluator)
    scheduler.register_action("experiment_watcher", _action_experiment_watcher)
    if "experiment_evaluator" not in scheduler._jobs:
        scheduler.add_job(
            "experiment_evaluator", "experiment_evaluator",
            interval_seconds=3600,  # Check for matured experiments every hour
        )
    if "experiment_watcher" not in scheduler._jobs:
        scheduler.add_job(
            "experiment_watcher", "experiment_watcher",
            interval_seconds=30,  # Check for new experiment epics every 30s
        )
    # V28: FinOps cost profiling and refactor watcher
    scheduler.register_action("finops_monitor", _action_finops_monitor)
    scheduler.register_action("refactor_watcher", _action_refactor_watcher)
    if "finops_monitor" not in scheduler._jobs:
        scheduler.add_job(
            "finops_monitor", "finops_monitor",
            interval_seconds=3600,  # Profile costs every hour
        )
    if "refactor_watcher" not in scheduler._jobs:
        scheduler.add_job(
            "refactor_watcher", "refactor_watcher",
            interval_seconds=30,  # Check for refactor epics every 30s
        )

    # V29: Qualitative Synthesis Engine + Infinite Polish Engine
    scheduler.register_action("feature_request_watcher", _action_feature_request_watcher)
    scheduler.register_action("feature_pipeline", _action_feature_pipeline)
    scheduler.register_action("user_injection_monitor", _action_user_injection_monitor)
    if "feature_request_watcher" not in scheduler._jobs:
        scheduler.add_job(
            "feature_request_watcher", "feature_request_watcher",
            interval_seconds=60,  # Check for FEATURE_EPIC.md every 60s
        )
    if "feature_pipeline" not in scheduler._jobs:
        scheduler.add_job(
            "feature_pipeline", "feature_pipeline",
            interval_seconds=3600,  # Run full clustering pipeline every hour
        )
    if "user_injection_monitor" not in scheduler._jobs:
        scheduler.add_job(
            "user_injection_monitor", "user_injection_monitor",
            interval_seconds=10,  # Check for user injection every 10s
        )

    return scheduler


# ─────────────────────────────────────────────────────────────
# Built-in Action Handlers
# ─────────────────────────────────────────────────────────────

def _action_context_compact() -> str:
    """Compact session memory history."""
    try:
        from .session_memory import SessionMemory
        memory = SessionMemory()
        memory.compact_history()
        return "Compaction completed"
    except Exception as exc:
        return f"Compaction failed: {exc}"


def _action_budget_report() -> str:
    """Log current context budget report."""
    try:
        from .retry_policy import get_context_budget
        budget = get_context_budget()
        report = budget.get_report()
        logger.info("📊  Scheduled budget report:\n%s", report)
        print(f"  {config.ANSI_CYAN}{report}{config.ANSI_RESET}")
        return f"Budget: {budget.budget_pct:.1f}% used"
    except Exception as exc:
        return f"Budget report failed: {exc}"


def _action_failover_check() -> str:
    """Log model failover chain status."""
    try:
        from .retry_policy import get_failover_chain
        chain = get_failover_chain()
        status = chain.get_status()
        active = status["active_model"]
        models_info_parts = []
        for m, info in status["models"].items():
            if info["available"]:
                models_info_parts.append(f"{m}(✅)")
            else:
                cd = info["cooldown_remaining"]
                models_info_parts.append(f"{m}(⏳{cd:.0f}s)")
        models_info = ", ".join(models_info_parts)
        logger.info("🔄  Failover status: active=%s | %s", active, models_info)
        return f"Active: {active}"
    except Exception as exc:
        return f"Failover check failed: {exc}"


def _action_rate_limit_report() -> str:
    """Log current rate limit statistics."""
    try:
        from .retry_policy import get_rate_tracker, get_router
        tracker = get_rate_tracker()
        router = get_router()
        rl_stats = tracker.get_stats()
        rt_stats = router.get_stats()

        report = (
            f"⚡ Rate limits: {rl_stats['total_rate_limits']} total, "
            f"{rl_stats['last_hour']} last hour | "
            f"🎯 Routing: Pro={rt_stats['pro']}, Flash={rt_stats['flash']}, "
            f"Auto={rt_stats['auto']} ({rt_stats['flash_pct']:.0f}% Flash)"
        )
        logger.info("⚡  %s", report)
        print(f"  {config.ANSI_CYAN}{report}{config.ANSI_RESET}")
        return report
    except Exception as exc:
        return f"Rate limit report failed: {exc}"


def _action_self_improvement() -> str:
    """Ask Gemini for self-improvement suggestions (async wrapper)."""
    try:
        import asyncio
        from .gemini_advisor import request_self_improvement

        # Collect recent error summary from session memory
        issue_summary = ""
        try:
            from .session_memory import SessionMemory
            memory = SessionMemory()
            recent = memory.get_recent_events(20)
            errors = [e for e in recent if "error" in str(e).lower() or "fail" in str(e).lower()]
            if errors:
                issue_summary = "\n".join(str(e)[:200] for e in errors[-5:])
        except Exception:
            pass

        # Run async function in a new loop if needed
        try:
            loop = asyncio.get_running_loop()
            # Already in async context, schedule it
            future = asyncio.ensure_future(request_self_improvement(issue_summary))
            # Can't await here since we're in a sync function
            # The future will run in the background
            return "Self-improvement analysis scheduled"
        except RuntimeError:
            # No running loop, create one
            result = asyncio.run(request_self_improvement(issue_summary))
            if result:
                return f"Suggestions logged: {result[:100]}"
            return "No suggestions generated"
    except Exception as exc:
        return f"Self-improvement failed: {exc}"


def _action_workspace_index() -> str:
    """Run the Omniscient Eye AST scan dynamically in the background."""
    try:
        from .workspace_indexer import WorkspaceMap
        from . import config
        project_path = config.get_project_path()
        if not project_path:
            return "Skipped: No project path set."
            
        wm = WorkspaceMap(project_path)
        wm.scan_workspace()
        
        return f"Indexed {len(wm.index)} files in workspace map."
    except Exception as exc:
        return f"Workspace index failed: {exc}"


def _action_telemetry_hud_update() -> str:
    """Update the LIVE_HUD.md markdown dashboard."""
    try:
        from . import telemetry_hud
        return telemetry_hud.update_hud()
    except Exception as exc:
        return f"HUD update failed: {exc}"

def _action_metacognitive_review() -> str:
    """V14: Read logs and proactively prompt the Architect for optimisations."""
    try:
        from .agent_council import AgentCouncil  # type: ignore
        import asyncio
        
        # V14.1 Concurrency Lock
        from .session_memory import SessionMemory
        mem = SessionMemory()
        snap = mem.get_latest_snapshot()
        if snap and snap.get("agent_status") == "WORKING":
            return "Skipped: Metacognitive review deferred. Supervisor is currently WORKING."
        
        from . import config
        project_path = config.get_project_path()
        if not project_path:
            return "Skipped: No project path set."

        hud_path = project_path / ".ag-supervisor" / "LIVE_HUD.md"
        hud_data = hud_path.read_text(encoding="utf-8") if hud_path.exists() else "No HUD data."
        
        council = AgentCouncil()
        
        async def run_architect_metacognition():
            try:
                res = await council._call_architect(
                    {"issue_type": "METACOGNITION", "trigger": "Proactive internal review schedule hit.",
                     "screenshot_path": "", "goal": "Identify inefficiencies in Antigravity supervisor.",
                     "consecutive_count": 1}, 
                    "System running fine, looking to optimize.",
                    hud_data,
                    "Target self-evolution of prompt engineering, error handlers, or performance parameters."
                )
                if res and res.get("recommended_action") == "EVOLVE":
                    from .self_evolver import self_evolve
                    self_evolve("Proactive Metacognition Request: " + res.get("design", ""), headless=True)
            except Exception as e:
                logger.error("Metacognitive Architect failure: %s", e)
                
        # Launch non-blocking
        try:
            loop = asyncio.get_running_loop()
            asyncio.ensure_future(run_architect_metacognition())
            return "Metacognitive Review Dispatched."
        except RuntimeError:
            asyncio.run(run_architect_metacognition())
            return "Metacognitive Review Executed."
            
    except Exception as exc:
        return f"Metacognitive review failed: {exc}"

def _action_memory_consolidation() -> str:
    """V18: Promote recurring episodic lessons into global environmental axioms."""
    try:
        # Concurrency Lock: defer if the agent is busy
        from .session_memory import SessionMemory
        mem = SessionMemory()
        snap = mem.get_latest_snapshot()
        if snap and snap.get("agent_status") == "WORKING":
            return "Skipped: Memory consolidation deferred. Supervisor is WORKING."

        from .local_orchestrator import LocalManager
        from .memory_consolidation import MemoryConsolidator
        import asyncio

        manager = LocalManager()
        consolidator = MemoryConsolidator(manager)

        try:
            loop = asyncio.get_running_loop()
            future = asyncio.ensure_future(consolidator.consolidate())
            return "Memory consolidation dispatched."
        except RuntimeError:
            result = asyncio.run(consolidator.consolidate())
            return result

    except Exception as exc:
        return f"Memory consolidation failed: {exc}"


# ─────────────────────────────────────────────────────────────
# V26: Hotfix Watcher + Telemetry Poll
# ─────────────────────────────────────────────────────────────

def _action_hotfix_watcher():
    """Check for HOTFIX_EPIC.md and signal the system to re-enter EPIC handler."""
    try:
        from pathlib import Path
        workspace = Path(os.getenv("PROJECT_CWD", "."))
        hotfix_path = workspace / "HOTFIX_EPIC.md"

        if hotfix_path.exists():
            # Check if system is idle (no active EPIC)
            from . import config
            state_path = workspace / ".ag-memory" / "temporal_state.json"
            if state_path.exists():
                return "Hotfix detected but an EPIC is already in progress. Queued."

            content = hotfix_path.read_text(encoding="utf-8")[:200]
            logger.info("🔥 HOTFIX_EPIC.md detected — signaling autonomous re-entry.")
            return f"HOTFIX_READY: {content}"

        return "No hotfix pending."
    except Exception as exc:
        return f"Hotfix watcher error: {exc}"


async def _action_telemetry_poll():
    """Poll production logs for fatal errors and auto-generate hotfix epics."""
    try:
        deploy_token = os.getenv("DEPLOY_TOKEN", "")
        if not deploy_token:
            return "Telemetry poll skipped: no DEPLOY_TOKEN."

        from .telemetry_ingester import TelemetryIngester
        from .local_orchestrator import LocalManager

        manager = LocalManager()
        workspace = os.getenv("PROJECT_CWD", ".")
        ingester = TelemetryIngester(manager, workspace)

        payloads = await ingester.poll_vercel_logs()
        if not payloads:
            return "No production errors found."

        processed = 0
        for payload in payloads[:3]:  # Cap at 3 per poll
            ok, result = await ingester.process_error(payload)
            if ok:
                processed += 1
                logger.info("🔥 Hotfix epic generated: %s", result)

        return f"Processed {processed}/{len(payloads)} production errors."

    except Exception as exc:
        return f"Telemetry poll failed: {exc}"


# ─────────────────────────────────────────────────────────────
# V27: Growth Engine Actions
# ─────────────────────────────────────────────────────────────

def _action_experiment_evaluator():
    """Check for matured A/B experiments (48h+) ready for evaluation."""
    try:
        from .growth_engine import GrowthEngine
        workspace = os.getenv("PROJECT_CWD", ".")
        engine = GrowthEngine(workspace_path=workspace)
        pending = engine.get_pending_evaluations()

        if not pending:
            return "No experiments ready for evaluation."

        results = []
        for exp in pending:
            flag = exp.get("flag_name", "")
            elapsed = exp.get("elapsed_hours", 0)
            results.append(f"{flag} ({elapsed:.0f}h elapsed)")

        logger.info("🧪 %d experiment(s) ready for evaluation: %s", len(results), results)
        return f"EXPERIMENTS_READY: {', '.join(results)}"

    except Exception as exc:
        return f"Experiment evaluator error: {exc}"


def _action_experiment_watcher():
    """Check for EXPERIMENT_EPIC.md and signal the system."""
    try:
        from pathlib import Path
        workspace = Path(os.getenv("PROJECT_CWD", "."))
        epic_path = workspace / "EXPERIMENT_EPIC.md"

        if epic_path.exists():
            from . import config
            state_path = workspace / ".ag-memory" / "temporal_state.json"
            if state_path.exists():
                return "Experiment epic detected but an EPIC is already in progress. Queued."

            content = epic_path.read_text(encoding="utf-8")[:200]
            logger.info("🧪 EXPERIMENT_EPIC.md detected — signaling growth pipeline.")
            return f"EXPERIMENT_READY: {content}"

        return "No experiment pending."
    except Exception as exc:
        return f"Experiment watcher error: {exc}"


# ─────────────────────────────────────────────────────────────
# V28: FinOps Actions
# ─────────────────────────────────────────────────────────────

def _action_finops_monitor():
    """Periodic cost profiling — check baselines and flag margin decay."""
    try:
        from .finops_engine import FinOpsEngine
        workspace = os.getenv("PROJECT_CWD", ".")
        engine = FinOpsEngine(workspace_path=workspace)

        # Look for APM report file
        from pathlib import Path
        apm_path = Path(workspace) / ".ag-memory" / "apm_report.json"
        if not apm_path.exists():
            return "No APM report available."

        report = json.loads(apm_path.read_text(encoding="utf-8"))
        has_issues, flagged = engine.ingest_apm_report(report)

        if has_issues:
            worst = flagged[0]
            logger.info(
                "💰 Margin decay detected: %s (+%.1f%%)",
                worst.get("route"), worst.get("cost_increase_pct", 0),
            )
            return f"MARGIN_DECAY: {worst.get('route')} (+{worst.get('cost_increase_pct', 0):.1f}%)"

        return "All routes within cost baseline."
    except Exception as exc:
        return f"FinOps monitor error: {exc}"


def _action_refactor_watcher():
    """Check for REFACTOR_EPIC.md and signal the system."""
    try:
        from pathlib import Path
        workspace = Path(os.getenv("PROJECT_CWD", "."))
        epic_path = workspace / "REFACTOR_EPIC.md"

        if epic_path.exists():
            state_path = workspace / ".ag-memory" / "temporal_state.json"
            if state_path.exists():
                return "Refactor epic detected but an EPIC is already in progress. Queued."

            content = epic_path.read_text(encoding="utf-8")[:200]
            logger.info("💰 REFACTOR_EPIC.md detected — signaling cost optimization.")
            return f"REFACTOR_READY: {content}"

        return "No refactor pending."
    except Exception as exc:
        return f"Refactor watcher error: {exc}"


# ─────────────────────────────────────────────────────────────
# V29: Qualitative Synthesis + Infinite Polish Actions
# ─────────────────────────────────────────────────────────────

def _action_feature_request_watcher():
    """Check for FEATURE_EPIC.md and signal the system."""
    try:
        from pathlib import Path
        workspace = Path(os.getenv("PROJECT_CWD", "."))
        epic_path = workspace / "FEATURE_EPIC.md"

        if epic_path.exists():
            state_path = workspace / ".ag-memory" / "temporal_state.json"
            if state_path.exists():
                return "Feature epic detected but an EPIC is already in progress. Queued."

            content = epic_path.read_text(encoding="utf-8")[:200]
            logger.info("🔬 FEATURE_EPIC.md detected — signaling feature build.")
            return f"FEATURE_READY: {content}"

        return "No feature epic pending."
    except Exception as exc:
        return f"Feature request watcher error: {exc}"


def _action_feature_pipeline():
    """Run the full qualitative synthesis pipeline."""
    try:
        import asyncio
        from .user_research_engine import UserResearchEngine
        workspace = os.getenv("PROJECT_CWD", ".")
        engine = UserResearchEngine(workspace_path=workspace)

        async def _run():
            result = await engine.run_pipeline()
            if result.get("epics_generated", 0) > 0:
                logger.info(
                    "🔬 Feature pipeline generated %d epic(s)",
                    result["epics_generated"],
                )
                return f"FEATURE_PIPELINE: {result['epics_generated']} epic(s) generated"
            return f"Feature pipeline: {result.get('clusters_updated', 0)} clusters, no new epics."

        try:
            loop = asyncio.get_running_loop()
            future = asyncio.ensure_future(_run())
            return "Feature pipeline dispatched (async)"
        except RuntimeError:
            return asyncio.run(_run())
    except Exception as exc:
        return f"Feature pipeline error: {exc}"


def _action_user_injection_monitor():
    """Monitor for live user feedback injections during polish loops."""
    try:
        from .polish_engine import PolishEngine
        workspace = os.getenv("PROJECT_CWD", ".")
        engine = PolishEngine(workspace_path=workspace)

        if not engine.is_active:
            return "No active polish session."

        injection = engine.check_user_injection()
        if injection:
            logger.info("✍️ User injection received during polish: %s", injection[:80])
            return f"USER_INJECTION: {injection[:200]}"

        return f"Polish active (iteration {engine.iterations}), no injection."
    except Exception as exc:
        return f"User injection monitor error: {exc}"
