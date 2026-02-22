"""
dom_prober.py — Dynamic DOM Discovery Engine.

Instead of relying on static CSS selectors that break when the IDE updates,
this module PROBES the live Antigravity DOM to find elements dynamically.

Strategy (cascading, first-match-wins):
  1. Try Antigravity/VS-Code-specific selectors (highest confidence)
  2. Try generic VS-Code-era selectors
  3. Try broad fallback selectors
  4. Live DOM scan — enumerate ALL textareas/contenteditable elements,
     score them by context, and return the best match.
  5. ASK GEMINI CLI — if all else fails, send the minified HTML to
     Gemini and let it identify the correct selector.
  6. Persist any discovered selector back to selectors.json.
"""

import asyncio
import json
import logging
import re
import textwrap
from typing import Optional

from bs4 import BeautifulSoup, Comment
from playwright.async_api import Page, ElementHandle

from . import config
from .gemini_advisor import ask_gemini_json

logger = logging.getLogger("supervisor.dom_prober")

# Tags to strip when sending DOM to Gemini (same set as self_healer)
_TAGS_TO_REMOVE = {"svg", "path", "script", "style", "head", "meta", "link"}
_ATTRS_TO_KEEP = {"id", "class", "placeholder", "aria-label", "role", "contenteditable"}


# ─────────────────────────────────────────────────────────────
# Selector Candidates — ordered by specificity/confidence.
#
# These are tried one at a time. The FIRST selector that
# returns a visible, interactable element wins.
# ─────────────────────────────────────────────────────────────

CHAT_INPUT_CANDIDATES: list[dict[str, str]] = [
    # ── GOLDEN SELECTORS — confirmed from real UI ────────────
    # The chat input placeholder says: 'Ask anything (Ctrl+L)'
    {"selector": "textarea[placeholder*='Ask anything']", "label": "GOLDEN: Ask anything textarea"},
    {"selector": "input[placeholder*='Ask anything']", "label": "GOLDEN: Ask anything input"},
    {"selector": "[aria-label*='Ask anything']", "label": "GOLDEN: Ask anything aria-label"},

    # ── Antigravity / Windsurf specific ──────────────────────
    {"selector": ".interactive-input-part textarea", "label": "VS Code interactive input textarea"},
    {"selector": ".chat-input-toolbars textarea", "label": "chat-input-toolbars textarea"},
    {"selector": "div.chat-widget textarea", "label": "chat-widget textarea"},
    {"selector": ".inline-chat-input textarea", "label": "inline-chat-input textarea"},
    {"selector": ".aichat-input-part textarea", "label": "AI chat input part textarea"},
    {"selector": ".agent-chat-input textarea", "label": "agent-chat-input textarea"},

    # ── contenteditable variants ─────────────────────────────
    {"selector": ".interactive-input-part div[contenteditable='true']", "label": "interactive input contenteditable"},
    {"selector": ".chat-input-toolbars div[contenteditable='true']", "label": "chat toolbars contenteditable"},
    {"selector": "div.chat-widget div[contenteditable='true'][role='textbox']", "label": "chat-widget contenteditable textbox"},
    {"selector": ".aichat-input-part div[contenteditable='true']", "label": "AI chat contenteditable"},

    # ── Monaco editor inside chat panel ──────────────────────
    {"selector": ".interactive-input-part .monaco-editor textarea.inputarea", "label": "Monaco input in interactive panel"},
    {"selector": ".chat-input-toolbars .monaco-editor textarea.inputarea", "label": "Monaco input in chat toolbars"},
    {"selector": ".aichat-input-part .monaco-editor textarea.inputarea", "label": "Monaco input in AI chat part"},

    # ── VS Code sidebar chat patterns ────────────────────────
    {"selector": "[class*='chat'] textarea", "label": "generic chat textarea"},
    {"selector": "[class*='chat'] div[contenteditable='true']", "label": "generic chat contenteditable"},
    {"selector": "[class*='chat'] .monaco-editor textarea.inputarea", "label": "generic chat Monaco textarea"},

    # ── Agent Manager patterns ───────────────────────────────
    {"selector": "[class*='agent'] textarea", "label": "agent panel textarea"},
    {"selector": "[class*='agent'] div[contenteditable='true']", "label": "agent panel contenteditable"},

    # ── Broad VS Code patterns ───────────────────────────────
    {"selector": ".sidebar .monaco-editor textarea.inputarea", "label": "sidebar Monaco textarea"},
    {"selector": ".panel .monaco-editor textarea.inputarea", "label": "panel Monaco textarea"},

    # ── Generic textarea fallbacks ───────────────────────────
    {"selector": "textarea[placeholder*='message' i]", "label": "textarea placeholder=message"},
    {"selector": "textarea[placeholder*='ask' i]", "label": "textarea placeholder=ask"},
    {"selector": "textarea[placeholder*='type' i]", "label": "textarea placeholder=type"},
    {"selector": "textarea[aria-label*='chat' i]", "label": "textarea aria-label=chat"},
    {"selector": "textarea[aria-label*='message' i]", "label": "textarea aria-label=message"},
    {"selector": "div[contenteditable='true'][role='textbox']", "label": "generic contenteditable textbox"},
]

