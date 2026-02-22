"""
context_engine.py — Deep Context Awareness Engine V2 (OpenClaw-Enhanced).

Replaces the shallow screenshot-first analysis with structured DOM-based
context gathering. Scrapes chat, terminal, server, approval state, and now
also tracks user presence and context budget consumption.

V2 UPGRADE — OpenClaw-Inspired:
  1. PresenceTracker: detects user activity via DOM focus/scroll signals.
  2. ContextBudget integration: reports chars/tokens sent to Gemini.
  3. format_context_report(): structured breakdown of all context dimensions.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import BrowserContext, Page, Frame

from . import config
from .frame_walker import find_element_in_all_frames, find_all_elements_in_all_frames
from .retry_policy import get_context_budget

logger = logging.getLogger("supervisor.context_engine")


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class MessageInfo:
    """A single chat message with metadata."""
    role: str = "unknown"          # "agent" | "user" | "system"
    content: str = ""              # Full text content
    message_type: str = "unknown"  # "code" | "error" | "question" | "progress" | "completion" | "generic"
    has_diff: bool = False         # Contains diff/code block
    has_error: bool = False        # Contains error patterns
    timestamp: float = 0.0        # When this message was scraped


@dataclass
class DiffReport:
    """A parsed file change from agent output."""
    filename: str = ""
    additions: int = 0
    deletions: int = 0
    summary: str = ""


@dataclass
class ServerInfo:
    """Dev server status."""
    running: bool = False
    port: int = 0
    url: str = ""
    last_check: float = 0.0


@dataclass
class ProgressInfo:
    """Agent progress tracking."""
    files_mentioned: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    errors_seen: list[str] = field(default_factory=list)
    completions_detected: int = 0
    percent_complete: float = 0.0  # 0-1 estimate


@dataclass
class ContextSnapshot:
    """Structured representation of the IDE's current state."""
    chat_messages: list[MessageInfo] = field(default_factory=list)
    terminal_output: list[str] = field(default_factory=list)
    diff_reports: list[DiffReport] = field(default_factory=list)
    agent_status: str = "UNKNOWN"     # WORKING | WAITING | IDLE | ASKING
    progress: ProgressInfo = field(default_factory=ProgressInfo)
    dev_server_status: ServerInfo = field(default_factory=ServerInfo)
    simple_browser_open: bool = False
    has_pending_approval: bool = False
    confidence: float = 0.0          # 0-1 how confident we are
    gathered_at: float = 0.0        # timestamp
    # OpenClaw-inspired: context budget tracking
    context_chars_sent: int = 0      # total chars sent to Gemini this session
    context_budget_pct: float = 0.0  # % of budget consumed
    user_idle_seconds: float = 0.0   # seconds since last user activity
    # V30: ARIA accessibility tree snapshot for token-efficient state detection
    aria_tree: str = ""              # Playwright accessibility tree text


# ─────────────────────────────────────────────────────────────
# Error / question / completion detection patterns
# ─────────────────────────────────────────────────────────────

_ERROR_PATTERNS = [
    re.compile(r"(?:Error|Exception|ENOENT|EACCES|FATAL|FAILED)[:\s]", re.IGNORECASE),
    re.compile(r"(?:traceback|stack trace)", re.IGNORECASE),
    re.compile(r"(?:cannot find|module not found|no such file)", re.IGNORECASE),
    re.compile(r"(?:syntax error|unexpected token|invalid)", re.IGNORECASE),
    re.compile(r"exit\s*(?:code|status)\s*[1-9]", re.IGNORECASE),
]

_QUESTION_PATTERNS = [
    re.compile(r"\?\s*$", re.MULTILINE),
    re.compile(r"(?:would you like|should I|do you want|which (?:one|option))", re.IGNORECASE),
    re.compile(r"(?:please (?:choose|select|confirm|specify))", re.IGNORECASE),
    re.compile(r"(?:waiting for (?:input|response|approval))", re.IGNORECASE),
]

_COMPLETION_PATTERNS = [
    re.compile(r"(?:task (?:is )?(?:complete|done|finished))", re.IGNORECASE),
    re.compile(r"(?:all (?:done|complete|set))", re.IGNORECASE),
    re.compile(r"(?:successfully (?:created|built|deployed|installed))", re.IGNORECASE),
    re.compile(r"(?:everything (?:is )?(?:ready|working|set up))", re.IGNORECASE),
    re.compile(r"(?:let me know if)", re.IGNORECASE),
]

_PROGRESS_PATTERNS = [
    re.compile(r"(?:creating|building|installing|setting up|configuring)", re.IGNORECASE),
    re.compile(r"(?:now (?:I'll|let me|I will|I'm going to))", re.IGNORECASE),
    re.compile(r"(?:next|step \d+|moving on to)", re.IGNORECASE),
]

_CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_DIFF_PATTERN = re.compile(r"```diff\n([\s\S]*?)```", re.MULTILINE)
_FILENAME_PATTERN = re.compile(
    r"(?:file|create|modify|edit|update|write)[:\s]+[`'\"]?([a-zA-Z0-9_./\\-]+\.\w+)[`'\"]?",
    re.IGNORECASE,
)

_SERVER_PORT_PATTERNS = [
    re.compile(r"(?:listening|running|started|ready)\s+(?:on|at)\s+.*?(?::|port\s*)(\d{4,5})", re.IGNORECASE),
    re.compile(r"http://(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{4,5})", re.IGNORECASE),
    re.compile(r"Local:\s*https?://.*?:(\d{4,5})", re.IGNORECASE),
]

# Activity indicator keywords — same as injector.py uses for verification
_ACTIVITY_KEYWORDS = ["Thinking", "Running", "Cancel", "Generating", "Stop"]


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

async def gather_context(
    context: BrowserContext,
    page: Page,
    goal: str = "",
) -> ContextSnapshot:
    """
    Deep context gathering pipeline.

    Scrapes the IDE's DOM across all frames to build a structured
    understanding of the current state. Returns a ContextSnapshot
    with confidence score.

    This is called from the main monitoring loop and replaces the
    need for frequent screenshot-based analysis.
    """
    snapshot = ContextSnapshot(gathered_at=time.time())

    # V32: Dynamic port wiring — check session memory for known ports first,
    # then fall back to common dev server ports
    _ports_to_check = [config.DEV_SERVER_CHECK_PORT]  # Primary port
    _COMMON_DEV_PORTS = [3000, 3001, 4200, 5000, 5173, 8000, 8080]
    try:
        from .session_memory import SessionMemory
        _mem = SessionMemory()
        _mem_ports = _mem._state.get("detected_ports", [])
        if _mem_ports:
            # Prioritize session-recorded ports
            _ports_to_check = list(set(_mem_ports + _ports_to_check))
    except Exception:
        pass
    # Add common ports not already in the list
    for p in _COMMON_DEV_PORTS:
        if p not in _ports_to_check:
            _ports_to_check.append(p)

    # Run all independent checks in parallel
    results = await asyncio.gather(
        _deep_chat_scrape(context),
        _read_terminal_output(context),
        _check_dev_server_multi(_ports_to_check),
        _check_simple_browser(context),
        _check_activity_indicators(context),
        _check_approval_buttons(context),
        _get_aria_snapshot(page),
        return_exceptions=True,
    )

    # Unpack results, handling errors gracefully
    chat_messages = results[0] if not isinstance(results[0], Exception) else []
    terminal_output = results[1] if not isinstance(results[1], Exception) else []
    server_info = results[2] if not isinstance(results[2], Exception) else ServerInfo()
    browser_open = results[3] if not isinstance(results[3], Exception) else False
    is_active = results[4] if not isinstance(results[4], Exception) else False
    has_approval = results[5] if not isinstance(results[5], Exception) else False
    aria_tree = results[6] if not isinstance(results[6], Exception) else ""

    snapshot.chat_messages = chat_messages
    snapshot.terminal_output = terminal_output
    snapshot.dev_server_status = server_info
    snapshot.simple_browser_open = browser_open
    snapshot.has_pending_approval = has_approval
    snapshot.aria_tree = aria_tree

    # Parse diff reports from chat messages
    snapshot.diff_reports = _parse_diff_blocks(chat_messages)

    # Build progress info
    snapshot.progress = _build_progress(chat_messages, terminal_output)

    # Determine agent status from signals
    snapshot.agent_status = _classify_agent_status(
        chat_messages, is_active, has_approval,
    )

    # Compute confidence
    snapshot.confidence = _compute_confidence(snapshot)

    # OpenClaw: populate context budget info
    try:
        budget = get_context_budget()
        snapshot.context_chars_sent = budget.total_sent
        snapshot.context_budget_pct = budget.budget_pct
    except Exception:
        pass

    # OpenClaw: populate presence info
    snapshot.user_idle_seconds = _presence_tracker.get_idle_seconds()

    logger.info(
        "📊  Context gathered: status=%s, confidence=%.0f%%, "
        "msgs=%d, terminal=%d, server=%s, browser=%s, approval=%s, "
        "budget=%.0f%%, idle=%.0fs",
        snapshot.agent_status,
        snapshot.confidence * 100,
        len(snapshot.chat_messages),
        len(snapshot.terminal_output),
        "UP" if snapshot.dev_server_status.running else "DOWN",
        "OPEN" if snapshot.simple_browser_open else "CLOSED",
        "YES" if snapshot.has_pending_approval else "NO",
        snapshot.context_budget_pct,
        snapshot.user_idle_seconds,
    )

    return snapshot


