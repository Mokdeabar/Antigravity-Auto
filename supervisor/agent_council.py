"""
agent_council.py — Multi-Agent Council V1.0 (The Million Dollar Team).

Orchestrates 6 specialist Gemini agents that collaborate to diagnose,
fix, test, and evolve the supervisor autonomously:

  • Diagnostician — Analyzes screenshots + logs to identify root cause
  • Architect     — Designs structural fixes, new modules, new agents
  • Debugger      — Deep-dives into specific errors with full code context
  • Auditor       — Reviews code quality, security, patterns
  • Fixer         — Generates actual code patches with validation
  • Tester        — Validates fixes: syntax, imports, screenshot diff

Council Session Flow:
  1. Check knowledge base for similar past issues
  2. Diagnostician analyzes (screenshot + logs + KB context)
  3. Routes to specialist (Debugger / Architect / Fixer)
  4. Specialist produces fix → Tester validates
  5. Auditor reviews → approve or reject with notes
  6. Record outcome to knowledge base

Each agent is a Gemini prompt-persona powered by gemini_advisor.
"""

from __future__ import annotations

import asyncio
import ast
import json
import logging
import time
from pathlib import Path

from . import config
from .gemini_advisor import ask_gemini, ask_gemini_json, call_gemini_with_file_json
from . import council_knowledge as kb
from .local_orchestrator import LocalManager, OllamaUnavailable

logger = logging.getLogger("supervisor.agent_council")

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

_SUPERVISOR_DIR = Path(__file__).resolve().parent
_LOG_FILE = _SUPERVISOR_DIR / "supervisor.log"
_COUNCIL_MAX_ROUNDS = 5


# ─────────────────────────────────────────────────────────────
# Agent Personas — each is an expert system prompt
# ─────────────────────────────────────────────────────────────

AGENT_PERSONAS = {
    "diagnostician": (
        "You are the DIAGNOSTICIAN — a world-class systems analyst with 30 years of "
        "experience debugging Electron apps, VS Code extensions, and browser automation. "
        "You have a 300 IQ for pattern recognition. Your job is to:\n"
        "1. Analyze the IDE screenshot and supervisor logs\n"
        "2. Identify the EXACT root cause of the issue\n"
        "3. Determine which specialist should handle the fix\n"
        "4. Search your memory of past similar issues for proven solutions\n\n"
        "You are meticulous, thorough, and never guess — you always find the real cause."
    ),

    "architect": (
        "You are the ARCHITECT — a legendary software architect with 30 years designing "
        "self-healing distributed systems. You have built autonomous AI platforms for "
        "Fortune 500 companies. Your job is to:\n"
        "1. Design structural fixes and new capabilities\n"
        "2. Create new specialist modules when existing ones can't handle the issue\n"
        "3. Identify architectural weaknesses and propose hardening\n"
        "4. Ensure all designs are robust, extensible, and production-grade\n\n"
        "You think in systems, not patches. Every fix must prevent the entire CLASS of failure."
    ),

    "debugger": (
        "You are the DEBUGGER — an elite reverse engineer who has debugged thousands of "
        "production crashes. You specialize in Python async code, Playwright browser "
        "automation, and Electron internals. Your job is to:\n"
        "1. Deep-dive into specific errors, tracebacks, and anomalies\n"
        "2. Read the relevant source code and identify the exact bug\n"
        "3. Trace the causal chain from symptom to root cause\n"
        "4. Propose a precise, minimal fix that doesn't break anything else\n\n"
        "You never reach for broad solutions when a scalpel will do."
    ),

    "auditor": (
        "You are the AUDITOR — a meticulous code reviewer and quality guardian with "
        "expertise in Python best practices, security, and reliability. Your job is to:\n"
        "1. Review proposed code changes for correctness and security\n"
        "2. Check for edge cases, race conditions, and failure modes\n"
        "3. Ensure the fix doesn't introduce new bugs or regressions\n"
        "4. Grade the fix on a PASS/FAIL/NEEDS_WORK scale\n"
        "5. Provide specific improvement suggestions if needed\n\n"
        "You are the last line of defense. Nothing ships without your approval."
    ),

    "fixer": (
        "You are the FIXER — a senior Python developer who writes flawless, "
        "production-grade code. You specialize in async/await patterns, Playwright "
        "automation, and self-healing systems. Your job is to:\n"
        "1. Generate actual Python code patches based on the diagnosis\n"
        "2. Write complete, drop-in replacement code (not diffs)\n"
        "3. Include proper error handling, logging, and fallbacks\n"
        "4. Ensure your code passes syntax validation\n"
        "5. Preserve ALL existing functionality\n\n"
        "Your code works the first time, every time."
    ),

    "tester": (
        "You are the TESTER — a QA mastermind who catches bugs nobody else can find. "
        "Your job is to:\n"
        "1. Validate that proposed fixes have correct Python syntax\n"
        "2. Check that imports resolve and modules load cleanly\n"
        "3. Compare before/after screenshots to verify visual changes\n"
        "4. Identify edge cases the fix might miss\n"
        "5. Give a clear PASS/FAIL verdict with evidence\n\n"
        "You are ruthlessly thorough. If there's a bug, you WILL find it."
    ),

    "synthesizer": (
        "You are the SYNTHESIZER — the supreme judge of the Swarm Debate. "
        "You receive competing or complementary analyses from the ARCHITECT and DEBUGGER. "
        "Your job is to:\n"
        "1. Review both specialist reports\n"
        "2. Identify flaws in either logic\n"
        "3. Synthesize them into a single, flawless, definitive plan of action\n"
        "4. Output the final chosen action to be executed\n\n"
        "You are the voice of consensus and the final arbiter before execution."
    ),
}


# ─────────────────────────────────────────────────────────────
# Issue dataclass
# ─────────────────────────────────────────────────────────────

class Issue:
    """Represents an issue the council needs to resolve."""

    def __init__(
        self,
        issue_type: str,
        trigger: str,
        screenshot_path: str = "",
        logs: str = "",
        source_context: str = "",
        goal: str = "",
        consecutive_count: int = 0,
    ):
        self.issue_type = issue_type
        self.trigger = trigger
        self.screenshot_path = screenshot_path
        self.logs = logs
        self.source_context = source_context
        self.goal = goal
        self.consecutive_count = consecutive_count


# ─────────────────────────────────────────────────────────────
# Resolution dataclass
# ─────────────────────────────────────────────────────────────

class Resolution:
    """Outcome of a council session."""

    def __init__(self):
        self.resolved: bool = False
        self.diagnosis: str = ""
        self.action: str = ""          # final action taken
        self.detail: str = ""          # action detail / selector
        self.agent_chain: list[str] = []
        self.actions_log: list[dict] = []
        self.rounds_used: int = 0
        self.code_patch: dict | None = None  # {file, code} if code was generated
        self.page = None  # updated page reference if it changed


# ─────────────────────────────────────────────────────────────
# Utility: read recent logs
# ─────────────────────────────────────────────────────────────

def _read_recent_logs(n_lines: int = 50) -> str:
    """Read the last N lines of supervisor.log."""
    try:
        if _LOG_FILE.exists():
            lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-n_lines:] if len(lines) > n_lines else lines
            return "\n".join(tail)
    except Exception:
        pass
    return "(no logs available)"


def _read_module_source(filename: str) -> str:
    """Read source code of a supervisor module."""
    try:
        path = _SUPERVISOR_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return f"(could not read {filename})"


