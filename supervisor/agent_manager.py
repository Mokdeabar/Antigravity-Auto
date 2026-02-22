"""
agent_manager.py — Agent Manager Interaction.

Interacts with the Antigravity Agent Manager panel to:
  • Open/close the Agent Manager.
  • Spawn new agent sessions.
  • Switch between active agents.
  • Read agent status from the DOM.
  • Dispatch tasks to specific agents.
"""

import asyncio
import logging
from typing import Optional

from playwright.async_api import Page, ElementHandle

from . import config
from . import command_resolver

logger = logging.getLogger("supervisor.agent_manager")


# ─────────────────────────────────────────────────────────────
# Agent Manager Panel Operations
# ─────────────────────────────────────────────────────────────

async def open_agent_manager(page: Page) -> bool:
    """
    Open the Agent Manager panel via the command palette.
    """
    try:
        # Pre-resolve via command resolver for the correct command string.
        resolved, source = command_resolver.resolve_command(
            "open_agent_manager", "Agent Manager"
        )
        cmd_text = resolved if source != "default" else "Agent Manager"

        # Try command palette first.
        await page.keyboard.press("Control+Shift+p")
        await asyncio.sleep(0.5)
        await page.keyboard.insert_text(cmd_text)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.0)
        logger.info("Opened Agent Manager via command palette (resolved: '%s').", cmd_text)
        return True
    except Exception as exc:
        logger.error("Failed to open Agent Manager: %s", exc)
        return False


async def close_agent_manager(page: Page) -> bool:
    """Close the Agent Manager panel."""
    try:
        # Try Escape or closing the panel via command palette.
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
        return True
    except Exception as exc:
        logger.error("Failed to close Agent Manager: %s", exc)
        return False


async def new_agent_session(page: Page) -> bool:
    """
    Start a new agent session/conversation via:
      1. Command palette: "New Agent"
      2. Keyboard shortcut if available.
      3. Click the "+" button in the Agent Manager.
    """
    try:
        # Pre-resolve via command resolver.
        resolved, source = command_resolver.resolve_command(
            "start_new_conversation", "New Agent"
        )
        cmd_text = resolved if source != "default" else "New Agent"

        # Try command palette.
        await page.keyboard.press("Control+Shift+p")
        await asyncio.sleep(0.5)
        await page.keyboard.insert_text(cmd_text)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)

        logger.info("Started new agent session (resolved: '%s').", cmd_text)
        return True

    except Exception as exc:
        logger.error("Failed to start new agent session: %s", exc)

    # Fallback: look for a "New" or "+" button in the agent panel.
    try:
        new_btn = await page.query_selector(
            "[class*='agent'] button[aria-label*='new' i], "
            "[class*='agent'] button[aria-label*='New'], "
            "[class*='agent'] button[title*='New'], "
            "[class*='chat'] button[aria-label*='new' i], "
            "button[aria-label*='new chat' i], "
            "button[aria-label*='New Chat']"
        )
        if new_btn and await new_btn.is_visible():
            await new_btn.click()
            await asyncio.sleep(1.5)
            logger.info("Started new agent session via button click.")
            return True
    except Exception:
        pass

    return False


async def list_agents(page: Page) -> list[dict[str, str]]:
    """
    List all active agent sessions by scraping the Agent Manager DOM.

    Returns a list of dicts with 'name', 'status', and 'index' keys.
    """
    agents = []
    try:
        # Look for agent list items.
        items = await page.query_selector_all(
            "[class*='agent'] [class*='list-item'], "
            "[class*='agent'] [class*='session'], "
            "[class*='chat'] [class*='history-item'], "
            "[class*='conversation-list'] > *"
        )

        for i, item in enumerate(items):
            try:
                text = (await item.inner_text()).strip()
                if text:
                    agents.append({
                        "name": text[:80],
                        "status": "active" if i == 0 else "idle",
                        "index": str(i),
                    })
            except Exception:
                continue

        logger.info("Found %d agent sessions.", len(agents))

    except Exception as exc:
        logger.error("Failed to list agents: %s", exc)

    return agents


