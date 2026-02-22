"""
approver.py — The Hands.

Auto-clicks approval / allow / run buttons found by the monitor.
"""

import asyncio
import logging

from playwright.async_api import ElementHandle, Frame

from . import config

logger = logging.getLogger("supervisor.approver")


async def auto_approve(buttons: list[tuple[Frame, ElementHandle]] | list[ElementHandle]) -> int:
    """
    Click each approval button in the list.
    Accepts either (Frame, ElementHandle) tuples or bare ElementHandles.
    Returns the number of buttons successfully clicked.
    """
    clicked = 0
    for item in buttons:
        # Handle both tuple and bare element formats.
        btn = item[1] if isinstance(item, tuple) else item
        try:
            label = (await btn.inner_text()).strip()
            logger.info("🟢  Auto-approving: [%s]", label)
            await btn.click()
            clicked += 1
            # Small delay between clicks to let the UI settle.
            await asyncio.sleep(config.ACTION_DELAY_MS / 1000)
        except Exception as exc:
            logger.warning("Failed to click approval button: %s", exc)
    return clicked

