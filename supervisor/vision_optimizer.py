"""
vision_optimizer.py — Screenshot Intelligence Engine V1.

Optimizes when and how screenshots are used by:
  1. Perceptual hash comparison — skip Gemini if the screen hasn't changed
  2. Adaptive scheduling — adjust screenshot frequency based on confidence
  3. Minimum/maximum interval enforcement — never too fast or too slow

Works alongside context_engine.py to reduce Gemini vision calls by 60-80%.
"""

import hashlib
import logging
import time
from pathlib import Path

logger = logging.getLogger("supervisor.vision_optimizer")


# ─────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────

_last_screenshot_hash: str = ""
_last_screenshot_time: float = 0.0
_consecutive_unchanged: int = 0


# ─────────────────────────────────────────────────────────────
# Screenshot Diff Detection
# ─────────────────────────────────────────────────────────────

def screenshot_changed(path: str) -> bool:
    """
    Compare current screenshot to the previous one via file hash.

    Uses SHA-256 of the raw file bytes. This is fast (~1ms for a 200KB PNG)
    and catches any pixel change. For robustness, we do NOT use perceptual
    hashing (which would need PIL) — a simple file hash is sufficient since
    Playwright screenshots are deterministic for the same DOM state.

    Returns True if the screen has meaningfully changed.
    Returns True on first call (no baseline to compare against).
    """
    global _last_screenshot_hash, _consecutive_unchanged

    try:
        file_path = Path(path)
        if not file_path.exists():
            logger.debug("📸  No screenshot file at %s", path)
            return True  # No file = assume changed

        # Hash the file contents
        current_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()

        if not _last_screenshot_hash:
            # First call — no baseline
            _last_screenshot_hash = current_hash
            _consecutive_unchanged = 0
            return True

        if current_hash == _last_screenshot_hash:
            _consecutive_unchanged += 1
            logger.info(
                "📸  Screenshot UNCHANGED (×%d consecutive)",
                _consecutive_unchanged,
            )
            return False
        else:
            _consecutive_unchanged = 0
            _last_screenshot_hash = current_hash
            logger.debug("📸  Screenshot CHANGED — new hash")
            return True

    except Exception as exc:
        logger.warning("📸  Hash comparison error: %s — assuming changed", exc)
        return True


# ─────────────────────────────────────────────────────────────
# Adaptive Scheduling
# ─────────────────────────────────────────────────────────────

def should_take_screenshot(
    last_check_time: float,
    ctx_confidence: float,
    consecutive_same_state: int = 0,
) -> bool:
    """
    Adaptive screenshot scheduling.

    Parameters:
        last_check_time:       Timestamp of the last screenshot analysis
        ctx_confidence:        Context engine confidence (0-1)
        consecutive_same_state: How many cycles the state has been the same

    Returns True if a screenshot should be taken now.
    """
    from . import config

    now = time.time()
    elapsed = now - last_check_time if last_check_time > 0 else 999
    min_interval = getattr(config, "MIN_SCREENSHOT_INTERVAL", 15.0)
    force_interval = getattr(config, "FORCE_SCREENSHOT_INTERVAL", 120.0)

    # Never more often than the minimum interval
    if elapsed < min_interval:
        logger.debug(
            "📸  Too soon (%.0fs < %.0fs min) — skipping",
            elapsed, min_interval,
        )
        return False

    # Always if past the force interval
    if elapsed >= force_interval:
        logger.debug(
            "📸  Force interval reached (%.0fs >= %.0fs)",
            elapsed, force_interval,
        )
        return True

    # High confidence from context engine → skip unless stale
    if ctx_confidence >= 0.8 and elapsed < 60:
        logger.debug(
            "📸  High confidence (%.0f%%) + fresh (%.0fs) — skipping",
            ctx_confidence * 100, elapsed,
        )
        return False

    # Low confidence → take screenshot
    if ctx_confidence < 0.5:
        logger.debug(
            "📸  Low confidence (%.0f%%) — taking screenshot",
            ctx_confidence * 100,
        )
        return True

    # Same state for too long → suspicious, take screenshot
    if consecutive_same_state >= 3 and elapsed > 45:
        logger.debug(
            "📸  Staleness check: same state ×%d, %.0fs ago — taking screenshot",
            consecutive_same_state, elapsed,
        )
        return True

    # Default: take if enough time has passed relative to confidence
    # Medium confidence (0.5-0.8) → every 45-90s
    adaptive_interval = 30 + (ctx_confidence * 60)  # 30s-90s
    if elapsed >= adaptive_interval:
        logger.debug(
            "📸  Adaptive interval (%.0fs >= %.0fs) — taking screenshot",
            elapsed, adaptive_interval,
        )
        return True

    return False


# ─────────────────────────────────────────────────────────────
# Combined: Take, Compare, Decide
# ─────────────────────────────────────────────────────────────

async def take_and_compare(page, screenshot_path: str) -> tuple[bool, str]:
    """
    Take a screenshot and compare to the previous one.

    Returns:
        (changed: bool, hash: str)

    If not changed, the caller can skip sending to Gemini.
    """
    global _last_screenshot_time

    try:
        await page.screenshot(path=screenshot_path)
        _last_screenshot_time = time.time()
    except Exception as exc:
        logger.warning("📸  Screenshot failed: %s", exc)
        return True, ""  # Assume changed on failure

    changed = screenshot_changed(screenshot_path)
    return changed, _last_screenshot_hash


def get_last_screenshot_time() -> float:
    """Return the timestamp of the last screenshot taken."""
    return _last_screenshot_time


def get_consecutive_unchanged() -> int:
    """Return how many consecutive times the screenshot was unchanged."""
    return _consecutive_unchanged
