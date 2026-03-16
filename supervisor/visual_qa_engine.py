"""
visual_qa_engine.py — V24 Multimodal Visual QA Engine (Headless Sandbox).

Gives the agent eyes. After code passes V22 TDD, frontend tasks undergo
a Visual Phase: the engine boots a dev server inside the sandbox, captures
a screenshot via headless Chromium, and routes it to Gemini's vision model
for pixel-level QA.

Pipeline:
  1. Detect if the node is a UI/frontend node
  2. Install Chromium snapshot tools inside sandbox (puppeteer/chrome-headless-shell)
  3. Boot dev server via sandbox exec
  4. Capture full-page screenshot via headless Chrome
  5. Compress screenshot to 720p
  6. Route to Gemini vision model for analysis
  7. Parse JSON verdict (PASS|FAIL with critique)
  8. Tear down dev server
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("supervisor.visual_qa_engine")

# UI keywords that trigger the Visual Phase
UI_KEYWORDS = [
    "component", "frontend", "view", "page", "layout", "ui",
    "button", "form", "modal", "dialog", "sidebar", "navbar",
    "header", "footer", "card", "dashboard", "chart", "table",
    "responsive", "css", "style", "animation", "render", "html",
    "react", "vue", "svelte", "next", "vite", "tailwind",
]

# CSS injected to freeze animations for consistent screenshots
FREEZE_CSS = """
* {
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
    Renders frontend code in a headless browser inside the Docker sandbox
    and validates it visually via the Gemini vision model.
    """

    VISION_PROMPT = (
        "You are a strict UI Quality Assurance inspector. Analyze this screenshot "
        "of a web application and check for:\n"
        "1. Visual bugs (overlapping elements, text clipping, invisible text)\n"
        "2. Broken layouts (elements outside viewport, misaligned grids)\n"
        "3. Missing assets (broken images, missing icons)\n"
        "4. Accessibility issues (low contrast, tiny text)\n"
        "5. Blank or empty pages\n"
        "\n"
        "Reply with ONLY a JSON object:\n"
        '{"verdict": "PASS" or "FAIL", "issues": ["issue1", ...], '
        '"score": 0-100, "summary": "brief overall assessment"}'
    )

    def __init__(self, sandbox_manager=None, gemini_advisor=None):
        """
        Args:
            sandbox_manager: SandboxManager for running commands inside container.
            gemini_advisor: Object with analyze_image(path, prompt) method,
                           or None to use the Gemini CLI fallback.
        """
        self._sandbox = sandbox_manager
        self._gemini = gemini_advisor
        self._screenshot_dir = Path(os.getenv(
            "VISUAL_QA_DIR",
            str(Path(__file__).parent / "_visual_qa_screenshots")
        ))
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_ui_node(description: str) -> bool:
        """Check if a node description contains frontend/UI keywords."""
        desc_lower = description.lower()
        return any(kw in desc_lower for kw in UI_KEYWORDS)

    async def capture_screenshot(
        self,
        url: str,
        output_path: str,
    ) -> Tuple[bool, str]:
        """
        Capture a screenshot using headless Chrome inside the sandbox.
        Returns (success, screenshot_path_or_error).
        """
        if not self._sandbox:
            return False, "No sandbox manager configured"

        # Use a Node.js one-liner with puppeteer-core or chrome-headless-shell
        # First check if chrome-headless-shell or chromium is available
        screenshot_script = f"""
const {{ execSync }} = require('child_process');
let browser_path = '';
try {{
    browser_path = execSync('which chromium || which google-chrome || which chrome-headless-shell', {{encoding: 'utf8'}}).trim();
}} catch(e) {{
    console.error('NO_BROWSER');
    process.exit(1);
}}

// Use puppeteer-core if available, otherwise use basic CDP
(async () => {{
    try {{
        const puppeteer = require('puppeteer-core');
        const browser = await puppeteer.launch({{
            executablePath: browser_path,
            headless: 'new',
            args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
        }});
        const page = await browser.newPage();
        await page.setViewport({{ width: {SCREENSHOT_WIDTH}, height: {SCREENSHOT_HEIGHT} }});
        await page.goto('{url}', {{ waitUntil: 'networkidle2', timeout: 20000 }});
        
        // Inject freeze CSS
        await page.addStyleTag({{ content: `{FREEZE_CSS}` }});
        await new Promise(r => setTimeout(r, 500));
        
        await page.screenshot({{ path: '/tmp/screenshot.png', fullPage: true }});
        await browser.close();
        console.log('SCREENSHOT_OK');
    }} catch(e) {{
        console.error('SCREENSHOT_FAIL: ' + e.message);
        process.exit(1);
    }}
}})();
"""
        try:
            # Write the screenshot script to the sandbox
            await self._sandbox.exec_command(
                f"cat > /tmp/capture.js << 'SCRIPT_EOF'\n{screenshot_script}\nSCRIPT_EOF",
                timeout=10,
            )

            # Install puppeteer-core if not present
            await self._sandbox.exec_command(
                "npm list puppeteer-core 2>/dev/null || npm install --no-save puppeteer-core 2>/dev/null",
                timeout=30,
            )

            # Run the capture script
            result = await self._sandbox.exec_command(
                "node /tmp/capture.js",
                timeout=30,
            )

            if result.exit_code != 0:
                return False, f"Screenshot capture failed: {result.stderr[:300]}"

            # Copy screenshot out from sandbox
            await self._sandbox.copy_file_out(
                "/tmp/screenshot.png", output_path
            )

            logger.info("📸  Screenshot captured: %s", output_path)
            return True, output_path

        except Exception as exc:
            return False, f"Screenshot error: {exc}"

    def _compress_screenshot(self, png_bytes: bytes) -> bytes:
        """Compress screenshot to 720p width for token-efficient vision analysis."""
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes))
            w, h = img.size
            if w > SCREENSHOT_WIDTH:
                ratio = SCREENSHOT_WIDTH / w
                new_size = (SCREENSHOT_WIDTH, int(h * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            logger.info(
                "📸  Compressed screenshot: %dx%d → %dx%d (%d bytes)",
                w, h, img.width, img.height, out.tell(),
            )
            return out.getvalue()

        except ImportError:
            logger.warning("📸  Pillow not installed — skipping compression.")
            return png_bytes

    async def analyze_screenshot(
        self,
        screenshot_path: str,
        objective: str,
    ) -> Tuple[bool, str]:
        """
        Route the screenshot to Gemini vision for UI quality analysis.
        Returns (passes_qa, critique_or_success_msg).
        """
        prompt = (
            f"{self.VISION_PROMPT}\n\n"
            f"The UI objective was: {objective}\n"
        )

        if self._gemini and hasattr(self._gemini, 'analyze_image'):
            try:
                result = await self._gemini.analyze_image(screenshot_path, prompt)
                return self._parse_vision_result(result)
            except Exception as exc:
                logger.warning("📸  Gemini advisor vision failed: %s", exc)

        # Fallback: use Gemini CLI
        return await self._gemini_cli_vision(screenshot_path, objective)

    async def _gemini_cli_vision(
        self,
        screenshot_path: str,
        objective: str,
    ) -> Tuple[bool, str]:
        """Use the Gemini CLI to analyze a screenshot."""
        try:
            from .gemini_advisor import call_gemini_with_file

            prompt = (
                f"{self.VISION_PROMPT}\n\n"
                f"The UI objective was: {objective}\n"
            )

            result = await call_gemini_with_file(
                screenshot_path, prompt, timeout=60
            )
            if result:
                return self._parse_vision_result(result)
            return False, "Gemini CLI returned empty result"

        except Exception as exc:
            return False, f"Gemini CLI vision error: {exc}"

    def _parse_vision_result(self, raw: str) -> Tuple[bool, str]:
        """Parse the vision model's JSON response."""
        import json
        try:
            # Extract JSON from response
            json_match = re.search(r'\{[^{}]*"verdict"[^{}]*\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                verdict = data.get("verdict", "FAIL").upper()
                summary = data.get("summary", "")
                issues = data.get("issues", [])
                score = data.get("score", 0)

                if verdict == "PASS" and score >= 60:
                    return True, f"PASS (score: {score}): {summary}"
                else:
                    critique = f"FAIL (score: {score}): {summary}"
                    if issues:
                        critique += "\nIssues: " + "; ".join(issues[:5])
                    return False, critique
        except Exception as exc:
            logger.warning("📸  Could not parse vision result: %s", exc)

        return False, f"Unparseable vision result: {raw[:200]}"

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
        1. Boot dev server in sandbox
        2. Poll until ready
        3. Capture screenshot via sandbox headless Chrome
        4. Route to Gemini vision
        5. Return verdict
        """
        if not self._sandbox:
            return False, "No sandbox manager — cannot run visual QA"

        if not self.is_ui_node(objective):
            return True, "Not a UI node — visual QA skipped"

        logger.info("📸  Starting Visual QA for node '%s': %s", node_id, objective[:60])
        screenshot_path = str(self._screenshot_dir / f"{node_id}.png")

        # 1. Start dev server in sandbox
        try:
            server_result = await self._sandbox.exec_command(
                f"cd {cwd} && nohup {dev_cmd} &",
                timeout=10,
            )
        except Exception as exc:
            return False, f"Failed to start dev server: {exc}"

        # 2. Wait for server
        url = f"http://localhost:{port}{url_path}"
        server_ready = False
        for _ in range(MAX_SERVER_WAIT_S):
            try:
                check = await self._sandbox.exec_command(
                    f"curl -s -o /dev/null -w '%{{http_code}}' {url}",
                    timeout=5,
                )
                if check.stdout.strip() == "200":
                    server_ready = True
                    break
            except Exception:
                pass
            await __import__('asyncio').sleep(POLL_INTERVAL_S)

        if not server_ready:
            return False, f"Dev server did not respond at {url} within {MAX_SERVER_WAIT_S}s"

        # 3. Capture screenshot
        ok, result = await self.capture_screenshot(url, screenshot_path)
        if not ok:
            return False, f"Screenshot capture failed: {result}"

        # 4. Analyze with Gemini vision
        passes, critique = await self.analyze_screenshot(screenshot_path, objective)

        # 5. Cleanup dev server
        try:
            await self._sandbox.exec_command(f"pkill -f '{dev_cmd}' || true", timeout=5)
        except Exception:
            pass

        logger.info("📸  Visual QA result: %s — %s", "PASS" if passes else "FAIL", critique[:80])
        return passes, critique


# ─────────────────────────────────────────────────────────────
# V74: Visual Regression Detection (Audit §4.4)
# ─────────────────────────────────────────────────────────────

from dataclasses import dataclass, field


@dataclass
class RegressionResult:
    """Result of a visual regression check."""
    has_regression: bool = False
    baseline_path: str = ""
    current_path: str = ""
    diff_details: list[str] = field(default_factory=list)
    score_before: int = 0
    score_after: int = 0
    duration_ms: int = 0

    def summary(self) -> str:
        if not self.has_regression:
            return f"✅ No visual regression (score: {self.score_after})"
        return (
            f"❌ Visual regression detected (score: {self.score_before} → {self.score_after}). "
            f"Issues: {'; '.join(self.diff_details[:3])}"
        )


class VisualRegressionDetector:
    """
    V74: Compares before/after screenshots to detect visual regressions.

    Pipeline:
      1. save_baseline() — capture and store a "known good" screenshot
      2. detect_regressions() — capture current state, compare with baseline
      3. generate_fix_tasks() — create DAG tasks for detected issues

    Usage:
        detector = VisualRegressionDetector(engine, project_path)
        await detector.save_baseline("homepage", port=5173)
        # ... tasks modify UI ...
        result = await detector.detect_regressions("homepage", port=5173)
        if result.has_regression:
            fix_tasks = detector.generate_fix_tasks(result, "homepage")
    """

    def __init__(self, engine: VisualQAEngine, project_path: str = ""):
        self._engine = engine
        self._baselines_dir = Path(project_path) / ".ag-supervisor" / "visual_baselines" if project_path else None
        if self._baselines_dir:
            self._baselines_dir.mkdir(parents=True, exist_ok=True)

    async def save_baseline(
        self,
        page_name: str,
        port: int = 5173,
        url_path: str = "/",
    ) -> bool:
        """
        Capture and store a baseline screenshot for a named page.

        Call this when the UI is in a known-good state (e.g., after
        a phase completes and tests pass).
        """
        if not self._baselines_dir:
            return False

        baseline_path = str(self._baselines_dir / f"{page_name}_baseline.png")
        url = f"http://localhost:{port}{url_path}"

        ok, result = await self._engine.capture_screenshot(url, baseline_path)
        if ok:
            # Also analyze and store the baseline score
            passes, critique = await self._engine.analyze_screenshot(
                baseline_path, f"Baseline capture of {page_name}",
            )
            score = self._extract_score(critique)
            meta_path = self._baselines_dir / f"{page_name}_meta.json"
            import json as _j
            _j.loads  # Ensure json is available
            meta_path.write_text(_j.dumps({
                "page_name": page_name,
                "score": score,
                "url": url,
                "timestamp": time.time(),
                "passes_qa": passes,
            }, indent=2), encoding="utf-8")

            logger.info("📸  Baseline saved for '%s' (score: %d)", page_name, score)
            return True
        else:
            logger.warning("📸  Failed to capture baseline for '%s': %s", page_name, result)
            return False

    async def detect_regressions(
        self,
        page_name: str,
        port: int = 5173,
        url_path: str = "/",
        objective: str = "",
    ) -> RegressionResult:
        """
        Compare current page state against stored baseline.

        Captures a new screenshot, sends BOTH images to Gemini vision
        for comparison, and reports any regressions.
        """
        start = time.time()
        result = RegressionResult()

        if not self._baselines_dir:
            return result

        baseline_path = self._baselines_dir / f"{page_name}_baseline.png"
        meta_path = self._baselines_dir / f"{page_name}_meta.json"

        if not baseline_path.exists():
            logger.info("📸  No baseline for '%s' — skipping regression check", page_name)
            return result

        result.baseline_path = str(baseline_path)

        # Load baseline metadata
        baseline_score = 0
        if meta_path.exists():
            try:
                import json as _j
                meta = _j.loads(meta_path.read_text(encoding="utf-8"))
                baseline_score = meta.get("score", 0)
            except Exception:
                pass
        result.score_before = baseline_score

        # Capture current screenshot
        current_path = str(self._baselines_dir / f"{page_name}_current.png")
        url = f"http://localhost:{port}{url_path}"

        ok, capture_result = await self._engine.capture_screenshot(url, current_path)
        if not ok:
            logger.warning("📸  Failed to capture current screenshot: %s", capture_result)
            return result

        result.current_path = current_path

        # Analyze current screenshot
        obj = objective or f"Visual regression check for {page_name}"
        passes, critique = await self._engine.analyze_screenshot(current_path, obj)
        current_score = self._extract_score(critique)
        result.score_after = current_score

        # Detect regression: score drop > 10 or new FAIL
        if not passes or (baseline_score > 0 and current_score < baseline_score - 10):
            result.has_regression = True
            # Extract issues from critique
            if "Issues:" in critique:
                issues_part = critique.split("Issues:")[1].strip()
                result.diff_details = [i.strip() for i in issues_part.split(";") if i.strip()]
            elif critique:
                result.diff_details = [critique[:200]]

        result.duration_ms = int((time.time() - start) * 1000)

        if result.has_regression:
            logger.warning("📸  %s", result.summary())
        else:
            logger.info("📸  %s", result.summary())

        return result

    def generate_fix_tasks(
        self,
        result: RegressionResult,
        page_name: str,
    ) -> list[dict]:
        """
        Generate DAG fix tasks for detected visual regressions.

        Returns a list of task dicts compatible with TemporalPlanner.inject_task().
        """
        if not result.has_regression:
            return []

        issues_text = "\n".join(f"- {d}" for d in result.diff_details[:5])
        desc = (
            f"[UIUX] Fix visual regression on {page_name} page.\n"
            f"Visual QA score dropped from {result.score_before} to {result.score_after}.\n"
            f"Issues detected:\n{issues_text}\n\n"
            f"REQUIREMENTS:\n"
            f"- Restore visual quality to at least the baseline score ({result.score_before})\n"
            f"- Fix all listed visual issues\n"
            f"- Do NOT break any existing functionality\n"
            f"- Test responsiveness at 1280x720"
        )

        task = {
            "task_id": f"t950-UIUX",
            "description": desc,
            "dependencies": [],
        }

        logger.info(
            "📸  Generated fix task for '%s' regression (score: %d → %d)",
            page_name, result.score_before, result.score_after,
        )
        return [task]

    @staticmethod
    def _extract_score(critique: str) -> int:
        """Extract numeric score from critique string."""
        import re as _re
        match = _re.search(r'score:\s*(\d+)', critique)
        return int(match.group(1)) if match else 0

