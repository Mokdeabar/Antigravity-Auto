"""
terminal.py — Integrated Terminal Automation.

Execute commands inside Antigravity's integrated terminal via:
  • Keyboard shortcut (Ctrl+`) to toggle the terminal panel.
  • Type commands directly into the terminal.
  • Read terminal output by scraping the terminal DOM.
  • Run npm, git, python, build commands, etc.
"""

import asyncio
import logging
from typing import Optional

from playwright.async_api import Page

from . import config
from . import command_resolver

logger = logging.getLogger("supervisor.terminal")

# Selectors for the integrated terminal.
TERMINAL_SELECTORS = [
    ".terminal-wrapper textarea",  # xterm textarea
    ".xterm-helper-textarea",      # xterm helper
    ".terminal textarea",
    "[class*='terminal'] textarea",
]


async def toggle_terminal(page: Page) -> bool:
    """Open/close the integrated terminal via keyboard shortcut."""
    try:
        await page.keyboard.press("Control+`")
        await asyncio.sleep(0.5)
        logger.info("Toggled integrated terminal.")
        return True
    except Exception as exc:
        logger.error("Failed to toggle terminal: %s", exc)
        return False


async def open_terminal(page: Page) -> bool:
    """Ensure the integrated terminal is open."""
    try:
        # Check if terminal is already visible.
        for sel in TERMINAL_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info("Terminal is already open.")
                    return True
            except Exception:
                continue

        # Open it via command palette.
        await _run_command_palette(page, "Terminal: Create New Terminal")
        await asyncio.sleep(1.0)
        logger.info("Opened new terminal.")
        return True

    except Exception as exc:
        logger.error("Failed to open terminal: %s", exc)
        # Try keyboard shortcut as fallback.
        return await toggle_terminal(page)


async def run_terminal_command(page: Page, command: str, wait_seconds: float = 3.0) -> bool:
    """
    Type a command into the integrated terminal and press Enter.

    Args:
        page: The Playwright page object.
        command: The shell command to execute.
        wait_seconds: How long to wait after pressing Enter for output.

    Returns:
        True if the command was typed successfully.
    """
    # Ensure terminal is open.
    await open_terminal(page)
    await asyncio.sleep(0.3)

    try:
        # Find the terminal input.
        terminal_input = None
        for sel in TERMINAL_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    terminal_input = el
                    break
            except Exception:
                continue

        if not terminal_input:
            logger.error("Could not find terminal input element.")
            return False

        # Click to focus the terminal.
        await terminal_input.click()
        await asyncio.sleep(0.1)

        # Type the command and press Enter.
        await page.keyboard.insert_text(command)
        await asyncio.sleep(0.1)
        await page.keyboard.press("Enter")
        await asyncio.sleep(wait_seconds)

        logger.info("Executed terminal command: %s", command[:100])
        return True

    except Exception as exc:
        logger.error("Failed to run terminal command: %s", exc)
        return False


async def read_terminal_output(page: Page, max_lines: int = 50) -> list[str]:
    """
    Scrape the visible terminal output from the DOM.

    Returns a list of text lines visible in the terminal.
    """
    try:
        # xterm renders text in rows.
        rows = await page.query_selector_all(".xterm-rows > div")
        if not rows:
            rows = await page.query_selector_all("[class*='terminal'] [class*='row']")

        lines = []
        for row in rows[-max_lines:]:
            try:
                text = (await row.inner_text()).strip()
                if text:
                    lines.append(text)
            except Exception:
                continue

        logger.info("Read %d lines from terminal.", len(lines))
        return lines

    except Exception as exc:
        logger.error("Failed to read terminal output: %s", exc)
        return []


async def _run_command_palette(page: Page, command: str) -> bool:
    """
    Open the VS Code command palette and execute a command.

    V10: Uses the self-healing _run_palette_command from main.py which:
      - Checks the command palette cache
      - Verifies via screenshot + Gemini that the right command matched
      - Self-corrects and caches if the name was wrong
    Falls back to raw keyboard if the self-healing import fails.
    """
    # Build an intent key from the command text (e.g. "Terminal: Create New Terminal" → "create_terminal")
    intent_key = command.lower().split(":")[-1].strip().replace(" ", "_")

    try:
        from .main import _run_palette_command
        result = await _run_palette_command(page, intent_key, command)
        if result:
            return True
        logger.warning("Self-healing palette command returned False — using raw fallback.")
    except Exception as exc:
        logger.warning("Could not use self-healing palette: %s — using raw fallback.", exc)

    # Raw fallback: Ctrl+Shift+P + type + Enter
    # V11 OpenClaw Upgrade: Always resolve to an exact command ID before typing blindly
    resolved_cmd, source = command_resolver.resolve_command(intent_key, command)
    
    warning = command_resolver.validate_and_warn(resolved_cmd)
    if warning:
        logger.warning("🗺️  (terminal raw fallback) %s", warning)

    if source in ("intent_map", "exact_match", "fuzzy"):
        logger.info("🗺️  Raw fallback upgraded to safe resolved command: %s (source: %s)", resolved_cmd, source)
        command_to_type = resolved_cmd
    else:
        command_to_type = command

    try:
        await page.keyboard.press("Control+Shift+p")
        await asyncio.sleep(0.5)
        await page.keyboard.insert_text(command_to_type)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)
        logger.info("Executed command palette (raw fallback): %s", command_to_type)
        return True
    except Exception as exc:
        logger.error("Command palette failed: %s", exc)
        return False