def _read_all_module_sources(max_chars: int = 30000) -> str:
    """Read source of all supervisor modules, truncated to max_chars."""
    parts = []
    total = 0
    for py_file in sorted(_SUPERVISOR_DIR.glob("*.py")):
        if py_file.name.startswith("_") and py_file.name != "__init__.py":
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            header = f"\n{'='*50}\nFILE: {py_file.name} ({len(source)} chars)\n{'='*50}\n"
            if total + len(header) + len(source) > max_chars:
                parts.append(f"\n... (truncated, {py_file.name} and remaining files omitted)")
                break
            parts.append(header + source)
            total += len(header) + len(source)
        except Exception:
            pass
    return "".join(parts) if parts else "(no source files)"


# ─────────────────────────────────────────────────────────────
# Agent Call Helper
# ─────────────────────────────────────────────────────────────

async def _ask_agent(
    agent_name: str,
    task_prompt: str,
    screenshot_path: str = "",
    expect_json: bool = True,
    kb_context: str = "",
) -> dict | None:
    """
    Call a specialist agent via Gemini CLI.

    Prepends the agent's persona system prompt, then the task prompt.
    If screenshot_path is provided, uses multimodal call.
    Returns parsed JSON dict or None.
    """
    persona = AGENT_PERSONAS.get(agent_name, "You are a helpful AI assistant.")
    if kb_context:
        persona += f"\n\nCRITICAL KNOWLEDGE FROM PAST SESSIONS:\n{kb_context}\nDo NOT repeat strategies that failed previously!"

    full_prompt = f"{persona}\n\n---\n\nTASK:\n{task_prompt}"

    B = config.ANSI_BOLD
    M = config.ANSI_MAGENTA
    R = config.ANSI_RESET
    
    # V14 AGI Hybrid Swarm: Route lightweight reasoning to Local LLM
    LOCAL_AGENTS = {"diagnostician", "tester", "auditor", "manager"}
    
    if agent_name.lower() in LOCAL_AGENTS:
        print(f"  {B}{M}🧠 [{agent_name.upper()}] Thinking LOCALLY (Ollama)...{R}")
        logger.info("🧠  Routing %s to LocalManager...", agent_name)
        try:
            manager = LocalManager()
            await manager.initialize()  # V37 FIX (H-1): Async init
            # Local LLM is text-only. Strip screenshots from context.
            raw_local_json = await manager.ask_local_model(
                system_prompt=persona, 
                user_prompt=f"TASK:\n{task_prompt}"
            )
            
            # The LocalManager blocklist catches bad responses and returns '{"error": "..."}'
            # or `{}` on fail, so `json.loads` is safe.
            try:
                data = json.loads(raw_local_json)
                return data
            except json.JSONDecodeError:
                logger.error("🧠 LocalManager returned invalid JSON: %s", raw_local_json)
                return None
        
        except OllamaUnavailable:
            # V32: Fall through to cloud path instead of returning None
            logger.warning("🧠 Ollama unavailable — falling back to cloud for %s", agent_name)
        except Exception as local_exc:
            logger.error("🧠 LocalManager router failed: %s", local_exc)
            return None

    # Fallback to Cloud (Gemini Pro/Flash) for heavy Execution/Architecture
    print(f"  {B}{M}☁️  [{agent_name.upper()}] Thinking (Google Cloud)...{R}")
    logger.info("☁️  Calling cloud agent: %s (prompt: %d chars)", agent_name, len(full_prompt))

    try:
        if screenshot_path and Path(screenshot_path).exists():
            data = await call_gemini_with_file_json(full_prompt, screenshot_path, timeout=180)
        elif expect_json:
            data = await ask_gemini_json(full_prompt, timeout=180, use_cache=False)
        else:
            raw = await ask_gemini(full_prompt, timeout=180, use_cache=False)
            data = {"response": raw}

        if data:
            logger.info("☁️  Cloud agent %s responded: %s", agent_name, str(data)[:120])
            return data
        else:
            logger.warning("☁️  Cloud agent %s returned no parseable response.", agent_name)
            return None

    except Exception as exc:
        logger.error("☁️  Cloud agent %s call failed: %s", agent_name, exc)
        return None


# ─────────────────────────────────────────────────────────────
# The Council
# ─────────────────────────────────────────────────────────────