CHAT_MESSAGE_CANDIDATES: list[dict[str, str]] = [
    # ── iframe specific matching ────────────────────────
    {"selector": "iframe.webview.ready[name*='antigravity.agentPanel'] >> .chat-message", "label": "antigravity.agentPanel iframe chat-message"},
    {"selector": "iframe.webview.ready[name*='antigravity.agentPanel'] >> .message-item", "label": "antigravity.agentPanel iframe message-item"},
    {"selector": "iframe.webview.ready[name*='antigravity.agentPanel'] >> [role='listitem']", "label": "antigravity.agentPanel iframe listitem"},
    {"selector": "iframe.webview.ready[name*='antigravity.agentPanel'] >> .markdown-body", "label": "antigravity.agentPanel iframe markdown-body"},

    # ── Original and fallback candidates ────────────────
    {"selector": ".interactive-result-editor", "label": "interactive result editor"},
    {"selector": "[class*='chat-message'][class*='agent']", "label": "chat-message agent"},
    {"selector": "[class*='chat-message'][class*='assistant']", "label": "chat-message assistant"},
    {"selector": "[class*='chat-message']", "label": "chat-message generic"},
    {"selector": "[data-role='assistant']", "label": "data-role assistant"},
    {"selector": "[class*='response'][class*='message']", "label": "response message"},
    {"selector": "[class*='chat'] [class*='message']:not([class*='user'])", "label": "chat message not-user"},
    {"selector": ".interactive-item-container", "label": "interactive item container"},
    {"selector": "[class*='message'][class*='agent']", "label": "message agent"},
    {"selector": "[class*='message'][class*='assistant']", "label": "message assistant"},
    {"selector": ".message-item", "label": "message item generic"},
    {"selector": ".markdown-body", "label": "markdown body generic"},
    {"selector": "[role='listitem']", "label": "listitem generic"},
]

APPROVAL_BUTTON_CANDIDATES: list[dict[str, str]] = [
    {"selector": "button[class*='approve']", "label": "button approve class"},
    {"selector": "button[class*='accept']", "label": "button accept class"},
    {"selector": "button[class*='allow']", "label": "button allow class"},
    {"selector": "a.monaco-button[class*='primary']", "label": "monaco primary button"},
]


# ─────────────────────────────────────────────────────────────
# Probing Functions
# ─────────────────────────────────────────────────────────────

async def probe_element(
    page: Page,
    candidates: list[dict[str, str]],
    selector_key: str,
    timeout_ms: int = 2000,
) -> Optional[str]:
    """
    Try each candidate selector against the live page.
    Returns the first selector that matches a visible element,
    and persists it to selectors.json.

    Returns None if nothing matched.
    """
    for candidate in candidates:
        sel = candidate["selector"]
        label = candidate["label"]
        try:
            elements = await page.query_selector_all(sel)
            if not elements:
                continue

            # Check if at least one element is visible.
            for el in elements:
                try:
                    visible = await el.is_visible()
                    if visible:
                        logger.info(
                            "✅  PROBE HIT for '%s': [%s] → selector: %s",
                            selector_key, label, sel,
                        )
                        # Persist this discovery.
                        _update_selector(selector_key, sel)
                        return sel
                except Exception:
                    continue

        except Exception as exc:
            logger.debug("Probe miss for [%s]: %s", label, exc)
            continue

    logger.warning("⚠️  All %d candidates failed for '%s'.", len(candidates), selector_key)
    return None


