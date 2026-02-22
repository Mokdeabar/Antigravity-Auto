"""
extensions.py — Extension Management.

Install, uninstall, and manage VS Code extensions in Antigravity via:
  • CLI: Antigravity.exe --install-extension <id>
  • UI: Ctrl+Shift+X → search → install via DOM
  • List installed extensions.
"""

import asyncio
import logging
import subprocess
import platform
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from . import config

logger = logging.getLogger("supervisor.extensions")


# ─────────────────────────────────────────────────────────────
# CLI-based extension management
# ─────────────────────────────────────────────────────────────

def _find_antigravity_exe() -> Optional[str]:
    """Find the Antigravity executable."""
    from .main import ANTIGRAVITY_EXE_CANDIDATES
    import shutil

    for candidate in ANTIGRAVITY_EXE_CANDIDATES:
        if candidate.exists():
            return str(candidate)

    on_path = shutil.which("Antigravity") or shutil.which("antigravity")
    return on_path


def install_extension_cli(extension_id: str) -> bool:
    """
    Install an extension via the Antigravity CLI.

    Args:
        extension_id: The marketplace extension ID (e.g., 'ms-python.python')

    Returns:
        True on success.
    """
    exe = _find_antigravity_exe()
    if not exe:
        logger.error("Cannot install extension: Antigravity executable not found.")
        return False

    cmd = [exe, "--install-extension", extension_id]
    logger.info("Installing extension via CLI: %s", extension_id)

    try:
        result = subprocess.run(
            cmd if not config.IS_WINDOWS else f'"{exe}" --install-extension {extension_id}',
            capture_output=True,
            text=True,
            timeout=120,
            shell=config.IS_WINDOWS,
        )

        if result.returncode == 0:
            logger.info("✅  Extension installed: %s", extension_id)
            logger.debug("Output: %s", result.stdout[:500])
            return True
        else:
            logger.error(
                "Extension install failed (code %d): %s",
                result.returncode,
                result.stderr[:500],
            )
            return False

    except subprocess.TimeoutExpired:
        logger.error("Extension install timed out: %s", extension_id)
        return False
    except Exception as exc:
        logger.error("Extension install error: %s", exc)
        return False


def uninstall_extension_cli(extension_id: str) -> bool:
    """Uninstall an extension via CLI."""
    exe = _find_antigravity_exe()
    if not exe:
        logger.error("Cannot uninstall extension: Antigravity executable not found.")
        return False

    try:
        result = subprocess.run(
            f'"{exe}" --uninstall-extension {extension_id}' if config.IS_WINDOWS else [exe, "--uninstall-extension", extension_id],
            capture_output=True,
            text=True,
            timeout=60,
            shell=config.IS_WINDOWS,
        )

        if result.returncode == 0:
            logger.info("✅  Extension uninstalled: %s", extension_id)
            return True
        else:
            logger.error("Extension uninstall failed: %s", result.stderr[:500])
            return False

    except Exception as exc:
        logger.error("Extension uninstall error: %s", exc)
        return False


def list_extensions_cli() -> list[str]:
    """List all installed extensions via CLI."""
    exe = _find_antigravity_exe()
    if not exe:
        return []

    try:
        result = subprocess.run(
            f'"{exe}" --list-extensions' if config.IS_WINDOWS else [exe, "--list-extensions"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=config.IS_WINDOWS,
        )

        if result.returncode == 0:
            extensions = [
                line.strip()
                for line in result.stdout.strip().splitlines()
                if line.strip()
            ]
            logger.info("Found %d installed extensions.", len(extensions))
            return extensions
        else:
            logger.error("Failed to list extensions: %s", result.stderr[:500])
            return []

    except Exception as exc:
        logger.error("List extensions error: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────
# UI-based extension management (via keyboard shortcuts)
# ─────────────────────────────────────────────────────────────

async def install_extension_ui(page: Page, extension_name: str) -> bool:
    """
    Install an extension via the Extensions panel UI.

    Opens Extensions panel (Ctrl+Shift+X), searches for the extension,
    and clicks the Install button.
    """
    try:
        # Open Extensions panel.
        await page.keyboard.press("Control+Shift+x")
        await asyncio.sleep(1.0)

        # Find the search input in the extensions panel.
        search_input = await page.query_selector(
            "[class*='extensions'] input[type='text'], "
            "[class*='extensions'] input[placeholder*='Search'], "
            ".extensions-search-container input"
        )

        if not search_input:
            logger.error("Could not find extensions search input.")
            return False

        # Clear and type the extension name.
        await search_input.click()
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Backspace")
        await search_input.fill(extension_name)
        await asyncio.sleep(2.0)  # Wait for search results.

        # Click the first Install button.
        install_btn = await page.query_selector(
            "[class*='extension-list-item'] button[class*='install'], "
            "button[aria-label*='Install']"
        )

        if install_btn and await install_btn.is_visible():
            await install_btn.click()
            await asyncio.sleep(3.0)
            logger.info("✅  Extension install initiated via UI: %s", extension_name)
            return True
        else:
            logger.warning("No Install button found for: %s", extension_name)
            return False

    except Exception as exc:
        logger.error("UI extension install failed: %s", exc)
        return False


async def install_extension(page: Page, extension_id: str) -> bool:
    """
    Install an extension, preferring CLI method, falling back to UI.
    """
    # Try CLI first (faster and more reliable).
    if install_extension_cli(extension_id):
        return True

    # Fallback to UI.
    logger.info("CLI install failed, trying UI method for: %s", extension_id)
    return await install_extension_ui(page, extension_id)
