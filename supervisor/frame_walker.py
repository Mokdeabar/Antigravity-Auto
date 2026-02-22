"""
frame_walker.py — Omni-Frame Traversal Engine.

Electron apps (like Antigravity, a VS Code fork) split their UI across
multiple BrowserContext pages AND nested iframes/webviews. Playwright's
default `page.locator()` is BLIND to elements inside iframes.

This module solves that by walking ALL pages and ALL frames to find
elements, execute actions, and extract HTML — regardless of which
webview or iframe the element lives in.

Architecture:
  • browser_context.pages → each page can have N frames
  • page.frames → includes the main frame + all child frames
  • For each frame, we try the selector
  • The first frame that returns a visible element wins
  • All actions execute against THAT specific frame

This is the foundation that dom_prober, self_healer, monitor,
and injector all build on.
"""

import asyncio
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup, Comment
from playwright.async_api import BrowserContext, Page, Frame, ElementHandle

from . import config

logger = logging.getLogger("supervisor.frame_walker")

# Tags to strip when extracting HTML for Gemini
_TAGS_TO_REMOVE = {"svg", "path", "script", "style", "head", "meta", "link"}
_ATTRS_TO_KEEP = {"id", "class", "placeholder", "aria-label", "role", "contenteditable"}


# ─────────────────────────────────────────────────────────────
# Recursive Frame Collector — pierces nested Webview iframes
# ─────────────────────────────────────────────────────────────

def _collect_all_frames(page: Page) -> list[Frame]:
    """
    Recursively collect ALL frames from a page, including deeply
    nested child frames inside VS Code Webview iframes.

    VS Code extensions render inside cross-origin <iframe> tags
    (Webviews). These are often nested 2-3 levels deep:
      page → main_frame → webview_iframe → inner_iframe

    Playwright's page.frames only returns direct frames. This
    helper walks frame.child_frames recursively to find them all.
    """
    all_frames: list[Frame] = []
    visited: set[str] = set()

    def _walk(frame: Frame, depth: int = 0) -> None:
        # Guard against infinite recursion (max 4 levels deep)
        if depth > 4:
            return
        frame_id = f"{frame.url}:{frame.name}:{id(frame)}"
        if frame_id in visited:
            return
        visited.add(frame_id)
        all_frames.append(frame)
        try:
            for child in frame.child_frames:
                _walk(child, depth + 1)
        except Exception:
            pass  # Frame may have been detached

    for frame in page.frames:
        _walk(frame)

    return all_frames


# ─────────────────────────────────────────────────────────────
# Core: Find element across ALL pages and frames
# ─────────────────────────────────────────────────────────────

async def find_element_in_all_frames(
    context: BrowserContext,
    selector: str,
    require_visible: bool = True,
) -> Optional[tuple[Frame, ElementHandle]]:
    """
    Search ALL pages and ALL frames for an element matching the selector.

    Returns (frame, element_handle) for the first visible match,
    or None if nothing was found anywhere.

    This is the core fix for the Electron Webview isolation issue:
    the chat input might live in workbench-jetski-agent.html while
    Playwright initially attached to workbench.html.
    """
    for page in context.pages:
        # Skip known background pages.
        url = page.url
        if any(url.startswith(p) for p in config.IGNORED_PAGE_URL_PREFIXES):
            continue

        # Recursively walk ALL frames including nested Webview iframes
        for frame in _collect_all_frames(page):
            try:
                # V32: Use Playwright auto-wait instead of raw query_selector_all.
                # This waits up to 1.5s for elements to appear in the DOM,
                # catching elements that are still rendering after navigation.
                try:
                    el = await frame.wait_for_selector(
                        selector,
                        state="visible" if require_visible else "attached",
                        timeout=1500,
                    )
                    if el:
                        logger.info(
                            "🔎  Found element in frame: page=%s, frame=%s, selector=%s",
                            _safe_url(page), _safe_frame_name(frame), selector,
                        )
                        return (frame, el)
                except Exception:
                    pass  # Timeout or selector not found — try next frame
            except Exception as exc:
                # Catch stale frame / destroyed execution context
                exc_msg = str(exc).lower()
                if "destroyed" in exc_msg or "detached" in exc_msg:
                    logger.debug("🔄  Stale frame detected, skipping: %s", _safe_frame_name(frame))
                continue

    return None