async def probe_chat_input(page: Page, timeout_ms: int = 3000) -> Optional[str]:
    """Probe specifically for the chat input element."""
    # First try the persisted selector (if it exists and works).
    persisted = config.SELECTORS.get("chat_input")
    if persisted:
        try:
            elements = await page.query_selector_all(persisted)
            for el in elements:
                if await el.is_visible():
                    logger.info("✅  Persisted 'chat_input' selector still works: %s", persisted)
                    return persisted
        except Exception:
            pass

    # Run cascading probe.
    result = await probe_element(page, CHAT_INPUT_CANDIDATES, "chat_input", timeout_ms)
    if result:
        return result

    # Live DOM scan.
    logger.info("🔍  Running live DOM scan for chat input …")
    result = await _live_dom_scan_for_input(page)
    if result:
        _update_selector("chat_input", result)
        return result

    # ── GEMINI FALLBACK — nuclear option ──────────────────
    logger.info("🧠  All probes failed for 'chat_input' — asking Gemini CLI …")
    result = await _gemini_probe_fallback(page, "chat_input",
        "the AI Agent Chat input box. DO NOT target the command palette or search bar. "
        "Look for textareas, contenteditable divs, or Monaco editors in the sidebar chat panel."
    )
    if result:
        _update_selector("chat_input", result)
    return result


async def probe_chat_messages(page: Page) -> Optional[str]:
    """Probe for chat message elements."""
    persisted = config.SELECTORS.get("chat_message")
    if persisted:
        try:
            elements = await page.query_selector_all(persisted)
            if elements:
                logger.info("✅  Persisted 'chat_message' selector works: %s", persisted)
                return persisted
        except Exception:
            pass

    result = await probe_element(page, CHAT_MESSAGE_CANDIDATES, "chat_message")
    if result:
        return result

    # ── GEMINI FALLBACK ──────────────────────────────────
    logger.info("🧠  All probes failed for 'chat_message' — asking Gemini CLI …")
    result = await _gemini_probe_fallback(page, "chat_message",
        "agent/assistant chat messages in the conversation panel (NOT user messages)"
    )
    if result:
        _update_selector("chat_message", result)
    return result


# ─────────────────────────────────────────────────────────────
# Live DOM Scan — last-resort element discovery
# ─────────────────────────────────────────────────────────────

