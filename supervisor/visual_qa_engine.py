"""
visual_qa_engine.py — V23 Multimodal Visual QA Engine

Gives the agent eyes. After code passes V22 TDD, frontend tasks undergo
a Visual Phase: the engine boots a dev server, renders the page in a
headless Playwright browser, captures a 720p screenshot, and routes it
to the Gemini vision model for a strict pass/fail visual QA check.

If the vision model detects overlapping elements, invisible text, broken
layouts, or missing components, the node is failed and the visual critique
is fed back to the CLI Worker for correction.

Pipeline:
  1. Detect UI node (keyword match on description)
  2. Boot dev server (npm run dev) in detached subprocess
  3. Poll localhost until 200 OK
  4. Launch Playwright headless, inject animation-disabling CSS
  5. Capture full-page screenshot, compress to 720p
  6. Route screenshot + objective to Gemini 3.1 Pro vision
  7. Parse strict boolean verdict
"""

import asyncio
import base64
import io
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("supervisor.visual_qa_engine")

# UI keywords that trigger the Visual Phase
UI_KEYWORDS = [
    "component", "frontend", "view", "page", "layout", "ui",
    "button", "form", "modal", "dialog", "sidebar", "navbar",
    "header", "footer", "card", "dashboard", "chart", "table",
    "css", "style", "theme", "responsive", "animation", "menu",
    "checkout", "login", "signup", "profile", "settings",
    "html", "template", "render", "display", "screen",
]

# CSS injected to disable animations and transitions for stable screenshots
FREEZE_CSS = """
*, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    transition-duration: 0s !important;
    transition-delay: 0s !important;
    scroll-behavior: auto !important;
}
"""

SCREENSHOT_WIDTH = 1280
SCREENSHOT_HEIGHT = 720
MAX_SERVER_WAIT_S = 30
POLL_INTERVAL_S = 1.0