async def find_all_elements_in_all_frames(
    context: BrowserContext,
    selector: str,
) -> list[tuple[Frame, ElementHandle]]:
    """
    Find ALL matching elements across ALL pages and frames.
    Returns a list of (frame, element_handle) tuples.
    """
    results: list[tuple[Frame, ElementHandle]] = []

    for page in context.pages:
        url = page.url
        if any(url.startswith(p) for p in config.IGNORED_PAGE_URL_PREFIXES):
            continue

        # Recursively walk ALL frames including nested Webview iframes
        for frame in _collect_all_frames(page):
            try:
                elements = await frame.query_selector_all(selector)
                for el in elements:
                    results.append((frame, el))
            except Exception as exc:
                # Catch stale frame / destroyed execution context
                exc_msg = str(exc).lower()
                if "destroyed" in exc_msg or "detached" in exc_msg:
                    logger.debug("🔄  Stale frame detected, skipping: %s", _safe_frame_name(frame))
                continue

    return results


async def find_chat_frame(
    context: BrowserContext,
) -> Optional[tuple[Frame, str]]:
    """
    Find the frame that contains the AI Agent Chat input.

    Tries the golden selectors first (based on real observed placeholder),
    then falls back to broader probing.

    Returns (frame, working_selector) or None.
    """
    # ── Golden Selectors — confirmed from real UI observation ──
    golden_selectors = [
        "textarea[placeholder*='Ask anything']",
        "input[placeholder*='Ask anything']",
        "[aria-label*='Ask anything']",
        # Monaco editor variant (VS Code forks use this heavily)
        ".monaco-editor textarea.inputarea",
    ]

    for selector in golden_selectors:
        result = await find_element_in_all_frames(context, selector)
        if result:
            logger.info("🏆  Golden selector matched: %s", selector)
            return (result[0], selector)

    # ── Broader agent-chat patterns ────────────────────────
    # These target the jetski-agent webview specifically.
    agent_selectors = [
        "[class*='chat'] textarea",
        "[class*='agent'] textarea",
        "[class*='chat'] div[contenteditable='true']",
        "div.chat-widget textarea",
        ".aichat-input-part textarea",
        ".interactive-input-part textarea",
        "textarea[placeholder*='message' i]",
        "textarea[placeholder*='ask' i]",
        "div[contenteditable='true'][role='textbox']",
        # V35: Antigravity-specific Agent Manager selectors (from research)
        "[aria-label*='chat' i][contenteditable='true']",
        "[aria-label*='agent' i][contenteditable='true']",
        "[data-testid*='chat'] textarea",
        "[data-testid*='chat'] div[contenteditable='true']",
        # V35: ARIA role-based fallbacks (VS Code accessibility mandate)
        "[role='textbox'][aria-multiline='true']",
        "div[contenteditable='true']",  # ultra-generic last resort
    ]

    for selector in agent_selectors:
        result = await find_element_in_all_frames(context, selector)
        if result:
            logger.info("✅  Agent selector matched: %s", selector)
            return (result[0], selector)

    logger.warning("⚠️  Could not find chat input in any frame.")
    return None


# ─────────────────────────────────────────────────────────────
# Omni-HTML Extraction — scrape ALL pages and frames
# ─────────────────────────────────────────────────────────────

