"""
main.py — The Orchestrator (V7.2 Background Mode).

Entry point for the Supervisor AI. V7.2 adds fully non-interactive
background operation — ZERO blocking input() calls, auto-restart
after self-evolution, and automatic recovery from all failures.

9 core systems + V7 patches + V7.2 Background Mode:

  1. LOCKFILE MEMORY (Anti-Amnesia)
  2. AUTO-RECONNECT ENGINE
  3. APPROVAL SNIPER (V7 Laser-Scoped)
  4. ERADICATE page.wait_for_timeout
  5. NAVIGATION DEFENSE (Anti-Hijack)
  6. 180-SECOND BRAIN (Global Timeouts)
  7. MANDATE FIREWALL
  8. BULLETPROOF WORKSPACE LOCKING
  9. DYNAMIC PAGE RESOLUTION
  10. V7.2 BACKGROUND MODE: No input() anywhere. Auto-recover,
      auto-restart, auto-resume. Runs unattended while user works.
"""

import argparse
import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── V7 Robustness: Ensure Windows System32 is in PATH ──
if sys.platform == "win32":
    _sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if _sys32.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = f"{_sys32}{os.pathsep}{os.environ.get('PATH', '')}"

from . import config
from .self_evolver import self_evolve
from .gemini_advisor import (
    ask_gemini,
    ask_gemini_sync,
    call_gemini_with_file,
    call_gemini_with_file_json,
)
from .agent_council import AgentCouncil, Issue as CouncilIssue
from . import command_resolver
from . import bootstrap
from .scheduler import CronScheduler

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

# Multiple fallback paths for the Antigravity executable.
ANTIGRAVITY_EXE_CANDIDATES = [
    Path(r"C:\Users\mokde\AppData\Local\Programs\Antigravity\Antigravity.exe"),
    Path(r"C:\Program Files\Antigravity\Antigravity.exe"),
    Path(r"C:\Program Files (x86)\Antigravity\Antigravity.exe"),
]

# How long to wait after launching Antigravity for it to visually render.
IDE_STARTUP_WAIT_SECONDS = 12


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