# ─────────────────────────────────────────────────────────────
# Deep Chat Scraper
# ─────────────────────────────────────────────────────────────

async def _deep_chat_scrape(context: BrowserContext) -> list[MessageInfo]:
    """
    Goes beyond _read_chat_content() in main.py by:
    - FIRST resolving the exact iframe that contains the chat (via find_chat_frame)
    - Scraping messages from THAT specific frame, not the top-level page
    - Separating user vs agent messages via DOM class analysis
    - Detecting message types (diff blocks, errors, questions)
    - Extracting structured data from code blocks
    - Reading complete messages, not just last N chars
    """
    from .frame_walker import find_chat_frame

    messages: list[MessageInfo] = []

    # ── Phase 1: Resolve the chat frame ──────────────────────
    # The chat DOM lives inside a nested webview iframe. We MUST
    # scrape from the resolved frame, not the top-level page.
    chat_frame = None
    try:
        chat_result = await find_chat_frame(context)
        if chat_result:
            chat_frame = chat_result[0]
            logger.info(
                "🎯  Chat frame resolved for scraping: %s",
                chat_frame.url[:80] if chat_frame.url else "<unnamed>",
            )
    except Exception as exc:
        logger.warning("🎯  Failed to resolve chat frame: %s", exc)

    # ── Phase 1.5: Scroll chat to bottom ─────────────────────
    # Long agent responses push recent messages out of view.
    # Force-scroll so Playwright can see them.
    if chat_frame:
        try:
            await chat_frame.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
            # Also try scrolling common chat containers
            await chat_frame.evaluate("""
                const containers = document.querySelectorAll(
                    '[class*="chat"], [class*="scroller"], [class*="scroll-container"], [role="log"]'
                );
                containers.forEach(el => el.scrollTop = el.scrollHeight);
            """)
        except Exception as scroll_exc:
            logger.debug("🔄  Chat scroll failed (non-fatal): %s", scroll_exc)

    # ── Phase 2: Scrape messages from resolved frame ─────────
    # Aggressive selectors targeting VS Code / Antigravity chat DOM
    agent_selectors = [
        '[class*="message"][class*="agent"]',
        '[class*="message"][class*="assistant"]',
        '[data-role="assistant"]',
        '.chat-message-assistant',
        '[class*="response-message"]',
        '.interactive-result-editor',
        '[role="listitem"]',
        '.chat-message',
        '.message-item',
        '[class*="chat-message-content"]',
        '[class*="message-body"]',
    ]

    user_selectors = [
        '[class*="message"][class*="user"]',
        '[data-role="user"]',
        '.chat-message-user',
        '[class*="request-message"]',
        '.interactive-input-part',
    ]

    # All message containers (when we can't distinguish role)
    generic_selectors = [
        '.chat-message',
        '[class*="message-content"]',
        '[class*="chat"] [class*="body"]',
        '[class*="chat-entry"]',
        '[role="listitem"]',
    ]

    async def _scrape_from_frame(frame: Frame, selectors: list[str], role: str) -> list[MessageInfo]:
        """Scrape messages from a specific frame using the given selectors."""
        found: list[MessageInfo] = []
        for selector in selectors:
            try:
                elements = await frame.query_selector_all(selector)
                for el in elements:
                    try:
                        text = await el.inner_text()
                        text = text.strip()
                        if text and len(text) > 5:
                            # V16+: Strict phantom message filter — ignore placeholder/transient
                            # text that inflates msg count. Without this, msgs=1 on a blank chat.
                            _placeholder_strings = [
                                "ask anything", "thinking", "generating",
                                "type a message", "send a message", "start a conversation",
                                "how can i help", "what can i do",
                                "ask a question", "type your message",
                                "enter a prompt", "what would you like",
                                "ask me anything", "start typing",
                                "waiting for input", "ready to help",
                            ]
                            text_lower = text.lower().strip()
                            # Skip if text is entirely a placeholder OR starts with one
                            if len(text) < 150 and any(
                                text_lower == ph or text_lower.startswith(ph)
                                for ph in _placeholder_strings
                            ):
                                logger.debug("🔇  Filtered phantom message: '%s'", text[:60])
                                continue  # Skip placeholders
                            msg = _classify_message(text, role=role)
                            found.append(msg)
                    except Exception:
                        continue
                if found:
                    break  # Found messages with the first working selector
            except Exception as exc:
                exc_msg = str(exc).lower()
                if "destroyed" in exc_msg or "detached" in exc_msg:
                    logger.debug("🔄  Stale frame during scrape, skipping selector: %s", selector)
                continue
        return found

    if chat_frame:
        # Scrape from the resolved chat frame
        messages = await _scrape_from_frame(chat_frame, agent_selectors, "agent")
        user_messages = await _scrape_from_frame(chat_frame, user_selectors, "user")

        # If role-specific selectors found nothing, try generic
        if not messages and not user_messages:
            messages = await _scrape_from_frame(chat_frame, generic_selectors, "unknown")

        # Also try child frames of the chat frame (deeply nested webviews)
        if not messages and not user_messages:
            try:
                for child in chat_frame.child_frames:
                    messages = await _scrape_from_frame(child, agent_selectors + generic_selectors, "agent")
                    if messages:
                        break
            except Exception:
                pass
    else:
        # Fallback: scan all frames (original behavior)
        logger.warning("🎯  Chat frame not resolved — falling back to all-frame scan.")
        for selector in agent_selectors:
            elements = await find_all_elements_in_all_frames(context, selector)
            for frame, el in elements:
                try:
                    text = await el.inner_text()
                    text = text.strip()
                    if text and len(text) > 5:
                        msg = _classify_message(text, role="agent")
                        messages.append(msg)
                except Exception:
                    continue
            if messages:
                break

        user_messages = []
        for selector in user_selectors:
            elements = await find_all_elements_in_all_frames(context, selector)
            for frame, el in elements:
                try:
                    text = await el.inner_text()
                    text = text.strip()
                    if text and len(text) > 5:
                        msg = _classify_message(text, role="user")
                        user_messages.append(msg)
                except Exception:
                    continue
            if user_messages:
                break

    # Merge and keep last N messages
    all_messages = messages + (user_messages if 'user_messages' in dir() else [])
    # Sort by timestamp approximation (position in DOM = chronological order)
    for i, msg in enumerate(all_messages):
        msg.timestamp = time.time() - (len(all_messages) - i) * 10  # approximate

    logger.info(
        "🎯  Chat scrape result: %d messages (%d agent, %d user/other)",
        len(all_messages),
        sum(1 for m in all_messages if m.role == "agent"),
        sum(1 for m in all_messages if m.role != "agent"),
    )

    # Keep the last 20 messages
    return all_messages[-20:]


