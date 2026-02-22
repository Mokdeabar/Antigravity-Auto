"""
injector.py — Chat Injection V4 (Clipboard + Trust-But-Verify).

Types a message into the Antigravity chat input and sends it.

V4 UPGRADE: Anti-hallucination architecture:
  1. frame_walker.find_chat_frame() — searches ALL pages/frames
  2. DOM-based injection in the found frame
  3. Ghost Hotkey fallback (F1 → Open Chat with Agent → clipboard paste → Enter)
  4. Gemini-assisted recovery as nuclear option
  5. 'Trust But Verify' — EVERY injection is verified against
     the DOM before returning success. If the agent didn't start,
     an Exception is raised to trigger self_evolve() → Code 42.

This fixes the Electron Webview isolation where the chat input
lives in workbench-jetski-agent.html but Playwright attaches to
workbench.html.
"""

import asyncio
import logging
import textwrap

from playwright.async_api import BrowserContext, Page, Frame, ElementHandle

from . import config
from .frame_walker import (
    find_chat_frame,
    find_element_in_all_frames,
    find_all_elements_in_all_frames,
    ghost_hotkey_inject,
    extract_all_html,
)
from .gemini_advisor import ask_gemini_json

logger = logging.getLogger("supervisor.injector")


async def inject_message(
    context: BrowserContext,
    page: Page,
    message: str,
) -> bool:
    """
    Type *message* into the Antigravity chat input and press Enter.

    Strategy cascade:
      1. Find chat frame via omni-frame traversal (golden selectors first)
      2. DOM-based fill/type in the found frame
      3. Ghost Hotkey fallback (F1 → Open Chat with Agent → Enter → paste)
      4. Gemini-assisted recovery

    Returns True on success, False on failure.
    """
    logger.info("💉  Injecting message (%d chars): %.100s…", len(message), message)

    # OpenClaw-inspired: human-like pacing delay before injection
    import random
    pacing_ms = random.randint(config.PACING_MIN_MS, config.PACING_MAX_MS)
    logger.debug("💉  Pacing delay: %dms", pacing_ms)
    await asyncio.sleep(pacing_ms / 1000.0)

    # ── Strategy 1: Omni-frame search + DOM injection ──────
    chat_result = await find_chat_frame(context)
    if chat_result:
        frame, selector = chat_result
        logger.info("💉  Found chat input in frame: %s (selector: %s)", frame.url[:60], selector)

        try:
            el = await frame.query_selector(selector)
            if el and await el.is_visible():
                success = await _type_and_send(frame, el, message)
                if success:
                    # Persist the working selector.
                    _update_selector("chat_input", selector)
                    # ── TRUST BUT VERIFY ──
                    if await _verify_injection(context, page):
                        return True
                    logger.warning("💉  DOM injection sent but verification FAILED.")
        except Exception as exc:
            logger.warning("💉  DOM injection failed in found frame: %s", exc)

    # ── Strategy 2: Ghost Hotkey fallback ──────────────────
    logger.info("👻  Trying Ghost Hotkey fallback (Command Palette) …")
    hotkey_ok = await ghost_hotkey_inject(page, message)
    if hotkey_ok:
        # ── TRUST BUT VERIFY ──
        if await _verify_injection(context, page):
            return True
        logger.warning("👻  Ghost Hotkey sent but verification FAILED.")

    # ── Strategy 3: Gemini-assisted injection ──────────────
    logger.warning("🧠  All standard injection methods failed — trying Gemini-assisted fallback …")
    gemini_ok = await _gemini_inject_fallback(context, page, message)
    if gemini_ok:
        # ── TRUST BUT VERIFY (Gemini path too) ──
        if await _verify_injection(context, page):
            return True
        logger.warning("🧠  Gemini injection sent but verification FAILED.")

    return False


async def _type_and_send(frame: Frame, input_el: ElementHandle, message: str) -> bool:
    """Type message into the found element and send it."""
    try:
        # Focus and clear existing content.
        await input_el.click()
        await asyncio.sleep(0.1)

        # Select all and delete.
        page = frame.page
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.05)

        # Determine the element type.
        tag_name = await input_el.evaluate("el => el.tagName.toLowerCase()")
        is_contenteditable = await input_el.evaluate(
            "el => el.getAttribute('contenteditable')"
        )

        if tag_name in ("textarea", "input"):
            await input_el.fill(message)
        elif is_contenteditable:
            await input_el.evaluate("el => el.textContent = ''")
            await page.keyboard.insert_text(message)
        else:
            # Try fill first, fall back to keyboard.
            try:
                await input_el.fill(message)
            except Exception:
                await page.keyboard.insert_text(message)

        await asyncio.sleep(0.1)

        # Send it.
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)

        logger.info("✅  Message injected successfully via DOM.")
        return True

    except Exception as exc:
        logger.warning("💉  _type_and_send failed: %s", exc)
        return False