async def _live_dom_scan_for_input(page: Page) -> Optional[str]:
    """
    Enumerate ALL textareas and contenteditable elements on the page,
    score them by context clues, and return a selector for the best match.
    """
    # JavaScript that finds and scores all potential input elements.
    scored_elements = await page.evaluate("""() => {
        const results = [];

        // Score keywords in ancestor class names / IDs.
        const positiveKeywords = [
            'chat', 'input', 'agent', 'interactive', 'message',
            'prompt', 'aichat', 'copilot', 'gemini', 'assistant'
        ];
        const negativeKeywords = [
            'search', 'command-palette', 'quick-open', 'quickopen',
            'find', 'replace', 'filter', 'explorer', 'terminal',
            'output', 'debug', 'problems'
        ];

        function scoreElement(el) {
            let score = 0;
            // Walk up to 6 ancestors.
            let current = el;
            for (let depth = 0; depth < 6 && current; depth++) {
                const cls = (current.className || '').toString().toLowerCase();
                const id = (current.id || '').toLowerCase();
                const combined = cls + ' ' + id;

                for (const kw of positiveKeywords) {
                    if (combined.includes(kw)) score += (6 - depth);  // closer ancestors score higher
                }
                for (const kw of negativeKeywords) {
                    if (combined.includes(kw)) score -= (8 - depth);
                }
                current = current.parentElement;
            }

            // Bonus for placeholder text.
            const placeholder = (el.placeholder || '').toLowerCase();
            if (placeholder.includes('message') || placeholder.includes('ask') || placeholder.includes('type')) {
                score += 10;
            }

            // Bonus for aria-label.
            const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
            if (ariaLabel.includes('chat') || ariaLabel.includes('message') || ariaLabel.includes('input')) {
                score += 8;
            }

            // Penalty if not visible.
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) {
                score -= 50;
            }

            // Bonus for being in sidebar region (right side of screen).
            if (rect.left > window.innerWidth * 0.5) {
                score += 3;
            }

            return score;
        }

        function buildSelector(el) {
            // Try to build a stable selector.
            if (el.id) return '#' + CSS.escape(el.id);

            const tag = el.tagName.toLowerCase();
            const classes = Array.from(el.classList).filter(c => c.length < 40).slice(0, 3);
            if (classes.length > 0) {
                return tag + '.' + classes.map(c => CSS.escape(c)).join('.');
            }

            // Use aria-label.
            const ariaLabel = el.getAttribute('aria-label');
            if (ariaLabel) {
                return tag + '[aria-label="' + ariaLabel.replace(/"/g, '\\\\"') + '"]';
            }

            // Use placeholder.
            const placeholder = el.getAttribute('placeholder');
            if (placeholder) {
                return tag + '[placeholder="' + placeholder.replace(/"/g, '\\\\"') + '"]';
            }

            return tag;
        }

        // Find all textareas.
        document.querySelectorAll('textarea').forEach(el => {
            results.push({
                selector: buildSelector(el),
                score: scoreElement(el),
                type: 'textarea',
                visible: el.getBoundingClientRect().width > 0
            });
        });

        // Find all contenteditable divs.
        document.querySelectorAll('div[contenteditable="true"]').forEach(el => {
            results.push({
                selector: buildSelector(el),
                score: scoreElement(el),
                type: 'contenteditable',
                visible: el.getBoundingClientRect().width > 0
            });
        });

        // Sort by score descending.
        results.sort((a, b) => b.score - a.score);
        return results.slice(0, 10);
    }""")

    if not scored_elements:
        logger.warning("🔍  Live DOM scan found zero input candidates.")
        return None

    logger.info("🔍  Live DOM scan results (top %d):", len(scored_elements))
    for i, item in enumerate(scored_elements):
        logger.info(
            "    #%d: score=%d type=%s visible=%s selector=%s",
            i + 1, item["score"], item["type"], item["visible"], item["selector"],
        )

    # Pick the highest-scoring visible element.
    for item in scored_elements:
        if item["visible"] and item["score"] > 0:
            logger.info("🔍  Best match: %s (score=%d)", item["selector"], item["score"])
            return item["selector"]

    # If no visible positive-scoring element, take the first visible one.
    for item in scored_elements:
        if item["visible"]:
            logger.warning(
                "🔍  No positive-scoring match. Using first visible: %s (score=%d)",
                item["selector"], item["score"],
            )
            return item["selector"]

    return None


# ─────────────────────────────────────────────────────────────
# Page Discovery — find the main IDE page
# ─────────────────────────────────────────────────────────────

async def find_best_page(pages: list, ignored_prefixes: list[str], desired_url_prefixes: list[str], title_keywords: list[str]) -> Optional[object]:
    """
    Given a list of Playwright pages, find the one most likely
    to be the main Antigravity IDE window with the chat panel.

    Strategy:
      1. Filter out background pages (extensions, devtools).
      2. Score remaining pages by URL and title signals.
      3. Probe for chat input in top-scored pages.
      4. Return the first page with a confirmed chat input.
    """
    candidates = []

    for page in pages:
        url = page.url
        if any(url.startswith(prefix) for prefix in ignored_prefixes):
            continue

        score = 0
        # URL-based scoring.
        if any(url.startswith(p) for p in desired_url_prefixes):
            score += 5

        # Title-based scoring.
        try:
            title = await page.title()
            title_lower = title.lower()
            for kw in title_keywords:
                if kw.lower() in title_lower:
                    score += 10
            logger.debug("Page: url=%s title=%s score=%d", url, title, score)
        except Exception:
            logger.debug("Page: url=%s (title unavailable) score=%d", url, score)

        candidates.append((score, page))

    if not candidates:
        return None

    # Sort by score descending.
    candidates.sort(key=lambda x: -x[0])

    # Try to find chat input in each candidate.
    for score, page in candidates:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            sel = await probe_chat_input(page, timeout_ms=3000)
            if sel:
                logger.info("✅  Found chat UI on page (score=%d): %s", score, page.url)
                return page
        except Exception:
            continue

    # Fallback: return highest-scored page even without confirmed chat input.
    best_page = candidates[0][1]
    logger.warning("⚠️  No page with confirmed chat input. Using highest-scored: %s", best_page.url)
    return best_page