async def extract_all_html(
    context: BrowserContext,
    minify: bool = True,
    max_total_chars: int = 150_000,
) -> str:
    """
    Extract and concatenate HTML from ALL pages and ALL frames.

    Because the Antigravity UI is split across Webviews (workbench.html,
    workbench-jetski-agent.html, etc.), we must scrape them ALL so
    Gemini can 'see' the entire application state.

    Returns a single string of (optionally minified) HTML with
    page/frame markers for context.
    """
    parts: list[str] = []
    total_chars = 0

    for page in context.pages:
        url = page.url
        if any(url.startswith(p) for p in config.IGNORED_PAGE_URL_PREFIXES):
            continue

        for frame in _collect_all_frames(page):
            try:
                raw = await frame.content()
            except Exception:
                continue

            if minify:
                cleaned = _minify_html(raw)
            else:
                cleaned = raw

            if not cleaned or len(cleaned) < 20:
                continue

            header = f"\n=== PAGE: {_safe_url(page)} | FRAME: {_safe_frame_name(frame)} ===\n"
            part = header + cleaned

            # Check total size limit.
            if total_chars + len(part) > max_total_chars:
                remaining = max_total_chars - total_chars
                if remaining > 100:
                    parts.append(part[:remaining] + "\n<!-- TRUNCATED -->")
                break

            parts.append(part)
            total_chars += len(part)

    result = "\n".join(parts)
    logger.info(
        "📄  Extracted HTML from %d page/frame segments (%d total chars)",
        len(parts), len(result),
    )
    return result


# ─────────────────────────────────────────────────────────────
# HTML Minification (shared by all modules)
# ─────────────────────────────────────────────────────────────

def _minify_html(raw_html: str) -> str:
    """
    Aggressive DOM minification using BeautifulSoup.
    Removes SVG, scripts, styles, meta; strips most attributes.
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    for tag_name in _TAGS_TO_REMOVE:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for tag in soup.find_all(True):
        attrs_to_delete = [a for a in tag.attrs if a not in _ATTRS_TO_KEEP]
        for a in attrs_to_delete:
            del tag[a]

    result = str(soup)
    result = re.sub(r"\s{2,}", " ", result)
    return result.strip()


# ─────────────────────────────────────────────────────────────
# Ghost Hotkey Fallback — keyboard-driven injection
# ─────────────────────────────────────────────────────────────

async def ghost_hotkey_inject(
    page: Page,
    message: str,
) -> bool:
    """
    The 'Ghost Hotkey' fallback for when DOM-based injection fails.

    V30: Uses CDP-native clipboard API instead of pyperclip.
    Opens "Open Chat with Agent" via F1 → type → Enter, then pastes
    the message via browser clipboard API.

    Steps:
      A. Force window focus: bring_to_front() + click(500, 300)
      B. Open Command Palette: F1
      C. Type "Open Chat with Agent", press Enter to select
      D. Wait 1.5s for the chat panel to slide open
      E. Playwright-native keyboard.insert_text() (zero clipboard dependency)
      F. Paste and Send: Ctrl+V, wait 500ms, Enter

    Returns True on success (but caller MUST still verify injection).
    """
    logger.info("👻  Attempting Ghost Hotkey injection via Command Palette …")

    try:
        # A. Force absolute OS focus — bring window to front AND click
        #    the center of the code editor to guarantee focus.
        await page.bring_to_front()
        await page.mouse.click(500, 300)
        await asyncio.sleep(0.3)

        # B. Open the Command Palette with F1.
        await page.keyboard.press("F1")
        await asyncio.sleep(1.0)

        # C. Type "Open Chat with Agent" and press Enter to select it.
        await page.keyboard.insert_text("Open Chat with Agent")
        await asyncio.sleep(0.7)  # Give dropdown time to filter
        await page.keyboard.press("Enter")

        # D. Wait 1.5s for the chat panel to slide open and focus.
        await asyncio.sleep(1.5)

        # E. V30: Playwright-native text insertion — no clipboard needed.
        #    Eliminates both pyperclip [WinError 0] AND navigator.clipboard
        #    NotAllowedError when window lacks focus.
        await page.keyboard.insert_text(message)
        logger.info("👻  Inserted %d chars via keyboard.insert_text().", len(message))

        # F. Send.
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)

        logger.info("👻  Ghost Hotkey injection completed (pending verification).")
        return True

    except Exception as exc:
        logger.error("👻  Ghost Hotkey injection failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _safe_url(page: Page) -> str:
    """Get a truncated URL for logging."""
    url = page.url or "<no-url>"
    return url[:80]


def _safe_frame_name(frame: Frame) -> str:
    """Get a readable frame identifier for logging."""
    name = frame.name or frame.url or "<main>"
    return name[:60]