async def switch_to_agent(page: Page, index: int) -> bool:
    """
    Switch to a specific agent session by clicking on it.
    """
    try:
        items = await page.query_selector_all(
            "[class*='agent'] [class*='list-item'], "
            "[class*='agent'] [class*='session'], "
            "[class*='chat'] [class*='history-item'], "
            "[class*='conversation-list'] > *"
        )

        if index < len(items):
            await items[index].click()
            await asyncio.sleep(0.5)
            logger.info("Switched to agent session #%d.", index)
            return True
        else:
            logger.warning("Agent index %d out of range (%d agents).", index, len(items))
            return False

    except Exception as exc:
        logger.error("Failed to switch agent: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────
# Command Dispatch
# ─────────────────────────────────────────────────────────────

async def run_command_palette_action(page: Page, action: str) -> bool:
    """
    Execute any VS Code command via the command palette.

    V8: Pre-validates action against ag-commands.txt and logs warnings
    with suggestions if the command is not found.

    Args:
        action: The command text (e.g., "Terminal: Create New Terminal",
                "Git: Push", "Extensions: Install Extensions", etc.)
    """
    # Pre-validate via command resolver
    warning = command_resolver.validate_and_warn(action)
    if warning:
        logger.warning("🗺️  %s", warning)

    try:
        await page.keyboard.press("Control+Shift+p")
        await asyncio.sleep(0.5)

        # Clear any existing text.
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Backspace")

        await page.keyboard.insert_text(action)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.0)

        logger.info("Executed command palette action: %s", action)
        return True

    except Exception as exc:
        logger.error("Command palette action failed (%s): %s", action, exc)
        return False


# ─────────────────────────────────────────────────────────────
# mDNS Setup
# ─────────────────────────────────────────────────────────────

async def setup_mdns(page: Page) -> bool:
    """
    Set up mDNS by running the appropriate command palette action
    or installing the required extension.
    """
    try:
        # Try command palette for mDNS / remote connection.
        return await run_command_palette_action(page, "Remote: Connect to Host")
    except Exception as exc:
        logger.error("mDNS setup failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────
# Quick actions
# ─────────────────────────────────────────────────────────────

async def open_simple_browser(page: Page, url: str = "http://localhost") -> bool:
    """
    Open the Simple Browser side panel via open_browser.ps1.
    
    CRITICAL: Do NOT use Command Palette keystrokes via Playwright.
    This causes top-level navigation that corrupts the IDE DOM.
    """
    import subprocess
    try:
        project_path = config.get_project_path()
        if not project_path:
            logger.warning("Cannot locate open_browser.ps1 — no project path set")
            return False

        script_path = project_path / "open_browser.ps1"
        if not script_path.exists():
            logger.warning("open_browser.ps1 not found at %s", script_path)
            return False

        from .proactive_engine import _resolve_powershell
        ps_exe = _resolve_powershell()
        subprocess.Popen(
            [ps_exe, "-ExecutionPolicy", "Bypass", "-File",
             str(script_path), "-Url", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if config.IS_WINDOWS else 0,
        )
        logger.info("Launched open_browser.ps1 → %s", url)
        await asyncio.sleep(5.0)
        return True
    except Exception as exc:
        logger.error("Failed to launch open_browser.ps1: %s", exc)
        return False


async def open_file(page: Page, file_path: str) -> bool:
    """Open a file in the editor via Ctrl+O or command palette."""
    try:
        await page.keyboard.press("Control+o")
        await asyncio.sleep(0.5)
        await page.keyboard.insert_text(file_path)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.0)

        logger.info("Opened file: %s", file_path)
        return True
    except Exception as exc:
        logger.error("Failed to open file: %s", exc)
        return False