async def _verify_injection(
    context: BrowserContext,
    page: Page,
    wait_seconds: float = 5.0,
) -> bool:
    """
    The 'Trust But Verify' paradigm — anti-hallucination gate.

    After ANY injection attempt (DOM or Hotkey), we MUST NOT blindly
    enter the monitoring loop. Wait, then scan the DOM for signs that
    the agent actually started working.

    Checks for:
      • Text indicators: 'Thinking', 'Running', 'Cancel', 'Generating'
      • The chat input box being empty (prompt was consumed)

    Returns True if the agent appears to be active.
    """
    logger.info("🔍  Verifying injection — waiting %.1fs for agent activity …", wait_seconds)
    await asyncio.sleep(wait_seconds)  # V6: asyncio.sleep — never page.wait_for_timeout

    # ── Check 1: Look for activity indicator text across all frames ──
    activity_keywords = ['Thinking', 'Running', 'Cancel', 'Generating', 'Stop']

    for kw_page in context.pages:
        for frame in kw_page.frames:
            try:
                for keyword in activity_keywords:
                    # Search for elements whose text content contains the keyword.
                    els = await frame.query_selector_all(
                        f"//*[contains(text(), '{keyword}')]"
                    )
                    # Also try button/span selectors with the keyword as text.
                    if not els:
                        els = await frame.query_selector_all(
                            f"button:has-text('{keyword}'), span:has-text('{keyword}')"
                        )
                    for el in els:
                        try:
                            if await el.is_visible():
                                logger.info(
                                    "✅  Verification PASSED — found '%s' indicator in frame: %s",
                                    keyword, frame.url[:60],
                                )
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

    # ── Check 2: See if the chat input is now empty (prompt consumed) ──
    chat_selectors = [
        "textarea[placeholder*='Ask anything']",
        "input[placeholder*='Ask anything']",
        "[aria-label*='Ask anything']",
        "[class*='chat'] textarea",
        ".interactive-input-part textarea",
    ]
    for selector in chat_selectors:
        result = await find_element_in_all_frames(context, selector, require_visible=True)
        if result:
            frame, el = result
            try:
                value = await el.evaluate("el => el.value || el.textContent || ''")
                if not value.strip():
                    logger.info(
                        "✅  Verification PASSED — chat input is empty (prompt consumed)."
                    )
                    return True
            except Exception:
                continue

    logger.error(
        "❌  Verification FAILED — no activity indicators found and "
        "chat input is not empty. The agent did NOT start."
    )
    return False


async def _gemini_inject_fallback(
    context: BrowserContext,
    page: Page,
    message: str,
) -> bool:
    """
    Ask Gemini CLI to analyze ALL pages/frames HTML and provide
    a selector + method for injection.
    """
    # Use omni-HTML extraction so Gemini can see all webviews.
    all_html = await extract_all_html(context, minify=True)

    if len(all_html) < 100:
        logger.error("Not enough HTML extracted for Gemini injection.")
        return False

    prompt = textwrap.dedent(f"""\
        I am a Playwright automation script controlling an Electron IDE app
        (Antigravity, a VS Code fork). The UI is split across multiple
        Electron Webviews (multiple pages/frames).

        I need to inject a message into the AI Agent Chat input box, but all
        my CSS selector probes and keyboard shortcuts have failed.

        Below is the minified HTML from ALL pages and frames. The chat input
        has placeholder text: 'Ask anything (Ctrl+L)'.

        Return a JSON object with:
        1. "selector" — the CSS selector for the chat input
        2. "method" — "fill", "contenteditable", or "keyboard"
        3. "frame_hint" — any URL substring that identifies which frame/page
           the element is in (e.g., "jetski-agent")

        Format: {{"selector": "...", "method": "...", "frame_hint": "..."}}
        No markdown, no explanations. ONLY the raw JSON.

        === HTML START ===
        {all_html[:80_000]}
        === HTML END ===
    """)

    data = await ask_gemini_json(prompt, use_cache=False)
    if not data or not data.get("selector"):
        logger.error("🧠  Gemini could not identify an injection target.")
        return False

    selector = data["selector"].strip()
    method = data.get("method", "fill").strip().lower()
    frame_hint = data.get("frame_hint", "")
    logger.info("🧠  Gemini suggests: selector=%s, method=%s, frame=%s", selector, method, frame_hint)

    # Find the element using frame_walker.
    result = await find_element_in_all_frames(context, selector)
    if not result:
        logger.error("🧠  Gemini selector '%s' matched nothing in any frame.", selector)
        return False

    frame, el = result

    try:
        await el.click()
        await asyncio.sleep(0.1)
        p = frame.page
        await p.keyboard.press("Control+a")
        await p.keyboard.press("Backspace")
        await asyncio.sleep(0.05)

        if method == "contenteditable":
            await el.evaluate("el => el.textContent = ''")
            await p.keyboard.insert_text(message)
        elif method == "keyboard":
            await p.keyboard.insert_text(message)
        else:
            await el.fill(message)

        await asyncio.sleep(0.1)
        await p.keyboard.press("Enter")
        await asyncio.sleep(0.3)

        # Persist the working selector.
        _update_selector("chat_input", selector)
        logger.info("🧠  Gemini-assisted injection succeeded!")
        return True

    except Exception as exc:
        logger.error("🧠  Gemini-assisted injection failed: %s", exc)
        return False


async def inject_goal(
    context: BrowserContext,
    page: Page,
    goal: str,
) -> bool:
    """
    Inject the initial goal prompt into the chat, wrapped with
    the ULTIMATE_MANDATE to set quality expectations from message #1.
    """
    wrapped = (
        f"{config.ULTIMATE_MANDATE}\n\n"
        f"Here is your task. Complete it fully and autonomously. "
        f"Do not ask me any questions — make your best judgment on all decisions.\n\n"
        f"GOAL: {goal}"
    )
    return await inject_message(context, page, wrapped)


# ─────────────────────────────────────────────────────────────
# Selector persistence
# ─────────────────────────────────────────────────────────────

def _update_selector(key: str, selector: str) -> None:
    """Update a selector in memory and persist to selectors.json."""
    import json
    old = config.SELECTORS.get(key)
    if old == selector:
        return

    config.SELECTORS[key] = selector
    logger.info("💾  Updated selector '%s': %s → %s", key, old, selector)

    try:
        with open(config.SELECTORS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(config.SELECTORS, f, indent=4, ensure_ascii=False)
        logger.info("💾  Persisted selectors to %s", config.SELECTORS_JSON_PATH)
    except Exception as exc:
        logger.error("Failed to persist selectors: %s", exc)