# V74: Cross-session council memory (Audit §4.7)
class CouncilMemory:
    """Persist council insights across sessions."""

    def __init__(self, project_path: str = ""):
        self._insights: list[dict] = []
        self._path = None
        if project_path:
            import pathlib
            self._path = pathlib.Path(project_path) / ".ag-supervisor" / "council_memory.json"
            self._load()

    def record(self, issue_type: str, diagnosis: str, action: str, resolved: bool) -> None:
        """Record a council outcome for future reference."""
        import time as _t
        self._insights.append({
            "issue_type": issue_type,
            "diagnosis": diagnosis[:200],
            "action": action,
            "resolved": resolved,
            "timestamp": _t.time(),
        })
        # Keep last 50 insights
        if len(self._insights) > 50:
            self._insights = self._insights[-50:]
        self._persist()

    def get_context(self, issue_type: str, max_entries: int = 5) -> str:
        """Get relevant past council insights for an issue type."""
        relevant = [i for i in reversed(self._insights) if i.get("issue_type") == issue_type]
        if not relevant:
            return ""
        lines = ["PAST COUNCIL INSIGHTS (from previous sessions):"]
        for entry in relevant[:max_entries]:
            status = "✅ Resolved" if entry.get("resolved") else "❌ Unresolved"
            lines.append(f"  - {status} via {entry.get('action', '?')}: {entry.get('diagnosis', '?')[:100]}")
        return "\n".join(lines)

    def _persist(self) -> None:
        if not self._path:
            return
        try:
            import json as _j
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(_j.dumps(self._insights, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            import json as _j
            self._insights = _j.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            pass


class AgentCouncil:
    """
    Orchestrates specialist agents in a structured diagnostic session.

    V74 Upgrades (Audit §4.7):
      - Full Swarm Debate on 3+ consecutive failures (not just LOW confidence)
      - Reviewer agent validates Fixer code patches before application
      - Cross-session council memory via CouncilMemory

    Usage:
        council = AgentCouncil()
        resolution = await council.convene(issue, page, context)
    """

    def __init__(self, project_path: str = ""):
        self._session_log: list[dict] = []
        self._memory = CouncilMemory(project_path)

    async def convene(
        self,
        issue: Issue,
        page=None,
        context=None,
        action_callbacks: dict = None,
    ) -> Resolution:
        """
        Convene the council to resolve an issue.

        Args:
            issue:           The Issue to resolve
            page:            Optional — sandbox or page reference
            context:         Optional — execution context
            action_callbacks: Dict mapping action names to async callables
                             for executing physical actions

        Returns:
            Resolution with diagnosis, actions taken, and success status
        """
        resolution = Resolution()
        resolution.page = page

        B = config.ANSI_BOLD
        M = config.ANSI_MAGENTA
        C = config.ANSI_CYAN
        G = config.ANSI_GREEN
        Y = config.ANSI_YELLOW
        RD = config.ANSI_RED
        R = config.ANSI_RESET

        print(f"\n  {B}{M}{'═' * 60}{R}")
        print(f"  {B}{M}  🏛️  AGENT COUNCIL IN SESSION  🏛️{R}")
        print(f"  {M}  Issue:  {issue.issue_type} — {issue.trigger[:50]}{R}")
        print(f"  {M}  Count:  {issue.consecutive_count}{R}")
        print(f"  {B}{M}{'═' * 60}{R}\n")

        logger.info(
            "🏛️  Council convened for %s: %s (count: %d)",
            issue.issue_type, issue.trigger[:60], issue.consecutive_count,
        )

        # ── Step 0: Check knowledge base ──────────────────
        past_issues = kb.find_similar_issues(issue.issue_type, issue.trigger)
        kb_context = kb.format_for_prompt(past_issues)

        if past_issues:
            print(f"  {C}📚 Found {len(past_issues)} similar past issues in KB{R}")

        # ── Fresh screenshot + logs ───────────────────────
        screenshot_path = issue.screenshot_path or str(getattr(config, 'SCREENSHOT_PATH', _SUPERVISOR_DIR / 'ide_state.png'))
        # V8: Screenshot capture is now optional — handled by sandbox or skipped in headless mode
        if page and hasattr(page, 'screenshot'):
            try:
                await page.screenshot(path=screenshot_path)
            except Exception as exc:
                logger.warning("🏛️  Could not take screenshot: %s", exc)

        logs = issue.logs or _read_recent_logs(50)

        # ── Council rounds ────────────────────────────────
        for round_num in range(1, _COUNCIL_MAX_ROUNDS + 1):
            print(f"\n  {B}{C}── Council Round {round_num}/{_COUNCIL_MAX_ROUNDS} ──{R}")
            logger.info("🏛️  Council round %d/%d", round_num, _COUNCIL_MAX_ROUNDS)

            # ── Phase 1: DIAGNOSTICIAN ────────────────────
            # V12 Time-Travel: Load the snapshot taken right before we intervened
            snapshot_context = self._load_snapshot_text()
            # V40: Build combined Fast Council prompt (Diagnostician + Fixer + Auditor)
            council_prompt = self._build_fast_council_prompt(
                issue, logs, kb_context,
                resolution.actions_log, round_num,
                snapshot_context=snapshot_context,
            )

            diag_result = await _ask_agent(
                "council", council_prompt, screenshot_path,
            )

            if not diag_result:
                logger.warning("🏛️  Council returned nothing — using fallback.")
                diag_result = {
                    "diagnosis": "Unable to analyze",
                    "route_to": "direct",
                    "recommended_action": "REINJECT",
                    "confidence": "LOW",
                }

            diagnosis = diag_result.get("diagnosis", "unknown")
            route_to = diag_result.get("route_to", "direct").lower()
            recommended_action = diag_result.get("recommended_action", "REINJECT").upper()
            action_detail = diag_result.get("action_detail", "")
            confidence = diag_result.get("confidence", "MEDIUM")

            resolution.diagnosis = diagnosis
            resolution.agent_chain.append("council")

            # V40: Log audit grade from fast council (merged auditor)
            audit_grade = diag_result.get("audit_grade", "")
            audit_suggestions = diag_result.get("audit_suggestions", "")

            print(f"  {M}🔍 Diagnosis: {diagnosis[:70]}{R}")
            print(f"  {M}📋 Route: {route_to.upper()} | Action: {recommended_action} | Confidence: {confidence}{R}")
            if audit_grade:
                print(f"  {M}📋 Council audit: {audit_grade}{R}")

            # V74: Swarm Debate triggers on LOW confidence OR 3+ consecutive failures
            # (Audit §4.7: use full multi-agent flow for stubborn issues)
            specialist_needed = (
                (confidence == "LOW" and route_to in ("debugger", "architect"))
                or issue.consecutive_count >= 3
            )

            if specialist_needed:
                print(f"  {B}{Y}⚡ Launching Swarm Debate (Architect || Debugger) ⚡{R}")
                logger.info("🏛️  Launching Swarm Debate.")

                # Fire both agents concurrently
                dbg_task = self._call_debugger(
                    issue, diagnosis, logs, screenshot_path, kb_context,
                )
                arch_task = self._call_architect(
                    issue, diagnosis, logs, kb_context,
                )

                dbg_res, arch_res = await asyncio.gather(dbg_task, arch_task)

                if dbg_res: resolution.agent_chain.append("debugger")
                if arch_res: resolution.agent_chain.append("architect")

                # Phase 2.5: SYNTHESIZER
                print(f"  {B}{C}🗣️ Synthesizer evaluating Swarm results...{R}")
                synth_result = await self._call_synthesizer(
                    issue, diagnosis, dbg_res, arch_res, kb_context,
                )

                if synth_result:
                    resolution.agent_chain.append("synthesizer")
                    recommended_action = synth_result.get("recommended_action", recommended_action).upper()
                    action_detail = synth_result.get("action_detail", action_detail)
                    diagnosis = synth_result.get("synthesized_diagnosis", diagnosis)
                    print(f"  {M}🧬 Synthesized Action: {recommended_action}{R}")

            # ── Phase 3: EXECUTE ACTION ───────────────────
            resolution.action = recommended_action
            resolution.detail = action_detail

            action_result = await self._execute_action(
                recommended_action, action_detail,
                page, context, issue.goal, action_callbacks,
            )

            resolution.actions_log.append({
                "round": round_num,
                "agent": route_to,
                "action": recommended_action,
                "detail": action_detail[:80],
                "result": action_result.get("status", "unknown"),
            })

            # Update page reference if it changed (e.g. after RESTART_HOST)
            if action_result.get("page"):
                page = action_result["page"]
                resolution.page = page

            print(f"  {C}⚡ Action result: {action_result.get('status', 'unknown')}{R}")

            # ── Phase 4: TESTER validates ─────────────────
            if action_result.get("status") in ("SUCCESS", "EXECUTED"):
                # Take a new screenshot to verify (if page supports it)
                if page and hasattr(page, 'screenshot'):
                    try:
                        await asyncio.sleep(3.0)
                        await page.screenshot(path=screenshot_path)
                    except Exception:
                        pass

                tester_result = await self._call_tester(
                    issue, diagnosis, recommended_action,
                    action_result, screenshot_path, kb_context,
                )

                if tester_result:
                    resolution.agent_chain.append("tester")
                    verdict = tester_result.get("verdict", "UNKNOWN").upper()
                    print(f"  {G if verdict == 'PASS' else Y}🧪 Tester verdict: {verdict}{R}")

                    if verdict == "PASS":
                        resolution.resolved = True
                        resolution.rounds_used = round_num

                        # V40: Auditor merged into Fast Council — log grade from Phase 1
                        if audit_grade:
                            print(f"  {G}📋 Council audit grade: {audit_grade}{R}")
                            if audit_suggestions:
                                print(f"  {Y}📋 Suggestions: {audit_suggestions[:60]}{R}")

                        # Record to KB
                        kb.record_resolution(
                            issue_type=issue.issue_type,
                            trigger=issue.trigger,
                            diagnosis=diagnosis,
                            actions_taken=resolution.actions_log,
                            resolution=f"Resolved in round {round_num} via {recommended_action}",
                            success=True,
                            agent_chain=resolution.agent_chain,
                        )

                        print(f"\n  {B}{G}✅ Council resolved issue in round {round_num}!{R}\n")
                        logger.info("🏛️  ✅ Council resolved issue in round %d.", round_num)

                        # V74: Record success to cross-session memory
                        self._memory.record(
                            issue_type=issue.issue_type,
                            diagnosis=diagnosis,
                            action=recommended_action,
                            resolved=True,
                        )

                        return resolution

            # ── If not resolved, update logs for next round ─
            try:
                logs = _read_recent_logs(50)
            except Exception:
                pass

            await asyncio.sleep(2.0)

        # ── All rounds exhausted ──────────────────────────
        resolution.rounds_used = _COUNCIL_MAX_ROUNDS

        # Check if we should escalate to EVOLVE
        if issue.consecutive_count >= config.WAITING_DEFIB_THRESHOLD:
            print(f"  {RD}🧬 Council exhausted — escalating to self-evolution!{R}")
            logger.warning("🏛️  Council exhausted — escalating to self-evolution.")
            resolution.action = "EVOLVE"

            # Get the Fixer to prepare a code patch
            fixer_result = await self._call_fixer_for_code(
                issue, diagnosis, logs, kb_context,
            )
            if fixer_result:
                resolution.agent_chain.append("fixer")

                # V74: Reviewer validates Fixer output before commit
                reviewer_result = await self._call_reviewer(
                    issue, fixer_result, diagnosis,
                )
                if reviewer_result:
                    resolution.agent_chain.append("reviewer")
                    reviewer_verdict = reviewer_result.get("verdict", "APPROVE").upper()
                    if reviewer_verdict == "REJECT":
                        logger.warning(
                            "🏛️  Reviewer REJECTED fixer patch: %s",
                            reviewer_result.get("reason", "unknown")[:100],
                        )
                        resolution.code_patch = None  # Don't apply rejected patch
                    else:
                        resolution.code_patch = fixer_result
                else:
                    resolution.code_patch = fixer_result

        # Record failure to KB
        kb.record_resolution(
            issue_type=issue.issue_type,
            trigger=issue.trigger,
            diagnosis=resolution.diagnosis,
            actions_taken=resolution.actions_log,
            resolution=f"UNRESOLVED after {_COUNCIL_MAX_ROUNDS} rounds",
            success=False,
            agent_chain=resolution.agent_chain,
        )

        # V74: Record to cross-session memory
        self._memory.record(
            issue_type=issue.issue_type,
            diagnosis=resolution.diagnosis,
            action=resolution.action,
            resolved=False,
        )

        print(f"\n  {Y}⚠️ Council couldn't resolve after {_COUNCIL_MAX_ROUNDS} rounds.{R}\n")
        logger.warning("🏛️  Council failed after %d rounds.", _COUNCIL_MAX_ROUNDS)
        return resolution

    # ─────────────────────────────────────────────────────
    # Prompt builders
    # ─────────────────────────────────────────────────────

    def _load_snapshot_text(self) -> str:
        """Load the V12 Time-Travel Snapshot as formatted text."""
        try:
            from .session_memory import SessionMemory
            mem = SessionMemory()
            snap = mem.get_latest_snapshot()
            if not snap:
                return "No pre-action snapshot available."
            
            return json.dumps(snap, indent=2)
        except Exception as exc:
            return f"Failed to load snapshot: {exc}"

    def _build_fast_council_prompt(
        self,
        issue: Issue,
        logs: str,
        kb_context: str,
        actions_log: list[dict],
        round_num: int,
        snapshot_context: str = "",
    ) -> str:
        """
        V40: Combined Fast Council prompt — merges Diagnostician + Fixer + Auditor
        into a single structured LLM payload.
        """
        history_str = ""
        if actions_log:
            history_str = "\n\nPREVIOUS ACTIONS THIS SESSION:\n"
            for a in actions_log:
                history_str += f"  Round {a['round']}: [{a['agent']}] {a['action']} → {a['result']}\n"

        return (
            f"ISSUE TYPE: {issue.issue_type}\n"
            f"TRIGGER: {issue.trigger}\n"
            f"CONSECUTIVE COUNT: {issue.consecutive_count}\n"
            f"GOAL: {issue.goal}\n"
            f"ROUND: {round_num}/{_COUNCIL_MAX_ROUNDS}\n\n"
            f"TIME-TRAVEL SNAPSHOT (Universe state 1s BEFORE failure):\n{snapshot_context}\n\n"
            f"PAST SIMILAR ISSUES FROM KNOWLEDGE BASE:\n{kb_context}\n\n"
            f"RECENT SUPERVISOR LOGS:\n{logs}\n"
            f"{history_str}\n\n"
            "A screenshot of the IDE is attached.\n\n"
            "You are the COUNCIL — a single-pass agent that combines the roles of "
            "Diagnostician (root cause analysis), Fixer (action plan), and Auditor (quality review).\n\n"
            "ANALYZE and respond with JSON:\n"
            '{\n'
            '  "diagnosis": "precise root cause analysis",\n'
            '  "route_to": "debugger" | "architect" | "direct",\n'
            '  "recommended_action": "REINJECT" | "GHOST_HOTKEY" | "CLICK_SELECTOR" | '
            '"SCREENSHOT" | "RESTART_HOST" | "EVOLVE" | "RUN_COMMAND",\n'
            '  "action_detail": "CSS selector, command, or specific instructions",\n'
            '  "confidence": "HIGH" | "MEDIUM" | "LOW",\n'
            '  "fix_instructions": "step-by-step fix plan if applicable",\n'
            '  "audit_grade": "PASS" | "NEEDS_IMPROVEMENT",\n'
            '  "audit_suggestions": "improvements for future similar issues"\n'
            '}\n'
        )

    async def _call_debugger(
        self, issue: Issue, diagnosis: str, logs: str, screenshot_path: str, kb_context: str,
    ) -> dict | None:
        """Call the Debugger for deep error analysis."""
        # Include relevant source code
        source = _read_module_source("main.py")[:8000]

        prompt = (
            f"DIAGNOSIS FROM DIAGNOSTICIAN: {diagnosis}\n\n"
            f"ISSUE: {issue.trigger}\n"
            f"ISSUE TYPE: {issue.issue_type}\n\n"
            f"RECENT LOGS:\n{logs}\n\n"
            f"RELEVANT SOURCE CODE (main.py, first 8000 chars):\n{source}\n\n"
            "Deep-dive into this error. Respond with JSON:\n"
            '{\n'
            '  "refined_diagnosis": "your deeper analysis",\n'
            '  "root_cause_line": "the specific code causing the issue",\n'
            '  "recommended_action": "REINJECT" | "GHOST_HOTKEY" | "CLICK_SELECTOR" | '
            '"RESTART_HOST" | "EVOLVE" | "RUN_COMMAND" | "OMNI_BRAIN",\n'
            '  "action_detail": "specific instructions (or the OMNI_BRAIN high-level objective)",\n'
            '  "fix_description": "what code change would prevent this"\n'
            '}\n'
        )
        return await _ask_agent("debugger", prompt, screenshot_path, kb_context=kb_context)

    async def _call_architect(
        self, issue: Issue, diagnosis: str, logs: str, kb_context: str,
    ) -> dict | None:
        """Call the Architect for structural design."""
        all_source = _read_all_module_sources(15000)

        prompt = (
            f"DIAGNOSIS: {diagnosis}\n"
            f"ISSUE: {issue.trigger}\n\n"
            f"ALL SUPERVISOR MODULES:\n{all_source}\n\n"
            "Design a structural fix. Respond with JSON:\n"
            '{\n'
            '  "design": "what needs to change architecturally",\n'
            '  "recommended_action": "EVOLVE" | "RUN_COMMAND" | "REINJECT",\n'
            '  "action_detail": "specific instructions",\n'
            '  "structural_impact": "what this fixes at a systems level"\n'
            '}\n'
        )
        return await _ask_agent("architect", prompt, expect_json=True, kb_context=kb_context)

    async def _call_synthesizer(
        self, issue: Issue, diagnosis: str, dbg_res: dict, arch_res: dict, kb_context: str,
    ) -> dict | None:
        """Call the Synthesizer to resolve the Swarm Debate."""
        dbg_str = json.dumps(dbg_res or {}, indent=2)
        arch_str = json.dumps(arch_res or {}, indent=2)

        prompt = (
            f"INITIAL DIAGNOSIS: {diagnosis}\n"
            f"ISSUE: {issue.trigger}\n\n"
            f"--- DEBATE REPORT 1: DEBUGGER ---\n{dbg_str}\n\n"
            f"--- DEBATE REPORT 2: ARCHITECT ---\n{arch_str}\n\n"
            "Synthesize these reports. If they conflict, choose the safer, more precise path.\n"
            "Respond with JSON:\n"
            '{\n'
            '  "synthesized_diagnosis": "merged understanding of the root cause",\n'
            '  "recommended_action": "REINJECT" | "GHOST_HOTKEY" | "CLICK_SELECTOR" | '
            '"RESTART_HOST" | "EVOLVE" | "RUN_COMMAND" | "OMNI_BRAIN",\n'
            '  "action_detail": "specific instructions or objective for the chosen action",\n'
            '  "reasoning": "why you chose this synthesis"\n'
            '}\n'
        )
        return await _ask_agent("synthesizer", prompt, expect_json=True, kb_context=kb_context)

    async def _call_tester(
        self,
        issue: Issue,
        diagnosis: str,
        action: str,
        action_result: dict,
        screenshot_path: str,
        kb_context: str,
    ) -> dict | None:
        """Call the Tester to validate the fix."""
        prompt = (
            f"ORIGINAL ISSUE: {issue.trigger}\n"
            f"DIAGNOSIS: {diagnosis}\n"
            f"ACTION TAKEN: {action}\n"
            f"ACTION RESULT: {json.dumps(action_result)}\n\n"
            "A post-fix screenshot is attached.\n\n"
            "Validate whether the fix worked. Respond with JSON:\n"
            '{\n'
            '  "verdict": "PASS" | "FAIL" | "PARTIAL",\n'
            '  "evidence": "what you see in the screenshot that confirms/denies",\n'
            '  "remaining_issues": "any issues still visible"\n'
            '}\n'
        )
        return await _ask_agent("tester", prompt, screenshot_path, kb_context=kb_context)

    async def _call_auditor(
        self,
        issue: Issue,
        diagnosis: str,
        actions_log: list[dict],
        kb_context: str,
    ) -> dict | None:
        """Call the Auditor to review the resolution."""
        actions_str = json.dumps(actions_log, indent=2)

        prompt = (
            f"ISSUE: {issue.trigger}\n"
            f"DIAGNOSIS: {diagnosis}\n"
            f"ACTIONS TAKEN:\n{actions_str}\n\n"
            "Review the resolution quality. Respond with JSON:\n"
            '{\n'
            '  "grade": "PASS" | "NEEDS_IMPROVEMENT",\n'
            '  "quality_score": 1-10,\n'
            '  "suggestions": "improvements for future similar issues",\n'
            '  "should_evolve": true/false,\n'
            '  "evolution_reason": "why the supervisor code should be updated"\n'
            '}\n'
        )
        return await _ask_agent("auditor", prompt, kb_context=kb_context)

    async def _call_fixer_for_code(
        self, issue: Issue, diagnosis: str, logs: str, kb_context: str,
    ) -> dict | None:
        """
        Call the Fixer to generate a code patch for self-evolution.
        Returns {file, code} dict or None.
        """
        all_source = _read_all_module_sources(25000)

        prompt = (
            f"CRITICAL: The supervisor needs a code fix.\n\n"
            f"ISSUE: {issue.trigger}\n"
            f"DIAGNOSIS: {diagnosis}\n"
            f"RECENT LOGS:\n{logs}\n\n"
            f"ALL SUPERVISOR MODULES:\n{all_source}\n\n"
            "Write the fix. Respond with JSON:\n"
            '{\n'
            '  "file": "filename.py (which file to patch)",\n'
            '  "code": "the COMPLETE fixed source code for that file",\n'
            '  "description": "what the fix does"\n'
            '}\n\n'
            "RULES:\n"
            "- Return the COMPLETE source for the file, not a diff\n"
            "- Preserve ALL existing functionality\n"
            "- Only modify what's needed to fix the bug\n"
            "- Ensure valid Python syntax\n"
        )
        return await _ask_agent("fixer", prompt, kb_context=kb_context)

    async def _call_reviewer(
        self, issue: Issue, fixer_result: dict, diagnosis: str,
    ) -> dict | None:
        """
        V74: Reviewer agent validates Fixer's code patch before it's applied.

        Checks:
          - Patch addresses the actual diagnosis
          - No unintended side effects or regressions
          - Code quality and completeness

        Returns {verdict: "APPROVE"|"REJECT", reason: str} or None.
        """
        file_name = fixer_result.get("file", "unknown")
        code_preview = str(fixer_result.get("code", ""))[:5000]
        description = fixer_result.get("description", "")

        prompt = (
            f"You are the REVIEWER — a meticulous code auditor.\n\n"
            f"The Fixer agent has proposed a code patch to resolve an issue.\n"
            f"Your job is to validate it BEFORE it gets applied.\n\n"
            f"ORIGINAL ISSUE: {issue.trigger}\n"
            f"DIAGNOSIS: {diagnosis}\n"
            f"FIX DESCRIPTION: {description}\n"
            f"TARGET FILE: {file_name}\n"
            f"PROPOSED CODE (first 5000 chars):\n```\n{code_preview}\n```\n\n"
            "REVIEW CHECKLIST:\n"
            "1. Does the patch actually address the diagnosed issue?\n"
            "2. Could it introduce new bugs or regressions?\n"
            "3. Does it preserve all existing functionality?\n"
            "4. Is the Python syntax valid?\n"
            "5. Are there any security concerns?\n\n"
            'Respond with JSON: {"verdict": "APPROVE" or "REJECT", "reason": "explanation"}\n'
        )
        return await _ask_agent("reviewer", prompt)

    # ─────────────────────────────────────────────────────
    # Action execution
    # ─────────────────────────────────────────────────────

    async def _execute_action(
        self,
        action: str,
        detail: str,
        page,
        context,
        goal: str,
        callbacks: dict = None,
    ) -> dict:
        """
        Execute a council-recommended action.

        Uses callbacks dict for physical actions (injecting, clicking, etc.)
        that are defined in main.py and passed to the council.
        """
        callbacks = callbacks or {}

        C = config.ANSI_CYAN
        R = config.ANSI_RESET

        try:
            if action == "REINJECT":
                print(f"  {C}🎯 Executing: Command Palette re-injection …{R}")
                if "reinject" in callbacks:
                    ok = await callbacks["reinject"](page, config.TINY_INJECT_STRING, context)
                    return {"status": "SUCCESS" if ok else "FAILED", "page": page}
                return {"status": "NO_CALLBACK", "page": page}

            elif action == "GHOST_HOTKEY":
                print(f"  {C}👻 Executing: Ghost Hotkey injection …{R}")
                if "ghost_hotkey" in callbacks:
                    ok = await callbacks["ghost_hotkey"](page, config.TINY_INJECT_STRING)
                    return {"status": "SUCCESS" if ok else "FAILED", "page": page}
                return {"status": "NO_CALLBACK", "page": page}

            elif action == "CLICK_SELECTOR":
                if detail:
                    print(f"  {C}🖱️ Executing: Click '{detail[:40]}' …{R}")
                    if "click_selector" in callbacks:
                        ok = await callbacks["click_selector"](context, detail)
                        return {"status": "SUCCESS" if ok else "NOT_FOUND", "page": page}
                return {"status": "NO_DETAIL", "page": page}

            elif action == "SCREENSHOT":
                print(f"  {C}📸 Taking fresh screenshot …{R}")
                if page and hasattr(page, 'screenshot'):
                    try:
                        await page.screenshot(path=str(getattr(config, 'SCREENSHOT_PATH', _SUPERVISOR_DIR / 'ide_state.png')))
                        return {"status": "EXECUTED", "page": page}
                    except Exception as exc:
                        return {"status": f"ERROR: {exc}", "page": page}
                return {"status": "SKIPPED_HEADLESS", "page": page}

            elif action == "RESTART_HOST":
                print(f"  {config.ANSI_RED}🫀 Executing: Defibrillator restart …{R}")
                if "resuscitate" in callbacks:
                    ok, new_page = await callbacks["resuscitate"](page, context, goal)
                    return {"status": "SUCCESS" if ok else "FAILED", "page": new_page}
                return {"status": "NO_CALLBACK", "page": page}

            elif action == "RUN_COMMAND":
                if detail:
                    print(f"  {C}💻 Executing command: {detail[:50]} …{R}")
                    # Execute a shell command — used for things like
                    # restarting services, checking ports, etc.
                    try:
                        proc = await asyncio.create_subprocess_shell(
                            detail,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(), timeout=30,
                        )
                        output = stdout.decode("utf-8", errors="replace")[:500]
                        return {
                            "status": "EXECUTED",
                            "output": output,
                            "page": page,
                        }
                    except Exception as exc:
                        return {"status": f"ERROR: {exc}", "page": page}
                return {"status": "NO_DETAIL", "page": page}
                
            elif action == "OMNI_BRAIN":
                print(f"  {config.ANSI_MAGENTA}🧠 Engaging V18 Omni-Brain (Long-Term Memory Loop)...{R}")
                from .local_orchestrator import LocalManager
                from .cli_worker import CLIWorker
                from .workspace_transaction import GitTransactionManager
                from .episodic_memory import EpisodicMemory
                from .reflection_engine import ReflectionEngine
                from .memory_consolidation import MemoryConsolidator
                import os
                import json

                manager = LocalManager()
                await manager.initialize()  # V37 FIX (H-1): Async init
                worker = CLIWorker()
                project_cwd = str(config.get_project_path() or ".")
                git_tx = GitTransactionManager(project_cwd)
                memory = EpisodicMemory()
                reflector = ReflectionEngine(manager)
                
                # V18: Check dependency staleness before any execution
                consolidator = MemoryConsolidator(manager, project_cwd)
                consolidator._check_staleness()
                
                # V15.1: Acquire the pre-execution lock (includes detached HEAD guard)
                ok, pristine_sha = git_tx.commit_pre_execution_state()
                if not ok:
                    logger.error("Git transaction prep failed: %s", pristine_sha)
                    return {"status": "FAILED", "output": f"Git transaction failed: {pristine_sha}", "page": page}
                
                snapshot_context = self._load_snapshot_text()
                logs = issue.logs
                
                test_cmd = os.getenv("TEST_COMMAND", "python -m tests.mock_repo_tests")
                max_attempts = 3
                worker_result = {"status": "failed", "output": ""}
                tests_passed = False
                
                # V18: Inject global environmental axioms into the base prompt
                global_axioms = MemoryConsolidator.load_axioms()
                base_system_prompt = (
                    "You are the Omni-Brain Manager. Analyze the following snapshot and logs. "
                    "Output a strict JSON object with an 'objective' string detailing the exact goal for the Gemini CLI."
                )
                if global_axioms:
                    base_system_prompt += f"\n\n{global_axioms}"
                    print(f"  {config.ANSI_YELLOW}📜 Injected global environmental axioms.{R}")
                
                # Track the original objective for memory hashing
                original_objective = issue.logs or issue.goal or "unknown"

                for attempt in range(1, max_attempts + 1):
                    # V16: Query episodic memory for prior semantic lessons
                    anti_patterns = memory.compress_anti_patterns(original_objective, pristine_sha)
                    system_prompt = base_system_prompt
                    if anti_patterns:
                        system_prompt += f"\n\n{anti_patterns}"
                        print(f"  {config.ANSI_YELLOW}🪞 Injected {anti_patterns.count(chr(10))} semantic lessons.{R}")
                    
                    user_prompt = f"Logs: {logs}\n\nSnapshot: {snapshot_context}"
                    
                    raw_response = await manager.ask_local_model(system_prompt, user_prompt)
                    if not raw_response or raw_response == "{}":
                        return {"status": "FAILED", "output": "Local Manager returned empty response.", "page": page}
                    
                    try:
                        data = json.loads(raw_response)
                        manager_objective = data.get("objective", raw_response)
                    except Exception:
                        manager_objective = raw_response

                    print(f"  {config.ANSI_CYAN}🧠 Local Objective (Attempt {attempt}): {manager_objective}{R}")
                    
                    # V15.1: CLI Worker mutates files. We do NOT commit these.
                    worker_result = await worker.execute_objective(manager_objective, cwd=project_cwd)
                    status_color = config.ANSI_GREEN if worker_result["status"] == "success" else config.ANSI_RED
                    print(f"  {status_color}⚡ CLI Worker Result: {worker_result['status']}{R}")
                    
                    # V15.1: Test against the DIRTY working tree. No intermediate commits.
                    tests_passed, test_logs = git_tx.run_tests(test_cmd)
                    
                    if tests_passed:
                        print(f"  {config.ANSI_GREEN}✅ Validation Tests PASSED!{R}")
                        git_tx.amend_success_commit()
                        break
                    else:
                        print(f"  {config.ANSI_RED}❌ Tests FAILED! Reflecting and hard resetting...{R}")
                        # V17: Capture diff BEFORE hard reset annihilates it
                        failed_diff = git_tx.capture_dirty_diff()
                        # V17: Route through Reflection Engine for semantic compression
                        lesson = await reflector.reflect(failed_diff, test_logs, original_objective)
                        print(f"  {config.ANSI_YELLOW}🪞 Lesson: {lesson}{R}")
                        # V17: Persist the compressed semantic lesson (NOT the raw diff)
                        memory.record_failure(original_objective, pristine_sha, lesson, test_logs[:500])
                        # V15.1: Annihilate ALL tracked + untracked changes
                        git_tx.hard_reset_to_pristine(pristine_sha)
                        # Feed failure context back for the next attempt
                        logs = f"PREVIOUS ATTEMPT {attempt} FAILED. TEST LOGS:\n{test_logs[:1000]}\n" + issue.logs

                memory.close()
                return {
                    "status": "success" if tests_passed else "failed",
                    "output": worker_result["output"][:1000] if tests_passed else "Failed all validation attempts.",
                    "page": page
                }

            elif action == "EPIC":
                print(f"  {config.ANSI_MAGENTA}📋 Engaging V25 Temporal Planner (Full-Stack Autonomous Matrix)...{R}")
                from .local_orchestrator import LocalManager
                from .cli_worker import CLIWorker
                from .workspace_transaction import GitTransactionManager
                from .episodic_memory import EpisodicMemory
                from .reflection_engine import ReflectionEngine
                from .memory_consolidation import MemoryConsolidator
                from .temporal_planner import TemporalPlanner
                from .merge_arbiter import MergeArbiter
                from .external_researcher import ExternalResearcher
                from .autonomous_verifier import AutonomousVerifier
                from .visual_qa_engine import VisualQAEngine
                from .compliance_gateway import ComplianceGateway
                from .deployment_engine import DeploymentEngine
                import os
                import json
                import asyncio

                manager = LocalManager()
                await manager.initialize()  # V37 FIX (H-1): Async init
                project_cwd = str(config.get_project_path() or ".")
                memory = EpisodicMemory()
                reflector = ReflectionEngine(manager)
                planner = TemporalPlanner(manager, project_cwd)
                arbiter = MergeArbiter(project_cwd, manager)
                researcher = ExternalResearcher()
                verifier = AutonomousVerifier(manager, project_cwd)
                visual_qa = VisualQAEngine(sandbox_manager=None)  # V8: pass sandbox when available
                compliance = ComplianceGateway(manager, project_cwd)
                test_cmd = os.getenv("TEST_COMMAND", "python -m tests.mock_repo_tests")
                dev_cmd = os.getenv("DEV_SERVER_CMD", "npm run dev")
                dev_port = int(os.getenv("DEV_SERVER_PORT", "3000"))
                max_workers = config.MAX_CONCURRENT_WORKERS

                # V20: Prune orphaned worktrees from previous crashed runs
                arbiter.prune_worktrees()

                # V18: Check dependency staleness
                consolidator = MemoryConsolidator(manager, project_cwd)
                consolidator._check_staleness()
                global_axioms = MemoryConsolidator.load_axioms()

                # Step 1: Load or resume epic
                resumed = planner.load_state()
                if not resumed:
                    epic_path = getattr(issue, "epic_path", None)
                    ok, epic_result = planner.load_epic(epic_path)
                    if not ok:
                        return {"status": "FAILED", "output": f"Epic load failed: {epic_result}", "page": page}

                    ok, dag_result = await planner.decompose_epic()
                    if not ok:
                        return {"status": "FAILED", "output": f"Decomposition failed: {dag_result}", "page": page}
                    print(f"  {config.ANSI_GREEN}📋 {dag_result}{R}")
                else:
                    progress = planner.get_progress()
                    print(f"  {config.ANSI_CYAN}📋 Resumed epic: {progress}{R}")

                # Step 2: Execute DAG nodes — parallel when possible
                epic_aborted = False
                while not planner.is_epic_complete():
                    ws_ok, ws_msg = planner.validate_workspace()
                    if not ws_ok:
                        print(f"  {config.ANSI_RED}⚠️ {ws_msg} Pausing epic.{R}")
                        return {"status": "PAUSED", "output": ws_msg, "page": page}

                    batch = planner.get_parallel_batch(max_workers)
                    if not batch:
                        print(f"  {config.ANSI_RED}❌ No unblocked tasks. Epic stuck.{R}")
                        epic_aborted = True
                        break

                    # Get current HEAD as the base for all worktrees
                    git_base = GitTransactionManager(project_cwd)
                    base_sha = git_base.get_current_sha()
                    if not base_sha:
                        return {"status": "FAILED", "output": "Cannot determine HEAD SHA.", "page": page}

                    if len(batch) == 1:
                        # ── SEQUENTIAL PATH (single node, no worktree overhead) ──
                        node = batch[0]
                        print(f"  {config.ANSI_CYAN}📋 Executing: [{node.task_id}] {node.description}{R}")

                        # V21: Pre-research knowledge gaps
                        if node.knowledge_gaps:
                            print(f"  {config.ANSI_YELLOW}🌐 Researching {len(node.knowledge_gaps)} knowledge gaps...{R}")
                            chunks = await researcher.research_gaps(node.knowledge_gaps)
                            print(f"  {config.ANSI_GREEN}🌐 Fetched {chunks} documentation chunks.{R}")

                        git_tx = GitTransactionManager(project_cwd)
                        ok, pristine_sha = git_tx.commit_pre_execution_state()
                        if not ok:
                            return {"status": "FAILED", "output": f"Git lock failed: {pristine_sha}", "page": page}

                        completed_summary = planner.get_completed_summary()
                        scoped_prompt = node.description
                        if completed_summary:
                            scoped_prompt = f"{completed_summary}\n\n[CURRENT TASK]\n{node.description}"
                        if global_axioms:
                            scoped_prompt = f"{global_axioms}\n\n{scoped_prompt}"

                        anti_patterns = memory.compress_anti_patterns(node.description, pristine_sha)
                        if anti_patterns:
                            scoped_prompt = f"{anti_patterns}\n\n{scoped_prompt}"

                        # V21: Inject retrieved documentation
                        rag_docs = researcher.query_docs(node.description)
                        if rag_docs:
                            scoped_prompt = f"{rag_docs}\n\n{scoped_prompt}"

                        # V22: TDD RED PHASE — generate test, verify it fails
                        test_ok, test_path = await verifier.generate_test(
                            node.task_id, node.description, rag_docs
                        )
                        if test_ok:
                            is_red, red_msg = verifier.run_red_phase(test_path)
                            if is_red:
                                print(f"  {config.ANSI_RED}🔴 {red_msg}{R}")
                            else:
                                print(f"  {config.ANSI_YELLOW}⚠️ {red_msg} Skipping TDD for this node.{R}")
                                test_ok = False  # Disable dual-pass gate
                        else:
                            print(f"  {config.ANSI_YELLOW}🧪 Test generation failed: {test_path}. Proceeding without TDD.{R}")

                        worker = CLIWorker()
                        node_result = await worker.execute_objective(scoped_prompt, cwd=project_cwd)

                        # V22: GREEN PHASE — dual-pass verification
                        if test_ok:
                            tests_passed, test_logs = verifier.run_green_phase(test_path, test_cmd)
                        else:
                            tests_passed, test_logs = git_tx.run_tests(test_cmd)

                        # V40: Per-node Visual QA removed — reserved for final deployment pass

                        if tests_passed:
                            # V24: COMPLIANCE GATE — SAST + semantic + financial audit
                            compliance_diff = git_tx.capture_dirty_diff()
                            print(f"  {config.ANSI_YELLOW}🛡️ Compliance audit for [{node.task_id}]...{R}")
                            comp_ok, comp_report = await compliance.run_full_audit(
                                compliance_diff, project_cwd, node.description
                            )
                            if not comp_ok:
                                tests_passed = False
                                test_logs = comp_report
                                print(f"  {config.ANSI_RED}🛡️ Compliance FAILED: {comp_report[:120]}{R}")
                            else:
                                print(f"  {config.ANSI_GREEN}🛡️ Compliance PASSED.{R}")

                        if tests_passed:
                            print(f"  {config.ANSI_GREEN}✅ Node [{node.task_id}] PASSED (fully verified)!{R}")
                            git_tx.amend_success_commit()
                            new_sha = git_tx.get_current_sha() or ""
                            planner.mark_complete(node.task_id, new_sha)
                            planner.update_workspace_hash()
                        else:
                            fail_msg = test_logs if isinstance(test_logs, str) else str(test_logs)
                            print(f"  {config.ANSI_RED}❌ Node [{node.task_id}] FAILED.{R}")
                            failed_diff = git_tx.capture_dirty_diff()
                            lesson = await reflector.reflect(failed_diff, fail_msg, node.description)
                            print(f"  {config.ANSI_YELLOW}🪞 Lesson: {lesson}{R}")
                            memory.record_failure(node.description, pristine_sha, lesson, fail_msg[:500])
                            git_tx.hard_reset_to_pristine(pristine_sha)
                            planner.mark_failed(node.task_id, lesson)

                            replan_ok, replan_msg = await planner.replan(node.task_id, lesson)
                            if not replan_ok:
                                print(f"  {config.ANSI_RED}🚫 {replan_msg}{R}")
                                epic_aborted = True
                                break
                            print(f"  {config.ANSI_CYAN}🔄 {replan_msg}{R}")

                    else:
                        # ── PARALLEL PATH (multiple independent nodes via worktrees) ──
                        print(f"  {config.ANSI_MAGENTA}⚡ Parallel tier: {[n.task_id for n in batch]}{R}")

                        # V21: Pre-research knowledge gaps for the entire batch
                        all_gaps = []
                        for n in batch:
                            all_gaps.extend(n.knowledge_gaps)
                        if all_gaps:
                            print(f"  {config.ANSI_YELLOW}🌐 Researching {len(all_gaps)} knowledge gaps for parallel tier...{R}")
                            chunks = await researcher.research_gaps(all_gaps)
                            print(f"  {config.ANSI_GREEN}🌐 Fetched {chunks} documentation chunks.{R}")

                        async def execute_in_worktree(node, base_sha):
                            """Execute a single node in an isolated worktree."""
                            wt_ok, wt_path = arbiter.create_worktree(node.task_id, base_sha)
                            if not wt_ok:
                                return node.task_id, False, wt_path

                            completed_summary = planner.get_completed_summary()
                            scoped_prompt = node.description
                            if completed_summary:
                                scoped_prompt = f"{completed_summary}\n\n[CURRENT TASK]\n{node.description}"
                            if global_axioms:
                                scoped_prompt = f"{global_axioms}\n\n{scoped_prompt}"

                            anti_patterns = memory.compress_anti_patterns(node.description, base_sha)
                            if anti_patterns:
                                scoped_prompt = f"{anti_patterns}\n\n{scoped_prompt}"

                            # V21: Inject retrieved documentation
                            rag_docs = researcher.query_docs(node.description)
                            if rag_docs:
                                scoped_prompt = f"{rag_docs}\n\n{scoped_prompt}"

                            w = CLIWorker()
                            await w.execute_objective(scoped_prompt, cwd=wt_path)

                            # V22: Dual-pass in worktree (node test + global)
                            wt_verifier = AutonomousVerifier(manager, wt_path)
                            t_ok, t_path = await wt_verifier.generate_test(
                                node.task_id, node.description, rag_docs
                            )
                            if t_ok:
                                is_red, _ = wt_verifier.run_red_phase(t_path)
                                # In worktree, red phase may not apply (code already written)
                                # Run green phase directly
                                passed, logs = wt_verifier.run_green_phase(t_path, test_cmd)
                                # V40: Per-node Visual QA removed — reserved for final deployment pass
                                # V24: Compliance gate in worktree
                                if passed:
                                    wt_diff_proc = subprocess.run(
                                        ["git", "diff", "HEAD"],
                                        cwd=wt_path, capture_output=True, text=True, timeout=10,
                                    )
                                    wt_diff = wt_diff_proc.stdout if wt_diff_proc.returncode == 0 else ""
                                    c_ok, c_report = await compliance.run_full_audit(
                                        wt_diff, wt_path, node.description
                                    )
                                    if not c_ok:
                                        passed = False
                                        logs = c_report
                            else:
                                wt_tx = GitTransactionManager(wt_path)
                                passed, logs = wt_tx.run_tests(test_cmd)
                            return node.task_id, passed, logs

                        # Spawn parallel coroutines
                        tasks_coros = [
                            execute_in_worktree(node, base_sha) for node in batch
                        ]
                        results = await asyncio.gather(*tasks_coros, return_exceptions=True)

                        # Process results
                        any_failed = False
                        for result in results:
                            if isinstance(result, Exception):
                                logger.error("Parallel node exception: %s", result)
                                any_failed = True
                                continue

                            node_id, passed, logs = result
                            if passed:
                                print(f"  {config.ANSI_GREEN}✅ Node [{node_id}] PASSED in worktree!{R}")
                                merge_ok, merge_msg = await arbiter.merge_worktree(node_id)
                                if merge_ok:
                                    new_sha = git_base.get_current_sha() or ""
                                    planner.mark_complete(node_id, new_sha)
                                else:
                                    print(f"  {config.ANSI_RED}⚠️ Merge failed for [{node_id}]: {merge_msg}{R}")
                                    planner.mark_failed(node_id, merge_msg)
                                    any_failed = True
                            else:
                                print(f"  {config.ANSI_RED}❌ Node [{node_id}] FAILED in worktree.{R}")
                                lesson = await reflector.reflect("", str(logs), node_id)
                                memory.record_failure(node_id, base_sha, lesson, str(logs)[:500])
                                arbiter.remove_worktree(node_id)
                                planner.mark_failed(node_id, lesson)
                                any_failed = True

                        planner.update_workspace_hash()

                        if any_failed:
                            # Find the first failed node for replanning
                            failed_nodes = [n for n in batch if planner._nodes.get(n.task_id, n).status == "failed"]
                            if failed_nodes:
                                fn = failed_nodes[0]
                                replan_ok, replan_msg = await planner.replan(
                                    fn.task_id, planner._nodes[fn.task_id].result
                                )
                                if not replan_ok:
                                    print(f"  {config.ANSI_RED}🚫 {replan_msg}{R}")
                                    epic_aborted = True
                                    break
                                print(f"  {config.ANSI_CYAN}🔄 {replan_msg}{R}")

                # Finalize
                memory.close()
                researcher.teardown()  # V21: Wipe RAG store
                verifier.teardown()    # V22: Purge .ag-tests/
                # V40: VQA dev server cleanup handled lazily at deployment time
                arbiter.prune_worktrees()
                if planner.is_epic_complete():
                    progress = planner.get_progress()
                    planner.clear_state()
                    print(f"  {config.ANSI_GREEN}🎉 Epic complete! {progress}{R}")

                    # V25: Interactive deployment confirmation (10s timer)
                    deployer = DeploymentEngine(project_cwd)
                    should_deploy, confirm_msg = deployer.request_deploy_confirmation()
                    if should_deploy:
                        print(f"  {config.ANSI_MAGENTA}🚀 Engaging OpsEngineer — deploying to production...{R}")
                        deploy_ok, deploy_report = await deployer.deploy_epic()
                        if deploy_ok:
                            print(f"  {config.ANSI_GREEN}🎉 Deployment successful!{R}")
                            status = deployer.get_status()
                            return {
                                "status": "success",
                                "output": f"Epic complete & deployed: {progress}\n{deploy_report}",
                                "deployment": status,
                                "page": page,
                            }
                        else:
                            print(f"  {config.ANSI_RED}🚀 Deployment FAILED: {deploy_report[:120]}{R}")
                            return {
                                "status": "DEPLOY_FAILED",
                                "output": f"Epic complete but deployment failed:\n{deploy_report}",
                                "page": page,
                            }
                    else:
                        print(f"  {config.ANSI_CYAN}⏭️ {confirm_msg}{R}")

                    return {"status": "success", "output": f"Epic complete: {progress}", "page": page}
                elif epic_aborted:
                    planner.clear_state()
                    return {"status": "ABORTED", "output": "Epic aborted after replan limit.", "page": page}
                else:
                    return {"status": "PAUSED", "output": "Epic paused.", "page": page}

            elif action == "EVOLVE":
                print(f"  {config.ANSI_RED}🧬 Self-evolution required!{R}")
                return {"status": "EVOLVE_NEEDED", "page": page}

            else:
                logger.warning("🏛️  Unknown action: %s — defaulting to REINJECT.", action)
                if "reinject" in callbacks:
                    ok = await callbacks["reinject"](page, config.TINY_INJECT_STRING, context)
                    return {"status": "SUCCESS" if ok else "FAILED", "page": page}
                return {"status": "UNKNOWN_ACTION", "page": page}

        except Exception as exc:
            logger.error("🏛️  Action execution failed: %s", exc)
            return {"status": f"EXCEPTION: {exc}", "page": page}