# ─────────────────────────────────────────────────────────────
# Gemini CLI Fallback — when all programmatic probes fail
# ─────────────────────────────────────────────────────────────

def _minify_html_for_gemini(raw_html: str) -> str:
    """Aggressively minify HTML before sending to Gemini."""
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
    return result.strip()[:100_000]


async def _gemini_probe_fallback(
    page: Page,
    selector_key: str,
    element_description: str,
) -> Optional[str]:
    """
    Ask Gemini CLI to analyze the page HTML and suggest a CSS selector.
    This is the nuclear-option fallback when all programmatic probes fail.
    """
    try:
        raw_html = await page.content()
    except Exception as exc:
        logger.error("Cannot extract page HTML for Gemini probe: %s", exc)
        return None

    cleaned = _minify_html_for_gemini(raw_html)
    if len(cleaned) < 50:
        logger.error("Minified HTML too short (%d chars) — Gemini probe aborted.", len(cleaned))
        return None

    prompt = textwrap.dedent(f"""\
        I am a Playwright automation script controlling an Electron IDE app
        (Antigravity, a VS Code fork). I need to find {element_description}.

        All my programmatic CSS selector probes have failed. Below is the
        aggressively minified HTML of the current page (SVG, script, style,
        head, meta, link tags removed; only id, class, placeholder, aria-label,
        role, contenteditable attributes kept).

        Analyze the HTML carefully and reply with ONLY a valid JSON object
        containing the CSS selector (or XPath prefixed with 'xpath=') for
        {element_description}.

        For iframe content, prefix with 'iframe[id="..."] >> ' syntax.

        Format: {{"new_selector": "your_selector_here"}}
        No markdown, no explanations, no code fences. ONLY the raw JSON.

        === HTML START ===
        {cleaned}
        === HTML END ===
    """)

    data = await ask_gemini_json(prompt, use_cache=False)
    if data and data.get("new_selector"):
        selector = data["new_selector"].strip()
        logger.info("🧠  Gemini suggested selector for '%s': %s", selector_key, selector)

        # Validate the selector against the page.
        try:
            elements = await page.query_selector_all(selector)
            if elements:
                for el in elements:
                    if await el.is_visible():
                        logger.info("🧠  Gemini selector VALIDATED for '%s' (%d elements)", selector_key, len(elements))
                        return selector
                logger.warning("🧠  Gemini selector matched %d elements but none visible.", len(elements))
            else:
                logger.warning("🧠  Gemini selector matched 0 elements — discarding.")
        except Exception as exc:
            logger.warning("🧠  Gemini selector failed validation: %s", exc)
    else:
        logger.warning("🧠  Gemini did not return a valid selector for '%s'.", selector_key)

    return None


# ─────────────────────────────────────────────────────────────
# Selector persistence
# ─────────────────────────────────────────────────────────────

def _update_selector(key: str, selector: str) -> None:
    """Update a selector in memory and persist to selectors.json."""
    old = config.SELECTORS.get(key)
    if old == selector:
        return  # No change needed.

    config.SELECTORS[key] = selector
    logger.info("💾  Updated selector '%s': %s → %s", key, old, selector)

    try:
        with open(config.SELECTORS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(config.SELECTORS, f, indent=4, ensure_ascii=False)
        logger.info("💾  Persisted selectors to %s", config.SELECTORS_JSON_PATH)
    except Exception as exc:
        logger.error("Failed to persist selectors: %s", exc)
