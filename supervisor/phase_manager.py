"""
phase_manager.py — V54 Phased Project Management System

Creates and maintains a living, phased implementation plan for every project.
The plan lives as two files inside the project's .ag-supervisor/ directory:

  project_plan.md   — Human-readable, updated by Gemini after every task
  phase_state.json  — Machine-readable phase state, read/written by this class

Integration hooks (called from main.py _execute_dag_recursive):
  A. get_phase_context_for_decomposition() — injected into DAG goal string
  B. on_node_completed()                  — called after each node marks done
  C. check_and_advance_phase()            — called after each full DAG drain
  D. initialize()                         — called once per project at depth=0
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING
from . import config

if TYPE_CHECKING:
    pass  # executor / state are duck-typed to avoid circular imports

logger = logging.getLogger("supervisor.phase_manager")

# ─────────────────────────────────────────────────────────────
# Schema version — bump when phase_state.json format changes
# ─────────────────────────────────────────────────────────────
_SCHEMA_VERSION = 1

_PLAN_DIR      = ".ag-supervisor"
_STATE_FILE    = "phase_state.json"
_MARKDOWN_FILE = "project_plan.md"
_REVIEW_FILE   = "phase_review_tmp.txt"


# ─────────────────────────────────────────────────────────────
# PhaseManager
# ─────────────────────────────────────────────────────────────

class PhaseManager:
    """
    Manages a living, phased implementation plan for a project.

    Lifecycle:
      1. initialize(goal) — load existing plan or create brand-new one
      2. get_phase_context_for_decomposition() — inject into each DAG goal
      3. on_node_completed() — lightweight plan update after each node
      4. check_and_advance_phase() — assess + advance after each DAG cycle
    """

    def __init__(self, project_path: str, executor, state) -> None:
        self._project_path = Path(project_path)
        self._executor     = executor
        self._state        = state
        self._plan: dict   = {}
        self._plan_dir     = self._project_path / _PLAN_DIR
        self._state_path   = self._plan_dir / _STATE_FILE
        self._md_path      = self._plan_dir / _MARKDOWN_FILE
        self._init_done    = False
        self._advance_lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────

    def is_initialized(self) -> bool:
        return self._init_done and bool(self._plan)

    async def initialize(self, goal: str) -> None:
        """
        Load existing plan or create a brand-new one.
        Safe to call on every DAG cycle — idempotent after first call.
        """
        try:
            self._plan_dir.mkdir(parents=True, exist_ok=True)
            if self._state_path.exists():
                self._load_plan()
                # V54: Stub plans are provisional — if one was saved, delete it
                # and re-run Gemini so we get a real project-specific plan.
                if self._plan.get("is_stub"):
                    logger.warning(
                        "📋  [Phase] Stub plan detected — deleting and re-running Gemini phase planning"
                    )
                    try:
                        self._state_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    self._plan = {}
                    await self._create_initial_plan(goal)
                    return
                if self._plan.get("version") == _SCHEMA_VERSION and self._plan.get("phases"):
                    self._init_done = True
                    _ph = self.get_current_phase()
                    logger.info(
                        "📋  [Phase] Loaded plan: Phase %d of ~%d — %s",
                        self._plan.get("current_phase", 1),
                        self._plan.get("total_phases_estimated", "?"),
                        _ph.get("name", "?") if _ph else "?",
                    )
                    return
            # No plan yet — create one
            await self._create_initial_plan(goal)
        except Exception as exc:
            logger.warning("📋  [Phase] initialize() error (non-fatal): %s", exc)

    def get_current_phase(self) -> dict | None:
        """Return the active phase dict, or None if plan is empty."""
        if not self._plan:
            return None
        phases = self._plan.get("phases", [])
        idx    = self._plan.get("current_phase", 1)
        for ph in phases:
            if ph.get("id") == idx:
                return ph
        return phases[0] if phases else None

    def get_phase_context_for_decomposition(self) -> str:
        """
        Returns a cross-phase prefix to inject before the DAG goal.

        V67: Now includes ALL phases (not just the current one) so Gemini
        generates DAG nodes for every pending task across the whole project.
        This maximises concurrent work — if Phase 2 has tasks that can start
        now (no unmet deps), they get DAG nodes alongside Phase 6 tasks.
        """
        if not self._plan:
            return ""
        _all_phases = self._plan.get("phases", [])
        if not _all_phases:
            return ""
        current = max(1, min(
            self._plan.get("current_phase", 1),
            len(_all_phases) if _all_phases else 1,
        ))
        total = len(_all_phases)

        sections: list[str] = []
        sections.append(
            f"## ⚙️  PROJECT PHASES — {total} total, active: Phase {current}\n\n"
        )

        _total_pending = 0
        for ph in _all_phases:
            ph_id = ph.get("id", "?")
            ph_status = ph.get("status", "pending")
            ph_tasks = ph.get("tasks", [])
            done = [t for t in ph_tasks if t.get("status") == "done"]
            pending = [t for t in ph_tasks if t.get("status") != "done"]
            _total_pending += len(pending)

            if ph_status == "completed" and not pending:
                sections.append(
                    f"### ✅ Phase {ph_id}: \"{ph.get('name', '')}\" — COMPLETED\n\n"
                )
                continue

            marker = "▶" if ph_id == current else ("✅" if ph_status == "completed" else "⏳")
            sections.append(
                f"### {marker} Phase {ph_id}: \"{ph.get('name', '')}\"\n"
                f"**Focus:** {ph.get('focus', '')}\n"
                f"**Exit Criteria:** {ph.get('exit_criteria', 'All phase tasks complete.')}\n"
                f"**Status:** {len(done)} done, {len(pending)} pending\n\n"
            )
            if pending:
                sections.append("**Pending tasks (generate DAG nodes for these):**\n")
                for t in pending[:20]:
                    sections.append(f"  - {t.get('title', '')}\n")
                if len(pending) > 20:
                    sections.append(f"  ... and {len(pending) - 20} more\n")
                sections.append("\n")

        sections.append(
            f"🚨 DECOMPOSITION RULE — MAXIMIZE TASK OUTPUT:\n"
            f"  - Generate DAG nodes for ALL {_total_pending} pending tasks listed above.\n"
            f"  - Cover EVERY phase that has pending work, not just Phase {current}.\n"
            f"  - Each pending task should have at least one corresponding DAG node.\n"
            f"  - Tasks from different phases with no interdependency can run in parallel.\n"
            f"  - Maximum 100 DAG nodes per decomposition.\n\n"
            f"{'─' * 60}\n\n"
        )

        return "".join(sections)

    def get_cross_phase_context_for_audit(self) -> str:
        """
        Returns a multi-phase context block for audit prompts.

        Unlike get_phase_context_for_decomposition() (which restricts to a single
        phase), this method includes ALL phases so the audit can generate tasks
        from any phase where dependencies/prerequisites are already met.

        This maximises concurrent work: if Phase 1 is nearly done and Phase 2
        tasks don't depend on the remaining Phase 1 work, those Phase 2 tasks
        should be generated now rather than waiting for Phase 1 to fully drain.
        """
        if not self._plan:
            return ""
        _all_phases = self._plan.get("phases", [])
        if not _all_phases:
            return ""
        current = max(1, min(
            self._plan.get("current_phase", 1),
            len(_all_phases) if _all_phases else 1,
        ))
        total = len(_all_phases)

        sections: list[str] = []
        sections.append(
            f"\n═══════════════════════════════════════════════════════════\n"
            f"ALL PROJECT PHASES (CROSS-PHASE SCOPE — MAXIMIZE WORK):\n"
            f"═══════════════════════════════════════════════════════════\n"
            f"Active phase: {current} of {total}\n\n"
        )

        for ph in _all_phases:
            ph_id = ph.get("id", "?")
            ph_status = ph.get("status", "pending")
            ph_tasks = ph.get("tasks", [])
            done = [t for t in ph_tasks if t.get("status") == "done"]
            pending = [t for t in ph_tasks if t.get("status") != "done"]
            marker = "✅" if ph_status == "completed" else ("▶" if ph_id == current else "⏳")

            sections.append(
                f"{marker} Phase {ph_id}: \"{ph.get('name', '')}\"\n"
                f"   Focus: {ph.get('focus', '')}\n"
                f"   Status: {ph_status} ({len(done)} done, {len(pending)} pending)\n"
            )
            if pending:
                for t in pending[:15]:
                    sections.append(f"     - [PENDING] {t.get('title', '')}\n")
                if len(pending) > 15:
                    sections.append(f"     ... and {len(pending) - 15} more pending tasks\n")
            if done:
                sections.append(f"     ({len(done)} tasks already done in this phase)\n")
            sections.append("\n")

        sections.append(
            f"🚨 CROSS-PHASE SCOPE RULE — MAXIMIZE TASK OUTPUT:\n"
            f"  - CREATE EXECUTABLE TASKS from ANY phase that has PENDING work.\n"
            f"    This includes EARLIER phases (even if the current phase is {current}).\n"
            f"    Phases 1 through {total} ALL may have unfinished tasks — check every one.\n"
            f"  - A phase marked PARTIAL means it has incomplete work. Generate tasks for it.\n"
            f"  - A phase marked COMPLETED (✅) can still have regressions — spot-check those.\n"
            f"  - Do NOT skip any phase just because it is before or after Phase {current}.\n"
            f"  - For DONE tasks in any phase: spot-check implementation. If clearly missing or broken,\n"
            f"    create a fix task tagged [PHASE-RECHECK].\n"
            f"  - For PENDING tasks that appear already implemented: note as\n"
            f"    '[PHASE-DONE] <task-id> appears complete — verify and mark done'.\n"
            f"  - Maximum 100 tasks per audit run across ALL phases.\n"
        )

        return "".join(sections)

    def get_all_pending_phase_tasks(self) -> list[dict]:
        """
        Return ALL pending/incomplete tasks across ALL phases.

        Each returned dict has: phase_id, task_id, title, status.
        Used by the gap-fill logic that ensures every phase task becomes
        a DAG node — preventing the situation where 19 phase tasks exist
        but only 5 get decomposed into the execution graph.
        """
        result: list[dict] = []
        if not self._plan:
            return result
        for ph in self._plan.get("phases", []):
            ph_id = ph.get("id", "?")
            ph_status = ph.get("status", "pending")
            # Skip fully completed phases
            if ph_status == "completed":
                continue
            for t in ph.get("tasks", []):
                if t.get("status") == "done":
                    continue
                result.append({
                    "phase_id": ph_id,
                    "task_id": t.get("id", ""),
                    "title": t.get("title", ""),
                    "status": t.get("status", "pending"),
                })
        return result

    async def on_node_completed(
        self,
        node_id:        str,
        description:    str,
        success:        bool,
        files_changed:  list[str],
        error_summary:  str = "",
    ) -> None:
        """
        Non-blocking. Called after each DAG node completes.
        Updates phase_state.json and appends a note to project_plan.md.
        Never raises — errors are swallowed so execution is never blocked.
        """
        try:
            if not self._plan:
                return
            ph = self.get_current_phase()
            if not ph:
                return

            # Find matching phase task by title keyword overlap
            matched_task = self._match_task(ph, description)

            ts   = time.strftime("%Y-%m-%d %H:%M")
            note = ""

            if success:
                status = "done"
                note   = f"✅ `{node_id}` done ({len(files_changed)} files) — {ts}"
                if files_changed:
                    note += f"\n    Files: {', '.join(files_changed[:5])}"
            else:
                status = "needs_review"
                note   = f"⚠️  `{node_id}` needs review — {ts}"
                if error_summary:
                    note += f"\n    Issue: {error_summary[:200]}"

            if matched_task:
                matched_task["status"] = status
                matched_task["dag_node_ids"] = list(
                    set(matched_task.get("dag_node_ids", []) + [node_id])
                )
                matched_task.setdefault("notes", []).append(note)
            else:
                # Unmatched node — append to phase as an untracked item
                ph.setdefault("untracked_nodes", []).append({
                    "node_id": node_id,
                    "status":  status,
                    "note":    note,
                })

            self._save_plan()
            self._append_node_to_markdown(ph.get("name", ""), node_id, description, note)

        except Exception as exc:
            logger.debug("📋  [Phase] on_node_completed error (non-fatal): %s", exc)

    async def check_and_advance_phase(self, planner, executor) -> bool:
        """
        Assesses if the current phase is complete and advances to the next.
        Returns True if we advanced to a new phase (so the DAG can be cleared).
        Called once after each full DAG drain.
        """
        if not self._plan or self._advance_lock.locked():
            return False
        async with self._advance_lock:
            try:
                return await self._assess_and_advance(planner, executor)
            except Exception as exc:
                logger.warning("📋  [Phase] check_and_advance_phase error (non-fatal): %s", exc)
                return False

    # ── Internal — Plan Creation ──────────────────────────────

    async def _create_initial_plan(self, goal: str) -> None:
        """
        Ask Gemini (via ask_gemini directly) to create the full phased plan,
        then write phase_state.json and project_plan.md in Python.

        V54 REWRITE: Previously used executor.execute_task() which runs Gemini
        inside a Docker sandbox — file writes went to the sandbox filesystem and
        were never synced back to the real project directory, so phase_state.json
        was never created and the stub always won.

        Now uses ask_gemini() directly (same pattern as decompose_epic) with a
        rich codebase context so phases are audit-aware.
        """
        logger.info("📋  [Phase] Creating initial project plan via ask_gemini …")
        if self._state:
            self._state.record_activity("system", "Creating phased project plan …")

        # ── Build rich codebase context (same approach as decompose_epic) ──────
        _project_context = ""
        try:
            _ws = Path(self._project_path)

            # ── Context notes — read DIRECTLY from authoritative source ──────────
            # CRITICAL: SUPERVISOR_MANDATE.md is 20,000+ chars; notes start at
            # char ~7,000+. The old [:4000] slice NEVER reached the notes.
            # Read context_notes.json directly — compact JSON, always complete.
            _cn_path = _ws / ".ag-supervisor" / "context_notes.json"
            if _cn_path.exists():
                try:
                    _notes_data = json.loads(_cn_path.read_text(encoding="utf-8"))
                    if _notes_data:
                        _notes_lines = [
                            "\n\n## PERSISTENT CONTEXT NOTES — USER-DEFINED HARD CONSTRAINTS\n"
                            "These notes are MANDATORY requirements that MUST shape every phase:\n\n"
                        ]
                        for _i, _n in enumerate(_notes_data, 1):
                            _notes_lines.append(f"{_i}. {_n['text']}\n\n")
                        _project_context += "".join(_notes_lines)
                        logger.info(
                            "📋  [Phase] Injected %d context note(s) directly from context_notes.json",
                            len(_notes_data),
                        )
                except Exception as _ne:
                    logger.debug("📋  [Phase] Could not read context notes: %s", _ne)

            # ── V69: research.md — use @-file reference instead of inline ─────────
            # Previously inlined 200K chars. The Gemini CLI reads @-referenced
            # files at its cwd, keeping the prompt lean.
            _research_path = _ws / "research.md"
            if not _research_path.exists():
                _research_path = _ws / "RESEARCH.md"
            if _research_path.exists():
                _project_context = (
                    "\n\nRESEARCH FINDINGS — AUTHORITATIVE DOMAIN RESEARCH\n"
                    "These findings represent deep research into the problem space. "
                    "Every generated phase and task MUST serve the ideal end-product described here.\n"
                    f"Read the full research document: @{_research_path.name}\n\n"
                ) + _project_context
                logger.info(
                    "📋  [Phase] Using @%s file reference (avoids inline bloat).",
                    _research_path.name,
                )


            # SUPERVISOR_MANDATE.md — just extract the mission/goal section (first 2000 chars)
            # Notes are already injected above directly from JSON — no need for full mandate.
            _mandate_path = _ws / "SUPERVISOR_MANDATE.md"
            if _mandate_path.exists():
                _mandate_text = _mandate_path.read_text(encoding="utf-8")
                # Extract only the YOUR MISSION section to avoid duplicating notes
                _mission_start = _mandate_text.find("## YOUR MISSION")
                _notes_cutoff = _mandate_text.find("\n\n## Persistent Context Notes")
                if _mission_start != -1:
                    _end = _notes_cutoff if _notes_cutoff != -1 else _mission_start + 2500
                    _mission_text = _mandate_text[_mission_start:_end].strip()
                else:
                    _mission_text = _mandate_text[:10000]
                _project_context += (
                    "\n\nPROJECT MISSION:\n" + _mission_text + "\n"
                )

            # V69: PROJECT_STATE.md — use @-file reference instead of inline
            # Previously inlined 50K chars.
            for _psname in ("PROJECT_STATE.md", ".ag-supervisor/PROGRESS.md"):
                _ps = _ws / _psname
                if _ps.exists():
                    _project_context += (
                        f"\n\nCURRENT PROJECT STATE ({_psname}):\n"
                        f"Read the full project state: @{_psname}\n"
                    )
                    break

            # V75: Smart file index — replaces old rglob + 100-file cap
            # Small repos: shows ALL files with exports (no cap)
            # Large repos (300+): compressed directory + signature view
            try:
                from .file_index import get_file_index
                _fidx = get_file_index(str(_ws))
                _tree = _fidx.get_tier1_context()
                if _tree:
                    _project_context += f"\n{_tree}\n"
            except Exception as _fidx_err:
                # Fallback to basic listing on any error
                logger.debug("📂  [Phase] FileIndex failed, using basic listing: %s", _fidx_err)
                _all_files = sorted(
                    str(f.relative_to(_ws))
                    for f in _ws.rglob("*")
                    if f.is_file()
                    and not any(skip in str(f) for skip in (
                        "node_modules", ".git", "__pycache__",
                        ".ag-supervisor", "dist", ".next", "coverage",
                    ))
                )
                if _all_files:
                    _project_context += (
                        "\nEXISTING FILES IN PROJECT:\n"
                        + "\n".join(f"  - {f}" for f in _all_files[:200]) + "\n"
                    )
                    if len(_all_files) > 200:
                        _project_context += f"  … and {len(_all_files) - 200} more files\n"

            # DAG completion history (what tasks have already been done)
            _hist = _ws / ".ag-supervisor" / "dag_history.jsonl"
            if _hist.exists():
                _prev_tasks: list[str] = []
                for _line in _hist.read_text(encoding="utf-8").strip().split("\n")[-5:]:
                    try:
                        _entry = json.loads(_line)
                        for _v in _entry.get("nodes", {}).values():
                            if _v.get("status") == "complete":
                                _prev_tasks.append(
                                    f"  [DONE] {_v['description'][:200]}"
                                )
                    except Exception:
                        pass
                if _prev_tasks:
                    _project_context += (
                        "\nPREVIOUSLY COMPLETED TASKS (from DAG history):\n"
                        + "\n".join(_prev_tasks[-40:]) + "\n"
                    )

            # Audit findings from the most recent auto-audit if available
            _audit_file = _ws / ".ag-supervisor" / "last_audit.md"
            if _audit_file.exists():
                _project_context += (
                    "\nLAST AUDIT FINDINGS (issues the system flagged):\n"
                    + _audit_file.read_text(encoding="utf-8")[:20000] + "\n"
                )
        except Exception as _ce:
            logger.debug("📋  [Phase] Context gather error (non-fatal): %s", _ce)

        # ── V54: Inject phase reset context if present ─────────────────────────
        _reset_ctx_path = self._plan_dir / "phase_reset_context.json"
        _prior_progress_section = ""
        if _reset_ctx_path.exists():
            try:
                _rctx = json.loads(_reset_ctx_path.read_text(encoding="utf-8"))
                _done = _rctx.get("completed_task_summaries", [])
                _pending = _rctx.get("pending_task_summaries", [])
                _is_fresh = _rctx.get("fresh_audit_also", False)

                _prior_progress_section = "\n\nPRIOR PROGRESS (from previous phase plan that is being reset):\n"
                if _done:
                    _prior_progress_section += (
                        f"The following {len(_done)} tasks were ALREADY COMPLETED — "
                        "do NOT include them as pending work in the new plan:\n"
                    )
                    for t in _done[:60]:
                        _prior_progress_section += f"  ✓ {t}\n"
                else:
                    _prior_progress_section += "  (No tasks were completed before the reset.)\n"

                if _pending and _is_fresh:
                    _prior_progress_section += (
                        f"\nThe following {len(_pending)} tasks were IN-FLIGHT when reset was triggered "
                        "(this is a Fresh Audit — re-evaluate whether they still apply after scanning the codebase):\n"
                    )
                    for t in _pending[:30]:
                        _prior_progress_section += f"  ⏳ {t}\n"
                elif _pending and not _is_fresh:
                    _prior_progress_section += (
                        f"\n{len(_pending)} tasks were PENDING. The new phase plan should decide "
                        "how to scope remaining work based on the current codebase state.\n"
                    )

                _prior_progress_section += (
                    "\nIMPORTANT: Account for the completed work above when building the new plan:\n"
                    "  1. Include already-completed tasks in the appropriate phase with status='done'\n"
                    "     so they are tracked. Do NOT omit them — they must appear in the plan as DONE.\n"
                    "  2. Scan the codebase to verify — if a task from the list is truly implemented,\n"
                    "     set its status to 'done'. If it looks incomplete, set status to 'pending'.\n"
                    "  3. Add NEW tasks for any remaining work not covered by the completed tasks.\n"
                    "  4. Determine the correct current_phase based on what's actually done.\n"
                )
                _reset_ctx_path.unlink(missing_ok=True)
                logger.info("📋  [Phase] Injecting reset context: %d done, %d pending tasks", len(_done), len(_pending))
            except Exception as _rce:
                logger.debug("📋  [Phase] Could not read reset context: %s", _rce)

        # ── Build prompt ────────────────────────────────────────────────────────
        gemini_prompt = (
            "You are a world-class senior technical architect creating a COMPLETE, COMPREHENSIVE "
            "implementation plan. This plan must cover EVERYTHING the project needs — not just "
            "the first wave of work. Think like an architect who has to guarantee the finished "
            "product will be production-ready, polished, and complete before a single line of code "
            "is written.\n\n"
            f"PROJECT GOAL:\n{goal}\n"
            f"{_prior_progress_section}"
            f"{_project_context}\n"
            "INSTRUCTIONS:\n"
            "1. ANALYSE EVERYTHING: Read the project goal AND all codebase context (files, completed "
            "tasks, audit findings, mandate, research, vision). The phases must reflect what is "
            "ACTUALLY needed in full — not a generic template, not just the obvious first steps.\n\n"
            "2. THINK COMPREHENSIVELY: Before writing a single phase, mentally enumerate the FULL "
            "scope of work. For every feature area, ask:\n"
            "   - What is the happy path? What are the error states? What are the edge cases?\n"
            "   - What does the UI/UX need — not just structure but polished micro-interactions, "
            "responsive layouts, loading/empty/error states, animations, dark mode?\n"
            "   - What integrations, data flows, and state management does it require?\n"
            "   - What will a Lighthouse/accessibility audit fail on? Plan to fix it.\n"
            "   - What security, auth, or performance concerns exist?\n"
            "   - What tests, validations, and quality gates are needed?\n\n"
            "3. DEFINE COMPLETE PHASES — each phase must contain ALL its tasks upfront:\n"
            "   - Do NOT use 'will be expanded later' as a cop-out. Every phase must list its real tasks now.\n"
            "   - Each phase = at least 15 specific, actionable task titles — as many as the phase genuinely requires.\n"
            "   - There is no upper cap on tasks per phase. A complex phase may need 30-50+.\n"
            "   - Phase count is determined by the project — no artificial cap. Use as many as needed.\n"
            "   - Phase names must be specific to THIS project (e.g. 'AI Chat Engine & Streaming', "
            "'3D Visualization Layer') — NOT generic labels ('Foundation', 'Polish').\n"
            "   - Greenfield projects: typically 5-8 phases. Partial projects: phases for what remains.\n\n"
            "4. COMPREHENSIVE COVERAGE MANDATE — every phase plan as a whole must address:\n"
            "   [FUNC]  All functional features, CRUD, business logic, API integrations\n"
            "   [UI]    All UI components with full visual polish, animations, micro-interactions\n"
            "   [DATA]  State management, data flows, persistence, caching\n"
            "   [ERR]   Error boundaries, loading states, empty states, offline handling\n"
            "   [PERF]  Lighthouse 100 optimisations: LCP, TBT, CLS, FCP, image formats\n"
            "   [A11Y]  WCAG 2.1 AA: ARIA, keyboard nav, focus management, screen reader support\n"
            "   [SEC]   Auth flows, input validation, CSP, secure headers\n"
            "   [QA]    TypeScript strictness, build/lint passing, integration tests\n\n"
            "5. EXIT CRITERIA: Every phase must have specific, measurable exit criteria. "
            "Include build commands and observable outcomes, not vague descriptions.\n\n"
            "OUTPUT: Respond with ONLY valid JSON matching this exact schema. "
            "No markdown, no explanation, no ```json fences — raw JSON only:\n"
            "{\n"
            '  "version": 1,\n'
            '  "goal": "...",\n'
            '  "created_at": <unix timestamp as integer>,\n'
            '  "current_phase": 1,\n'
            '  "total_phases_estimated": <N>,\n'
            '  "phases": [\n'
            "    {\n"
            '      "id": 1,\n'
            '      "name": "...",\n'
            '      "focus": "One sentence describing exactly what this phase delivers end-to-end.",\n'
            '      "status": "active",\n'
            '      "exit_criteria": "Specific measurable criteria: e.g. npm run build passes with 0 errors, '
            'all listed components render, tsc --noEmit clean, dev server starts.",\n'
            '      "tasks": [\n'
            '        { "id": "p1_t1", "title": "...", "status": "pending", "dag_node_ids": [], "notes": [] },\n'
            '        { "id": "p1_t2", "title": "...", "status": "done", "dag_node_ids": [], "notes": ["Already implemented"] }\n'
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "REMINDER: At least 15 task titles per phase, as many as genuinely needed — no upper limit. "
            "All phases must be fully planned now — no placeholders, no 'TBD', no 'will expand later'.\n"
        )

        # ── Call Gemini directly (3 attempts) ───────────────────────────────────
        raw = None
        try:
            from .gemini_advisor import ask_gemini
            for _attempt in range(1, 4):
                # V73: Bail immediately if stop was requested
                try:
                    from .gemini_advisor import _stop_requested as _pm_stop
                    if _pm_stop:
                        logger.info("📋  [Phase] Stop requested — aborting plan generation.")
                        break
                except ImportError:
                    pass
                try:
                    logger.info(
                        "📋  [Phase→Gemini] Phase plan attempt %d/3 (%d chars prompt)",
                        _attempt, len(gemini_prompt),
                    )
                    raw = await ask_gemini(gemini_prompt, timeout=120, cwd=self._project_path, model=config.GEMINI_FALLBACK_MODEL)
                    if raw and raw.strip() not in ("", "{}"):
                        logger.info(
                            "📋  [Phase←Gemini] Response received (%d chars): %.200s…",
                            len(raw), raw,
                        )
                        break
                    logger.warning("📋  [Phase] Gemini attempt %d/3 returned empty — retrying …", _attempt)
                    raw = None
                except Exception as _exc:
                    logger.warning("📋  [Phase] Gemini attempt %d/3 failed: %s", _attempt, _exc)
                    raw = None
                if _attempt < 3:
                    await asyncio.sleep(5 * _attempt)
        except Exception as _gimport_exc:
            logger.warning("📋  [Phase] ask_gemini import failed: %s", _gimport_exc)
            raw = None

        if not raw or raw.strip() in ("", "{}"):
            logger.warning("📋  [Phase] All Gemini attempts failed — using stub plan")
            self._create_stub_plan(goal)
            return

        # ── Parse JSON response ─────────────────────────────────────────────────
        try:
            import re
            _cleaned = re.sub(r"```json?\s*", "", raw)
            _cleaned = re.sub(r"```\s*", "", _cleaned).strip()
            plan_data = json.loads(_cleaned)
        except json.JSONDecodeError:
            # Try to extract JSON object from the response
            try:
                _match = re.search(r'\{.*\}', raw, re.DOTALL)
                if _match:
                    plan_data = json.loads(_match.group())
                else:
                    raise ValueError("No JSON object found in response")
            except Exception as _pe:
                logger.warning("📋  [Phase] JSON parse failed: %s — using stub", _pe)
                self._create_stub_plan(goal)
                return

        if not plan_data.get("phases"):
            logger.warning("📋  [Phase] Parsed plan has no phases — using stub")
            self._create_stub_plan(goal)
            return

        # ── Ensure required fields and timestamps ───────────────────────────────
        plan_data.setdefault("version", _SCHEMA_VERSION)
        plan_data.setdefault("goal", goal[:500])
        plan_data.setdefault("created_at", time.time())
        plan_data.setdefault("current_phase", 1)
        plan_data.setdefault("total_phases_estimated", len(plan_data["phases"]))

        # ── Write phase_state.json directly in Python (no sandbox involved) ─────
        try:
            self._plan_dir.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(plan_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("📋  [Phase] Wrote phase_state.json (%d phases)", len(plan_data["phases"]))
        except Exception as _we:
            logger.warning("📋  [Phase] Could not write phase_state.json: %s", _we)
            self._create_stub_plan(goal)
            return

        # ── Write project_plan.md (human-readable) ──────────────────────────────
        try:
            _md_lines = [
                f"# Project Plan\n",
                f"**Goal:** {goal[:200]}\n\n",
                "## Phase Overview\n",
                "| # | Phase | Focus | Status |\n",
                "|---|-------|-------|--------|\n",
            ]
            for ph in plan_data["phases"]:
                _md_lines.append(
                    f"| {ph.get('id','?')} | {ph.get('name','?')} | {ph.get('focus','?')} | {ph.get('status','?')} |\n"
                )
            for ph in plan_data["phases"]:
                _md_lines.append(f"\n## Phase {ph.get('id','?')}: {ph.get('name','?')}\n")
                _md_lines.append(f"**Focus:** {ph.get('focus','')}\n\n")
                _md_lines.append(f"**Exit Criteria:** {ph.get('exit_criteria','')}\n\n")
                for t in ph.get("tasks", []):
                    _chk = "x" if t.get("status") == "done" else " "
                    _md_lines.append(f"- [{_chk}] {t.get('title','?')}\n")
            (self._plan_dir / "project_plan.md").write_text(
                "".join(_md_lines), encoding="utf-8"
            )
        except Exception as _mde:
            logger.debug("📋  [Phase] project_plan.md write failed (non-fatal): %s", _mde)

        # ── Load into memory ────────────────────────────────────────────────────
        self._plan = plan_data
        self._init_done = True

        # ── Post-plan auto-mark: cross-reference fingerprint store + DAG history ──
        # After reset, Gemini may set some tasks as 'pending' even though the work
        # is done. Scan all pending tasks and mark matching ones as 'done'.
        _auto_marked = 0
        try:
            import hashlib as _hashlib
            _ws = Path(self._project_path)
            _fp_path = _ws / ".ag-supervisor" / "audit_done_fingerprints.json"
            _done_descs_lower: set[str] = set()
            if _fp_path.exists():
                _fp_data = json.loads(_fp_path.read_text(encoding="utf-8"))
                for d in _fp_data.get("descriptions", []):
                    _done_descs_lower.add(" ".join(d.lower().split())[:100])
            # Also gather completed DAG descriptions
            _hist = _ws / ".ag-supervisor" / "dag_history.jsonl"
            if _hist.exists():
                for _line in _hist.read_text(encoding="utf-8").strip().split("\n")[-10:]:
                    try:
                        _entry = json.loads(_line)
                        for _v in _entry.get("nodes", {}).values():
                            if _v.get("status") == "complete":
                                _d = _v.get("description", "")
                                _done_descs_lower.add(" ".join(_d.lower().split())[:100])
                    except Exception:
                        pass
            if _done_descs_lower:
                for ph in plan_data.get("phases", []):
                    for t in ph.get("tasks", []):
                        if t.get("status") == "done":
                            continue
                        _title_norm = " ".join(t.get("title", "").lower().split())[:100]
                        if _title_norm and _title_norm in _done_descs_lower:
                            t["status"] = "done"
                            t.setdefault("notes", []).append("Auto-marked done: matches completed work from prior session")
                            _auto_marked += 1
                if _auto_marked:
                    self._save_plan()
                    logger.info("📋  [Phase] Post-plan auto-mark: %d tasks matched prior work → done", _auto_marked)
        except Exception as _am_exc:
            logger.debug("📋  [Phase] Post-plan auto-mark error (non-fatal): %s", _am_exc)

        ph_count = len(plan_data["phases"])
        ph_name  = plan_data["phases"][0].get("name", "Phase 1")
        logger.info(
            "📋  [Phase] Plan created: %d phases, starting with '%s'",
            ph_count, ph_name,
        )
        if self._state:
            self._state.record_activity(
                "system",
                f"Project plan created: {ph_count} phases. Starting Phase 1: {ph_name}",
            )


    def _create_stub_plan(self, goal: str) -> None:
        """
        Minimal in-memory stub when Gemini's output couldn't be parsed.

        V54: This stub is PROVISIONAL — it is NOT saved to disk (or if it
        must be saved, it is tagged 'is_stub: true' so initialize() will
        delete it and re-run Gemini on the next boot). The stub only exists
        to keep the current DAG tick running without crashing.
        """
        self._plan = {
            "version":                _SCHEMA_VERSION,
            "is_stub":                True,   # ← sentinel: initialize() will retry Gemini
            "goal":                   goal[:300],
            "created_at":             time.time(),
            "current_phase":          1,
            "total_phases_estimated": 1,
            "phases": [
                {
                    "id":            1,
                    "name":          "Initial Phase (stub — re-planning pending)",
                    "focus":         "Gemini phase plan could not be parsed; will retry on next boot.",
                    "status":        "active",
                    "exit_criteria": "Phase plan successfully generated by Gemini.",
                    "tasks": [
                        {"id": "p1_t1", "title": "[Auto] Re-generate project phase plan",
                         "status": "pending", "dag_node_ids": [], "notes": []},
                    ],
                },
            ],
        }
        # Save to disk WITH the is_stub marker so initialize() detects and retries
        self._save_plan()
        # Also immediately delete so the NEXT initialize() call re-creates properly
        try:
            self._state_path.unlink(missing_ok=True)
        except Exception:
            pass
        self._init_done = True
        logger.warning(
            "📋  [Phase] Stub plan created (Gemini plan unavailable) — will retry on next boot"
        )

    # ── Internal — Phase Assessment & Advancement ─────────────

    async def _assess_and_advance(self, planner, executor) -> bool:
        """Core logic: assess completion, optionally advance."""
        ph = self.get_current_phase()
        if not ph:
            return False

        tasks   = ph.get("tasks", [])
        total   = len(tasks)
        done    = sum(1 for t in tasks if t.get("status") == "done")
        pct     = (done / total * 100) if total else 100

        logger.info(
            "📋  [Phase] Phase %d assessment: %d/%d tasks done (%.0f%%)",
            ph["id"], done, total, pct,
        )

        # Fast-path: clearly not done
        # V54: Raised from 60% → 80% so fuzzy-matched task completions can't
        # slip through without a proper Gemini assessment.
        if total > 3 and pct < 80:
            logger.info("📋  [Phase] Phase not complete yet (%.0f%% done) — continuing.", pct)
            self._update_md_timestamp(ph)
            return False

        # Ask Gemini to assess — it can see the actual code state
        phase_complete = await self._gemini_assess_phase(ph, done, total, executor)

        if not phase_complete:
            logger.info("📋  [Phase] Gemini: Phase %d not yet complete — continuing.", ph["id"])
            return False

        # Phase is done — advance
        current_id = int(ph["id"])  # Cast: Gemini may return "id" as a string
        ph["status"]       = "completed"
        ph["completed_at"] = time.time()
        logger.info("📋  [Phase] ✅ Phase %d complete! Planning Phase %d …", current_id, current_id + 1)
        if self._state:
            self._state.record_activity(
                "success",
                f"Phase {current_id} complete! Planning Phase {current_id + 1} …",
            )

        # Plan the next phase
        advanced = await self._plan_next_phase(current_id, executor)
        self._save_plan()
        return advanced

    async def _gemini_assess_phase(self, ph: dict, done: int, total: int, executor) -> bool:
        """
        Ask Gemini (via executor with full sandbox tool access) whether the phase is complete.

        V54 FIX: Original wrote YES/NO to a sandbox temp file that never synced.
        Now we ask Gemini to OUTPUT the answer in its final response text (which is
        captured in result.output), AND optionally write the file as a secondary check.
        This preserves full tool access (file reading, npm run build, etc.) while
        eliminating the file-sync dependency.
        """
        ph_name  = ph.get("name", "")
        criteria = ph.get("exit_criteria", "")
        prompt   = (
            f"Review the project at the current codebase state.\n"
            f"The active implementation phase is: '{ph_name}'\n"
            f"Exit criteria: {criteria}\n"
            f"Task progress: {done}/{total} tasks marked done in the plan.\n\n"
            f"ASSESSMENT CHECKLIST — check ALL before answering YES:\n"
            f"  1. Are all the phase's required source files actually written and correct (not stubs/placeholders)?\n"
            f"  2. Did `npm install` (or pip install) complete without errors?\n"
            f"     (REMINDER: you can and must run shell commands inside /workspace in the container)\n"
            f"  3. Does the dev server start without errors (`npm run dev`)?\n"
            f"  4. Does `npm run build` produce zero TypeScript/build errors?\n"
            f"  5. Are there any obvious bugs, broken imports, or missing wiring in the code?\n"
            f"  6. Are the exit criteria FULLY met — every clause, not just partially?\n\n"
            f"If ANY checklist item is false or uncertain, the answer is NO.\n"
            f"DO NOT say YES just because most tasks are checked off — read the actual code.\n\n"
            f"Your response MUST end with a single line containing only 'YES' or 'NO' (uppercase).\n"
            f"Also write that same word to `.ag-supervisor/phase_review_tmp.txt` for logging."
        )
        try:
            result = await asyncio.wait_for(
                executor.execute_task(prompt, timeout=120),
                timeout=150,
            )
            # Primary: parse YES/NO from output text (doesn't depend on sandbox file sync)
            if result and hasattr(result, "output") and result.output:
                import re
                _lines = [l.strip().upper() for l in result.output.strip().splitlines()]
                for _line in reversed(_lines):  # answer is at the end
                    if _line in ("YES", "NO"):
                        logger.info("📋  [Phase] Gemini phase assessment (stdout): %s", _line)
                        return _line == "YES"
                # Fallback: search anywhere in output
                _m = re.search(r'\b(YES|NO)\b', result.output.upper())
                if _m:
                    _ans = _m.group(1)
                    logger.info("📋  [Phase] Gemini phase assessment (regex): %s", _ans)
                    return _ans == "YES"
            # Secondary: check temp file (may or may not have synced)
            review_path = self._plan_dir / _REVIEW_FILE
            if review_path.exists():
                answer = review_path.read_text(encoding="utf-8").strip().upper()
                review_path.unlink(missing_ok=True)
                logger.info("📋  [Phase] Gemini phase assessment (file): %s", answer)
                return answer.startswith("YES")
        except Exception as exc:
            logger.debug("📋  [Phase] Gemini assessment unavailable: %s — using heuristic", exc)

        # Fallback heuristic: ≥ 80% done = complete
        return (done / total >= 0.80) if total else True


    async def _plan_next_phase(self, completed_phase_id: int, executor) -> bool:
        """
        Ask Gemini (via ask_gemini directly) to plan the next phase in detail.
        Dynamically adds new phases if the project needs them — no artificial cap.
        Returns True if a new phase was activated.

        V54 REWRITE: Previously used executor.execute_task() which writes files to
        the sandbox filesystem (never synced). Now uses ask_gemini() + Python writes.
        """
        phases = self._plan.get("phases", [])
        next_phase = next((p for p in phases if p.get("id") == completed_phase_id + 1), None)

        # Build context: what's done, current plan
        done_summary = "\n".join(
            f"  - Phase {p['id']} ({p['name']}): {p.get('focus', '')}"
            for p in phases if p.get("status") == "completed"
        )
        upcoming_summary = "\n".join(
            f"  - Phase {p['id']} ({p['name']}): {p.get('focus', '')} [{p.get('status','pending')}]"
            for p in phases if p.get("status") != "completed"
        )

        # Read SUPERVISOR_MANDATE.md fresh so context notes are always included
        _mandate_live = ""
        try:
            _mandate_path = Path(self._project_path) / "SUPERVISOR_MANDATE.md"
            if _mandate_path.exists():
                _mandate_live = _mandate_path.read_text(encoding="utf-8")[:8000]
        except Exception:
            pass

        plan_prompt = (
            f"Phase {completed_phase_id} of the project is now complete. "
            f"Plan Phase {completed_phase_id + 1} in full detail AND comprehensively update all remaining phases.\n\n"
            + (f"SUPERVISOR MANDATE (goal + all persistent context notes — MUST be respected):\n{_mandate_live}\n\n" if _mandate_live else "")
            + f"PROJECT GOAL:\n{self._plan.get('goal', '')}\n\n"
            f"COMPLETED PHASES:\n{done_summary or '  (none)'}\n\n"
            f"REMAINING PLAN (must be fully updated — not just the next phase):\n{upcoming_summary or '  (none — you may need to add phases)'}\n\n"
            f"INSTRUCTIONS:\n"
            f"1. Scan the current codebase: what was ACTUALLY built in Phase {completed_phase_id}? "
            f"   Identify anything missing — those gaps belong in Phase {completed_phase_id + 1}.\n"
            f"2. Plan Phase {completed_phase_id + 1} COMPREHENSIVELY with at least 15 specific, detailed task titles — "
            f"   as many as the phase genuinely requires (a complex phase may need 30-50+), covering:\n"
            f"   [FUNC]  All functional features, carryover gaps from Phase {completed_phase_id}, integrations\n"
            f"   [UI]    Full visual polish, animations, micro-interactions, responsive layouts\n"
            f"   [DATA]  State management, data flows, caching, persistence\n"
            f"   [ERR]   Error boundaries, loading/empty/offline states\n"
            f"   [PERF]  Lighthouse optimisations: LCP, TBT, CLS, image formats\n"
            f"   [A11Y]  WCAG 2.1 AA: ARIA, keyboard nav, screen reader support\n"
            f"   [SEC]   Auth, input validation, CSP, secure headers\n"
            f"   [QA]    TypeScript strictness, build/lint passing, integration tests\n\n"
            f"3. ALSO update ALL remaining future phases — each must have at least 15 task titles now, "
            f"   as many as genuinely required. Do NOT leave future phases with 5-10 placeholder titles.\n"
            f"4. If the project needs MORE phases beyond the current plan, ADD THEM. No phase count cap.\n"
            f"5. If Phase {completed_phase_id + 1} IS the final phase, include one task titled exactly 'FINAL_PHASE_MARKER'.\n\n"
            f"Respond with ONLY valid JSON — no markdown fences, no explanation. Schema:\n"
            f"{{\n"
            f'  "current_phase": {completed_phase_id + 1},\n'
            f'  "phases": [ /* ALL phases: completed + active + all future — fully planned */ ]\n'
            f"}}\n"
            f"Each phase: id, name, focus, status, exit_criteria, tasks.\n"
            f"Completed: status='completed'. Active: status='active'. Future: status='planned'.\n"
            f"Task objects: {{\"id\": \"p{completed_phase_id+1}_t1\", \"title\": \"...\", \"status\": \"pending\", \"dag_node_ids\": [], \"notes\": []}}\n"
            f"REMINDER: At least 15 task titles per phase, as many as genuinely needed — no upper limit. No placeholders, no 'TBD', no 'will expand later'."
        )

        # Call ask_gemini directly (3 attempts)
        raw = None
        try:
            from .gemini_advisor import ask_gemini
            for _attempt in range(1, 4):
                # V73: Bail immediately if stop was requested
                try:
                    from .gemini_advisor import _stop_requested as _pm_stop
                    if _pm_stop:
                        logger.info("📋  [Phase] Stop requested — aborting next-phase planning.")
                        break
                except ImportError:
                    pass
                try:
                    logger.info("📋  [Phase→Gemini] Next-phase plan attempt %d/3", _attempt)
                    raw = await ask_gemini(plan_prompt, timeout=180, model=config.GEMINI_FALLBACK_MODEL)
                    if raw and raw.strip() not in ("", "{}"):
                        break
                    raw = None
                except Exception as _exc:
                    logger.warning("📋  [Phase] Next-phase attempt %d/3 failed: %s", _attempt, _exc)
                    raw = None
                if _attempt < 3:
                    await asyncio.sleep(5 * _attempt)
        except Exception as _gimp:
            logger.warning("📋  [Phase] ask_gemini import failed: %s", _gimp)
            raw = None

        if raw:
            try:
                import re
                _cleaned = re.sub(r"```json?\s*", "", raw)
                _cleaned = re.sub(r"```\s*", "", _cleaned).strip()
                update = json.loads(_cleaned)
            except json.JSONDecodeError:
                try:
                    import re
                    _m = re.search(r'\{.*\}', raw, re.DOTALL)
                    update = json.loads(_m.group()) if _m else None
                except Exception:
                    update = None

            if update and update.get("phases"):
                # Merge into existing plan
                self._plan["phases"] = update["phases"]
                self._plan["current_phase"] = update.get("current_phase", completed_phase_id + 1)
                self._plan["total_phases_estimated"] = len(update["phases"])
                self._plan_dir.mkdir(parents=True, exist_ok=True)
                self._state_path.write_text(
                    json.dumps(self._plan, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                new_phase = self.get_current_phase()
                if new_phase:
                    is_final = any(
                        t.get("title") == "FINAL_PHASE_MARKER"
                        for t in new_phase.get("tasks", [])
                    )
                    if is_final:
                        logger.info("📋  [Phase] Gemini declared project complete after Phase %d.", completed_phase_id)
                        if self._state:
                            self._state.record_activity("success", "🎉 Project plan complete — all phases done!")
                        return False
                    logger.info(
                        "📋  [Phase] Advanced to Phase %d: %s (%d total phases)",
                        new_phase["id"], new_phase.get("name", ""), len(update["phases"]),
                    )
                    if self._state:
                        self._state.record_activity(
                            "system",
                            f"Phase {completed_phase_id} complete → Phase {new_phase['id']}: {new_phase.get('name', '')}",
                        )
                    return True
            else:
                logger.warning("📋  [Phase] Next-phase JSON parse failed — using fallback")

        # Fallback: activate the pre-existing next phase stub
        if next_phase:
            next_phase["status"] = "active"
            self._plan["current_phase"] = next_phase["id"]
            self._save_plan()
            logger.info("📋  [Phase] Activated Phase %d (stub fallback).", next_phase["id"])
            return True
        logger.info("📋  [Phase] No next phase — project plan complete.")
        return False


    def record_audit_tasks(self, task_items: list[dict]) -> None:
        """
        Called by _audit_completed_work after injecting tasks into the DAG.
        Appends each injected audit task as a pending item on the current phase
        so the Phases panel reflects the extra work Gemini discovered.

        task_items: list of {"id": dag_node_id, "description": str} dicts.

        Never raises — errors are swallowed so audit injection is never blocked.
        """
        if not self._plan or not task_items:
            return
        try:
            ph = self.get_current_phase()
            if not ph:
                return
            existing_dag_ids = {
                nid
                for t in ph.get("tasks", [])
                for nid in t.get("dag_node_ids", [])
            }
            # Bug-fix: also dedup by title (case-insensitive, first 100 chars)
            existing_titles = {
                t.get("title", "")[:100].strip().lower()
                for t in ph.get("tasks", [])
            }
            added = 0
            for item in task_items:
                dag_id = item.get("id", "")
                desc   = item.get("description", "").strip()
                if not desc or dag_id in existing_dag_ids:
                    continue
                # Derive a short title (strip '[Audit Fix] ' prefix if present)
                title = desc.removeprefix("[Audit Fix] ")
                # Title dedup — skip if a task with matching title already exists
                _title_key = title[:100].strip().lower()
                if _title_key in existing_titles:
                    logger.debug(
                        "📋  [Phase] record_audit: skipping duplicate title: %s",
                        title[:60],
                    )
                    continue
                _tsid = f"audit_{dag_id}"
                ph.setdefault("tasks", []).append({
                    "id":          _tsid,
                    "title":       title,
                    "status":      "pending",
                    "dag_node_ids": [dag_id],
                    "notes":       [f"Discovered by post-completion audit"],
                    "source":      "audit",
                })
                existing_titles.add(_title_key)  # prevent within-batch dupes
                added += 1
            if added:
                self._save_plan()
                logger.info(
                    "📋  [Phase] Added %d audit-discovered task(s) to Phase %d task list.",
                    added, ph.get("id", "?"),
                )
                if self._state:
                    self._state.record_activity(
                        "system",
                        f"Phase {ph.get('id','?')}: {added} new task(s) added from audit scan",
                    )
        except Exception as exc:
            logger.debug("📋  [Phase] record_audit_tasks error (non-fatal): %s", exc)

    def sync_completion_from_dag(self, planner) -> None:
        """
        Sync DAG node completion status back to phase tasks.

        Iterates ALL phases' tasks (not just the current phase) and
        checks if all their dag_node_ids are 'complete' in the planner.
        If so, marks the phase task as 'done'. This prevents the audit
        from seeing stale 'pending' phase tasks that were actually
        completed — especially important for cross-phase DAGs where
        nodes from earlier/later phases complete in the same run.

        Never raises — best-effort sync.
        """
        if not self._plan or planner is None:
            return
        try:
            _synced = 0
            for ph in self._plan.get("phases", []):
                if ph.get("status") == "completed":
                    continue  # Already fully done — skip for performance
                for task in ph.get("tasks", []):
                    if task.get("status") == "done":
                        continue
                    dag_ids = task.get("dag_node_ids", [])
                    if not dag_ids:
                        continue
                    # Check if ALL linked DAG nodes are complete
                    all_done = all(
                        getattr(planner._nodes.get(nid), 'status', None) == 'complete'
                        for nid in dag_ids
                    )
                    if all_done:
                        task["status"] = "done"
                        _synced += 1
            if _synced:
                self._save_plan()
                logger.info(
                    "📋  [Phase] Synced %d phase task(s) to 'done' from DAG completion.",
                    _synced,
                )
        except Exception as exc:
            logger.debug("📋  [Phase] sync_completion_from_dag error: %s", exc)

    def get_incomplete_phases(self) -> list[dict]:
        """
        Return details about every phase that has incomplete/pending tasks.

        Each returned dict has: phase_id, phase_name, pending_count,
        total_count, exit_criteria, pending_tasks (list of task titles).

        Used by the final completion gate to decide whether the project
        is genuinely done before declaring session_complete.
        """
        result: list[dict] = []
        if not self._plan:
            return result
        for ph in self._plan.get("phases", []):
            tasks = ph.get("tasks", [])
            pending = [t for t in tasks if t.get("status") != "done"]
            if pending:
                result.append({
                    "phase_id": ph.get("id", "?"),
                    "phase_name": ph.get("name", ""),
                    "pending_count": len(pending),
                    "total_count": len(tasks),
                    "exit_criteria": ph.get("exit_criteria", ""),
                    "pending_tasks": [t.get("title", "") for t in pending],
                })
        return result

    def is_project_complete(self) -> bool:
        """
        Return True only if EVERY phase has all tasks marked 'done'.

        This is the gate check before declaring session_complete.
        A project is NOT complete if any phase has any task whose
        status is not 'done'.
        """
        if not self._plan:
            return False
        phases = self._plan.get("phases", [])
        if not phases:
            return False
        for ph in phases:
            tasks = ph.get("tasks", [])
            if not tasks:
                continue  # empty phase = vacuously complete
            if any(t.get("status") != "done" for t in tasks):
                return False
        return True

    def get_all_phases_summary_for_verification(self) -> str:
        """
        Build a comprehensive summary of ALL phases with their exit criteria
        and task completion status, formatted for the final Gemini verification
        audit prompt.
        """
        if not self._plan:
            return ""
        sections: list[str] = []
        phases = self._plan.get("phases", [])
        for ph in phases:
            ph_id = ph.get("id", "?")
            ph_name = ph.get("name", "")
            criteria = ph.get("exit_criteria", "N/A")
            tasks = ph.get("tasks", [])
            done = sum(1 for t in tasks if t.get("status") == "done")
            total = len(tasks)
            sections.append(
                f"Phase {ph_id}: \"{ph_name}\"\n"
                f"  Tasks: {done}/{total} done\n"
                f"  Exit Criteria: {criteria}\n"
            )
        return "\n".join(sections)

    # ── Internal — Matching & File I/O ────────────────────────

    def _match_task(self, phase: dict, node_description: str) -> dict | None:
        """
        Find the best-matching pending phase task for a DAG node description.
        Uses word-overlap scoring — no external fuzzy library needed.
        """
        desc_words = set(node_description.lower().split())
        best_score = 0
        best_task  = None

        for task in phase.get("tasks", []):
            if task.get("status") == "done":
                continue
            task_words = set(task.get("title", "").lower().split())
            # Remove common stopwords
            stopwords  = {"a","an","the","and","or","in","of","to","for","with","on","at","is","it","by"}
            desc_clean = desc_words - stopwords
            task_clean = task_words - stopwords
            if not task_clean:
                continue
            overlap = len(desc_clean & task_clean)
            score   = overlap / max(len(task_clean), 1)
            if score > best_score:
                best_score = score
                best_task  = task

        # Require at least 30% word overlap to count as a match
        return best_task if best_score >= 0.30 else None

    def _load_plan(self) -> None:
        raw        = self._state_path.read_text(encoding="utf-8")
        self._plan = json.loads(raw)
        # Clamp current_phase to [1, len(phases)] so a stale value from a
        # previous larger plan (e.g. current_phase=6 with only 2 phases now)
        # doesn't produce nonsensical "6/2" displays.
        _phases = self._plan.get("phases", [])
        if _phases:
            _cp = self._plan.get("current_phase", 1)
            if _cp < 1 or _cp > len(_phases):
                self._plan["current_phase"] = max(1, min(_cp, len(_phases)))

    def _save_plan(self) -> None:
        self._plan_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._plan, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._state_path)  # atomic rename

    def force_save(self) -> None:
        """Force-persist phase state immediately (use in shutdown/atexit handlers)."""
        try:
            self._save_plan()
        except Exception as _fse:
            pass  # Best-effort — don't raise during shutdown


    def _append_node_to_markdown(
        self, phase_name: str, node_id: str, description: str, note: str
    ) -> None:
        """Append a concise node-completion note to project_plan.md."""
        try:
            entry = f"\n> {note}  \n> *Node `{node_id}`:* {description}\n"
            with open(self._md_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass

    def _update_md_timestamp(self, ph: dict) -> None:
        """Append a brief phase-progress line to the markdown."""
        try:
            tasks  = ph.get("tasks", [])
            done   = sum(1 for t in tasks if t.get("status") == "done")
            ts     = time.strftime("%Y-%m-%d %H:%M")
            entry  = (
                f"\n*[{ts}] Phase {ph['id']} progress: {done}/{len(tasks)} tasks done — "
                f"phase not yet complete, continuing …*\n"
            )
            with open(self._md_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass
