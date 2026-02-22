"""
monitor.py — The Eyes V3 (Omni-Frame).

Connects to Antigravity via CDP and scrapes the chat UI for
agent messages, approval buttons, questions, and completion signals.

V3 UPGRADE: Uses frame_walker for omni-frame traversal. No longer
limited to a single page — walks ALL pages and ALL frames in the
browser context to find elements, fixing the Electron Webview
isolation issue (chat lives in jetski-agent.html, not workbench.html).
"""

import asyncio
import logging
import textwrap
import time
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Frame,
    ElementHandle,
    Playwright,
)

from . import config
from .frame_walker import (
    find_element_in_all_frames,
    find_all_elements_in_all_frames,
    find_chat_frame,
)
from .gemini_advisor import ask_gemini

logger = logging.getLogger("supervisor.monitor")

# ─────────────────────────────────────────────────────────────
# Track agent activity for smart detection
# ─────────────────────────────────────────────────────────────
_last_message_time: float = 0.0
_idle_threshold_seconds: float = 30.0


class ChatMonitor:
    """Manages the Playwright CDP connection and DOM queries."""

    def __init__(self) -> None:
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None  # Primary page (kept for Ghost Hotkey)
        self._chat_frame: Optional[Frame] = None  # The frame with the chat input
        self._chat_selector: Optional[str] = None  # The selector that works
        self._goal: str = ""

    def set_goal(self, goal: str) -> None:
        """Store the goal so Gemini-powered detection has context."""
        self._goal = goal

    @property
    def context(self) -> BrowserContext:
        """Expose the browser context for frame_walker."""
        if self._context is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._context

    # ── connection ─────────────────────────────────────────────

    async def connect(self) -> BrowserContext:
        """Connect to Antigravity's CDP and locate the agent chat."""
        logger.info("Connecting to Antigravity via CDP at %s …", config.CDP_URL)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(config.CDP_URL)

        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError(
                "No browser contexts found. Is Antigravity running with "
                "--remote-debugging-port=9222?"
            )
        self._context = contexts[0]

        # Log all detected pages.
        all_pages = self._context.pages
        logger.info("Found %d pages in Electron context:", len(all_pages))
        for p in all_pages:
            try:
                title = await p.title()
                logger.info("  Page: url=%s title=%s", p.url[:80], title)
            except Exception:
                logger.info("  Page: url=%s title=<unavailable>", p.url[:80])

        # ── Find the chat frame using omni-frame traversal ────
        chat_result = await find_chat_frame(self._context)
        if chat_result:
            self._chat_frame, self._chat_selector = chat_result
            logger.info(
                "🎯  Chat frame found! selector=%s, frame=%s",
                self._chat_selector, self._chat_frame.url[:80],
            )
        else:
            logger.warning(
                "⚠️  Could not find chat frame in any page/frame. "
                "Will rely on Ghost Hotkey fallback."
            )

        # Keep a reference to the primary page (for Ghost Hotkey).
        self._page = self._find_primary_page(all_pages)

        return self._context

    def _find_primary_page(self, pages: list[Page]) -> Page:
        """Pick the best primary page for Ghost Hotkey and general use."""
        # Prefer pages with jetski-agent in the URL.
        for hint in config.CHAT_PAGE_URL_HINTS:
            for p in pages:
                if hint in p.url.lower():
                    logger.info("Primary page (by hint '%s'): %s", hint, p.url[:80])
                    return p

        # Prefer pages with desired URL prefixes.
        for p in pages:
            for prefix in config.DESIRED_PAGE_URL_PREFIXES:
                if p.url.startswith(prefix):
                    return p

        # Fallback: first non-extension page.
        for p in pages:
            if not any(p.url.startswith(x) for x in config.IGNORED_PAGE_URL_PREFIXES):
                return p

        return pages[0]

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._page

    @property
    def chat_frame(self) -> Optional[Frame]:
        return self._chat_frame

    # ── message scraping (omni-frame) ──────────────────────────

    async def get_latest_messages(self, n: int = 5) -> list[str]:
        """Return the text content of the last *n* agent messages."""
        messages: list[str] = []
        selector = config.SELECTORS.get("chat_message", "")

        # Try omni-frame search.
        results = await find_all_elements_in_all_frames(self._context, selector)

        if not results:
            # Try fallback selector.
            fallback = config.SELECTORS.get("chat_message_fallback", "")
            if fallback:
                results = await find_all_elements_in_all_frames(self._context, fallback)

        # Extract text from the last N elements.
        tail = results[-n:] if len(results) > n else results
        for frame, el in tail:
            try:
                text = await el.inner_text()
                text = text.strip()
                if text:
                    messages.append(text)
            except Exception:
                continue

        return messages

    async def get_new_messages_since(self, last_count: int) -> tuple[list[str], int]:
        """
        Return only messages that appeared after *last_count* total.
        Also returns the new total count.
        """
        selector = config.SELECTORS.get("chat_message", "")
        results = await find_all_elements_in_all_frames(self._context, selector)

        if not results:
            fallback = config.SELECTORS.get("chat_message_fallback", "")
            if fallback:
                results = await find_all_elements_in_all_frames(self._context, fallback)

        total = len(results)
        if total <= last_count:
            return [], total

        new_pairs = results[last_count:]
        new_msgs: list[str] = []
        for frame, el in new_pairs:
            try:
                text = (await el.inner_text()).strip()
                if text:
                    new_msgs.append(text)
            except Exception:
                continue

        global _last_message_time
        if new_msgs:
            _last_message_time = time.time()

        return new_msgs, total

    # ── approval buttons (omni-frame) ──────────────────────────

    async def find_approval_buttons(self) -> list[tuple[Frame, ElementHandle]]:
        """
        Return visible approval / allow / run buttons across ALL frames.
        """
        buttons: list[tuple[Frame, ElementHandle]] = []

        # XPath matching.
        try:
            xpath_sel = f"xpath={config.APPROVAL_BUTTON_XPATH}"
            results = await find_all_elements_in_all_frames(self._context, xpath_sel)
            for frame, el in results:
                try:
                    if await el.is_visible():
                        buttons.append((frame, el))
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("XPath button search error: %s", exc)

        # Text-based search via get_by_role.
        if not buttons:
            for page in self._context.pages:
                for text in config.APPROVAL_BUTTON_TEXTS:
                    try:
                        locator = page.get_by_role("button", name=text, exact=False)
                        count = await locator.count()
                        for i in range(count):
                            handle = await locator.nth(i).element_handle()
                            if handle and await handle.is_visible():
                                buttons.append((page.main_frame, handle))
                    except Exception:
                        continue

        return buttons

    # ── question / completion detection ────────────────────────

    async def detect_situation(self, recent_messages: list[str]) -> str:
        """
        Analyse recent messages and return a situation label:
          'question'   — the agent is asking the user something
          'completion' — the agent declared the task done
          'error'      — the agent is encountering errors that need help
          'idle'       — the agent hasn't produced output in a while
          'normal'     — nothing special
        """
        if not recent_messages:
            return "normal"

        last = recent_messages[-1].lower()

        # ── Fast keyword matching ──────────────────────────
        question_signals = [
            "?", "could you", "can you", "please provide", "please confirm",
            "what would you", "how should i", "do you want", "which option",
            "let me know", "your input", "your feedback", "waiting for",
            "need your", "clarification",
        ]
        if any(sig in last for sig in question_signals):
            return "question"

        completion_signals = [
            "task is complete", "task is done", "completed successfully",
            "finished implementing", "all done", "implementation is complete",
            "changes have been applied", "everything is working",
            "successfully completed", "work is complete",
        ]
        if any(sig in last for sig in completion_signals):
            return "completion"

        error_signals = [
            "error:", "failed to", "cannot ", "unable to", "exception",
            "traceback", "enoent", "permission denied",
            "module not found", "command not found",
        ]
        error_count = sum(1 for sig in error_signals if sig in last)
        if error_count >= 2:
            return "error"

        # ── Idle detection ────────────────────────────────
        if _last_message_time > 0 and (time.time() - _last_message_time) > _idle_threshold_seconds:
            logger.info("⏰  Agent appears idle (%.0fs since last message)", time.time() - _last_message_time)
            return "idle"

        # ── Gemini classification for ambiguous cases ──────
        if error_count >= 1 or _has_ambiguous_signals(last):
            return await _gemini_classify_situation(recent_messages, self._goal)

        return "normal"

    # ── cleanup ────────────────────────────────────────────────

    async def close(self) -> None:
        """Gracefully disconnect."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        logger.info("Disconnected from Antigravity.")


# ─────────────────────────────────────────────────────────────
# Gemini-powered situation classification
# ─────────────────────────────────────────────────────────────

def _has_ambiguous_signals(message: str) -> bool:
    """Check if a message has signals that MIGHT need intervention."""
    ambiguous_indicators = [
        "stuck", "not sure", "struggling", "help", "workaround",
        "alternative", "issue", "problem", "retry", "again", "still",
    ]
    return sum(1 for sig in ambiguous_indicators if sig in message) >= 2


async def _gemini_classify_situation(
    recent_messages: list[str],
    goal: str,
) -> str:
    """Ask Gemini to classify the agent's current situation."""
    chat_log = "\n---\n".join(recent_messages[-5:])

    prompt = textwrap.dedent(f"""\
        You are monitoring an AI coding agent working toward this goal:

        GOAL: {goal or "unknown"}

        Here are its most recent messages:

        {chat_log}

        Classify the agent's current situation as EXACTLY ONE of:
        - "question" — the agent is asking the user a question
        - "completion" — the agent declared the task done
        - "error" — the agent is stuck on errors and needs new guidance
        - "normal" — the agent is making progress, no intervention needed

        Reply with ONLY the single word classification. Nothing else.
    """)

    try:
        response = await ask_gemini(prompt, timeout=180, use_cache=True)
        classification = response.strip().lower().strip('"').strip("'")

        if classification in ("question", "completion", "error"):
            logger.info("🧠  Gemini classified situation as: %s", classification)
            return classification

        return "normal"
    except Exception as exc:
        logger.debug("🧠  Gemini classification failed: %s", exc)
        return "normal"
