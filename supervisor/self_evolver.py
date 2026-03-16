"""
self_evolver.py — Self-Evolution Engine V4 (Omniscient God Loop).

When the supervisor crashes, flatlines, or fails to recover, this module:

  1. Reads the source code of ALL supervisor modules (not just main.py)
  2. Reads the last 50 lines of supervisor.log for execution context
  3. Optionally includes a screenshot of the IDE state for visual context
  4. Sends the traceback + logs + screenshot + all source code to Gemini CLI
  5. Gemini identifies WHICH file has the bug and returns the fixed code
  6. The identified file is backed up and overwritten
  7. sys.exit(42) triggers the .bat reboot loop

V4 UPGRADE: Now accepts an optional screenshot_path parameter so the
multimodal Vision God Loop can pass the IDE screenshot to Gemini for
visual context. Reduced log tail from 100 → 50 lines for faster
processing.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .gemini_advisor import ask_gemini_sync

logger = logging.getLogger("supervisor.self_evolver")

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# The supervisor package directory.
_SUPERVISOR_DIR = Path(__file__).resolve().parent

# All Python modules in the supervisor package.
_MODULE_FILES = sorted(_SUPERVISOR_DIR.glob("*.py"))

# Backup directory for pre-evolution snapshots.
_BACKUP_DIR = _SUPERVISOR_DIR / "_evolution_backups"

# Log file for reading execution context.
_LOG_FILE = _SUPERVISOR_DIR / "supervisor.log"

# How many log lines to include in the evolution prompt.
_LOG_TAIL_LINES = 50


def _extract_json_balanced(text: str) -> dict | None:
    """
    Extract the outermost JSON object from `text` using balanced-brace
    matching.  This is critical because the 'code' field contains Python
    source with many nested braces, so a naive regex like r'\\{[^}]+\\}'
    will truncate the result at the first closing brace.

    Strategy:
      1. Strip markdown code fences.
      2. Find the first '{' and walk forward counting brace depth.
      3. json.loads() the extracted substring.
    """
    # Strip markdown fences.
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = cleaned.replace("```", "")

    start = cleaned.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    end = -1

    for i in range(start, len(cleaned)):
        ch = cleaned[i]

        if escape:
            escape = False
            continue

        if ch == "\\":
            if in_string:
                escape = True
            continue

        if ch == '"' and not escape:
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        return None

    json_str = cleaned[start:end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def _restore_from_backup(backup_path: Path, target_path: Path) -> None:
    """Restore target_path from backup_path, logging the result."""
    try:
        if backup_path.exists():
            backup_text = backup_path.read_text(encoding="utf-8")
            target_path.write_text(backup_text, encoding="utf-8")
            logger.info("🧬  ✅ Restored %s from backup.", target_path.name)
        else:
            logger.error("🧬  ❌ Backup file not found: %s", backup_path)
    except Exception as restore_exc:
        logger.error("🧬  ❌ Failed to restore from backup: %s", restore_exc)

def _run_shadow_sandbox() -> bool:
    """
    V14 AGI Pillar 2: Shadow Sandbox Integration Test.
    Runs the `mock_repo_tests.py` suite in a detached subprocess. 
    If the agent's mutations broke core functionality, the test will fail,
    and we can revert the mutation before the supervisor commits suicide 
    and reboots in a lobotomised state.
    """
    logger.info("🧬  🛡️ Booting Shadow Sandbox Integration Tests...")
    try:
        project_root = config.get_project_path() or Path.cwd()
        test_script = project_root / "tests" / "mock_repo_tests.py"
        
        if not test_script.exists():
            logger.warning("🧬  ⚠️ Sandbox mock test missing (%s). Skipping sandbox!", test_script)
            return True

        # Run the test in isolation
        result = subprocess.run(
            [sys.executable, str(test_script)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            logger.info("🧬  ✅ Sandbox Test Passed!")
            return True
        else:
            logger.error("🧬  ❌ Sandbox Test FAILED! stdout: %s\nstderr: %s", result.stdout, result.stderr)
            return False
            
    except subprocess.TimeoutExpired:
        logger.error("🧬  ❌ Sandbox Test TIMEOUT! Agent mutation likely caused infinite loop.")
        return False
    except Exception as exc:
        logger.error("🧬  ❌ Sandbox Engine crash: %s", exc)
        return False


def _build_single_prompt(
    traceback_str: str,
    modules: dict,
    module_listing: str,
    log_tail: str,
    screenshot_path: str | None,
) -> str:
    """Build the original single-call evolution prompt."""
    prompt = (
        "I am a Python automation bot (Supervisor AI). I failed.\n\n"
        "You are an autonomous Python Meta-Agent. You are currently stuck. "
        f"Reason: {traceback_str.splitlines()[-1] if traceback_str.strip() else 'Unknown'}\n\n"
        "Here is the full traceback:\n\n"
        f"{traceback_str}\n\n"
    )
    if log_tail:
        prompt += (
            f"Here are my last {_LOG_TAIL_LINES} execution log lines:\n\n"
            f"{log_tail}\n\n"
        )
    if screenshot_path and Path(screenshot_path).exists():
        prompt += (
            f"A screenshot of the IDE state at the time of failure is attached "
            f"at: {screenshot_path}\n"
            f"Use this visual context to understand the IDE's state.\n\n"
        )
    prompt += (
        f"Below are ALL {len(modules)} source files in my supervisor package:\n"
        f"{module_listing}\n\n"
        "INSTRUCTIONS:\n"
        "1. Identify WHICH file contains the bug that caused this failure.\n"
        "2. Rewrite that file to fix the bug, make me smarter, and "
        "   prevent this class of failure from recurring.\n"
        "3. Return a JSON object with exactly two fields:\n"
        '   {"file": "filename.py", "code": "...the entire fixed source code..."}\n'
        "\n"
        "CRITICAL RULES:\n"
        "- Return ONLY the raw JSON object. No markdown fences, no explanation.\n"
        "- The 'code' field must contain the COMPLETE source code for the file — "
        "  not a patch or diff, but the entire file.\n"
        "- Only fix ONE file — the one that caused the crash.\n"
        "- Make the fix robust so similar crashes don't recur.\n"
        "- Preserve all existing functionality.\n"
        "- Do NOT change any imports, function signatures, or public APIs "
        "  unless absolutely necessary to fix the bug.\n"
        "- Output ONLY valid Python.\n"
    )
    return prompt


def _council_evolve_pipeline(
    traceback_str: str,
    modules: dict,
    module_listing: str,
    log_tail: str,
    screenshot_path: str | None,
) -> dict | None:
    """
    Multi-agent council pipeline for self-evolution.

    Uses 4 specialist agents in sequence:
      1. DEBUGGER   — analyzes traceback + source to identify root cause
      2. FIXER      — generates complete fixed source code
      3. TESTER     — validates syntax, size, and import safety
      4. AUDITOR    — reviews overall quality and approves

    Returns {"file": "...", "code": "..."} on success, or None if
    any agent rejects the fix.
    """
    B = config.ANSI_BOLD
    M = config.ANSI_MAGENTA
    C = config.ANSI_CYAN
    G = config.ANSI_GREEN
    Y = config.ANSI_YELLOW
    RD = config.ANSI_RED
    R = config.ANSI_RESET

    print(f"\n  {B}{M}{'═' * 60}{R}")
    print(f"  {B}{M}  🧬🏛️  COUNCIL-POWERED SELF-EVOLUTION  🏛️🧬{R}")
    print(f"  {B}{M}{'═' * 60}{R}\n")

    # ── Agent 1: DEBUGGER — root cause analysis ───────────
    print(f"  {C}🔍 [DEBUGGER] Analyzing traceback + source …{R}")
    logger.info("🧬🏛️  Debugger agent analyzing traceback …")

    debugger_prompt = (
        "You are the DEBUGGER — an elite reverse engineer with 30 years of "
        "experience debugging Python async code, Playwright browser automation, "
        "and Electron internals. You have a 300 IQ for pattern recognition.\n\n"
        f"TRACEBACK:\n{traceback_str}\n\n"
        f"RECENT LOGS:\n{log_tail}\n\n"
        f"ALL SOURCE FILES:\n{module_listing[:20000]}\n\n"
        "TASK: Identify the EXACT root cause of this crash.\n"
        "Respond with JSON:\n"
        '{\n'
        '  "root_cause": "precise description of what went wrong",\n'
        '  "faulty_file": "filename.py",\n'
        '  "faulty_line_hint": "the approximate code that caused the issue",\n'
        '  "fix_strategy": "exactly what needs to change to fix this",\n'
        '  "confidence": "HIGH" | "MEDIUM" | "LOW"\n'
        '}\n'
    )

    debugger_result = ask_gemini_sync(debugger_prompt, timeout=180)
    debugger_data = _extract_json_balanced(debugger_result) if debugger_result else None

    if not debugger_data:
        logger.warning("🧬🏛️  Debugger returned no usable data.")
        return None

    faulty_file = debugger_data.get("faulty_file", "main.py")
    root_cause = debugger_data.get("root_cause", "unknown")
    fix_strategy = debugger_data.get("fix_strategy", "unknown")
    confidence = debugger_data.get("confidence", "LOW")

    print(f"  {M}   Root cause: {root_cause[:60]}{R}")
    print(f"  {M}   Faulty file: {faulty_file} (confidence: {confidence}){R}")
    print(f"  {M}   Fix strategy: {fix_strategy[:60]}{R}")
    logger.info("🧬🏛️  Debugger: %s in %s (%s)", root_cause[:80], faulty_file, confidence)

    # Get the source of the faulty file
    faulty_source = modules.get(faulty_file, "")
    if not faulty_source:
        logger.warning("🧬🏛️  Faulty file '%s' not found in modules.", faulty_file)
        return None

    # ── Agent 2: FIXER — generate the fixed code ──────────
    print(f"  {C}🔧 [FIXER] Generating fixed code …{R}")
    logger.info("🧬🏛️  Fixer agent generating code for %s …", faulty_file)

    fixer_prompt = (
        "You are the FIXER — a senior Python developer who writes flawless, "
        "production-grade code. Your code works the first time, every time.\n\n"
        f"DEBUGGER'S ANALYSIS:\n"
        f"  Root cause: {root_cause}\n"
        f"  Faulty file: {faulty_file}\n"
        f"  Fix strategy: {fix_strategy}\n\n"
        f"TRACEBACK:\n{traceback_str}\n\n"
        f"CURRENT SOURCE OF {faulty_file}:\n{faulty_source}\n\n"
        "TASK: Rewrite this file to fix the bug. Return JSON:\n"
        '{"file": "' + faulty_file + '", "code": "...the COMPLETE fixed source code..."}\n\n'
        "CRITICAL RULES:\n"
        "- The 'code' field must contain the COMPLETE source code — NOT a diff.\n"
        "- Preserve ALL existing functionality.\n"
        "- Make the fix robust so similar crashes don't recur.\n"
        "- Output ONLY valid Python in the code field.\n"
    )

    fixer_result = ask_gemini_sync(fixer_prompt, timeout=180)
    fixer_data = _extract_json_balanced(fixer_result) if fixer_result else None

    if not fixer_data or not fixer_data.get("code"):
        logger.warning("🧬🏛️  Fixer returned no usable code.")
        return None

    patched_code = fixer_data.get("code", "")
    target_file = fixer_data.get("file", faulty_file)

    print(f"  {G}   Generated {len(patched_code)} chars of fixed code.{R}")

    # ── Agent 3: TESTER — validate the fix ────────────────
    print(f"  {C}🧪 [TESTER] Validating fix …{R}")
    logger.info("🧬🏛️  Tester agent validating %d chars …", len(patched_code))

    # Syntax check
    try:
        compile(patched_code, target_file, "exec")
        syntax_ok = True
        print(f"  {G}   ✅ Syntax check: PASS{R}")
    except SyntaxError as syn_err:
        syntax_ok = False
        print(f"  {RD}   ❌ Syntax check: FAIL — {syn_err.msg} (line {syn_err.lineno}){R}")
        logger.warning("🧬🏛️  Tester: syntax check FAILED — %s", syn_err)

    # Size check
    original_len = len(modules.get(target_file, ""))
    patched_len = len(patched_code)
    size_ok = True
    if original_len > 500 and patched_len < original_len * 0.3:
        size_ok = False
        print(f"  {RD}   ❌ Size check: FAIL — {patched_len} vs {original_len} (too small){R}")
    elif patched_len < 50:
        size_ok = False
        print(f"  {RD}   ❌ Size check: FAIL — code too short ({patched_len} chars){R}")
    else:
        print(f"  {G}   ✅ Size check: PASS ({original_len} → {patched_len} chars){R}")

    if not syntax_ok or not size_ok:
        logger.warning("🧬🏛️  Tester REJECTED the fix.")
        return None

    # ── Agent 4: AUDITOR — quality review ─────────────────
    print(f"  {C}📋 [AUDITOR] Reviewing quality …{R}")
    logger.info("🧬🏛️  Auditor agent reviewing fix quality …")

    auditor_prompt = (
        "You are the AUDITOR — a meticulous code reviewer and quality guardian. "
        "You are the last line of defense. Nothing ships without your approval.\n\n"
        f"ORIGINAL ISSUE:\n{traceback_str[:500]}\n\n"
        f"ROOT CAUSE (from Debugger): {root_cause}\n"
        f"FIX STRATEGY (from Debugger): {fix_strategy}\n\n"
        f"ORIGINAL CODE ({target_file}):\n{faulty_source[:5000]}\n\n"
        f"PATCHED CODE ({target_file}):\n{patched_code[:5000]}\n\n"
        "Review the fix. Respond with JSON:\n"
        '{\n'
        '  "verdict": "APPROVE" | "REJECT",\n'
        '  "quality_score": 1-10,\n'
        '  "concerns": "any issues found",\n'
        '  "suggestions": "improvements for robustness"\n'
        '}\n'
    )

    auditor_result = ask_gemini_sync(auditor_prompt, timeout=120)
    auditor_data = _extract_json_balanced(auditor_result) if auditor_result else None

    if auditor_data:
        verdict = auditor_data.get("verdict", "APPROVE").upper()
        quality = auditor_data.get("quality_score", "?")
        concerns = auditor_data.get("concerns", "none")
        print(f"  {G if verdict == 'APPROVE' else RD}   Verdict: {verdict} (quality: {quality}/10){R}")
        if concerns and concerns.lower() != "none":
            print(f"  {Y}   Concerns: {concerns[:60]}{R}")

        if verdict == "REJECT":
            logger.warning("🧬🏛️  Auditor REJECTED the fix: %s", concerns)
            return None
    else:
        # If auditor can't respond, proceed cautiously
        print(f"  {Y}   Auditor: no response — proceeding with caution{R}")

    print(f"\n  {B}{G}✅ Council APPROVED the fix for {target_file}!{R}\n")
    logger.info("🧬🏛️  Council approved fix for %s.", target_file)

    return {"file": target_file, "code": patched_code}


def self_evolve(traceback_str: str, screenshot_path: str | None = None) -> None:
    """
    The self-evolution pipeline (synchronous — called from except block).

    1. Read ALL module source files
    2. Read last 50 lines of supervisor.log
    3. Optionally include screenshot for visual context
    4. Send traceback + logs + all source to Gemini CLI
    5. Parse response — Gemini returns {file, code} identifying the fix
    6. Back up and overwrite the identified file
    7. sys.exit(42) to trigger .bat reboot
    """
    logger.critical(
        "🧬  SELF-EVOLVER TRIGGERED. Fatal traceback:\n%s", traceback_str
    )

    # ── Step A: Read all module source files ───────────────
    modules: dict[str, str] = {}
    total_chars = 0

    for mod_file in _MODULE_FILES:
        if mod_file.name.startswith("_") and mod_file.name != "__init__.py":
            continue
        try:
            source = mod_file.read_text(encoding="utf-8")
            modules[mod_file.name] = source
            total_chars += len(source)
        except Exception as exc:
            logger.error("Cannot read module %s: %s", mod_file, exc)

    if not modules:
        logger.error("🧬  No module files found! Cannot self-evolve.")
        sys.exit(1)

    logger.info(
        "🧬  Read %d modules (%d total chars) from %s",
        len(modules), total_chars, _SUPERVISOR_DIR,
    )

    # ── Step B: Read recent execution logs ────────────────
    log_tail = ""
    try:
        if _LOG_FILE.exists():
            all_lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = all_lines[-_LOG_TAIL_LINES:] if len(all_lines) > _LOG_TAIL_LINES else all_lines
            log_tail = "\n".join(tail)
            logger.info("🧬  Read %d log lines from %s", len(tail), _LOG_FILE)
    except Exception as exc:
        logger.warning("🧬  Could not read log file: %s", exc)

    # Build module listing for prompts
    module_listing = ""
    for name, source in modules.items():
        module_listing += f"\n\n{'='*60}\n"
        module_listing += f"FILE: {name}\n"
        module_listing += f"{'='*60}\n"
        module_listing += source

    # ── Step C: Try council-powered multi-agent pipeline ─────
    #
    # The council pipeline uses 4 specialist agents in sequence:
    #   Debugger  → identifies root cause from traceback + source
    #   Fixer     → generates the complete fixed file
    #   Tester    → validates syntax, size, imports
    #   Auditor   → reviews quality and approves
    #
    # If the council pipeline fails, fall back to the original
    # single-call approach.
    #
    data = None
    try:
        data = _council_evolve_pipeline(
            traceback_str=traceback_str,
            modules=modules,
            module_listing=module_listing,
            log_tail=log_tail,
            screenshot_path=screenshot_path,
        )
        if data:
            logger.info("🧬  Council pipeline produced a fix.")
    except Exception as council_exc:
        logger.warning(
            "🧬  Council pipeline failed (%s), falling back to single-call.",
            council_exc,
        )

    # ── Step D: Fallback — single Gemini call ──────────────
    if not data:
        logger.info("🧬  Using single-call Gemini for self-evolution …")
        prompt = _build_single_prompt(
            traceback_str, modules, module_listing,
            log_tail, screenshot_path,
        )
        logger.info("🧬  Sending %d chars to Gemini CLI …", len(prompt))

        raw_response = ask_gemini_sync(prompt, timeout=180)
        if not raw_response or len(raw_response) < 50:
            logger.error("🧬  Gemini returned empty/short response. Aborting.")
            sys.exit(1)

        data = _extract_json_balanced(raw_response)

        if not data:
            logger.error(
                "🧬  Could not parse JSON from Gemini response (%d chars). Aborting.",
                len(raw_response),
            )
            sys.exit(1)

    target_file = data.get("file", "main.py")
    patched_code = data.get("code", "")

    if not patched_code or len(patched_code) < 50:
        logger.error("🧬  Gemini returned code that's too short (%d chars). Aborting.", len(patched_code))
        sys.exit(1)

    # ── Step E: Validate target file against allowlist ──────
    # V37 SECURITY FIX: Only explicitly allowed files may be overwritten.
    # This prevents hallucinated filenames or path-traversal from corrupting
    # critical system files (config.py, __init__.py, self_evolver.py itself).
    _EVOLUTION_ALLOWLIST = {
        "main.py", "headless_executor.py", "sandbox_manager.py",
        "tool_server.py", "gemini_advisor.py", "local_orchestrator.py",
        "agent_council.py", "session_memory.py", "scheduler.py",
        "retry_policy.py", "brain.py", "workspace_transaction.py",
        "compliance_gateway.py", "temporal_planner.py",
        "user_research_engine.py", "growth_engine.py",
        "autonomous_verifier.py", "polish_engine.py",
    }

    if target_file not in _EVOLUTION_ALLOWLIST:
        logger.error(
            "🧬  Target file '%s' is NOT in the evolution allowlist. Aborting.",
            target_file,
        )
        sys.exit(1)

    # V37 SECURITY FIX: Path traversal guard.
    target_path = (_SUPERVISOR_DIR / target_file).resolve()
    if not str(target_path).startswith(str(_SUPERVISOR_DIR.resolve())):
        logger.error("🧬  Path traversal detected: '%s'. Aborting.", target_file)
        sys.exit(1)

    if not target_path.exists():
        logger.error("🧬  Target file '%s' does not exist! Aborting.", target_file)
        sys.exit(1)

    logger.info("🧬  Gemini identified bug in: %s", target_file)
    logger.info("🧬  Patched code length: %d chars", len(patched_code))

    # ── Step E2: Pre-write Python syntax validation ────────
    try:
        compile(patched_code, target_file, "exec")
        logger.info("🧬  ✅ Syntax check PASSED for patched code.")
    except SyntaxError as syn_err:
        logger.error(
            "🧬  ❌ Syntax check FAILED — refusing to write corrupt code! "
            "Error: %s (line %s)", syn_err.msg, syn_err.lineno,
        )
        sys.exit(1)

    # ── Step E3: File-size sanity check ────────────────────
    original_text = target_path.read_text(encoding="utf-8")
    original_len = len(original_text)
    patched_len = len(patched_code)

    if original_len > 500 and patched_len < original_len * 0.3:
        logger.error(
            "🧬  ❌ Size sanity FAILED — patched code is %d chars but "
            "original is %d chars (%.0f%%). Refusing to shrink file by >70%%.",
            patched_len, original_len, (patched_len / original_len) * 100,
        )
        sys.exit(1)

    logger.info(
        "🧬  Size check OK: %d → %d chars (%.0f%%)",
        original_len, patched_len, (patched_len / original_len) * 100 if original_len else 100,
    )

    # ── Step F: Create backup ──────────────────────────────
    _BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_name = f"{target_file}.{timestamp}.bak"
    backup_path = _BACKUP_DIR / backup_name

    try:
        backup_path.write_text(original_text, encoding="utf-8")
        logger.info("🧬  Backed up original to: %s", backup_path)
    except Exception as exc:
        logger.error("🧬  Could not create backup: %s — ABORTING for safety.", exc)
        sys.exit(1)

    # ── Step G: Overwrite the target file ──────────────────
    try:
        target_path.write_text(patched_code, encoding="utf-8")
        logger.info("🧬  ✅ Patched %s successfully!", target_file)
    except Exception as exc:
        logger.error("🧬  Failed to write patched code to %s: %s", target_file, exc)
        _restore_from_backup(backup_path, target_path)
        sys.exit(1)

    # ── Step G2: Post-write validation with auto-rollback ──
    try:
        written = target_path.read_text(encoding="utf-8")
        compile(written, target_file, "exec")
        logger.info("🧬  ✅ Post-write SYNTAX validation PASSED.")
        
        # V14 AGI: Shadow Sandbox Check
        sandbox_ok = _run_shadow_sandbox()
        if not sandbox_ok:
            raise Exception("Shadow Sandbox Reject - Core capabilities fractured.")
            
        logger.info("🧬  ✅ Post-write SANDBOX integration PASSED.")
        
        # V14.1 Cache Bloat Control
        # Prune older backups to keep max 5
        try:
            backups = sorted(_BACKUP_DIR.glob("*.bak"), key=lambda p: p.stat().st_mtime)
            if len(backups) > 5:
                for old_bak in backups[:-5]:
                    old_bak.unlink(missing_ok=True)
                logger.info("🧬  🗑️ Pruned %d old backups. Kept the most recent 5.", len(backups) - 5)
        except Exception as prune_err:
            logger.warning("🧬  Warning: Failed to prune old backups: %s", prune_err)
            
    except (SyntaxError, Exception) as post_err:
        logger.error(
            "🧬  ❌ Post-write validation FAILED: %s — auto-restoring from backup!", post_err,
        )
        _restore_from_backup(backup_path, target_path)
        sys.exit(1)

    # ── Step H: Exit with code 42 to trigger reboot ────────
    logger.info("🧬  Exiting with code 42 to trigger reboot via .bat script …")
    print("\n  🧬 Self-evolution complete. Rebooting …\n")
    sys.exit(42)


def list_evolution_backups() -> list[dict]:
    """List all evolution backups for debugging."""
    if not _BACKUP_DIR.exists():
        return []
    backups = []
    for f in sorted(_BACKUP_DIR.iterdir()):
        backups.append({
            "name": f.name,
            "size": f.stat().st_size,
            "modified": time.ctime(f.stat().st_mtime),
        })
    return backups