class AutoRecoveryEngine:
    """
    V7.2 Genius-Level Recovery Engine.

    A stateful recovery system that gets SMARTER with each failure:

      • Exponential backoff: 10s → 20s → 40s → 80s → 160s → 300s (cap)
      • Strategy rotation: each recovery attempt escalates through
        progressively more aggressive strategies
      • Crash forensics: logs every crash with timestamps for post-mortem
      • Health auto-reset: consecutive successes reduce backoff
      • Self-evolution trigger: after all strategies exhausted, triggers
        sys.exit(42) to force the .bat reboot with fresh code

    Strategies (in escalation order):
      1. RECONNECT — just re-attach to IDE via CDP
      2. RESTART_HOST — restart the Extension Host process
      3. FULL_RELAUNCH — kill everything and relaunch Antigravity
      4. EVOLVE — trigger self-evolution via Gemini
    """

    STRATEGIES = ["RECONNECT", "RESTART_HOST", "FULL_RELAUNCH", "EVOLVE"]
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
        # Decay backoff toward minimum on success
        self._backoff = max(self.MIN_BACKOFF, self._backoff // 2)

    def recover(self, error_context: str = "") -> str:
        """
        Called when the supervisor exhausts its normal retry logic.

        Returns the recommended strategy string. The caller should
        act on it. After calling this, the engine sleeps for the
        current backoff duration.
        """
        self._consecutive_failures += 1
        self._total_failures += 1

        strategy = self.current_strategy

        # Log crash forensics
        crash_entry = {
            "time": datetime.now().isoformat(),
            "failure_num": self._consecutive_failures,
            "total": self._total_failures,
            "strategy": strategy,
            "backoff": self._backoff,
            "context": error_context[:200],
        }
        self._crash_log.append(crash_entry)
        # Keep only last 50 entries
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
        print(f"  {B}{M}  🔄 AUTO-RECOVERY ENGINE V7.2{R}")
        print(f"  {M}  Failure:  #{self._consecutive_failures} (lifetime: #{self._total_failures}){R}")
        print(f"  {M}  Strategy: {strategy}{R}")
        print(f"  {M}  Backoff:  {self._backoff}s{R}")
        if error_context:
            print(f"  {Y}  Context:  {error_context[:80]}{R}")
        print(f"  {B}{M}{'═' * 56}{R}\n")

        # Sleep with backoff
        time.sleep(self._backoff)

        # Escalate: increase backoff and advance strategy
        self._backoff = min(self._backoff * 2, self.MAX_BACKOFF)
        self._strategy_index += 1

        # If we've exhausted all strategies, trigger evolution reboot
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
# System 8: Bulletproof Workspace Locking
# ─────────────────────────────────────────────────────────────

def _find_antigravity_exe() -> Path:
    """Find the Antigravity executable, checking multiple fallback locations."""
    for candidate in ANTIGRAVITY_EXE_CANDIDATES:
        if candidate.exists():
            logger.info("Found Antigravity at: %s", candidate)
            return candidate

    # Try to find it on PATH.
    import shutil
    on_path = shutil.which("Antigravity") or shutil.which("antigravity")
    if on_path:
        logger.info("Found Antigravity on PATH: %s", on_path)
        return Path(on_path)

    logger.error("Antigravity executable not found in any known location.")
    return ANTIGRAVITY_EXE_CANDIDATES[0]


def _launch_antigravity(project_path: str) -> None:
    """
    Visually launch Antigravity pointing at the given project folder.

    V6: Uses --new-window to strictly prevent old workspace ghosting.
    """
    exe_path = _find_antigravity_exe()
    exe = str(exe_path)

    if not exe_path.exists():
        print(f"\n  [ERROR] Antigravity not found at any known location.")
        print("  Checked:")
        for c in ANTIGRAVITY_EXE_CANDIDATES:
            print(f"    - {c}")
        sys.exit(1)

    # Validate project path.
    project_dir = Path(project_path)
    if not project_dir.exists():
        logger.info("Creating project directory: %s", project_path)
        project_dir.mkdir(parents=True, exist_ok=True)

    # V7: UTF-8 Subprocess Armor + Bulletproof launch with --new-window
    # V30.4/V31: Chromium stability flags to prevent blank Electron window
    # V33: Removed --disable-software-rasterizer (unrecognized by Antigravity's Electron)
    chromium_flags = "--disable-gpu --no-sandbox --disable-dev-shm-usage"

    logger.info(
        "\N{ROCKET}  Launching Antigravity:\n"
        '    %s "%s" --new-window --remote-debugging-port=9222 %s',
        exe, project_path, chromium_flags,
    )
    print(f"\n  \N{ROCKET} Launching Antigravity (NEW WINDOW) → {project_path}")
    logger.info("🚀 Launching Antigravity (NEW WINDOW) → %s", project_path)
    utf8_env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}

    # V31: Capture Electron stderr via background thread instead of black-holing it
    import threading

    def _drain_electron_stderr(proc):
        """Background daemon thread: reads Electron stderr → logger.warning()."""
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    logger.warning("🖥️  [Electron] %s", line)
        except Exception:
            pass  # Process died or pipe closed

    if platform.system() == "Windows":
        cmd = f'"{exe}" "{project_path}" --new-window --remote-debugging-port=9222 {chromium_flags}'
        _electron_proc = subprocess.Popen(
            cmd, shell=True, encoding='utf-8', errors='replace', env=utf8_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    else:
        _electron_proc = subprocess.Popen(
            [exe, project_path, "--new-window", "--remote-debugging-port=9222",
             "--disable-gpu", "--disable-software-rasterizer",
             "--no-sandbox", "--disable-dev-shm-usage"],
            encoding='utf-8', errors='replace', env=utf8_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

    # Start daemon thread to drain stderr
    _stderr_thread = threading.Thread(
        target=_drain_electron_stderr, args=(_electron_proc,), daemon=True
    )
    _stderr_thread.start()
    logger.info("🖥️  Electron stderr capture thread started (PID: %d)", _electron_proc.pid)

    print(f"  ⏳ Waiting for IDE to render …")
    logger.info("⏳  Waiting for IDE to render (progressive polling)…")

    # V33: Progressive CDP page polling — check every 2s instead of 12s blind sleep
    _max_startup_polls = 8  # 8 × 2s = 16s max
    for _poll in range(_max_startup_polls):
        time.sleep(2)
        # Early exit not possible here (no event loop yet), but log progress
        logger.debug("⏳  Startup poll %d/%d…", _poll + 1, _max_startup_polls)
    print("  ✅ IDE should be visible now.\n")
    logger.info("✅  IDE should be visible now.")


async def _lockdown_workspace(context, project_path: str) -> None:
    """
    STRICT WORKSPACE LOCKDOWN: Close any page not matching the project.
    """
    project_name = Path(project_path).name.lower()
    logger.info("🔒  Workspace Lockdown: keeping only pages matching '%s'", project_name)

    C = config.ANSI_CYAN
    R = config.ANSI_RESET

    for page in list(context.pages):
        try:
            title = (await page.title()).lower()
            url = page.url.lower()

            if project_name in title or project_name in url:
                logger.info("🔒  KEEPING page: title='%s'", title[:60])
                print(f"  {C}🔒 KEEPING:{R} {title[:60]}")
                continue

            if any(url.startswith(p.lower()) for p in config.DESIRED_PAGE_URL_PREFIXES):
                logger.info("🔒  KEEPING (desired URL): url=%s", url[:80])
                continue

            if any(url.startswith(p.lower()) for p in config.IGNORED_PAGE_URL_PREFIXES):
                continue

            logger.warning("🔒  CLOSING contaminated page: title='%s'", title[:60])
            print(f"  {config.ANSI_RED}🔒 CLOSING:{R} {title[:60]}")
            logger.info("🔒 CLOSING rogue page: %s", title[:60])
            await page.close()

        except Exception as exc:
            logger.debug("🔒  Could not inspect page: %s", exc)


# ─────────────────────────────────────────────────────────────
# System 9: Dynamic Page Resolution
# ─────────────────────────────────────────────────────────────

async def _get_best_page(context):
    """
    Dynamically resolve the best IDE page from context.pages.

    Priority:
      1. Pages with 'jetski-agent' in URL (the chat webview)
      2. Pages with 'workbench' in URL
      3. Pages with file:// or the project name in the title
      4. First non-extension page

    Returns the best Page, or None if no valid pages exist.
    """
    pages = context.pages
    if not pages:
        return None

    # Priority 1: jetski-agent (the chat webview)
    for p in pages:
        try:
            if "jetski-agent" in p.url.lower():
                return p
        except Exception:
            continue

    # Priority 2: workbench pages
    for p in pages:
        try:
            if "workbench" in p.url.lower():
                return p
        except Exception:
            continue

    # Priority 3: file:// or desired URL prefix pages
    for p in pages:
        try:
            url = p.url.lower()
            if any(url.startswith(pfx.lower()) for pfx in config.DESIRED_PAGE_URL_PREFIXES):
                return p
        except Exception:
            continue

    # Priority 4: first non-extension page
    for p in pages:
        try:
            url = p.url.lower()
            if not any(url.startswith(pfx.lower()) for pfx in config.IGNORED_PAGE_URL_PREFIXES):
                return p
        except Exception:
            continue

    # Absolute fallback: first page
    return pages[0] if pages else None


# ─────────────────────────────────────────────────────────────
# System 7: Mandate Firewall + File-Drop Injection
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# System 1: Lockfile Memory (Anti-Amnesia)
# ─────────────────────────────────────────────────────────────

def _lockfile_exists(project_path: str) -> bool:
    """
    Check if .supervisor_lock exists AND its PID is still alive.

    V30: PID-based lockfile verification. If the PID in the lockfile
    is dead, the lock is stale and gets auto-removed.
    """
    lock_path = Path(project_path) / config.LOCKFILE_NAME
    if not lock_path.exists():
        return False

    # V30: Verify the locking PID is still alive
    try:
        content = lock_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("pid="):
                pid = int(line.split("=", 1)[1].strip())
                try:
                    os.kill(pid, 0)  # Signal 0 = existence check only
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

    # Fallback: if we can't parse PID, treat as legacy lockfile (exists)
    return True


def _create_lockfile(project_path: str) -> None:
    """Create the .supervisor_lock file with PID for V30 staleness detection."""
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
# System 4a: Self-Healing Command Palette Engine
# ─────────────────────────────────────────────────────────────

def _load_palette_cache() -> dict[str, str]:
    """Load the command palette name cache from disk."""
    cache_path = config.COMMAND_PALETTE_CACHE_PATH
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("Failed to load command_palette_cache.json: %s", exc)
    return {}


def _save_palette_cache(cache: dict[str, str]) -> None:
    """Persist the command palette name cache to disk."""
    try:
        with open(config.COMMAND_PALETTE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=4)
        logger.info("💾  Saved command palette cache (%d entries).", len(cache))
    except Exception as exc:
        logger.warning("Failed to save command_palette_cache.json: %s", exc)


# Module-level cache — loaded once at import, updated at runtime.
_palette_cache: dict[str, str] = _load_palette_cache()


async def _scrape_palette_entries(page) -> list[str]:
    """
    DOM fallback: scrape visible command palette entries directly.

    Used when Gemini vision fails (screenshot not sent, response unparseable).
    Tries multiple selectors that match the VS Code quick-open list items.
    """
    selectors = [
        ".quick-input-list .monaco-list-row .label-name",
        ".quick-input-list .monaco-list-row .monaco-highlighted-label",
        ".quick-input-list .monaco-list-row",
        "[class*='quick-input'] [class*='list-row']",
    ]
    for sel in selectors:
        try:
            elements = await page.query_selector_all(sel)
            if elements:
                entries = []
                for el in elements[:20]:  # Cap at 20 to avoid noise
                    txt = (await el.inner_text()).strip()
                    if txt:
                        entries.append(txt)
                if entries:
                    logger.info("🔍  DOM scrape found %d palette entries.", len(entries))
                    return entries
        except Exception:
            continue
    return []


async def _run_palette_command(
    page,
    intent_key: str,
    default_command: str,
    context=None,
) -> bool:
    """
    Command Palette Execution — type and submit immediately.

    V9: Simplified flow — types the human-readable command name and presses
    Enter right away. Screenshot + Gemini verification is ONLY used on error
    (when post-injection verification fails), not on every call.

    Steps:
      1. Press F1 to open the Command Palette.
      2. Type the human-readable command name (e.g. 'Open Chat with Agent').
      3. Wait briefly for dropdown to filter, then press Enter to select.
      4. Return True. Caller handles post-execution verification.

    If the command fails (caller detects), the self-healing path in
    _find_best_palette_match can be invoked separately.

    Returns True on success, False on failure (caller handles fallback).
    """
    # Always use the human-readable default command — NOT the programmatic ID.
    # The palette's fuzzy search matches display names, not command IDs.
    command = default_command
    logger.info("🎯  Palette command for '%s': '%s'", intent_key, command)

    try:
        # ── Step 1: Open the Command Palette ──
        await page.keyboard.press("F1")
        await asyncio.sleep(1.0)

        # ── Step 2: Type the command name ──
        await page.keyboard.insert_text(command)
        await asyncio.sleep(0.7)  # Give the dropdown time to filter
        logger.info("🎯  Typed '%s' in Command Palette.", command)

        # ── Step 3: Press Enter to select the top result ──
        await page.keyboard.press("Enter")
        logger.info("🎯  Pressed Enter — command '%s' submitted.", command)
        return True

    except Exception as exc:
        logger.warning("🎯  _run_palette_command failed: %s", exc)
        # Try to dismiss palette if stuck
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False


async def _find_best_palette_match(
    intent_key: str, typed_command: str, visible_entries: list[str]
) -> Optional[str]:
    """
    Find the best match from visible palette entries.

    V8: Pre-filters via command_resolver before calling Gemini.
    Falls back to simple substring matching if both fail.
    """
    if not visible_entries:
        return None

    # ── Pre-filter: check if any visible entry is a known command ID ──
    for entry in visible_entries:
        if command_resolver.is_valid_command(entry.strip()):
            logger.info(
                "🗺️  Visible entry '%s' is a valid command ID — using directly.",
                entry.strip(),
            )
            return entry.strip()

    # ── Check command resolver for suggestions based on intent ──
    suggestions = command_resolver.find_command(intent_key.replace('_', '.'))
    if not suggestions:
        suggestions = command_resolver.find_command(intent_key.replace('_', ' '))
    if suggestions:
        # Cross-reference suggestions with visible entries
        for suggestion in suggestions:
            for entry in visible_entries:
                if suggestion.lower() in entry.lower() or entry.lower() in suggestion.lower():
                    logger.info(
                        "🗺️  Command resolver cross-matched '%s' with visible entry '%s'.",
                        suggestion, entry,
                    )
                    return entry

    # ── Try Gemini ──
    try:
        from .gemini_advisor import ask_gemini_json
        match_prompt = (
            'Reply with ONLY a JSON object:\n'
            '{"best_match": "exact entry text", "confidence": "HIGH|MEDIUM|LOW"}\n\n'
            f'Intent: "{intent_key.replace("_", " ")}"\n'
            f'Originally typed: "{typed_command}"\n'
            f'Available entries in the command palette dropdown:\n'
        )
        for i, entry in enumerate(visible_entries[:15], 1):
            match_prompt += f"  {i}. {entry}\n"
        match_prompt += (
            "\nWhich entry best matches the intent? "
            "Pick the EXACT text from the list above.\n"
            'Reply ONLY with JSON: {"best_match": "...", "confidence": "..."}'
        )

        data = await ask_gemini_json(match_prompt, timeout=30)
        if data and data.get("best_match"):
            best = data["best_match"]
            confidence = data.get("confidence", "UNKNOWN")
            logger.info(
                "🎯  Gemini matched intent '%s' → '%s' (confidence: %s)",
                intent_key, best, confidence,
            )
            return best
    except Exception as exc:
        logger.warning("🎯  Gemini matching failed: %s — trying substring fallback.", exc)

    # ── Fallback: simple case-insensitive substring matching ──
    intent_words = intent_key.replace("_", " ").lower().split()
    best_score = 0
    best_entry = None
    for entry in visible_entries:
        entry_lower = entry.lower()
        score = sum(1 for word in intent_words if word in entry_lower)
        if score > best_score:
            best_score = score
            best_entry = entry
    if best_entry and best_score > 0:
        logger.info("🎯  Substring fallback matched '%s' → '%s' (score: %d)", intent_key, best_entry, best_score)
        return best_entry

    return None


# ─────────────────────────────────────────────────────────────
# System 4: Command Palette Injection (ZERO wait_for_timeout)
# ─────────────────────────────────────────────────────────────

async def _command_palette_inject(page, text: str, context=None) -> bool:
    """
    Use the Developer Command Palette for 100% reliable chat focus.

    V30: ALL text injection uses Playwright-native keyboard.insert_text()
    instead of clipboard APIs. Zero clipboard dependency.

    Steps:
      A. page.bring_to_front()
      B. Press F1 (sleep 1s)
      C. Type 'Open Chat with Agent' (sleep 0.5s), press Enter
      D. Sleep 1.5s for the sidebar to physically slide open
      E. CDP clipboard write → Ctrl+V to paste, Enter to send
      F. VERIFY the chat panel actually received input (if context given)

    Returns True on success, False on failure.
    """
    B = config.ANSI_BOLD
    G = config.ANSI_GREEN
    Y = config.ANSI_YELLOW
    R = config.ANSI_RESET
    print(f"\n  {B}{G}🎯 COMMAND PALETTE INJECTION{R}")
    logger.info("🎯 COMMAND PALETTE INJECTION starting")

    # V33+V35: Direct injection — try the chat textbox FIRST before Command Palette
    # V35: Use multi-fallback selectors from research (Antigravity may differ from VS Code)
    if context is not None:
        from .frame_walker import find_element_in_all_frames
        _direct_selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'textarea[placeholder*="Ask anything"]',
            '[aria-label*="chat" i][contenteditable="true"]',
            '[aria-label*="agent" i][contenteditable="true"]',
            '[role="textbox"][aria-multiline="true"]',
        ]
        for _dselector in _direct_selectors:
            try:
                direct_result = await find_element_in_all_frames(
                    context, _dselector, require_visible=True,
                )
                if direct_result:
                    frame, el = direct_result
                    logger.info("🎯  V35 Direct injection: found chat via %s", _dselector)
                    # V35: Try locator.fill() first — fires proper input events
                    try:
                        locator = frame.locator(_dselector).first
                        await locator.fill(text, timeout=3000)
                        logger.info("🎯  V35 locator.fill() succeeded (%d chars)", len(text))
                    except Exception:
                        # Fallback: execCommand for contenteditable elements
                        await el.focus()
                        await asyncio.sleep(0.3)
                        await frame.evaluate(
                            "(el, txt) => { el.focus(); document.execCommand('insertText', false, txt); }",
                            el, text,
                        )
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(0.3)
                    logger.info("🎯  ✅ Direct injection succeeded (%s)! Sent %d chars.", _dselector, len(text))
                    print(f"  {G}✅ Injected via direct {_dselector[:40]}!{R}\n")
                    return True
            except Exception as direct_exc:
                logger.debug("🎯  Direct injection via %s failed: %s", _dselector, direct_exc)

    try:
        # Check if chat is already open to avoid toggling it closed
        from .frame_walker import find_chat_frame
        chat_result = None
        if context is not None:
            chat_result = await find_chat_frame(context)
            
        if chat_result:
            logger.info("🎯  Chat panel already open (visible) — skipping Command Palette F1 toggle.")
            frame, sel = chat_result
            await frame.click(sel, force=True, timeout=2000)
            await asyncio.sleep(0.3)
        else:
            # A. Force absolute OS focus
            await page.bring_to_front()
            await asyncio.sleep(0.3)
            logger.info("🎯  Step A: Brought page to front.")

            # B+C. Self-healing command palette: open chat with agent
            palette_ok = await _run_palette_command(
                page, "open_agent_chat", "Open Chat with Agent", context=context,
            )
            if not palette_ok:
                logger.warning("🎯  Self-healing palette command failed — raising to trigger fallback.")
                raise RuntimeError("_run_palette_command returned False")
            logger.info("🎯  Step B+C: Chat opened via self-healing Command Palette.")

            # D. Wait for the sidebar to physically slide open
            await asyncio.sleep(1.5)  # V6: asyncio.sleep
            logger.info("🎯  Step D: Waited 1.5s for sidebar slide.")

        # E. V30: Playwright-native text insertion (no clipboard needed)
        await page.keyboard.insert_text(text)
        logger.info("🎯  Step E: Inserted %d chars via keyboard.insert_text().", len(text))

        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)

        # F. VERIFY the chat panel actually opened (V7.4 Anti-Void Gate)
        if context is not None:
            await asyncio.sleep(3.0)
            from .frame_walker import find_chat_frame
            chat_result = await find_chat_frame(context)
            if chat_result is None:
                logger.warning("🎯  Step F: Post-injection verification FAILED — chat frame not found.")
                print(f"  {Y}⚠️ Chat panel didn't open — will retry.{R}")
                return False
            logger.info("🎯  Step F: Post-injection verification PASSED — chat frame found.")

        logger.info("🎯  Command Palette injection completed successfully.")
        print(f"  {G}✅ Injected via Command Palette!{R}\n")
        logger.info("✅ Injected successfully via Command Palette.")
        return True

    except Exception as exc:
        logger.warning("🎯  Command Palette injection failed: %s — trying DOM fallback.", exc)

        # V35 Fallback: Multi-selector DOM injection with locator.fill()
        try:
            _fallback_selectors = [
                'div[contenteditable="true"][role="textbox"]',
                '[aria-label*="chat" i][contenteditable="true"]',
                'div[contenteditable="true"]',
            ]
            for _fselector in _fallback_selectors:
                el = await page.query_selector(_fselector)
                if el:
                    try:
                        locator = page.locator(_fselector).first
                        await locator.fill(text, timeout=3000)
                    except Exception:
                        await el.focus()
                        await asyncio.sleep(0.3)
                        await page.keyboard.insert_text(text)
                    await asyncio.sleep(0.3)
                    await page.keyboard.press("Enter")
                    logger.info("🎯  DOM fallback injection succeeded via %s.", _fselector)
                    print(f"  {G}✅ Injected via DOM fallback!{R}\n")
                    return True
        except Exception as dom_exc:
            logger.error("🎯  DOM fallback also failed: %s", dom_exc)

        print(f"  {config.ANSI_RED}❌ All injection methods failed: {exc}{R}\n")
        logger.error("❌ All injection methods failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────
# System 3: Approval Sniper (Lightning-Fast Unblock) — V7 Laser-Scoped
# ─────────────────────────────────────────────────────────────

_last_sniper_click_time: float = 0.0  # Module-level cooldown timestamp
_SNIPER_COOLDOWN_SECONDS: float = 3.0  # Reduced from 10s — don't miss rapid prompts

async def _approval_sniper(context) -> bool:
    """
    Lightning-fast check to unblock terminal commands BEFORE Vision.

    V11 FIX (Aggressive Three-Phase Sniper):
    Phase 1: Click "Always run" / "Always Allow" checkbox (enables auto-run)
    Phase 2: Click ANY confirmation button (Run, Allow, Accept, Continue, etc.)
    Phase 3: Fallback — press Enter AND Alt+Enter to force-execute

    Also scans for other common approval buttons like "Continue", "Yes", "OK".
    Iterates pages in reverse (most recent first) to catch the LATEST prompt.

    Uses a short timeout to avoid blocking the loop.
    Returns True if a command was executed, False otherwise.
    """
    global _last_sniper_click_time
    timeout = config.APPROVAL_SNIPER_TIMEOUT_MS

    # Cooldown guard: don't spam-click while the agent is generating code
    if (time.time() - _last_sniper_click_time) < _SNIPER_COOLDOWN_SECONDS:
        return False

    # Phase 1 selector: the auto-run checkbox
    always_run_selector = (
        'div[role="button"]:has-text("Always run"), '
        'button:has-text("Always run"), '
        'div[role="button"]:has-text("Always Allow"), '
        'button:has-text("Always Allow"), '
        'input[type="checkbox"]:near(:text("Always run")), '
        'label:has-text("Always run")'
    )
    # Phase 2 selector: the actual Run/Allow/Accept execution buttons
    # Ordered from most specific to least to avoid false positives
    run_button_selector = (
        'div[role="button"]:has-text("Run Alt+Enter"), '
        'button:has-text("Run Alt+Enter"), '
        'div[role="button"]:has-text("Accept all"), '
        'button:has-text("Accept all"), '
        'div[role="button"]:has-text("Allow"), '
        'button:has-text("Allow"), '
        'a:has-text("Allow VS Code"), '
        'button:has-text("Allow VS Code"), '
        'div[role="button"]:has-text("Continue"), '
        'button:has-text("Continue"), '
        'div[role="button"]:has-text("Confirm"), '
        'button:has-text("Confirm"), '
        'div[role="button"]:has-text("Proceed"), '
        'button:has-text("Proceed"), '
        'div[role="button"]:has-text("Yes"), '
        'button:has-text("Yes"), '
        'div[role="button"]:has-text("OK"), '
        'button:has-text("OK"), '
        'div[role="button"]:has-text("Run"), '
        'button:has-text("Run")'
    )

    # ── V30.1 Phase 0: Notification Toast Auto-Clicker ──
    # Catches VS Code / Antigravity system notification toasts
    # (network permissions, port forwarding, extension prompts)
    # that are completely invisible to the chat-only approval logic.
    notification_selectors = [
        '.notification-toast a:has-text("Always Allow")',
        '.notification-toast button:has-text("Always Allow")',
        'a.monaco-button:has-text("Always Allow")',
        '.notification-toast a:has-text("Allow")',
        '.notification-toast button:has-text("Allow")',
        '.notification-toast a:has-text("Configure")',
        '.notifications-toasts button:has-text("Always Allow")',
        '.notifications-toasts a:has-text("Always Allow")',
        '.notifications-toasts button:has-text("Allow")',
        '.notifications-toasts a:has-text("Allow")',
        # V30.4: JS execution permission toasts
        '.notification-toast button:has-text("Run All")',
        '.notifications-toasts button:has-text("Run All")',
        '.notification-toast button:has-text("Trust")',
        '.notifications-toasts button:has-text("Trust")',
        '.notification-toast button:has-text("Yes, I trust")',
        '.notifications-toasts button:has-text("Yes, I trust")',
    ]
    # V31 Fix 3.5: Filter pages — skip chrome-extension:// and devtools:// 
    _sniper_pages = [
        p for p in context.pages
        if not any(p.url.startswith(pfx) for pfx in config.IGNORED_PAGE_URL_PREFIXES)
    ]
    for page in reversed(_sniper_pages):
        for sel in notification_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    clicked_text = await loc.first.inner_text()
                    await loc.first.click(force=True, timeout=timeout)
                    _last_sniper_click_time = time.time()
                    logger.info(
                        "🔔  [NOTIFICATION] Auto-clicked toast button: '%s'",
                        clicked_text.strip(),
                    )
                    print(
                        f"  {config.ANSI_GREEN}🔔 [NOTIFICATION] Clicked "
                        f"'{clicked_text.strip()}' in toast{config.ANSI_RESET}"
                    )
                    return True
            except Exception:
                continue

    # Iterate filtered pages in REVERSE order — most recently opened page first
    # This ensures we catch the LATEST approval prompt
    for page in reversed(_sniper_pages):
        try:
            for frame in page.frames:
                try:
                    clicked_always_run = False

                    # ── Phase 1: Click "Always run" checkbox ──
                    always_btn = frame.locator(always_run_selector)
                    if await always_btn.count() > 0:
                        await always_btn.first.click(force=True, timeout=timeout)
                        clicked_always_run = True
                        _last_sniper_click_time = time.time()
                        logger.info(
                            "⚡  [ACTION] Clicked 'Always run' checkbox via Laser Sniper.",
                        )
                        print(
                            f"  {config.ANSI_GREEN}⚡ [ACTION] Selected "
                            f"'Always run' mode{config.ANSI_RESET}"
                        )
                        await asyncio.sleep(0.5)  # Let UI update

                    # ── Phase 2: Click the actual "Run" / confirmation button ──
                    run_btn = frame.locator(run_button_selector)
                    if await run_btn.count() > 0:
                        clicked_text = await run_btn.first.inner_text()
                        await run_btn.first.click(force=True, timeout=timeout)
                        _last_sniper_click_time = time.time()
                        logger.info(
                            "⚡  [ACTION] Auto-approved command via Laser Sniper: '%s'",
                            clicked_text.strip(),
                        )
                        print(
                            f"  {config.ANSI_GREEN}⚡ [ACTION] Pressed Run "
                            f"button: '{clicked_text.strip()}'{config.ANSI_RESET}"
                        )
                        # Phase 3 bonus: ALSO press Enter and Alt+Enter after clicking
                        # to guarantee the command executes even if the button click
                        # didn't fully register
                        await asyncio.sleep(0.3)
                        try:
                            await page.keyboard.press("Enter")
                            await asyncio.sleep(0.2)
                            await page.keyboard.press("Alt+Enter")
                        except Exception:
                            pass
                        return True

                    # ── Phase 3: Fallback — press Enter AND Alt+Enter ──
                    if clicked_always_run:
                        # "Always run" was clicked but no Run button found.
                        # Press BOTH Enter and Alt+Enter as fallback.
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(0.2)
                        await page.keyboard.press("Alt+Enter")
                        logger.info(
                            "⚡  [ACTION] Pressed Enter + Alt+Enter as fallback after 'Always run'.",
                        )
                        print(
                            f"  {config.ANSI_GREEN}⚡ [ACTION] Pressed Enter + Alt+Enter "
                            f"(fallback after Always run){config.ANSI_RESET}"
                        )
                        return True

                except Exception:
                    # Button not found or not clickable — try next frame
                    continue
        except Exception:
            # Page might be closed or in a bad state
            continue

    return False


# ─────────────────────────────────────────────────────────────
# System 5: Navigation Defense (Anti-Hijack)
# ─────────────────────────────────────────────────────────────

async def _navigation_defense(page, context) -> tuple:
    """
    Detects if the AI hijacked the main IDE window to localhost.

    If page.url starts with 'http://localhost', forces go_back()
    and injects a strict reprimand.

    Returns (page, was_hijacked: bool).
    The page may be updated if re-resolution was needed.
    """
    try:
        url = page.url
    except Exception:
        # Page is already dead, need reconnect
        new_page = await _get_best_page(context)
        return (new_page or page, False)

    if url.startswith("http://localhost") or url.startswith("https://localhost"):
        Y = config.ANSI_YELLOW
        R = config.ANSI_RESET
        logger.warning("🛡️  HIJACK DETECTED! Page navigated to: %s", url)
        print(f"\n  {Y}🛡️ HIJACK DETECTED! IDE navigated to {url}{R}")
        print(f"  {Y}🛡️ Route blocked — re-acquiring page and injecting reprimand …{R}")
        logger.warning("🛡️ HIJACK DETECTED! IDE navigated to %s — route blocked", url)

        # V34: Do NOT call go_back(). The route intercept already blocked the
        # navigation. Calling go_back() after a blocked nav corrupts
        # Electron's IPC channels and kills the renderer → black screen.
        # V35: Use wait_for_load_state instead of fixed sleep for reliability.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            await asyncio.sleep(3.0)  # fallback if wait_for_load_state fails
        page = await _get_best_page(context) or page

        # Inject reprimand
        reprimand = (
            "CRITICAL ERROR: You navigated the main IDE window to a local URL. "
            "NEVER do this. For VISUAL PREVIEW, open the Simple Browser side panel "
            "by running: powershell -File open_browser.ps1 -Url 'http://localhost:PORT'. "
            "For TESTING, use your native "
            "browser_subagent tool (open_browser_url) to open the localhost URL "
            "in your own invisible browser. Do not apologize. Immediately read "
            "your state file, determine the exact next steps, and resume "
            "building autonomously."
        )
        try:
            await _command_palette_inject(page, reprimand)
        except Exception:
            pass

        return (page, True)

    return (page, False)


# ─────────────────────────────────────────────────────────────
# Chat Context Reader — Scrape visible chat content for awareness
# ─────────────────────────────────────────────────────────────

async def _read_chat_content(context, max_chars: int = 2000) -> str:
    """
    Read visible text from the AI agent chat panel.

    Traverses all pages/frames looking for chat message containers
    and extracts the last N characters of visible text content.
    This gives the supervisor context about what work has been completed.
    """
    chat_text = ""
    chat_selectors = [
        '.chat-message',                    # Common chat message class
        '[class*="message-content"]',        # Message content containers
        '[class*="chat"] [class*="body"]',   # Chat body containers
        '[class*="response"]',               # Response containers
        '[class*="assistant"]',              # Assistant message containers
        '[data-role="assistant"]',           # Role-tagged messages
    ]

    for page in context.pages:
        for frame in page.frames:
            try:
                for sel in chat_selectors:
                    elements = await frame.query_selector_all(sel)
                    if elements:
                        for el in elements[-10:]:  # Last 10 messages
                            try:
                                text = (await el.inner_text()).strip()
                                if text and len(text) > 5:  # Skip tiny fragments
                                    chat_text += text + "\n---\n"
                            except Exception:
                                continue
                        if chat_text:
                            break
            except Exception:
                continue
        if chat_text:
            break

    # Truncate and return
    if len(chat_text) > max_chars:
        chat_text = "..." + chat_text[-max_chars:]
    return chat_text.strip()


# ─────────────────────────────────────────────────────────────
# Multimodal Vision — The All-Seeing Eye
# ─────────────────────────────────────────────────────────────

async def _analyze_ide_state(
    page, goal: str = "", consecutive_waiting: int = 0,
    context=None,
) -> dict:
    """
    Use EYESIGHT to analyze the IDE state.

    V14: Vision-First Pipeline — tries local Ollama vision model first
    (fast, free, no API quota). Falls back to Gemini CLI only if local
    model fails, times out, or returns LOW/MEDIUM confidence.

    Returns: {"state": "WORKING"|"WAITING"|"CRASHED", "reason": "..."}
    """
    screenshot_path = config.SCREENSHOT_PATH

    try:
        await page.screenshot(path=screenshot_path)
        logger.info("📸  Screenshot saved: %s", screenshot_path)

        # Read recent logs for execution context
        log_tail = ""
        try:
            log_file = Path(config.SCREENSHOT_PATH).parent / "supervisor.log"
            if log_file.exists():
                lines = log_file.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                log_tail = "\n".join(lines[-15:])
        except Exception:
            pass

        # Build the vision analysis prompt (shared by both local and Gemini)
        prompt = (
            'Reply with ONLY a JSON object in this exact format, nothing else:\n'
            '{"state": "WORKING", "reason": "what you see"}\n\n'
            'Values for "state": WORKING, WAITING, or CRASHED.\n\n'
            "Analyze the attached screenshot of the Antigravity IDE "
            "(a VS Code fork with an AI Agent sidebar on the right).\n\n"
            "DETERMINE: Is the AI Agent chat panel WORKING, WAITING, or CRASHED?\n\n"
            "WORKING = There are recent messages/code blocks visible AND "
            "a spinner/Stop button is showing OR text is actively streaming.\n"
            "WAITING = Chat area is empty OR shows only placeholder text "
            "'Ask anything' OR has a question/approval button but no active generation.\n"
            "CRASHED = Error dialogs, blank screen, 'Extension Host terminated'.\n\n"
            "⚠️ IGNORE the model selector label for example 'Claude Opus 4.6 (Thinking)' — "
            "that is the MODEL NAME, not a status indicator.\n"
        )

        if consecutive_waiting > 0:
            prompt += (
                f"\n⚠️ WAITING has been detected {consecutive_waiting}× already. "
                "Be EXTRA skeptical of WORKING claims.\n"
            )

        if goal:
            prompt += f"\nCurrent goal: {goal[:150]}\n"

        if log_tail:
            prompt += f"\nRecent logs:\n```\n{log_tail}\n```\n"

        # V10: Chat context awareness — read what the AI has written
        if context is not None:
            try:
                chat_content = await _read_chat_content(context, max_chars=1500)
                if chat_content:
                    prompt += (
                        f"\nRecent chat content (what the AI agent has written so far):\n"
                        f"```\n{chat_content}\n```\n"
                        "Use this to understand what work has been completed. "
                        "If the agent has clearly finished a task and is waiting "
                        "for new instructions, report WAITING.\n"
                    )
            except Exception:
                pass

        prompt += (
            '\nReply with ONLY the JSON object. '
            'Example: {"state": "WAITING", "reason": "chat area is empty, input shows placeholder"}'
        )

        # ── V14: Vision-First Pipeline ────────────────────────
        # Step 1: Try local Ollama vision model (fast, free, no API quota)
        local_result = {}
        try:
            from .local_orchestrator import LocalManager, OllamaUnavailable
            local_manager = LocalManager()
            local_result = await local_manager.analyze_screenshot(
                screenshot_path, prompt
            )
        except OllamaUnavailable:
            logger.warning("🧠👁️  Local LLM unavailable — skipping vision analysis (non-fatal)")
        except Exception as local_exc:
            logger.warning("🧠👁️  Local vision attempt failed: %s", local_exc)

        if local_result and "state" in local_result:
            confidence = local_result.get("confidence", "LOW").upper()
            confidence_threshold = getattr(config, "OLLAMA_VISION_CONFIDENCE_THRESHOLD", 0.7)

            # Map textual confidence to numeric
            confidence_map = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2}
            confidence_score = confidence_map.get(confidence, 0.2)

            if confidence_score >= confidence_threshold:
                # HIGH confidence — accept local result, skip Gemini entirely
                state = local_result["state"].upper()
                reason = local_result.get("reason", "No reason provided")
                logger.info(
                    "👁️  Vision (LOCAL, %s confidence): state=%s, reason=%s",
                    confidence, state, reason,
                )
                print(
                    f"  {config.ANSI_GREEN}👁️ Vision analysis (LOCAL): "
                    f"{state} — skipped Gemini CLI{config.ANSI_RESET}"
                )
                return {"state": state, "reason": f"[LOCAL/{confidence}] {reason}"}
            else:
                # LOW/MEDIUM confidence — escalate to Gemini for second opinion
                logger.info(
                    "🧠👁️  Local vision confidence=%s (< threshold). Escalating to Gemini CLI...",
                    confidence,
                )
                print(
                    f"  {config.ANSI_YELLOW}🧠👁️ Local vision confidence {confidence} "
                    f"— escalating to Gemini CLI for confirmation{config.ANSI_RESET}"
                )

        # Step 2: Gemini CLI fallback (original path, now with retry/failover)
        data = await call_gemini_with_file_json(prompt, screenshot_path, timeout=180)

        if data and "state" in data:
            state = data["state"].upper()
            reason = data.get("reason", "No reason provided")
            logger.info("👁️  Vision analysis (GEMINI): state=%s, reason=%s", state, reason)
            return {"state": state, "reason": reason}
        else:
            # If Gemini also failed but local had a result (even low confidence), use it
            if local_result and "state" in local_result:
                state = local_result["state"].upper()
                reason = local_result.get("reason", "Gemini failed, using local fallback")
                logger.warning(
                    "👁️  Gemini vision failed — using local result as fallback: state=%s",
                    state,
                )
                return {"state": state, "reason": f"[LOCAL-FALLBACK] {reason}"}

            logger.warning("👁️  Vision analysis returned no valid state. Defaulting to WAITING to trigger recovery.")
            return {"state": "WAITING", "reason": "Vision parsing failed — defaulting to WAITING to trigger local manager."}

    except Exception as exc:
        logger.error("👁️  Vision analysis failed: %s", exc)
        return {"state": "WAITING", "reason": f"Vision analysis error (defaulting to WAITING): {exc}"}


