"""
self_healer.py — Self-Healing DOM Engine V3.

Wraps every Playwright selector interaction in try/except. When a selector
from config.SELECTORS fails (TimeoutError or empty result), this module:

  1. Extracts the raw HTML of the current page.
  2. Uses BeautifulSoup for AGGRESSIVE minification:
     - Removes ALL <svg>, <path>, <script>, <style>, <head>, <meta>, <link> tags
     - Strips ALL attributes except id, class, placeholder, aria-label, role
  3. Calls Gemini CLI via the centralized gemini_advisor module,
     asking it to return a JSON object with the corrected selector.
  4. Parses the JSON response and updates config.SELECTORS in memory.
  5. Writes selectors.json via json.dump() to persist the fix permanently.
  6. Retries the original action with the healed selector.
  7. Tracks healing history to provide better prompts on repeated failures.

If healing itself fails, the error propagates — the supervisor's
outer monitoring loop handles it gracefully.

V3 UPGRADE: Uses gemini_advisor for retries/caching, tracks healing
history for smarter prompts, and escalates broader context on repeated failures.
"""

import asyncio
import json
import logging
import re
import textwrap
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup, Comment
from playwright.async_api import Page, ElementHandle, BrowserContext

from . import config
from .gemini_advisor import ask_gemini_json
from .frame_walker import extract_all_html

logger = logging.getLogger("supervisor.self_healer")

# ─────────────────────────────────────────────────────────────
# Tags to completely remove from the DOM before sending to Gemini.
# ─────────────────────────────────────────────────────────────
_TAGS_TO_REMOVE = {"svg", "path", "script", "style", "head", "meta", "link"}

# Attributes to KEEP — everything else is stripped.
_ATTRS_TO_KEEP = {"id", "class", "placeholder", "aria-label", "role"}

# ─────────────────────────────────────────────────────────────
# Healing history — track past healing attempts per selector key
# so Gemini can learn from what's been tried.
# ─────────────────────────────────────────────────────────────
_healing_history: dict[str, list[dict]] = {}
_MAX_HISTORY_PER_KEY = 5