def _classify_message(text: str, role: str = "unknown") -> MessageInfo:
    """Classify a message by its content patterns."""
    msg = MessageInfo(
        role=role,
        content=text[:2000],  # Cap at 2000 chars
        timestamp=time.time(),
    )

    # Check for errors
    for pattern in _ERROR_PATTERNS:
        if pattern.search(text):
            msg.has_error = True
            msg.message_type = "error"
            break

    # Check for questions (only if not already classified as error)
    if msg.message_type == "unknown":
        for pattern in _QUESTION_PATTERNS:
            if pattern.search(text):
                msg.message_type = "question"
                break

    # Check for completion signals
    if msg.message_type == "unknown":
        for pattern in _COMPLETION_PATTERNS:
            if pattern.search(text):
                msg.message_type = "completion"
                break

    # Check for progress signals
    if msg.message_type == "unknown":
        for pattern in _PROGRESS_PATTERNS:
            if pattern.search(text):
                msg.message_type = "progress"
                break

    # Check for code blocks
    if _CODE_BLOCK_PATTERN.search(text):
        msg.has_diff = True
        if msg.message_type == "unknown":
            msg.message_type = "code"

    # Default
    if msg.message_type == "unknown":
        msg.message_type = "generic"

    return msg


# ─────────────────────────────────────────────────────────────
# Diff Report Parser
# ─────────────────────────────────────────────────────────────

def _parse_diff_blocks(messages: list[MessageInfo]) -> list[DiffReport]:
    """Extract and parse diff/code blocks from agent messages."""
    reports: list[DiffReport] = []

    for msg in messages:
        if not msg.has_diff or msg.role == "user":
            continue

        # Find explicit diff blocks
        for match in _DIFF_PATTERN.finditer(msg.content):
            diff_text = match.group(1)
            additions = diff_text.count("\n+") - diff_text.count("\n+++")
            deletions = diff_text.count("\n-") - diff_text.count("\n---")

            # Try to extract filename from diff header
            filename = "unknown"
            header_match = re.search(r"[+-]{3}\s+[ab]/(.+)", diff_text)
            if header_match:
                filename = header_match.group(1)

            reports.append(DiffReport(
                filename=filename,
                additions=max(0, additions),
                deletions=max(0, deletions),
                summary=diff_text[:200],
            ))

        # Find mentioned filenames in the message
        for match in _FILENAME_PATTERN.finditer(msg.content):
            fname = match.group(1)
            # Avoid false positives
            if len(fname) > 3 and "." in fname:
                # Check if we already have a report for this file
                if not any(r.filename == fname for r in reports):
                    reports.append(DiffReport(filename=fname, summary="mentioned"))

    return reports