async def _skeptical_recheck(
    page, goal: str, consecutive_working: int, prior_reasons: list,
    context=None,
) -> dict:
    """
    V30.2: DOM-only skeptical re-check — ZERO screenshots.

    When the system suspects a false WORKING loop, re-analyzes using
    the ARIA tree and chat context text. If the chat panel is lost,
    attempts to reopen it via Command Palette instead of screenshotting.
    """
    try:
        # Use DOM/ARIA context instead of a screenshot
        from .context_engine import gather_context, format_context_for_prompt

        ctx_snapshot = None
        if context:
            try:
                ctx_snapshot = await gather_context(context, page, goal)
            except Exception as ctx_exc:
                logger.debug("🔍  Skeptical re-check context gather failed: %s", ctx_exc)

        # If we couldn't gather context (chat panel lost), try reopening
        if not ctx_snapshot or not ctx_snapshot.chat_messages:
            logger.info("🔍  Chat panel lost during re-check — reopening via Command Palette.")
            try:
                await _command_palette_inject(page, "Open Chat with Agent", context)
                await asyncio.sleep(2.0)
                if context:
                    ctx_snapshot = await gather_context(context, page, goal)
            except Exception:
                pass

        # Build context text for Gemini analysis
        ctx_text = ""
        if ctx_snapshot:
            ctx_text = format_context_for_prompt(ctx_snapshot)

        # Build prior claims section
        prior_section = ""
        if prior_reasons:
            prior_section = "\nPREVIOUS CLAIMS:\n"
            for i, r in enumerate(prior_reasons[-5:], 1):
                prior_section += f"  {i}. {r[:100]}\n"
            prior_section += (
                "\n⚠️ The above claims may ALL be wrong. The supervisor has "
                "seen NO actual progress despite these claims.\n"
            )

        prompt = (
            'Reply with ONLY a JSON object — no other text:\n'
            '{"state": "WORKING" or "WAITING" or "CRASHED", '
            '"reason": "what you see", "previous_wrong": true or false}\n\n'
            f"CONTEXT: The supervisor reported WORKING {consecutive_working}× in a row "
            f"but ZERO actual progress was detected.\n"
            f"{prior_section}\n"
            f"DOM/ARIA CONTEXT:\n{ctx_text[:4000]}\n\n"
            "WORKING = visible streaming text, active generation, new code appearing.\n"
            "WAITING = empty chat, placeholder text, no active generation.\n"
            "CRASHED = error dialogs, blank screen.\n"
        )

        if goal:
            prompt += f"\nGoal: {goal[:150]}\n"

        prompt += (
            '\nReply ONLY with JSON: '
            '{"state": "...", "reason": "...", "previous_wrong": true/false}'
        )

        logger.info(
            "🔍  SKEPTICAL RE-CHECK (DOM-only) after %d consecutive WORKING reports",
            consecutive_working,
        )
        print(
            f"  {config.ANSI_MAGENTA}🔍 Staleness detected "
            f"({consecutive_working}× WORKING with no progress) — "
            f"running DOM-only re-check …{config.ANSI_RESET}"
        )

        # Call Gemini with text-only prompt (no file attachment)
        from .gemini_advisor import ask_gemini_json
        data = await ask_gemini_json(prompt, timeout=120)

        if data and "state" in data:
            state = data["state"].upper()
            reason = data.get("reason", "")
            was_wrong = data.get("previous_wrong", False)

            logger.info(
                "🔍  DOM re-check: state=%s, previous_wrong=%s",
                state, was_wrong,
            )

            if was_wrong:
                print(
                    f"  {config.ANSI_YELLOW}🔍 RE-CHECK CORRECTED: "
                    f"previous analyses were WRONG — actual state is {state}{config.ANSI_RESET}"
                )
            else:
                print(
                    f"  {config.ANSI_GREEN}🔍 RE-CHECK CONFIRMED: "
                    f"state is genuinely {state}{config.ANSI_RESET}"
                )

            return {"state": state, "reason": reason}
        else:
            return {"state": "WAITING", "reason": "DOM re-check: could not parse response — defaulting to WAITING."}

    except Exception as exc:
        logger.error("🔍  DOM re-check failed: %s", exc)
        return {"state": "WAITING", "reason": f"Re-check error (defaulting to WAITING): {exc}"}