def _minify_html(raw_html: str) -> str:
    """
    Aggressive DOM minification using BeautifulSoup.

    - Removes all tags in _TAGS_TO_REMOVE (and their contents)
    - Strips ALL attributes except _ATTRS_TO_KEEP
    - Removes HTML comments
    - Collapses whitespace
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # Remove unwanted tags entirely (including their children).
    for tag_name in _TAGS_TO_REMOVE:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Remove HTML comments.
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Strip all attributes except the allowed set.
    for tag in soup.find_all(True):
        attrs_to_delete = [
            attr for attr in tag.attrs if attr not in _ATTRS_TO_KEEP
        ]
        for attr in attrs_to_delete:
            del tag[attr]

    # Collapse whitespace in the final output.
    result = str(soup)
    result = re.sub(r"\s{2,}", " ", result)
    return result.strip()


# ─────────────────────────────────────────────────────────────
# Resilient query wrappers
# ─────────────────────────────────────────────────────────────

async def resilient_query(
    page: Page,
    selector_key: str,
    timeout_ms: int | None = None,
    context: BrowserContext | None = None,
) -> list[ElementHandle]:
    """
    Query for ALL elements matching config.SELECTORS[selector_key].
    On failure, trigger self-healing and retry once.

    Returns a list of ElementHandle (may be empty after healing attempt).
    """
    timeout = timeout_ms or config.SELECTOR_TIMEOUT_MS
    selector = config.SELECTORS.get(selector_key)
    if not selector:
        logger.error("Unknown selector key: %s", selector_key)
        return []

    try:
        # First attempt — use the current selector.
        elements = await _query_with_timeout(page, selector, timeout)
        if elements:
            return elements
        # Empty result is also a "failure" worth healing.
        raise TimeoutError(f"Selector '{selector_key}' returned 0 elements")
    except Exception as first_err:
        logger.warning(
            "🩹  Selector '%s' failed (%s) — attempting self-heal …",
            selector_key,
            first_err,
        )

    # ── Heal ─────────────────────────────────────────────────
    new_selector = await _heal_selector(page, selector_key, selector, context=context)
    if not new_selector:
        logger.error("Self-healing failed for '%s'. Returning empty.", selector_key)
        return []

    # ── Retry with healed selector ─────────────────────────
    try:
        elements = await _query_with_timeout(page, new_selector, timeout)
        logger.info(
            "🩹  Healed '%s' → found %d element(s) with new selector.",
            selector_key,
            len(elements),
        )
        return elements
    except Exception as retry_err:
        logger.error(
            "Healed selector also failed for '%s': %s",
            selector_key,
            retry_err,
        )
        return []


async def resilient_query_one(
    page: Page,
    selector_key: str,
    timeout_ms: int | None = None,
    context: BrowserContext | None = None,
) -> Optional[ElementHandle]:
    """
    Query for a SINGLE element matching config.SELECTORS[selector_key].
    Returns the first match or None.
    """
    elements = await resilient_query(page, selector_key, timeout_ms, context=context)
    return elements[0] if elements else None


async def _query_with_timeout(
    page: Page,
    selector: str,
    timeout_ms: int,
) -> list[ElementHandle]:
    """
    Attempt query_selector_all with a wait-for-selector guard.
    Raises TimeoutError if nothing appears within timeout_ms.
    """
    try:
        # Wait for at least one element matching the selector to appear.
        await page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
    except Exception:
        # wait_for_selector can throw on complex comma-separated selectors.
        # Fall through and try query_selector_all directly.
        await asyncio.sleep(timeout_ms / 1000)

    elements = await page.query_selector_all(selector)
    return elements


# ─────────────────────────────────────────────────────────────
# Self-Healing Core
# ─────────────────────────────────────────────────────────────

async def _heal_selector(
    page: Page,
    selector_key: str,
    old_selector: str,
    context: BrowserContext | None = None,
) -> Optional[str]:
    """
    The healing pipeline:
      1. Extract & aggressively minify HTML from ALL pages/frames (omni-HTML)
      2. Build a prompt with healing history context
      3. Call Gemini CLI via gemini_advisor (with retry)
      4. Parse the JSON response
      5. Update config in memory + persist to selectors.json
    Returns the new selector string, or None on failure.
    """
    # ── Step A: Extract HTML (omni-frame if context available) ──
    try:
        if context:
            # Use omni-HTML extraction from all pages/frames.
            cleaned = await extract_all_html(context, minify=True, max_total_chars=150_000)
        else:
            raw_html = await page.content()
            cleaned = _minify_html(raw_html)
    except Exception as exc:
        logger.error("Cannot extract HTML for healing: %s", exc)
        return None
    if len(cleaned) < 50:
        logger.error("Minified HTML is too short (%d chars) — healing aborted.", len(cleaned))
        return None

    # Truncate to ~100k chars to avoid overwhelming the CLI.
    if len(cleaned) > 100_000:
        cleaned = cleaned[:100_000] + "\n<!-- TRUNCATED -->"

    logger.info(
        "🩹  Cleaned HTML for healing: %d chars (from %d raw)",
        len(cleaned),
        len(raw_html),
    )

    # ── Step B: Build the hyper-specific healing prompt ─────
    element_descriptions = {
        "chat_message": "agent/assistant chat messages in the conversation panel",
        "chat_message_fallback": "any chat message entries in the conversation",
        "chat_input": (
            "the AI Agent Chat input box. DO NOT target the global command "
            "palette or the 'Open window...' search bar. Look for textareas "
            "or Monaco editors in the sidebar"
        ),
        "send_button": "the send/submit button for the chat input",
    }
    description = element_descriptions.get(
        selector_key,
        f"the UI element called '{selector_key}'",
    )

    # Include healing history so Gemini knows what's been tried.
    history_context = ""
    past_attempts = _healing_history.get(selector_key, [])
    if past_attempts:
        history_lines = []
        for attempt in past_attempts[-3:]:  # Last 3 attempts
            history_lines.append(
                f"  - Tried: {attempt['selector']} → "
                f"{'FAILED' if not attempt['success'] else 'worked temporarily'}"
            )
        history_context = (
            "\n\nPREVIOUS HEALING ATTEMPTS (these already failed, do NOT suggest them again):\n"
            + "\n".join(history_lines)
        )

    # On repeated failures, broaden context with more DOM.
    max_html_len = 100_000
    if len(past_attempts) >= 2:
        max_html_len = min(len(cleaned), 150_000)
        cleaned = cleaned[:max_html_len]
        logger.info("🩹  Broadening DOM context for healing (attempt #%d)", len(past_attempts) + 1)

    prompt = textwrap.dedent(f"""\
        I am a Playwright automation script controlling an Electron IDE app
        (Antigravity, a VS Code fork).
        My CSS selector for {description} (previously: {old_selector}) just failed
        — no matching elements were found in the DOM.
        {history_context}

        Below is the aggressively minified HTML of the current page (all SVG, path,
        script, style, head, meta, and link tags removed; only id, class, placeholder,
        aria-label, and role attributes kept).

        Analyze it carefully and reply with ONLY a valid JSON object containing the
        exact new CSS selector (or XPath prefixed with 'xpath=') needed to target
        the {description}.

        For iframe content, use Playwright's 'iframe[id="..."] >> selector' syntax.

        Format: {{"new_selector": "your_selector_here"}}

        No markdown, no explanations, no code fences. ONLY the raw JSON.

        === HTML START ===
        {cleaned}
        === HTML END ===
    """)

    # ── Step C: Call Gemini CLI via advisor ─────────────────
    data = await ask_gemini_json(prompt, use_cache=False)

    if not data or not data.get("new_selector"):
        logger.error("Could not parse a valid selector from Gemini's response.")
        _record_healing_attempt(selector_key, old_selector, success=False)
        return None

    new_selector = data["new_selector"].strip()
    logger.info("🩹  Gemini healing response: {\"new_selector\": \"%s\"}", new_selector)

    # ── Step D: Update in memory ───────────────────────────
    config.SELECTORS[selector_key] = new_selector
    logger.info(
        "🩹  Selector '%s' healed in memory:\n    OLD: %s\n    NEW: %s",
        selector_key,
        old_selector,
        new_selector,
    )

    # ── Step E: Persist to selectors.json ──────────────────
    _persist_selectors()

    # Record success in healing history.
    _record_healing_attempt(selector_key, new_selector, success=True)

    return new_selector


# ─────────────────────────────────────────────────────────────
# Healing History
# ─────────────────────────────────────────────────────────────

def _record_healing_attempt(
    selector_key: str,
    selector: str,
    success: bool,
) -> None:
    """Record a healing attempt for future reference."""
    if selector_key not in _healing_history:
        _healing_history[selector_key] = []
    _healing_history[selector_key].append({
        "selector": selector,
        "success": success,
    })
    # Keep only the last N attempts.
    if len(_healing_history[selector_key]) > _MAX_HISTORY_PER_KEY:
        _healing_history[selector_key] = _healing_history[selector_key][-_MAX_HISTORY_PER_KEY:]


def get_healing_history() -> dict[str, list[dict]]:
    """Return the healing history for debugging."""
    return dict(_healing_history)


# ─────────────────────────────────────────────────────────────
# JSON Persistence — simple and bulletproof
# ─────────────────────────────────────────────────────────────

def _persist_selectors() -> None:
    """
    Write the entire SELECTORS dict to selectors.json.
    Simple json.dump() — no regex surgery, no file parsing.
    """
    try:
        with open(config.SELECTORS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(config.SELECTORS, f, indent=4, ensure_ascii=False)
        logger.info(
            "🩹  Persisted all selectors to %s", config.SELECTORS_JSON_PATH
        )
    except Exception as exc:
        logger.error(
            "Failed to persist selectors to %s: %s. "
            "In-memory update still active.",
            config.SELECTORS_JSON_PATH,
            exc,
        )