class VisualQAEngine:
    """
    Renders frontend code in a headless browser and validates it
    visually via the Gemini vision model.
    """

    VISION_PROMPT = (
        "You are a strict UI Quality Assurance inspector. Analyze this screenshot "
        "of a web application against the given requirement.\n\n"
        "CHECK FOR:\n"
        "1. Invisible text (same color as background)\n"
        "2. Overlapping or clipped elements\n"
        "3. Broken layouts or misaligned components\n"
        "4. Missing UI elements mentioned in the requirement\n"
        "5. Blank or empty pages\n"
        "6. Console-style error messages rendered on screen\n\n"
        "Output strict JSON:\n"
        '{"pass": true/false, "critique": "one-sentence explanation"}'
    )

    def __init__(self, gemini_advisor=None):
        """
        Args:
            gemini_advisor: An object with an `analyze_image(path, prompt)` method,
                            or None to use the Gemini CLI fallback.
        """
        self._advisor = gemini_advisor
        self._server_proc = None

    # ────────────────────────────────────────────────
    # UI Node Detection
    # ────────────────────────────────────────────────

    @staticmethod
    def is_ui_node(description: str) -> bool:
        """Check if a node description contains frontend/UI keywords."""
        lower = description.lower()
        return any(kw in lower for kw in UI_KEYWORDS)

    # ────────────────────────────────────────────────
    # Dev Server Management
    # ────────────────────────────────────────────────

    def start_dev_server(
        self,
        cwd: str,
        cmd: str = "npm run dev",
        port: int = 3000,
    ) -> Tuple[bool, str]:
        """
        Boot a local development server in a detached subprocess.
        Returns (success, url_or_error).
        """
        try:
            self._server_proc = subprocess.Popen(
                cmd.split(),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            url = f"http://localhost:{port}"
            logger.info("🖥️ Dev server started (PID %d) at %s", self._server_proc.pid, url)
            return True, url
        except FileNotFoundError:
            return False, f"Command not found: {cmd}"
        except Exception as e:
            return False, f"Failed to start dev server: {e}"

    async def wait_for_server(self, url: str, timeout: int = MAX_SERVER_WAIT_S) -> bool:
        """Poll the dev server until it returns 200 OK."""
        import urllib.request
        import urllib.error

        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = urllib.request.urlopen(url, timeout=3)
                if resp.status == 200:
                    logger.info("🖥️ Dev server ready at %s", url)
                    return True
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            await asyncio.sleep(POLL_INTERVAL_S)

        logger.warning("🖥️ Dev server did not respond within %ds.", timeout)
        return False

    def stop_dev_server(self):
        """Terminate the detached dev server."""
        if self._server_proc:
            try:
                self._server_proc.terminate()
                self._server_proc.wait(timeout=5)
            except Exception:
                try:
                    self._server_proc.kill()
                except Exception:
                    pass
            self._server_proc = None
            logger.info("🖥️ Dev server stopped.")

    # ────────────────────────────────────────────────
    # Screenshot Capture
    # ────────────────────────────────────────────────

    async def capture_screenshot(
        self,
        url: str,
        output_path: str,
    ) -> Tuple[bool, str]:
        """
        Launch Playwright headless browser, inject freeze CSS,
        capture a full-page screenshot compressed to 720p.
        Returns (success, screenshot_path_or_error).
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return False, "Playwright not installed. Install with: pip install playwright && playwright install chromium"

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(
                    viewport={"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT}
                )

                # Navigate and wait for network idle
                await page.goto(url, wait_until="networkidle", timeout=20000)

                # Inject freeze CSS to disable animations
                await page.add_style_tag(content=FREEZE_CSS)
                await page.wait_for_timeout(500)  # Brief settle after CSS injection

                # Capture screenshot
                screenshot_bytes = await page.screenshot(full_page=True, type="png")
                await browser.close()

            # Compress to 720p using PIL if available
            compressed = self._compress_screenshot(screenshot_bytes)

            # Save to disk
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(compressed)

            logger.info("📸 Screenshot captured: %s (%d KB)", out.name, len(compressed) // 1024)
            return True, str(out)

        except Exception as exc:
            return False, f"Screenshot capture failed: {exc}"

    @staticmethod
    def _compress_screenshot(png_bytes: bytes) -> bytes:
        """Compress screenshot to 720p width for token-efficient vision analysis."""
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(png_bytes))

            # Resize to 720p width, maintaining aspect ratio
            target_width = 1280
            if img.width > target_width:
                ratio = target_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((target_width, new_height), Image.LANCZOS)

            # Save as JPEG with quality 80 for size reduction
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80, optimize=True)
            return buf.getvalue()

        except ImportError:
            # PIL not available, return original PNG
            return png_bytes

    # ────────────────────────────────────────────────
    # Gemini Vision QA
    # ────────────────────────────────────────────────

    async def analyze_screenshot(
        self,
        screenshot_path: str,
        objective: str,
    ) -> Tuple[bool, str]:
        """
        Route the screenshot to Gemini vision for UI quality analysis.
        Returns (passes_qa, critique_or_success_msg).
        """
        import json

        # Try using the Gemini advisor's image analysis if available
        if self._advisor and hasattr(self._advisor, "analyze_image"):
            try:
                result = await self._advisor.analyze_image(
                    screenshot_path,
                    f"UI REQUIREMENT: {objective[:500]}\n\n{self.VISION_PROMPT}"
                )
                return self._parse_vision_result(result)
            except Exception as exc:
                logger.warning("Vision advisor failed: %s. Trying CLI fallback.", exc)

        # Fallback: use Gemini CLI with file attachment
        return await self._gemini_cli_vision(screenshot_path, objective)

    async def _gemini_cli_vision(
        self,
        screenshot_path: str,
        objective: str,
    ) -> Tuple[bool, str]:
        """Use the Gemini CLI to analyze a screenshot."""
        import json

        prompt = (
            f"UI REQUIREMENT: {objective[:500]}\n\n{self.VISION_PROMPT}"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-f", screenshot_path, "-e", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            raw = stdout.decode("utf-8", errors="replace").strip()

            if not raw:
                return False, "Gemini vision returned empty response."

            return self._parse_vision_result(raw)

        except Exception as exc:
            return False, f"Gemini CLI vision failed: {exc}"

    @staticmethod
    def _parse_vision_result(raw: str) -> Tuple[bool, str]:
        """Parse the vision model's JSON response."""
        import json

        try:
            # Extract JSON from potential markdown wrapping
            json_match = re.search(r'\{[^}]+\}', raw)
            if json_match:
                data = json.loads(json_match.group())
                passes = data.get("pass", False)
                critique = data.get("critique", "No explanation provided.")
                return passes, critique
        except (json.JSONDecodeError, AttributeError):
            pass

        # Fallback: keyword detection
        lower = raw.lower()
        if "pass" in lower and "fail" not in lower:
            return True, raw[:200]
        return False, raw[:200]

    # ────────────────────────────────────────────────
    # Full Visual QA Pipeline
    # ────────────────────────────────────────────────

    async def run_visual_qa(
        self,
        node_id: str,
        objective: str,
        cwd: str,
        dev_cmd: str = "npm run dev",
        port: int = 3000,
        url_path: str = "/",
    ) -> Tuple[bool, str]:
        """
        Complete Visual QA pipeline:
        1. Boot dev server
        2. Poll until ready
        3. Capture screenshot
        4. Route to Gemini vision
        5. Return verdict
        """
        screenshot_dir = Path(cwd) / ".ag-memory" / "screenshots"
        screenshot_path = str(screenshot_dir / f"vqa_{node_id}.jpg")

        # Step 1: Boot server
        server_ok, server_result = self.start_dev_server(cwd, dev_cmd, port)
        if not server_ok:
            return False, f"Dev server failed: {server_result}"

        url = f"http://localhost:{port}{url_path}"

        try:
            # Step 2: Wait for server
            ready = await self.wait_for_server(url)
            if not ready:
                return False, "Dev server did not become ready."

            # Step 3: Capture screenshot
            sc_ok, sc_result = await self.capture_screenshot(url, screenshot_path)
            if not sc_ok:
                return False, f"Screenshot failed: {sc_result}"

            # Step 4: Vision QA
            passes, critique = await self.analyze_screenshot(screenshot_path, objective)

            if passes:
                logger.info("👁️ Visual QA PASSED for [%s]", node_id)
                return True, f"Visual QA passed: {critique}"
            else:
                logger.warning("👁️ Visual QA FAILED for [%s]: %s", node_id, critique)
                return False, f"Visual QA failed: {critique}"

        finally:
            # Step 5: Always stop the server
            self.stop_dev_server()