async def _resuscitate_ide(page, context, goal: str):
    """
    The Defibrillator — restart the Extension Host and re-inject the goal.

    V6: ALL waits are asyncio.sleep(). Returns (success, updated_page).
    """
    logger.warning("🫀  DEFIBRILLATOR ACTIVATED — attempting to resuscitate IDE …")

    M = config.ANSI_MAGENTA
    B = config.ANSI_BOLD
    R = config.ANSI_RESET
    print(f"\n  {B}{M}🫀 DEFIBRILLATOR ACTIVATED{R}")

    try:
        # A. Force window focus.
        await page.bring_to_front()
        await page.mouse.click(500, 300)
        await asyncio.sleep(0.5)

        # B+C. Self-healing command palette: restart extension host
        palette_ok = await _run_palette_command(
            page, "restart_extension_host", "Developer: Restart Extension Host",
        )
        if not palette_ok:
            logger.warning("🫀  Self-healing palette command failed for restart — trying raw F1.")
            await page.keyboard.press("F1")
            await asyncio.sleep(1.0)
            await page.keyboard.insert_text("Developer: Restart Extension Host")
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")

        # D. Wait 20 seconds for IDE internal reboot.
        logger.info("🫀  Waiting 20s for Extension Host to restart …")
        print(f"  {M}⏳ Waiting 20s for Extension Host restart …{R}")
        await asyncio.sleep(20.0)  # V6: asyncio.sleep — NEVER page.wait_for_timeout

        # E. Re-resolve the page (it may have changed)
        page = await _get_best_page(context) or page

        # F. Re-inject recovery prompt via Command Palette.
        recovery_prompt = (
            " @workspace The extension host was just restarted. "
            "Review the workspace, determine where you left off, and "
            "immediately resume building. Read SUPERVISOR_MANDATE.md "
            "for the full instructions. If a dev server should be running, "
            "start it and load the preview in the Antigravity Browser Extension."
        )

        ok = await _command_palette_inject(page, recovery_prompt)
        if ok:
            logger.info("🫀  Defibrillator SUCCESS — recovery prompt injected.")
            print(f"  {config.ANSI_GREEN}🫀 Defibrillator SUCCESS!{R}")
            return (True, page)
        else:
            logger.error("🫀  Defibrillator FAILED — re-injection failed.")
            return (False, page)

    except Exception as exc:
        logger.error("🫀  Defibrillator EXCEPTION: %s", exc)
        # Try to get a fresh page for the caller
        try:
            page = await _get_best_page(context) or page
        except Exception:
            pass
        return (False, page)


# ─────────────────────────────────────────────────────────────
# Gemini-assisted diagnostics
# ─────────────────────────────────────────────────────────────

async def _diagnose_connection_failure(error: Exception) -> None:
    """Ask Gemini to help diagnose why CDP connection failed."""
    try:
        prompt = (
            f"I'm trying to connect to an Electron IDE app (Antigravity, a VS Code fork) "
            f"via Chrome DevTools Protocol at {config.CDP_URL}.\n\n"
            f"The connection failed with this error:\n{error}\n\n"
            f"What are the most likely causes and quick fixes? Keep it under 5 bullet points."
        )
        advice = await ask_gemini(prompt, timeout=180)
        logger.info("🧠  Gemini diagnosis:\n%s", advice)
    except Exception as exc:
        logger.debug("Gemini diagnosis failed: %s", exc)


def _triage_fatal_error(tb_str: str) -> str:
    """
    Ask Gemini if a fatal error is transient or a code bug.
    V6: 180s timeout — no more triage timeouts.
    """
    try:
        prompt = (
            f"A Python supervisor script just crashed with this traceback:\n\n"
            f"{tb_str}\n\n"
            f"Is this:\n"
            f"A) A TRANSIENT error (network timeout, race condition, temporary resource "
            f"   unavailability) that would likely succeed on retry?\n"
            f"B) A CODE BUG that requires fixing the source code?\n\n"
            f"Reply with ONLY the single word: RETRY or EVOLVE"
        )
        response = ask_gemini_sync(prompt, timeout=180)  # V6: 180s
        decision = response.strip().upper()
        if "RETRY" in decision:
            return "retry"
        return "evolve"
    except Exception:
        return "evolve"


# ─────────────────────────────────────────────────────────────
# V7.4: Gemini-Powered Autonomous Diagnostic Engine
# ─────────────────────────────────────────────────────────────

def _read_recent_logs(n_lines: int = 40) -> str:
    """Read the last N lines of supervisor.log for diagnostic context."""
    log_path = Path(__file__).resolve().parent / "supervisor.log"
    try:
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-n_lines:] if len(lines) > n_lines else lines
            return "\n".join(tail)
    except Exception:
        pass
    return "(no logs available)"