# ─────────────────────────────────────────────────────────────
# Terminal Output Reader
# ─────────────────────────────────────────────────────────────

async def _read_terminal_output(context: BrowserContext) -> list[str]:
    """
    Read visible terminal output from the IDE's integrated terminal.
    Searches across all frames for xterm rows.
    """
    terminal_selectors = [
        ".xterm-rows > div",
        "[class*='terminal'] [class*='row']",
        ".xterm-screen .xterm-rows div",
    ]

    lines: list[str] = []

    for selector in terminal_selectors:
        elements = await find_all_elements_in_all_frames(context, selector)
        if elements:
            for frame, el in elements[-30:]:  # Last 30 lines
                try:
                    text = (await el.inner_text()).strip()
                    if text:
                        lines.append(text)
                except Exception:
                    continue
            if lines:
                break

    return lines


# ─────────────────────────────────────────────────────────────
# Dev Server Health Check
# ─────────────────────────────────────────────────────────────

async def _check_dev_server(port: int = 3000) -> ServerInfo:
    """
    HTTP check to see if a dev server is running on the given port.
    Uses asyncio sockets for speed — no external dependencies.
    """
    info = ServerInfo(port=port, url=f"http://localhost:{port}", last_check=time.time())

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=2.0,
        )
        # Send a minimal HTTP HEAD request
        writer.write(f"HEAD / HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()

        # Read response (just the status line)
        response = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()

        if response:
            info.running = True
            logger.debug("🌐  Dev server check: UP on port %d", port)
        else:
            logger.debug("🌐  Dev server check: connection closed immediately on port %d", port)

    except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
        logger.debug("🌐  Dev server check: DOWN on port %d", port)
    except Exception as exc:
        logger.debug("🌐  Dev server check error on port %d: %s", port, exc)

    return info


async def _check_dev_server_multi(ports: list[int]) -> ServerInfo:
    """
    V32: Check multiple ports concurrently for a running dev server.
    Returns the first UP server found, or an empty ServerInfo.
    Records discovered ports to session memory for future priority.
    """
    if not ports:
        return ServerInfo()

    # Fire all port checks concurrently
    checks = [_check_dev_server(p) for p in ports]
    results = await asyncio.gather(*checks, return_exceptions=True)

    for result in results:
        if isinstance(result, ServerInfo) and result.running:
            # V32: Record discovered port to session memory for future scans
            try:
                from .session_memory import SessionMemory
                mem = SessionMemory()
                mem.record_port(result.port)
            except Exception:
                pass
            logger.info("🌐  Dev server found on port %d", result.port)
            return result

    # No server found on any port
    return ServerInfo()

# ─────────────────────────────────────────────────────────────
# Browser Preview Check
# ─────────────────────────────────────────────────────────────

async def _check_simple_browser(context: BrowserContext) -> bool:
    """Check if browser preview panel is open in any frame."""
    browser_selectors = [
        "[class*='simple-browser']",
        "[class*='simpleBrowser']",
        "webview[src*='localhost']",
        "[class*='webview'][src*='localhost']",
        "iframe[src*='localhost']",
    ]

    for selector in browser_selectors:
        result = await find_element_in_all_frames(context, selector, require_visible=True)
        if result:
            logger.debug("🔍  Browser preview detected via: %s", selector)
            return True

    return False


# ─────────────────────────────────────────────────────────────
# Activity Indicator Check
# ─────────────────────────────────────────────────────────────

async def _check_activity_indicators(context: BrowserContext) -> bool:
    """Check if the agent shows activity indicators (Thinking, Running, etc)."""
    for page in context.pages:
        for frame in page.frames:
            try:
                for keyword in _ACTIVITY_KEYWORDS:
                    els = await frame.query_selector_all(
                        f"button:has-text('{keyword}'), "
                        f"span:has-text('{keyword}'), "
                        f"div:has-text('{keyword}')"
                    )
                    for el in els:
                        try:
                            if await el.is_visible():
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

    return False


# ─────────────────────────────────────────────────────────────
# Approval Button Check
# ─────────────────────────────────────────────────────────────

async def _check_approval_buttons(context: BrowserContext) -> bool:
    """Check if there are pending approval buttons visible."""
    approval_texts = config.APPROVAL_BUTTON_TEXTS + ["Always run"]

    for page in context.pages:
        for frame in page.frames:
            try:
                for text in approval_texts:
                    els = await frame.query_selector_all(
                        f"button:has-text('{text}')"
                    )
                    for el in els:
                        try:
                            if await el.is_visible():
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

    return False


# ─────────────────────────────────────────────────────────────
# V30: ARIA Accessibility Tree Snapshot
# ─────────────────────────────────────────────────────────────

async def _get_aria_snapshot(page: Page) -> str:
    """
    Extract Playwright's accessibility tree as a compact text map.

    This provides a lightweight, token-efficient representation of
    the UI state without needing screenshots or full HTML dumps.
    Inspired by OpenClaw's ARIA-first state detection.

    Returns a formatted text tree, or empty string on failure.
    """
    try:
        snapshot = await page.accessibility.snapshot()
        if not snapshot:
            return ""

        lines: list[str] = []
        _walk_aria_node(snapshot, lines, depth=0, max_depth=4, max_lines=60)
        result = "\n".join(lines)
        logger.debug("♿  ARIA snapshot: %d lines, %d chars", len(lines), len(result))
        return result

    except Exception as exc:
        logger.debug("♿  ARIA snapshot failed (non-fatal): %s", exc)
        return ""


def _walk_aria_node(
    node: dict,
    lines: list[str],
    depth: int = 0,
    max_depth: int = 4,
    max_lines: int = 60,
) -> None:
    """Recursively walk ARIA tree nodes into a compact text representation."""
    if depth > max_depth or len(lines) >= max_lines:
        return

    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    # Skip generic/empty nodes
    if role in ("none", "generic", "presentation") and not name:
        # Still walk children
        for child in node.get("children", []):
            _walk_aria_node(child, lines, depth, max_depth, max_lines)
        return

    indent = "  " * depth
    parts = [role]
    if name:
        parts.append(f'"{name[:80]}"')
    if value:
        parts.append(f'val="{value[:60]}"')

    # Include useful states
    for key in ("checked", "pressed", "expanded", "disabled", "selected"):
        if node.get(key):
            parts.append(key)

    lines.append(f"{indent}{' '.join(parts)}")

    for child in node.get("children", []):
        _walk_aria_node(child, lines, depth + 1, max_depth, max_lines)


# ─────────────────────────────────────────────────────────────
# Progress Builder
# ─────────────────────────────────────────────────────────────

def _build_progress(
    messages: list[MessageInfo],
    terminal_output: list[str],
) -> ProgressInfo:
    """Build progress information from messages and terminal output."""
    progress = ProgressInfo()

    for msg in messages:
        # Collect filenames mentioned
        for match in _FILENAME_PATTERN.finditer(msg.content):
            fname = match.group(1)
            if fname not in progress.files_mentioned and len(fname) > 3:
                progress.files_mentioned.append(fname)

        # Collect errors
        if msg.has_error:
            error_snippet = msg.content[:120].strip()
            if error_snippet not in progress.errors_seen:
                progress.errors_seen.append(error_snippet)

        # Count completions
        if msg.message_type == "completion":
            progress.completions_detected += 1

    # Check terminal for commands
    for line in terminal_output:
        if line.startswith("$") or line.startswith(">"):
            cmd = line.lstrip("$> ").strip()
            if cmd and cmd not in progress.commands_run:
                progress.commands_run.append(cmd)

    # Rough estimate of completion
    if progress.completions_detected > 0:
        progress.percent_complete = min(1.0, 0.5 + progress.completions_detected * 0.15)
    elif len(progress.files_mentioned) > 3:
        progress.percent_complete = 0.3
    elif progress.errors_seen:
        progress.percent_complete = 0.1

    return progress


# ─────────────────────────────────────────────────────────────
# Agent Status Classifier
# ─────────────────────────────────────────────────────────────

def _classify_agent_status(
    messages: list[MessageInfo],
    is_active: bool,
    has_approval: bool,
) -> str:
    """
    Determine the agent's current status from text signals.

    Returns: WORKING, WAITING, ASKING, IDLE
    """
    # Active indicators always mean WORKING
    if is_active:
        return "WORKING"

    # Pending approval = WAITING
    if has_approval:
        return "WAITING"

    if not messages:
        return "UNKNOWN"

    # Analyze the last few messages
    recent = messages[-3:]
    last_msg = recent[-1]

    # Question detected → ASKING
    if last_msg.message_type == "question":
        return "ASKING"

    # Completion → IDLE
    if last_msg.message_type == "completion":
        return "IDLE"

    # Error → WAITING (agent might be stuck)
    if last_msg.has_error:
        return "WAITING"

    # Code / progress → WORKING
    if last_msg.message_type in ("code", "progress"):
        return "WORKING"

    # If we have messages but can't determine status, likely WORKING
    return "WORKING"


# ─────────────────────────────────────────────────────────────
# Confidence Scorer
# ─────────────────────────────────────────────────────────────

def _compute_confidence(snapshot: ContextSnapshot) -> float:
    """
    Compute how confident we are in the text-based analysis.
    Higher confidence = fewer reasons to take a screenshot.

    Returns 0.0 to 1.0.
    """
    score = 0.0
    reasons = []

    # More messages = more data = higher confidence
    msg_count = len(snapshot.chat_messages)
    if msg_count >= 5:
        score += 0.25
        reasons.append("5+ messages")
    elif msg_count >= 2:
        score += 0.15
        reasons.append(f"{msg_count} messages")
    elif msg_count == 0:
        # No messages at all — very low confidence
        reasons.append("no messages")

    # Activity indicators give strong confidence
    if snapshot.agent_status == "WORKING":
        score += 0.25
        reasons.append("activity detected")

    # Approval buttons detected — high confidence in WAITING
    if snapshot.has_pending_approval:
        score += 0.30
        reasons.append("approval pending")

    # Recent messages with clear signals
    if snapshot.chat_messages:
        last_msg = snapshot.chat_messages[-1]
        if last_msg.message_type != "generic":
            score += 0.15
            reasons.append(f"typed={last_msg.message_type}")

        # How recent is the last message (approximation)?
        age = time.time() - last_msg.timestamp
        if age < 30:
            score += 0.10
            reasons.append("recent msg")
        elif age > 120:
            score -= 0.15
            reasons.append("stale msgs")

    # Terminal output available
    if snapshot.terminal_output:
        score += 0.05
        reasons.append("terminal data")

    # Dev server status is known
    if snapshot.dev_server_status.running:
        score += 0.05
        reasons.append("server up")

    # Clamp to [0, 1]
    score = max(0.0, min(1.0, score))

    logger.debug("📊  Confidence=%.0f%%: %s", score * 100, ", ".join(reasons))
    return score


# ─────────────────────────────────────────────────────────────
# Decision: Should we take a screenshot?
# ─────────────────────────────────────────────────────────────

_last_screenshot_decision_time: float = 0.0

def needs_screenshot(snapshot: ContextSnapshot) -> bool:
    """
    V30.1: VISION SEVERED — always returns False.

    State detection is now 100% DOM/ARIA-driven via ContextSnapshot.
    Screenshots are never taken in the main polling loop.
    The _analyze_ide_state path is dead code and will never trigger.

    Previous logic checked confidence thresholds and forced screenshots
    on a timer. This caused Ollama timeouts and Gemini rate limit
    cascades. ZERO pixels are now processed for state checking.
    """
    logger.debug(
        "📸  V30.1: Screenshot request DENIED (vision severed). "
        "Using ARIA/DOM text (confidence=%.0f%%).",
        snapshot.confidence * 100,
    )
    return False


# ─────────────────────────────────────────────────────────────
# Context Formatting — for Gemini prompts
# ─────────────────────────────────────────────────────────────

def format_context_for_prompt(snapshot: ContextSnapshot, max_chars: int = 8000) -> str:
    """
    Format the context snapshot into a string suitable for inclusion
    in a Gemini prompt. This enriches the screenshot analysis with
    structured text data.
    """
    parts: list[str] = []
    total = 0

    parts.append(f"AGENT STATUS: {snapshot.agent_status}")
    parts.append(f"CONFIDENCE: {snapshot.confidence:.0%}")

    if snapshot.chat_messages:
        parts.append("\nRECENT CHAT MESSAGES:")
        for msg in snapshot.chat_messages[-5:]:
            role_tag = f"[{msg.role.upper()}]"
            type_tag = f"({msg.message_type})"
            content_preview = msg.content[:300]
            line = f"  {role_tag} {type_tag}: {content_preview}"
            parts.append(line)

    if snapshot.diff_reports:
        parts.append(f"\nFILES CHANGED ({len(snapshot.diff_reports)}):")
        for dr in snapshot.diff_reports[-5:]:
            parts.append(f"  • {dr.filename} (+{dr.additions}/-{dr.deletions})")

    if snapshot.terminal_output:
        parts.append("\nTERMINAL (last 5 lines):")
        for line in snapshot.terminal_output[-5:]:
            parts.append(f"  {line[:150]}")

    parts.append(f"\nDev server: {'RUNNING on port ' + str(snapshot.dev_server_status.port) if snapshot.dev_server_status.running else 'DOWN'}")
    parts.append(f"Browser Preview: {'OPEN' if snapshot.simple_browser_open else 'CLOSED'}")
    parts.append(f"Pending approval: {'YES' if snapshot.has_pending_approval else 'NO'}")

    # V30: Include ARIA tree for token-efficient UI understanding
    if snapshot.aria_tree:
        parts.append("\nARIA ACCESSIBILITY TREE (compact):")
        # Truncate to avoid budget blow-up
        aria_budget = min(2000, max_chars // 4)
        parts.append(snapshot.aria_tree[:aria_budget])
    if snapshot.progress.files_mentioned:
        parts.append(f"Files worked on: {', '.join(snapshot.progress.files_mentioned[:10])}")
    if snapshot.progress.errors_seen:
        parts.append(f"Errors seen: {len(snapshot.progress.errors_seen)}")

    # OpenClaw Tier 3: Inject custom skills
    from . import skills_loader
    skills_text = skills_loader.load_active_skills(max_chars=3000)
    if skills_text:
        parts.append("\n" + skills_text)

    # V12 Flagship: The Omniscient Eye (Workspace RAG)
    try:
        from .workspace_indexer import WorkspaceMap
        project_path = config.get_project_path()
        if project_path:
            wm = WorkspaceMap(project_path)
            # Combine all text the AI is about to read to find relevant symbols
            full_text = "\n".join(parts)
            rag_context = wm.query_relevant_signatures(full_text, top_k=3)
            if rag_context:
                parts.append("\n" + rag_context)
    except Exception as exc:
        logger.debug("Omniscient Eye injection failed: %s", exc)

    result = "\n".join(parts)
    return result[:max_chars]


# ─────────────────────────────────────────────────────────────
# OpenClaw-Inspired: Presence Tracker
# ─────────────────────────────────────────────────────────────

class PresenceTracker:
    """
    Track user activity in the IDE via DOM signals.

    Inspired by OpenClaw's presence tracking:
      - Monitors focus changes, scroll events, typing activity
      - Reports idle time since last detected activity
      - Used to decide aggressive vs conservative actions
    """

    def __init__(self):
        self._last_activity = time.time()
        self._last_chat_hash = ""
        self._activity_count = 0

    def record_activity(self) -> None:
        """Record that user activity was detected."""
        self._last_activity = time.time()
        self._activity_count += 1

    def check_chat_change(self, chat_messages: list) -> bool:
        """Check if chat messages changed (indicates user/agent activity)."""
        current_hash = str(len(chat_messages))
        if chat_messages:
            last_content = chat_messages[-1].content[:100] if hasattr(chat_messages[-1], 'content') else ""
            current_hash += last_content

        if current_hash != self._last_chat_hash:
            self._last_chat_hash = current_hash
            self.record_activity()
            return True
        return False

    def get_idle_seconds(self) -> float:
        """Return seconds since last detected activity."""
        return time.time() - self._last_activity

    def is_user_idle(self, threshold: Optional[float] = None) -> bool:
        """Return True if user appears idle."""
        threshold = threshold or config.PRESENCE_IDLE_THRESHOLD_S
        return self.get_idle_seconds() > threshold

    def get_status(self) -> dict:
        """Return presence status."""
        idle = self.get_idle_seconds()
        return {
            "idle_seconds": idle,
            "is_idle": self.is_user_idle(),
            "activity_count": self._activity_count,
            "status": "idle" if self.is_user_idle() else "active",
        }


# Module-level singleton
_presence_tracker = PresenceTracker()


def get_presence_tracker() -> PresenceTracker:
    """Get the global presence tracker."""
    return _presence_tracker


# ─────────────────────────────────────────────────────────────
# OpenClaw-Inspired: Context Budget Report
# ─────────────────────────────────────────────────────────────

def format_context_report(snapshot: ContextSnapshot) -> str:
    """
    Generate a structured context report combining all dimensions.

    Inspired by OpenClaw's /context command which breaks down
    the context window usage per component.
    """
    lines = ["=== CONTEXT REPORT ==="]

    # Agent status
    lines.append(f"Agent: {snapshot.agent_status} (confidence: {snapshot.confidence:.0%})")

    # Context budget
    try:
        budget = get_context_budget()
        lines.append(budget.get_report())
    except Exception:
        lines.append("📊 Context Budget: N/A")

    # Presence
    presence = _presence_tracker.get_status()
    lines.append(
        f"👤 User: {presence['status']} "
        f"(idle {presence['idle_seconds']:.0f}s, "
        f"{presence['activity_count']} actions)"
    )

    # Server
    if snapshot.dev_server_status.running:
        lines.append(f"🌐 Server: UP on port {snapshot.dev_server_status.port}")
    else:
        lines.append("🌐 Server: DOWN")

    # Browser
    lines.append(f"🔍 Browser: {'OPEN' if snapshot.simple_browser_open else 'CLOSED'}")

    # Progress
    if snapshot.progress.files_mentioned:
        lines.append(f"📁 Files: {', '.join(snapshot.progress.files_mentioned[:8])}")
    if snapshot.progress.errors_seen:
        lines.append(f"⚠️ Errors: {len(snapshot.progress.errors_seen)}")

    return "\n".join(lines)