async def _gemini_diagnose_and_act(
    page,
    context,
    goal: str,
    trigger: str,
    consecutive_count: int,
) -> tuple:
    """
    V7.4 Gemini-Powered Autonomous Diagnostic Engine.

    Instead of following a fixed escalation ladder, this function engages
    Gemini CLI in a multi-turn diagnostic conversation:

      Round 1: Send screenshot + logs + trigger context → get action
      Round 2: Execute action, report result → get follow-up
      Round 3: If still stuck, escalate or evolve

    Gemini returns structured JSON with one of these actions:
      • REINJECT      — Re-inject the goal via Command Palette
      • GHOST_HOTKEY  — Use Command Palette 'Open Chat with Agent' injection
      • CLICK_SELECTOR — Click a specific CSS selector
      • SCREENSHOT    — Take another screenshot for deeper analysis
      • RESTART_HOST  — Restart the Extension Host (Defibrillator)
      • EVOLVE        — Self-modify the supervisor code to fix the root cause

    Returns (page, resolved: bool) — page may be updated after resuscitation.
    """
    B = config.ANSI_BOLD
    M = config.ANSI_MAGENTA
    C = config.ANSI_CYAN
    G = config.ANSI_GREEN
    Y = config.ANSI_YELLOW
    RD = config.ANSI_RED
    R = config.ANSI_RESET

    print(f"\n  {B}{M}{'═' * 56}{R}")
    print(f"  {B}{M}  🧠 GEMINI DIAGNOSTIC ENGINE V7.4{R}")
    print(f"  {M}  Trigger: {trigger[:60]}{R}")
    print(f"  {M}  Count:   {consecutive_count}{R}")
    print(f"  {B}{M}{'═' * 56}{R}\n")

    logger.info("🧠  Gemini Diagnostic Engine activated — trigger: %s (count: %d)", trigger, consecutive_count)

    # ── Take a fresh screenshot ────────────────────────────
    screenshot_path = config.SCREENSHOT_PATH
    try:
        await page.screenshot(path=screenshot_path)
        logger.info("🧠  Fresh screenshot saved for Gemini diagnosis.")
    except Exception as exc:
        logger.warning("🧠  Could not take screenshot: %s", exc)

    # ── Gather context ─────────────────────────────────────
    recent_logs = _read_recent_logs(40)

    max_rounds = 3
    action_history: list[str] = []

    for round_num in range(1, max_rounds + 1):
        print(f"  {C}🧠 Diagnostic Round {round_num}/{max_rounds} …{R}")
        logger.info("🧠  Diagnostic round %d/%d", round_num, max_rounds)

        # ── Build the prompt ───────────────────────────────
        if round_num == 1:
            prompt = (
                'Reply with ONLY a JSON object — no other text:\n'
                '{"diagnosis": "what is wrong", "action": "REINJECT|GHOST_HOTKEY|'
                'CLICK_SELECTOR|SCREENSHOT|RESTART_HOST|EVOLVE", '
                '"action_detail": "CSS selector or instructions", '
                '"confidence": "HIGH|MEDIUM|LOW"}\n\n'
                "You are the brain of a Supervisor AI for an IDE called Antigravity "
                "(VS Code fork). The supervisor injects prompts into the AI Agent "
                "chat sidebar and monitors progress via screenshots.\n\n"
                f"PROBLEM: {trigger}\n"
                f"Occurred {consecutive_count} consecutive times.\n"
                f"Goal: {goal[:200]}\n\n"
                f"Recent logs (last 40 lines):\n{recent_logs}\n\n"
                "Screenshot is attached. Diagnose WHY the agent is stuck and "
                "pick an action to fix it.\n\n"
                "Actions:\n"
                "- REINJECT: Re-inject prompt via Command Palette (F1)\n"
                "- GHOST_HOTKEY: Use Command Palette 'Open Chat with Agent'\n"
                "- CLICK_SELECTOR: Click element (provide CSS selector in action_detail)\n"
                "- SCREENSHOT: Take another screenshot for analysis\n"
                "- RESTART_HOST: Restart Extension Host (nuclear)\n"
                "- EVOLVE: Supervisor code has a bug — trigger self-modification\n\n"
                'Reply ONLY with the JSON object.'
            )
        else:
            # Follow-up round — include previous actions and results
            history_str = "\n".join(f"  Round {i+1}: {a}" for i, a in enumerate(action_history))
            prompt = (
                'Reply with ONLY a JSON object — no other text:\n'
                '{"diagnosis": "...", "action": "...", "action_detail": "...", '
                '"confidence": "..."}\n\n'
                f"Previous actions taken:\n{history_str}\n\n"
                f"Still unresolved after {round_num - 1} rounds.\n"
                f"Recent logs:\n{recent_logs}\n\n"
                "Updated screenshot attached. What should be tried NEXT?\n"
                'Reply ONLY with the JSON object.'
            )

        # ── Call Gemini with screenshot ────────────────────
        try:
            data = await call_gemini_with_file_json(prompt, screenshot_path, timeout=180)
        except Exception as exc:
            logger.error("🧠  Gemini diagnostic call failed: %s", exc)
            print(f"  {RD}❌ Gemini diagnostic failed: {exc}{R}")
            break

        if not data:
            logger.warning("🧠  Gemini returned no parseable JSON — falling back.")
            print(f"  {Y}⚠️ Gemini returned no valid JSON.{R}")
            break

        diagnosis = data.get("diagnosis", "unknown")
        action = data.get("action", "REINJECT").upper()
        detail = data.get("action_detail", "")
        confidence = data.get("confidence", "MEDIUM")

        logger.info("🧠  Gemini diagnosis: %s", diagnosis[:120])
        logger.info("🧠  Gemini action: %s (confidence: %s, detail: %s)", action, confidence, detail[:80])
        print(f"  {M}🧠 Diagnosis: {diagnosis[:80]}{R}")
        print(f"  {M}🧠 Action: {action} (confidence: {confidence}){R}")
        if detail:
            print(f"  {M}🧠 Detail: {detail[:60]}{R}")

        resolved = False

        # ── Execute the recommended action ─────────────────
        try:
            if action == "REINJECT":
                logger.info("🧠  Executing REINJECT via Command Palette …")
                print(f"  {C}🎯 Executing: Command Palette re-injection …{R}")
                ok = await _command_palette_inject(page, config.TINY_INJECT_STRING, context)
                action_history.append(f"REINJECT → {'SUCCESS' if ok else 'FAILED'}")
                if ok:
                    resolved = True

            elif action == "GHOST_HOTKEY":
                logger.info("🧠  Executing GHOST_HOTKEY injection …")
                print(f"  {C}👻 Executing: Ghost Hotkey injection …{R}")
                from .frame_walker import ghost_hotkey_inject
                ok = await ghost_hotkey_inject(page, config.TINY_INJECT_STRING)
                action_history.append(f"GHOST_HOTKEY → {'SUCCESS' if ok else 'FAILED'}")
                if ok:
                    resolved = True

            elif action == "CLICK_SELECTOR":
                if detail:
                    logger.info("🧠  Executing CLICK_SELECTOR: %s", detail)
                    print(f"  {C}🖱️ Executing: Click '{detail[:40]}' …{R}")
                    try:
                        from .frame_walker import find_element_in_all_frames
                        result = await find_element_in_all_frames(context, detail)
                        if result:
                            frame, el = result
                            await el.click(force=True)
                            action_history.append(f"CLICK_SELECTOR '{detail[:40]}' → SUCCESS")
                            await asyncio.sleep(2.0)
                            resolved = True
                        else:
                            action_history.append(f"CLICK_SELECTOR '{detail[:40]}' → NOT FOUND")
                    except Exception as click_exc:
                        action_history.append(f"CLICK_SELECTOR → ERROR: {click_exc}")
                else:
                    action_history.append("CLICK_SELECTOR → NO SELECTOR PROVIDED")

            elif action == "SCREENSHOT":
                logger.info("🧠  Gemini requested another screenshot for deeper analysis.")
                print(f"  {C}📸 Taking another screenshot for deeper analysis …{R}")
                try:
                    await page.screenshot(path=screenshot_path)
                    action_history.append("SCREENSHOT → taken")
                except Exception as ss_exc:
                    action_history.append(f"SCREENSHOT → ERROR: {ss_exc}")
                # Don't mark resolved — let the next round analyze the new screenshot
                continue

            elif action == "RESTART_HOST":
                logger.info("🧠  Executing RESTART_HOST (Defibrillator) …")
                print(f"  {RD}🫀 Executing: Defibrillator restart …{R}")
                ok, page = await _resuscitate_ide(page, context, goal)
                action_history.append(f"RESTART_HOST → {'SUCCESS' if ok else 'FAILED'}")
                if ok:
                    resolved = True

            elif action == "EVOLVE":
                logger.warning("🧠  Gemini recommends EVOLVE — triggering self-evolution!")
                print(f"  {RD}🧬 Gemini says the supervisor code needs to evolve!{R}")
                action_history.append("EVOLVE → triggering self_evolve()")
                from .self_evolver import self_evolve
                self_evolve(
                    f"Gemini diagnostic: {diagnosis}\n"
                    f"Trigger: {trigger}\n"
                    f"Recent logs:\n{recent_logs}",
                    screenshot_path=screenshot_path,
                )
                # self_evolve calls sys.exit(42) — we won't reach here
                return (page, False)

            else:
                logger.warning("🧠  Unknown action '%s' — defaulting to REINJECT.", action)
                ok = await _command_palette_inject(page, config.TINY_INJECT_STRING, context)
                action_history.append(f"UNKNOWN({action}) fallback REINJECT → {'SUCCESS' if ok else 'FAILED'}")
                if ok:
                    resolved = True

        except Exception as action_exc:
            logger.error("🧠  Action execution failed: %s", action_exc)
            action_history.append(f"{action} → EXCEPTION: {action_exc}")

        if resolved:
            logger.info("🧠  ✅ Diagnostic engine resolved the issue in round %d.", round_num)
            print(f"  {G}✅ Issue resolved in round {round_num}!{R}\n")
            return (page, True)

        # Wait before next round
        await asyncio.sleep(3.0)

        # Re-take screenshot for the next round's analysis
        try:
            await page.screenshot(path=screenshot_path)
            recent_logs = _read_recent_logs(40)
        except Exception:
            pass

    # All rounds exhausted — not resolved
    logger.warning("🧠  Diagnostic engine exhausted %d rounds without resolution.", max_rounds)
    print(f"  {Y}⚠️ Diagnostic engine couldn't resolve after {max_rounds} rounds.{R}\n")
    return (page, False)


# ─────────────────────────────────────────────────────────────
# CDP Connection Helper
# ─────────────────────────────────────────────────────────────

async def _connect_cdp():
    """
    Connect to Antigravity via CDP and return (browser, context, page).
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(config.CDP_URL)
    context = browser.contexts[0] if browser.contexts else None

    if not context:
        raise RuntimeError("No browser context found after CDP connection.")

    # V33: Page enumeration logging — critical for diagnosing boot failures
    for i, p in enumerate(context.pages):
        try:
            title = await p.title()
        except Exception:
            title = "<error>"
        logger.info(
            "🔌  Page %d: url=%s title=%s frames=%d",
            i, p.url[:100], title[:50], len(p.frames),
        )

    page = await _get_best_page(context)
    if not page:
        raise RuntimeError("No valid IDE page found in browser context.")

    logger.info("🔌  Connected to Antigravity via CDP. Page: %s", page.url[:80])

    # ── V33: Multi-Strategy Boot Detection Pipeline ─────────────
    # Replaces the V30 single `.monaco-grid-view` selector that never
    # matched Antigravity's DOM. Uses 7 progressive strategies, each
    # with a short timeout. ANY strategy succeeding = boot OK.
    _boot_rendered = False
    _BOOT_STRATEGIES = [
        # Strategy 1: Any div with 'workbench' in its id (broad match)
        ('div[id*="workbench"]', 'workbench div'),
        # Strategy 2: Contenteditable textbox (chat input the user sees ready)
        ('[contenteditable="true"]', 'contenteditable input'),
        # Strategy 3: Monaco editor area (if it exists in this fork)
        ('.monaco-grid-view', 'monaco grid view'),
        # Strategy 4: Any interactive input or textarea
        ('input, textarea, [role="textbox"]', 'any input element'),
    ]

    for selector, label in _BOOT_STRATEGIES:
        if _boot_rendered:
            break
        try:
            await page.wait_for_selector(selector, timeout=8000)
            logger.info("🔌  ✅ Boot detection succeeded via '%s' (selector: %s)", label, selector)
            _boot_rendered = True
            break
        except Exception:
            logger.debug("🔌  Boot strategy '%s' timed out, trying next…", label)

    # Strategy 5: JavaScript DOM size check (> 50 divs = something rendered)
    if not _boot_rendered:
        try:
            await page.wait_for_function(
                'document.querySelectorAll("div").length > 50',
                timeout=8000,
            )
            logger.info("🔌  ✅ Boot detection succeeded via JS DOM size check (>50 divs)")
            _boot_rendered = True
        except Exception:
            logger.debug("🔌  JS DOM size check timed out")

    # Strategy 6: Frame-walk ALL pages for any contenteditable element
    if not _boot_rendered:
        logger.info("🔌  Trying frame-walk across all pages…")
        from .frame_walker import find_element_in_all_frames
        try:
            result = await find_element_in_all_frames(
                context, '[contenteditable="true"]', require_visible=True
            )
            if result:
                frame, el = result
                logger.info("🔌  ✅ Boot detection via frame-walk: found contenteditable in frame")
                _boot_rendered = True
                # Use the page that contains this frame
                for p in context.pages:
                    if frame in p.frames:
                        page = p
                        break
        except Exception as fw_exc:
            logger.debug("🔌  Frame-walk boot check failed: %s", fw_exc)

    # Strategy 7: F1 Command Palette probe on ALL pages (last resort)
    if not _boot_rendered:
        logger.warning("🔌  All selector strategies failed. Trying F1 probe on ALL pages…")
        for p_idx, probe_page in enumerate(context.pages):
            try:
                url = probe_page.url
                if any(url.startswith(pfx) for pfx in config.IGNORED_PAGE_URL_PREFIXES):
                    continue
                logger.info("🔌  F1 probe on page %d: %s", p_idx, url[:60])
                await probe_page.keyboard.press("F1")
                await asyncio.sleep(2.0)
                palette = probe_page.locator(".quick-input-widget, .quick-input-box")
                if await palette.count() > 0:
                    visible = False
                    try:
                        visible = await palette.first.is_visible()
                    except Exception:
                        pass
                    if visible:
                        logger.info("🔌  ✅ F1 probe succeeded on page %d! IDE is alive.", p_idx)
                        await probe_page.keyboard.press("Escape")
                        await asyncio.sleep(0.5)
                        page = probe_page
                        _boot_rendered = True
                        break
            except Exception as f1_exc:
                logger.debug("🔌  F1 probe failed on page %d: %s", p_idx, f1_exc)

    if not _boot_rendered:
        # V33: Log full diagnostic state before raising error
        logger.critical(
            "🚫  FATAL: IDE not detected after 7 strategies. "
            "Pages: %d, URL: %s. This likely means Antigravity's DOM "
            "has changed. Check page enumeration logs above.",
            len(context.pages), page.url[:80],
        )
        raise RuntimeError(
            "IDEBootError: Workbench UI not detected after 7 boot strategies. "
            "Antigravity may have changed its DOM. Check page enumeration logs."
        )

    # Allow the extension host process to finish binding its commands
    await asyncio.sleep(3.0)

    return (pw, browser, context, page)


# ─────────────────────────────────────────────────────────────
# Main loop — V6 Unbreakable God Mode
# ─────────────────────────────────────────────────────────────

async def run(goal: str, project_path: str | None = None, dry_run: bool = False) -> None:
    """Main supervisor loop — V11 Deep Context."""
    if project_path:
        config.set_project_path(project_path)

    G = config.ANSI_GREEN
    C = config.ANSI_CYAN
    B = config.ANSI_BOLD
    R = config.ANSI_RESET

    print(f"\n{B}{G}{'='*60}{R}")
    print(f"{B}{G}  🌐 SUPERVISOR AI V11 — DEEP CONTEXT{R}")
    print(f"{B}{G}{'='*60}{R}")
    print(f"{C}  Goal: {goal[:100]}{'…' if len(goal) > 100 else ''}{R}")
    print(f"{C}  Project: {project_path or 'N/A'}{R}")
    print(f"{C}  Dry-run: {dry_run}{R}")
    print(f"{C}  Proactive: {config.PROACTIVE_MODE}{R}")
    print(f"{B}{G}{'='*60}{R}\n")

    logger.info("=" * 60)
    logger.info("🚀  Supervisor AI V11 Deep Context starting")
    logger.info("   Goal: %s", goal)
    logger.info("   Dry-run: %s", dry_run)
    logger.info("   Proactive mode: %s", config.PROACTIVE_MODE)
    logger.info("=" * 60)

    if dry_run:
        logger.info("[DRY-RUN] Skipping CDP connection.")
        logger.info("[DRY-RUN] Would inject goal: %s", goal)
        print(f"  {G}[DRY-RUN] All systems nominal. Exiting.{R}")
        return

    # ── Persist session state for auto-resume after reboot ──
    _save_session_state(goal, project_path)

    # ── System 7: Workspace Bootstrap (Mandate + Agents) ──────
    if project_path:
        bootstrap.bootstrap_workspace(project_path, goal)

    # ── V7 System 10: Zombie Hunter (Pre-flight Kill) ─────
    if project_path:
        logger.info("🧟  Zombie Hunter: Killing stale Electron/Node processes …")
        print(f"  {C}🧟 Zombie Hunter: Clearing stale processes …{R}")
        
        # Kill specific known culprits
        os.system('taskkill /f /im antigravity.exe >nul 2>&1')
        os.system('taskkill /f /im node.exe >nul 2>&1')
        
        # Brutally kill anything that happens to be using port 9222
        kill_port_cmd = (
            'for /f "tokens=5" %a in (\'netstat -ano ^| findstr :9222\') '
            'do @taskkill /f /pid %a >nul 2>&1'
        )
        os.system(kill_port_cmd)
        
        logger.info("🧟  Killed stale processes. Waiting 3s for port release …")
        print(f"  {C}⏳ Waiting 3s for port 9222 to release …{R}")
        time.sleep(3)

    # ── System 8: Visually launch Antigravity (--new-window) ──
    if project_path:
        _launch_antigravity(project_path)

    # V30.5: Connect with retry on boot failure (no more sys.exit on first try)
    for _boot_try in range(2):
        try:
            pw, browser, context, page = await _connect_cdp()
            break  # Connected successfully
        except Exception as exc:
            logger.error("Failed to connect to Antigravity (attempt %d): %s", _boot_try + 1, exc)
            if _boot_try == 0:
                await _diagnose_connection_failure(exc)
                # V31 Fix 1.4: Close old Playwright before relaunching to prevent port leak
                try:
                    logger.info("🔌  Closing old Playwright connection before relaunch…")
                    await browser.close()
                    await pw.stop()
                except Exception:
                    pass  # May not be bound yet
                logger.info("Retrying: killing zombies and relaunching Antigravity...")
                os.system('taskkill /f /im antigravity.exe >nul 2>&1')
                os.system('taskkill /f /im node.exe >nul 2>&1')
                time.sleep(3)
                if project_path:
                    _launch_antigravity(project_path)
            else:
                logger.critical("FATAL: Could not connect after 2 attempts. Exiting.")
                await _diagnose_connection_failure(exc)
                # V30.6: Flush all log handlers before exit to prevent silent death
                logging.shutdown()
                sys.stdout.flush()
                sys.exit(1)

    # ── System 8: Strict Workspace Lockdown ────────────────
    # V33.1: Lockdown runs before injection, but we MUST re-resolve
    # the page afterward because closing pages invalidates references.
    if project_path:
        await _lockdown_workspace(context, project_path)
        # Re-resolve the best page since lockdown may have closed our current one
        new_page = await _get_best_page(context)
        if new_page:
            page = new_page
            logger.info("🔒  Page re-resolved after lockdown: %s", page.url[:80])
        else:
            logger.warning("🔒  No pages left after lockdown! Using original page.")

    # ── V30: Proactive Navigation Guard (Route Interceptor) ──
    # Prevents dev servers and localhost links from hijacking the
    # Playwright IDE page context. Only allows vscode-file:// and
    # file:// navigations. All localhost requests are silently aborted
    # so they can only open in the Antigravity Browser Extension.
    async def _v30_navigation_guard(route):
        """Route interceptor: block document navigations away from IDE."""
        request = route.request
        url = request.url.lower()
        resource_type = request.resource_type

        # Only intercept top-level document navigations (not XHR, images, etc.)
        if resource_type != "document":
            await route.continue_()
            return

    # Allow IDE-internal URLs (generous whitelist)
        safe_prefixes = ("vscode-file://", "file://", "vscode-webview://",
                         "chrome-extension://", "devtools://", "data:", "about:")
        if any(url.startswith(p) for p in safe_prefixes):
            await route.continue_()
            return

        # ONLY block external http(s):// navigations if it's the main IDE window
        if url.startswith("http://") or url.startswith("https://"):
            # Frame-Aware Route Guard: allow localhost/127.0.0.1 on all frames,
            # allow fully external URLs in subframes (like the browser extension)
            import re as _re
            is_localhost = bool(_re.match(r'https?://(localhost|127\.0\.0\.1)(:\d+)?', url))

            for ide_page in context.pages:
                if request.frame == ide_page.main_frame:
                    if is_localhost:
                        logger.warning("🛡️  Blocked localhost navigation on main frame: %s", url)
                        # V35: fulfill with 204 No Content instead of abort.
                        # route.abort('blockedbyclient') causes ERR_BLOCKED_BY_CLIENT
                        # which can corrupt Electron's IPC channels.
                        await route.fulfill(status=204, body="")
                        return
                    else:
                        logger.warning("🛡️  Blocked external navigation on main frame: %s", url)
                        await route.fulfill(status=204, body="")
                        return

            # If it's a subframe (like Antigravity Browser Extension inner iframe) or localhost
            await route.continue_()
            return

        # Everything else (unknown schemes, internal) — allow
        await route.continue_()

    for ide_page in context.pages:
        try:
            await ide_page.route("**/*", _v30_navigation_guard)
            logger.debug("🛡️  V30 route guard installed on page: %s", ide_page.url[:80])
        except Exception as rg_exc:
            logger.debug("🛡️  Could not install route guard: %s", rg_exc)

    # ── V35: Instant Crash Detection via page.on('crash') ──────
    # Playwright fires 'crash' when the Electron renderer process dies.
    # This is 100× faster than waiting for the UNKNOWN coma detector (3 min).
    _renderer_crashed = False

    def _on_page_crash():
        nonlocal _renderer_crashed
        _renderer_crashed = True
        logger.critical("💀  RENDERER CRASH DETECTED via page.on('crash')!")
        print(f"  {config.ANSI_RED}💀 RENDERER CRASHED — instant detection!{config.ANSI_RESET}")

    try:
        page.on("crash", _on_page_crash)
        logger.info("💀  V35 crash listener installed on page.")
    except Exception as crash_exc:
        logger.debug("💀  Could not install crash listener: %s", crash_exc)

    # ── Defibrillator state ─────────────────────────────
    resuscitate_fail_count = 0
    last_vision_check = time.time()
    consecutive_reconnects = 0
    consecutive_working = 0            # V8: staleness detector
    working_reasons: list[str] = []    # track Gemini's WORKING claims
    consecutive_waiting = 0  # V7.4: WAITING escalation counter
    consecutive_unknown = 0  # V30.3: UNKNOWN coma detector

    # ── V11: Initialize Context Engine + Proactive Systems ──
    from .context_engine import gather_context, needs_screenshot, format_context_for_prompt
    from .vision_optimizer import should_take_screenshot, take_and_compare, screenshot_changed
    from .proactive_engine import ProactiveEngine
    from .session_memory import SessionMemory
    from .supervisor_state import StateTracker, SupervisorState

    # ── V12: Initialize OpenClaw-Inspired Systems ──
    from . import retry_policy
    from .scheduler import create_default_scheduler

    retry_policy.init()  # Initialize RetryPolicy + ModelFailoverChain + ContextBudget singletons
    scheduler = create_default_scheduler()
    logger.info("🔄  V12 OpenClaw systems initialized: RetryPolicy + ModelFailoverChain + ContextBudget + CronScheduler")

    session_mem = SessionMemory(project_path)
    session_mem.set_goal(goal)
    
    from .chat_handler import ChatHandler
    import supervisor.chat_handler as chat_module
    chat_handler = ChatHandler(session_mem)
    proactive = ProactiveEngine()
    
    # V35: Structured state machine (runs alongside existing counters)
    state_tracker = StateTracker()
    logger.info("🔄  V35 StateTracker initialized: %s", state_tracker.state.value)

    last_heartbeat = time.time()
    _chat_was_found = False  # V35: Extension host health tracking
    logger.info("🧠  V11 systems initialized: Context Engine + Vision Optimizer + Proactive Engine + Session Memory")

    logger.info("✅ Entering monitoring loop (poll every %.1fs, vision every %.1fs) …",
                 config.POLL_INTERVAL_SECONDS, config.VISION_POLL_INTERVAL_SECONDS)
    print(f"  {G}✅ Entering V11 God Loop …{R}\n")

    # ── THE MAIN MONITORING LOOP ─────────────────────────
    is_first_boot = True
    _monitor_start_time = time.time()  # Used by msgs=0 grace period

    try:
        while True:
            # ── V15 Pre-emptive Strike (Zero-Latency Injection) ──
            if is_first_boot:
                is_first_boot = False
                # We skip the initial 10s wait and 30s Vision Poll to instantly boot
                if project_path and _lockfile_exists(project_path):
                    # V16+: Even with lockfile, check if chat is actually empty.
                    # A stale lockfile from a crashed session would skip injection
                    # and leave the supervisor staring at a blank chat forever.
                    # FIX: With the phantom message filter, msgs=0 on blank chats.
                    logger.info("🔐  [INFO] Lockfile found. Checking if chat is populated...")
                    try:
                        _boot_ctx = await gather_context(context, page, goal)
                        _boot_msgs = len(_boot_ctx.chat_messages) if _boot_ctx else 0
                    except Exception:
                        _boot_msgs = 0

                    if _boot_msgs == 0:
                        # Chat is genuinely empty — lockfile is definitely stale
                        logger.warning(
                            "🔐  STALE LOCKFILE: chat has 0 messages. "
                            "Ignoring lockfile and forcing immediate mandate injection."
                        )
                        print(
                            f"  {config.ANSI_YELLOW}🔐 STALE LOCKFILE — empty chat "
                            f"(msgs=0). Forcing immediate re-injection...{R}"
                        )
                        _remove_lockfile(project_path)
                        is_first_boot = True
                        continue  # Re-enter loop to hit the else branch
                    elif _boot_msgs <= 1:
                        # Only placeholder or minimal content — likely stale too
                        logger.warning(
                            "🔐  Lockfile exists but chat has only %d messages — "
                            "STALE LOCKFILE. Forcing re-injection.", _boot_msgs
                        )
                        print(
                            f"  {config.ANSI_YELLOW}🔐 STALE LOCKFILE detected "
                            f"(msgs={_boot_msgs}). Forcing re-injection...{R}"
                        )
                        _remove_lockfile(project_path)
                        is_first_boot = True
                        continue  # Re-enter loop to hit the else branch
                    else:
                        logger.info(
                            "🔐  Lockfile valid — chat has %d messages. Resuming.",
                            _boot_msgs,
                        )
                        print(f"  {C}🔐 [INFO] Lockfile valid (msgs={_boot_msgs}). Resuming.{R}")
                        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                else:
                    logger.info("💉  No lockfile — first injection via File-Drop + Command Palette …")
                    print(f"\n  {B}{C}💉 FIRST INJECTION VIA PRE-EMPTIVE STRIKE{R}")
    
                    tiny_string = config.TINY_INJECT_STRING
    
                    # Wait until the chat frame is discoverable (extension host is bound)
                    from .frame_walker import find_chat_frame
                    chat_ready = False
                    for _boot_attempt in range(10):
                        if await find_chat_frame(context):
                            chat_ready = True
                            break
                        await asyncio.sleep(1.0)
                    if chat_ready:
                        logger.info("💉  Chat frame ready — proceeding with injection.")
                    else:
                        logger.warning("💉  Chat frame not found after 10s — proceeding anyway.")
    
                    injection_success = False
                    for attempt in range(3):
                        try:
                            if await _command_palette_inject(page, tiny_string, context):
                                injection_success = True
                                break
    
                            logger.warning("🎯  Command Palette failed — trying DOM fallback …")
                            golden_sel = 'div[contenteditable="true"][role="textbox"]'
                            el = await page.query_selector(golden_sel)
                            if el:
                                await el.focus()
                                await asyncio.sleep(0.3)
                                await page.keyboard.insert_text(tiny_string)
                                await asyncio.sleep(0.3)
                                await page.keyboard.press("Enter")
                                injection_success = True
                                logger.info("💉  DOM fallback injection succeeded.")
                                break
                        except Exception as inj_exc:
                            exc_str = str(inj_exc).lower()
                            if "closed" in exc_str or "connection" in exc_str:
                                logger.warning("💉  Injection attempt %d failed (Target Closed). Re-acquiring page...", attempt + 1)
                                await asyncio.sleep(2.0)
                                try:
                                    page = await _get_best_page(context) or page
                                except Exception:
                                    pass
                            else:
                                logger.error("💉  Injection attempt %d failed: %s", attempt + 1, inj_exc)
                            await asyncio.sleep(1.0)
    
                    if not injection_success:
                        raise Exception("Injection Verification Failed: All strategies failed. UI is completely unresponsive.")
    
                    if project_path:
                        _create_lockfile(project_path)
                    
                    # Instantly reset vision check timer so we DO NOT run vision on the same tick we inject
                    last_vision_check = time.time()
                    continue  # Skip to next tick so it sleeps 10s
            else:
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

            # ── V12: Scheduler tick ──
            try:
                sched_results = await scheduler.tick()
                if sched_results:
                    for sr in sched_results:
                        logger.info("⏰  %s", sr)
            except Exception as sched_exc:
                logger.debug("⏰  Scheduler tick error: %s", sched_exc)

            # ====================================================
            # V35: Instant Renderer Crash Recovery
            # page.on('crash') fires instantly when Electron renderer
            # dies — 100× faster than the UNKNOWN coma detector.
            # ====================================================
            if _renderer_crashed:
                _renderer_crashed = False
                logger.critical("💀  Handling renderer crash — forcing resuscitation …")
                print(f"  {config.ANSI_RED}💀 Renderer crashed — resuscitating immediately!{config.ANSI_RESET}")
                session_mem.record_event("renderer_crash", "Detected via page.on('crash')")
                ok, page = await _resuscitate_ide(page, context, goal)
                if ok:
                    resuscitate_fail_count = 0
                    consecutive_unknown = 0
                    consecutive_waiting = 0
                    # Re-install crash listener on new page
                    try:
                        page.on("crash", _on_page_crash)
                    except Exception:
                        pass
                else:
                    resuscitate_fail_count += 1
                    if resuscitate_fail_count >= config.MAX_RESUSCITATE_FAILURES:
                        raise Exception(
                            f"Defibrillator failed {config.MAX_RESUSCITATE_FAILURES} "
                            f"times after renderer crash."
                        )
                continue

            # ====================================================
            # System 0: Proactive Page Staleness Detection
            # The page handle can go stale when Antigravity
            # reloads, closes tabs, or the extension host crashes.
            # Internal code (vision, command palette) catches these
            # silently, so the reconnect path at the bottom of the
            # loop never fires. Check BEFORE using the page.
            # ====================================================
            try:
                if page.is_closed():
                    logger.warning("🔄  Page handle is stale — re-acquiring …")
                    print(f"  {config.ANSI_YELLOW}🔄 Page stale — re-acquiring …{config.ANSI_RESET}")
                    await asyncio.sleep(2.0)
                    new_page = await _get_best_page(context)
                    if new_page:
                        page = new_page
                        logger.info("🔄  Re-acquired page: %s", page.url[:80])
                    else:
                        logger.warning("🔄  No valid page found. Will retry next tick.")
                        continue
            except Exception as stale_exc:
                logger.debug("🔄  Staleness check failed: %s", stale_exc)
                await asyncio.sleep(3.0)
                try:
                    new_page = await _get_best_page(context)
                    if new_page:
                        page = new_page
                except Exception:
                    pass
                continue

            # ====================================================
            # System 2: Auto-Reconnect Engine (Anti-Crash)
            # Wraps ALL Playwright interactions in a try/except
            # that catches TargetClosedError gracefully.
            # ====================================================
            try:
                # ── System 5: Navigation Defense ───────────
                page, was_hijacked = await _navigation_defense(page, context)
                if was_hijacked:
                    session_mem.record_event("navigation_hijack", "Recovered")
                    await asyncio.sleep(2.0)
                    continue

                # ── System 3: Approval Sniper ──────────────
                sniped = await _approval_sniper(context)
                if sniped:
                    consecutive_waiting = 0  # V7.4: reset on approval
                    consecutive_working = 0  # V8: real progress detected
                    working_reasons.clear()
                    session_mem.record_event("approval_clicked", f"Sniped {sniped} buttons")
                    await asyncio.sleep(2.0)
                    continue

                # ── OpenClaw Tier 2: Background Cron Tasks ─
                try:
                    cron_results = await scheduler.tick()
                    if cron_results:
                        for cr in cron_results:
                            logger.info("⏰  %s", cr)
                except Exception as _cron_err:
                    logger.debug("⏰  Scheduler tick error: %s", _cron_err)

                # ── V11: Proactive Heartbeat ───────────────
                if config.PROACTIVE_MODE:
                    hb_now = time.time()
                    if hb_now - last_heartbeat >= config.HEARTBEAT_INTERVAL_SECONDS:
                        last_heartbeat = hb_now
                        try:
                            ctx_snap = await gather_context(context, page, goal)
                            session_mem.record_context_gather()
                            actions = await proactive.heartbeat(ctx_snap, page, context)
                            for a in actions:
                                logger.info("💡  Proactive: %s", a)
                                print(f"  {C}💡 Proactive: {a}{R}")
                                session_mem.record_event("proactive_action", a)

                            # V35: Extension host health monitoring
                            # If chat was previously found but now vanished,
                            # the extension host likely restarted.
                            from .frame_walker import find_chat_frame
                            _chat_now = await find_chat_frame(context)
                            if _chat_now:
                                _chat_was_found = True
                            elif _chat_was_found:
                                # Chat was found before but gone now → extension host restart
                                logger.critical(
                                    "🔌  EXTENSION HOST RESTART DETECTED: "
                                    "chat frame vanished while page is alive."
                                )
                                print(
                                    f"  {config.ANSI_RED}🔌 Extension host restart detected! "
                                    f"Re-opening chat …{config.ANSI_RESET}"
                                )
                                session_mem.record_event("extension_host_restart", "Chat frame vanished")
                                _chat_was_found = False  # Reset for re-detection
                                # Try to re-open chat
                                try:
                                    await _run_palette_command(
                                        page, "open_agent_chat",
                                        "Open Chat with Agent", context=context,
                                    )
                                    await asyncio.sleep(3.0)
                                except Exception as ext_exc:
                                    logger.warning("🔌  Failed to re-open chat: %s", ext_exc)

                        except Exception as hb_exc:
                            logger.warning("💡  Heartbeat error: %s", hb_exc)

                # ── V30.2: Rate Limit Cooldown Guard ─────────
                # If ALL Gemini models are on cooldown, skip the tick
                # entirely instead of crashing into a 429 wall.
                from .retry_policy import get_failover_chain
                _failover = get_failover_chain()
                if _failover.all_models_on_cooldown():
                    _wait_secs = min(60, _failover.get_soonest_cooldown_remaining())
                    logger.warning(
                        "⚡  ALL models on cooldown — skipping tick (sleeping %.0fs)",
                        _wait_secs,
                    )
                    print(
                        f"  {config.ANSI_MAGENTA}⚡ All models on cooldown — "
                        f"sleeping {_wait_secs:.0f}s{config.ANSI_RESET}"
                    )
                    await asyncio.sleep(max(5.0, _wait_secs))
                    continue

                # ── Multimodal Vision God Loop ─────────────
                now = time.time()
                if now - last_vision_check >= config.VISION_POLL_INTERVAL_SECONDS:
                    last_vision_check = now

                    # V11: Text-first analysis — gather structured context
                    try:
                        ctx_snapshot = await gather_context(context, page, goal)
                        session_mem.record_context_gather()

                        # OpenClaw Tier 3: Runtime Slash Commands
                        if await chat_handler.process_slash_commands(ctx_snapshot):
                            continue
                            
                        # If paused by a slash command, skip logic
                        if chat_module.IS_PAUSED:
                            continue

                    except Exception as ctx_exc:
                        logger.warning("📊  Context gather error: %s — using default state", ctx_exc)
                        ctx_snapshot = None

                    # V11: Skip screenshot if text context is sufficient
                    if ctx_snapshot and not needs_screenshot(ctx_snapshot):
                        # Use text-derived state directly
                        state = ctx_snapshot.agent_status
                        reason = f"Text analysis (confidence={ctx_snapshot.confidence:.0%})"
                        session_mem.record_event("screenshot_skipped", reason)

                        # Map context states to vision states
                        if state in ("WORKING", "IDLE"):
                            state = "WORKING"
                        elif state == "ASKING":
                            state = "WAITING"
                        # fall through to state handling below
                    else:
                        # V11: Screenshot with diff detection
                        changed = True
                        if ctx_snapshot:
                            try:
                                changed, _ = await take_and_compare(page, config.SCREENSHOT_PATH)
                            except Exception:
                                changed = True  # Assume changed on error

                        if not changed:
                            # Screen unchanged — skip Gemini call
                            state = "WORKING"  # Assume same as last
                            reason = "Screenshot unchanged — skipping vision"
                            session_mem.record_event("screenshot_skipped", "unchanged")
                            logger.info("📸  %s", reason)
                        else:
                            # Full Gemini vision analysis (with enriched context)
                            ctx_prompt = ""
                            if ctx_snapshot:
                                ctx_prompt = format_context_for_prompt(ctx_snapshot)

                            try:
                                vision_result = await _analyze_ide_state(
                                    page, goal=goal,
                                    consecutive_waiting=consecutive_waiting,
                                    context=context,
                                )
                                state = vision_result.get("state", "WORKING")
                                reason = vision_result.get("reason", "")
                            except Exception as v_exc:
                                if "ALL_MODELS_EXHAUSTED" in str(v_exc):
                                    logger.warning("⚡  ALL models exhausted due to rate limits. Sleeping 60s...")
                                    print(f"  {config.ANSI_MAGENTA}⚡ All models exhausted. Sleeping 60s...{config.ANSI_RESET}")
                                    await asyncio.sleep(60.0)
                                    continue
                                else:
                                    logger.error("📸  Vision system error: %s", v_exc)
                                    state = "WORKING"
                                    reason = f"Vision offline: {v_exc}"
                            session_mem.record_event("screenshot_taken", f"{state}: {reason[:100]}")

                    # V11: Update session memory with status
                    session_mem.update_status(state)
                    if ctx_snapshot and ctx_snapshot.progress.files_mentioned:
                        session_mem.record_files(ctx_snapshot.progress.files_mentioned)

                    if state == "CRASHED":
                        consecutive_working = 0  # V8: not stale, it's broken
                        working_reasons.clear()
                        logger.warning("👁️  Vision detected CRASHED: %s", reason)
                        print(f"  {config.ANSI_RED}👁️ CRASHED: {reason}{R}")

                        ok, page = await _resuscitate_ide(page, context, goal)
                        if ok:
                            resuscitate_fail_count = 0
                            await asyncio.sleep(5.0)
                            continue
                        else:
                            resuscitate_fail_count += 1
                            logger.error(
                                "🫀  Resuscitation failed (%d/%d).",
                                resuscitate_fail_count,
                                config.MAX_RESUSCITATE_FAILURES,
                            )
                            if resuscitate_fail_count >= config.MAX_RESUSCITATE_FAILURES:
                                raise Exception(
                                    f"Defibrillator failed {config.MAX_RESUSCITATE_FAILURES} "
                                    f"times. Last reason: {reason}"
                                )
                            continue

                    elif state == "WAITING":
                        consecutive_working = 0  # V8: not stale, it's stuck
                        working_reasons.clear()
                        consecutive_waiting += 1
                        logger.info(
                            "👁️  Vision detected WAITING (%d consecutive): %s",
                            consecutive_waiting, reason,
                        )
                        print(
                            f"  {config.ANSI_YELLOW}👁️ WAITING "
                            f"({consecutive_waiting}×): {reason}{R}"
                        )

                        # V8: WAITING Escalation — Fast retry → Agent Council
                        if consecutive_waiting == 1:
                            logger.info("⚡  Fast-path: AI is waiting. Synthesizing follow-up...")
                            
                            # 1. Grab chat history
                            from .context_engine import gather_context, format_context_for_prompt
                            chat_history = ""
                            if ctx_snapshot and ctx_snapshot.chat_messages:
                                chat_history = format_context_for_prompt(ctx_snapshot, max_chars=2000)

                            # GUARD: If context engine returned 0 messages, the chat frame
                            # is unreadable. Do NOT blindly synthesize.
                            # Use os._exit(1) to bypass the AutoRecoveryEngine which
                            # would catch a normal exception and reconnect blindly.
                            # Grace period: only kill if supervisr has been running 5+ min.
                            if not ctx_snapshot or len(ctx_snapshot.chat_messages) == 0:
                                uptime_s = time.time() - _monitor_start_time
                                if uptime_s >= 300:  # 5 minutes
                                    err_msg = (
                                        "FATAL: SUPERVISOR IS BLIND. "
                                        "Chat frame unreadable: context engine returned msgs=0 "
                                        f"after {uptime_s:.0f}s uptime. "
                                        "HALTING TO PREVENT BLIND INJECTIONS."
                                    )
                                    logger.critical("🚫  %s", err_msg)
                                    print(f"\n  {config.ANSI_RED}{'='*65}")
                                    print(f"  {err_msg}")
                                    print(f"  {'='*65}{config.ANSI_RESET}\n")
                                    # V31 Fix 1.2: Graceful shutdown instead of os._exit(1)
                                    # os._exit skips ALL cleanup: no log flush, no lockfile removal.
                                    # sys.exit(42) is the self-evolution restart code — the daemon
                                    # will restart us cleanly with flushed logs.
                                    if project_path:
                                        _remove_lockfile(project_path)
                                    logging.shutdown()
                                    sys.stdout.flush()
                                    sys.exit(42)
                                else:
                                    logger.info(
                                        "⏳  msgs=0 but uptime is only %.0fs — within grace period.",
                                        uptime_s,
                                    )
                                    continue  # Skip this WAITING cycle
                                
                            # 2. Synthesize follow-up locally (No cost)
                            # V32: Lazy-init singleton — don't re-create on every tick
                            from .local_orchestrator import LocalManager, OllamaUnavailable
                            if '_local_mgr_singleton' not in dir():
                                try:
                                    _local_mgr_singleton = LocalManager()
                                except OllamaUnavailable:
                                    logger.warning("🧠  Ollama unavailable — skipping local LLM follow-up (non-fatal)")
                                    _local_mgr_singleton = None
                            local_mgr = _local_mgr_singleton
                            
                            # V30.2 Total Automation: Feed project state & mandate to local LLM
                            import os as _os
                            project_state = ""
                            mandate_text = ""
                            if project_path:
                                state_file = _os.path.join(project_path, "PROJECT_STATE.md")
                                mandate_file = _os.path.join(project_path, config.MANDATE_FILENAME)
                                if _os.path.exists(state_file):
                                    try:
                                        with open(state_file, "r", encoding="utf-8") as f:
                                            project_state = f.read()
                                    except Exception:
                                        pass
                                if _os.path.exists(mandate_file):
                                    try:
                                        with open(mandate_file, "r", encoding="utf-8") as f:
                                            mandate_text = f.read()
                                    except Exception:
                                        pass

                            prompt = await local_mgr.synthesize_followup(
                                chat_history=chat_history, 
                                system_goal=goal,
                                project_state=project_state,
                                mandate=mandate_text
                            )
                            
                            logger.info("🧠  Synthesized Follow-up: %s", prompt)
                            print(f"  {config.ANSI_CYAN}🧠 Local LLM Synthesized: '{prompt}'{config.ANSI_RESET}")

                            # 3. Inject
                            from .frame_walker import ghost_hotkey_inject
                            await ghost_hotkey_inject(page, prompt)
                            continue

                        if consecutive_waiting >= config.WAITING_GHOST_THRESHOLD:
                            # Lightweight re-inject failed — convene the council
                            logger.info(
                                "🏛️  WAITING %d× — convening Agent Council",
                                consecutive_waiting,
                            )

                            # Build action callbacks so the council can execute
                            async def _cb_click(ctx, sel):
                                from .frame_walker import find_element_in_all_frames
                                r = await find_element_in_all_frames(ctx, sel)
                                if r:
                                    _, el = r
                                    await el.click()
                                    return True
                                return False

                            from .frame_walker import ghost_hotkey_inject
                            action_callbacks = {
                                "reinject": _command_palette_inject,
                                "ghost_hotkey": ghost_hotkey_inject,
                                "click_selector": _cb_click,
                                "resuscitate": _resuscitate_ide,
                            }

                            # V12 Time-Travel: Snapshot the universe right before we intervene
                            if ctx_snapshot:
                                session_mem.snapshot_state(ctx_snapshot)

                            council = AgentCouncil()
                            council_issue = CouncilIssue(
                                issue_type="WAITING",
                                trigger=f"WAITING state detected: {reason}",
                                screenshot_path=str(config.SCREENSHOT_PATH),
                                goal=goal,
                                consecutive_count=consecutive_waiting,
                            )
                            try:
                                resolution = await council.convene(
                                    council_issue, page, context, action_callbacks,
                                )

                                # Update page reference if changed
                                if resolution.page:
                                    page = resolution.page

                                if resolution.resolved:
                                    consecutive_waiting = 0
                                    resuscitate_fail_count = 0
                                else:
                                    consecutive_waiting = 0
                                    resuscitate_fail_count += 1

                                    # If council says EVOLVE, trigger self-evolution
                                    if resolution.action == "EVOLVE" and resolution.code_patch:
                                        from .self_evolver import self_evolve
                                        self_evolve(
                                            f"Council directive: {resolution.diagnosis}\n"
                                            f"Trigger: {reason}",
                                            screenshot_path=str(config.SCREENSHOT_PATH),
                                        )
                            except Exception as c_exc:
                                if "ALL_MODELS_EXHAUSTED" in str(c_exc):
                                    # V31 Fix 2.4: Query actual cooldown instead of hardcoded 60s
                                    from .retry_policy import get_failover_chain as _gfc
                                    _actual_wait = _gfc().seconds_until_any_available()
                                    _wait_s = max(60, min(_actual_wait, 600))  # 60s..600s
                                    logger.warning("⚡  Rate limit exhaustion during council. Sleeping %ds (actual cooldown: %ds)...", _wait_s, _actual_wait)
                                    print(f"  {config.ANSI_MAGENTA}⚡ All models exhausted. Sleeping {_wait_s}s...{config.ANSI_RESET}")
                                    await asyncio.sleep(_wait_s)
                                    continue
                                else:
                                    logger.error("🏛️  Council session failed: %s", c_exc)
                                    consecutive_waiting = 0

                        elif consecutive_waiting >= config.WAITING_REINJECT_THRESHOLD:
                            # Quick lightweight re-inject (no council needed)
                            logger.warning(
                                "🎯  WAITING %d× — quick re-inject via Command Palette",
                                consecutive_waiting,
                            )
                            print(
                                f"  {config.ANSI_CYAN}🎯 WAITING {consecutive_waiting}× "
                                f"— Command Palette re-injection{R}"
                            )
                            # V12 Time-Travel: Snapshot the universe right before we hit the hotkey
                            if ctx_snapshot:
                                session_mem.snapshot_state(ctx_snapshot)
                                
                            await _command_palette_inject(
                                page, config.TINY_INJECT_STRING, context
                            )

                    # V34: UNKNOWN Coma Detection — MUST be at elif level
                    # (was previously dead code: nested inside WAITING block)
                    elif state == "UNKNOWN":
                        consecutive_working = 0
                        working_reasons.clear()
                        consecutive_unknown += 1
                        logger.warning(
                            "💤  UNKNOWN (%d consecutive): %s",
                            consecutive_unknown, reason,
                        )
                        print(
                            f"  {config.ANSI_YELLOW}💤 UNKNOWN state "
                            f"({consecutive_unknown}×): {reason}{R}"
                        )

                        if consecutive_unknown >= 6:
                            logger.critical(
                                "🚫  UNKNOWN COMA: %d consecutive UNKNOWN states (~3min blind). "
                                "Forcing page reload to recover.",
                                consecutive_unknown,
                            )
                            print(
                                f"  {config.ANSI_RED}🚫 UNKNOWN COMA detected! "
                                f"Reloading page to recover…{config.ANSI_RESET}"
                            )
                            try:
                                await page.reload(timeout=15000)
                                await asyncio.sleep(5.0)
                                page = await _get_best_page(context)
                                consecutive_unknown = 0
                            except Exception as reload_exc:
                                logger.error("🚫  Page reload failed: %s", reload_exc)
                                ok, page = await _resuscitate_ide(page, context, goal)
                                consecutive_unknown = 0
                                if not ok:
                                    resuscitate_fail_count += 1

                        # V34 Fix 4: Hard idle watchdog — if blind 10min+ and msgs=0, force resuscitation
                        if ctx_snapshot and getattr(ctx_snapshot, 'idle_seconds', 0) > 600 and len(ctx_snapshot.chat_messages) == 0:
                            logger.critical(
                                "🚨  WATCHDOG: idle=%ds, msgs=0. Forcing full resuscitation.",
                                ctx_snapshot.idle_seconds,
                            )
                            print(
                                f"  {config.ANSI_RED}🚨 WATCHDOG: "
                                f"idle={ctx_snapshot.idle_seconds}s, msgs=0 — resuscitating!{config.ANSI_RESET}"
                            )
                            ok, page = await _resuscitate_ide(page, context, goal)
                            if ok:
                                consecutive_unknown = 0
                                consecutive_waiting = 0
                        continue

                    else:
                        # WORKING state — reset unknown counter
                        consecutive_unknown = 0
                        if consecutive_waiting > 0:
                            logger.info(
                                "✅  Agent resumed working after %d WAITING cycles.",
                                consecutive_waiting,
                            )
                        consecutive_waiting = 0

                        # V8: Staleness detection — track consecutive WORKING
                        consecutive_working += 1
                        working_reasons.append(reason)
                        logger.debug(
                            "👁️  Vision: WORKING (%d×) — %s",
                            consecutive_working, reason,
                        )

                        # If WORKING reported too many times with no actual
                        # progress, do a skeptical re-check with a pointed
                        # prompt that forces Gemini to justify its claim.
                        if consecutive_working >= config.WORKING_STALE_THRESHOLD:
                            recheck = await _skeptical_recheck(
                                page, goal, consecutive_working, working_reasons,
                                context=context,
                            )
                            recheck_state = recheck.get("state", "WORKING")
                            recheck_reason = recheck.get("reason", "")

                            if recheck_state != "WORKING":
                                # The re-check disagrees — use the corrected state
                                logger.warning(
                                    "🔍  Skeptical re-check overrode WORKING → %s: %s",
                                    recheck_state, recheck_reason,
                                )
                                # Feed the corrected state back into the
                                # state machine by re-assigning and re-running
                                # the state handling (set state, clear working)
                                consecutive_working = 0
                                working_reasons.clear()
                                state = recheck_state
                                reason = recheck_reason

                                if state == "WAITING":
                                    consecutive_waiting += 1
                                    print(
                                        f"  {config.ANSI_YELLOW}👁️ CORRECTED → WAITING "
                                        f"({consecutive_waiting}×): {reason}{config.ANSI_RESET}"
                                    )
                                    # Jump to re-injection immediately
                                    if consecutive_waiting >= config.WAITING_REINJECT_THRESHOLD:
                                        await _command_palette_inject(
                                            page, config.TINY_INJECT_STRING, context
                                        )
                                elif state == "CRASHED":
                                    print(
                                        f"  {config.ANSI_RED}👁️ CORRECTED → CRASHED: "
                                        f"{reason}{config.ANSI_RESET}"
                                    )
                                    ok, page = await _resuscitate_ide(
                                        page, context, goal
                                    )
                                    if ok:
                                        resuscitate_fail_count = 0
                            else:
                                # Re-check confirmed WORKING — reset counters
                                logger.info(
                                    "🔍  Skeptical re-check confirmed WORKING. "
                                    "Resetting staleness counter."
                                )
                                consecutive_working = 0
                                working_reasons.clear()

                # If we got here without error, reset reconnect counter
                consecutive_reconnects = 0
                _recovery_engine.record_success()

            except Exception as loop_exc:
                # ============================================
                # System 2: Auto-Reconnect Engine
                # ============================================
                exc_str = str(loop_exc).lower()
                is_page_closed = (
                    "target page" in exc_str
                    or "closed" in exc_str
                    or "targetclosederror" in exc_str
                    or "target closed" in exc_str
                    or "connection" in exc_str
                )

                if is_page_closed:
                    consecutive_reconnects += 1
                    Y = config.ANSI_YELLOW
                    logger.warning(
                        "🔄  [WARNING] IDE webview refreshed. "
                        "Re-attaching … (attempt %d)",
                        consecutive_reconnects,
                    )
                    print(
                        f"  {Y}🔄 [WARNING] IDE webview refreshed. "
                        f"Re-attaching … (attempt {consecutive_reconnects}){R}"
                    )

                    # Wait 3 seconds for the IDE to stabilize
                    await asyncio.sleep(3.0)

                    # Try to re-fetch best page
                    try:
                        new_page = await _get_best_page(context)
                        if new_page:
                            page = new_page
                            logger.info(
                                "🔄  Re-attached to page: %s", page.url[:80]
                            )
                            print(f"  {G}🔄 Re-attached to: {page.url[:60]}{R}")
                        else:
                            logger.warning("🔄  No valid page found — waiting …")
                            await asyncio.sleep(5.0)
                    except Exception as refetch_exc:
                        logger.error("🔄  Re-fetch failed: %s", refetch_exc)
                        await asyncio.sleep(5.0)

                    # Safety: if too many consecutive reconnects, use recovery engine
                    if consecutive_reconnects >= 10:
                        strategy = _recovery_engine.recover(
                            f"Too many consecutive reconnects ({consecutive_reconnects})"
                        )
                        logger.warning("🔄  Recovery engine recommends: %s", strategy)
                        consecutive_reconnects = 0
                        # Strategy handling: RECONNECT just loops, others need action
                        if strategy == "EVOLVE":
                            tb = traceback.format_exc()
                            self_evolve(tb, screenshot_path=config.SCREENSHOT_PATH)

                    continue  # ← Resume the loop, do NOT crash
                else:
                    # Use recovery engine for non-page-closed errors too
                    strategy = _recovery_engine.recover(str(loop_exc))
                    logger.warning("🔄  Recovery engine strategy for real error: %s", strategy)
                    if strategy == "EVOLVE":
                        tb = traceback.format_exc()
                        self_evolve(tb, screenshot_path=config.SCREENSHOT_PATH)
                    continue

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as fatal_exc:
        # ── SELF-EVOLUTION TRIGGER ─────────────────────────
        tb = traceback.format_exc()
        logger.critical("💀  FATAL EXCEPTION in main loop: %s\n%s", fatal_exc, tb)
        print("\n" + "=" * 60)
        print("💀  FATAL ERROR — consulting Gemini for triage …")
        print("=" * 60)

        # V7.2: Include crash forensics in triage and evolution context
        crash_history = _recovery_engine.get_crash_log()
        if crash_history:
            crash_ctx = json.dumps(crash_history[-5:], indent=2)
            tb += f"\n\n=== RECOVERY ENGINE CRASH LOG (last 5) ===\n{crash_ctx}"

        # V6: 180s timeout for triage — no more triage timeouts
        decision = _triage_fatal_error(tb)

        if decision == "retry":
            logger.info("🧠  Gemini says RETRY — exiting with code 42 for reboot.")
            print("  🧠 Gemini diagnosis: Transient error — will retry.")
            sys.exit(42)
        else:
            logger.info("🧠  Gemini says EVOLVE — triggering self-evolution.")
            print("  🧠 Gemini diagnosis: Code bug — triggering self-evolution.")
            self_evolve(tb, screenshot_path=config.SCREENSHOT_PATH)
    finally:
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass
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
        description="Supervisor AI V7 — Flawless Precision",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python -m supervisor --goal "Build a React dashboard" -p ./myproject\n'
            '  python -m supervisor --goal "Fix failing tests" --dry-run\n'
            '  python -m supervisor  (interactive mode)\n'
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

    # ── Infinite Memory: dual logging (console + file) ──────
    log_level = getattr(logging, args.log_level)
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    fmt = logging.Formatter(config.LOG_FORMAT, datefmt=config.LOG_DATE_FORMAT)

    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(log_level)
        console.setFormatter(fmt)
        root_logger.addHandler(console)

    # ── V30/V30.5 Ephemeral Logging ────────────────────────────
    # V30: Rotate previous log to .log.bak on fresh user session.
    # V30.5: If the log was modified recently (< 120s), this is an
    # auto-restart — APPEND to preserve context. Only wipe on fresh start.
    _log_mode = "w"  # Default: fresh session, wipe the log
    try:
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > 0:
            log_age_s = time.time() - _LOG_FILE.stat().st_mtime
            if log_age_s < 120:
                # Recent log = auto-restart, preserve it
                _log_mode = "a"
                logger.info("📝  Auto-restart detected (log age: %.0fs). Appending to existing log.", log_age_s)
            else:
                # Stale log = fresh user session, rotate and wipe
                bak_path = _LOG_FILE.with_suffix(".log.bak")
                import shutil
                shutil.copy2(str(_LOG_FILE), str(bak_path))
                logger.info("📝  Rotated previous log to %s", bak_path)
    except Exception:
        pass  # Non-fatal

    try:
        file_handler = logging.FileHandler(str(_LOG_FILE), encoding="utf-8", mode=_log_mode)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root_logger.addHandler(file_handler)
        if _log_mode == "w":
            logger.info("📝  V30 Ephemeral Log: fresh session → %s", _LOG_FILE)
        else:
            logger.info("📝  V30.5: Appending to existing log → %s", _LOG_FILE)
    except Exception as exc:
        logger.warning("Could not set up file logging: %s", exc)

    # ── Resolve goal + project path ────────────────────────
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

            # 5-second countdown with non-blocking key check
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
                                    # Enter pressed — continue immediately
                                    remaining = 0
                                    break
                            time.sleep(0.05)
                        if cancelled or remaining == 0:
                            break
                    print()  # newline after countdown
                else:
                    # Non-Windows fallback: simple timed input
                    import select
                    print("  ⏳ Auto-continuing in 5s …", flush=True)
                    ready, _, _ = select.select([sys.stdin], [], [], 5.0)
                    if ready:
                        user_input = sys.stdin.readline().strip().upper()
                        if user_input == "N":
                            cancelled = True
            except Exception:
                pass  # On any input error, just auto-continue

            if cancelled:
                print(f"\n  {Y}⛔ Session cancelled. Opening interactive menu …{R}\n")
                _clear_session_state()
                # Fall through to interactive menu below
            else:
                print(f"  {G}✅ Continuing saved session.{R}")
                logger.info("✅  Continuing saved session.")
                goal = saved_goal
                project_path = saved_project

    if not goal:
        # No saved session or user cancelled — show interactive menu
        goal, project_path = _interactive_goal()

    # ── Run the async loop ─────────────────────────────────
    asyncio.run(run(goal, project_path, args.dry_run))


if __name__ == "__main__":
    main()
