"""
V74: Automated Lighthouse Integration (Audit §4.6)

Runs Lighthouse audits inside the sandbox container against the dev server,
parses JSON results for Performance/Accessibility/Best-Practices/SEO scores,
and auto-generates targeted fix tasks when metrics fall below targets.

Integration points:
  - main.py: call after dev server confirmed running (post-boot or post-phase)
  - temporal_planner.py: inject generated fix tasks into the live DAG
  - dag_history.jsonl: track scores over time for regression detection

Requirements:
  - Dev server must be running inside the sandbox
  - Chrome/Chromium available in the sandbox (most Node images include it)
  - lighthouse npm package (auto-installed if missing)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("supervisor.lighthouse_runner")


# Target scores — anything below triggers fix task generation
TARGET_SCORES = {
    "performance": 90,
    "accessibility": 95,
    "best-practices": 90,
    "seo": 90,
}


@dataclass
class LighthouseResult:
    """Parsed result from a Lighthouse audit."""
    url: str = ""
    timestamp: float = 0.0
    duration_ms: int = 0
    scores: dict[str, float] = field(default_factory=dict)  # category -> 0-100
    metrics: dict[str, float] = field(default_factory=dict)  # metric_name -> value
    diagnostics: list[str] = field(default_factory=list)     # human-readable issues
    raw_json_path: str = ""
    success: bool = True
    error: str = ""

    @property
    def all_passing(self) -> bool:
        """True if all scores meet their targets."""
        for cat, target in TARGET_SCORES.items():
            if self.scores.get(cat, 0) < target:
                return False
        return True

    @property
    def failing_categories(self) -> list[str]:
        """Categories that are below their target scores."""
        failing = []
        for cat, target in TARGET_SCORES.items():
            score = self.scores.get(cat, 0)
            if score < target:
                failing.append(f"{cat}: {score:.0f}/{target}")
        return failing

    def summary(self) -> str:
        if not self.success:
            return f"❌ Lighthouse failed: {self.error}"
        parts = []
        for cat in ("performance", "accessibility", "best-practices", "seo"):
            score = self.scores.get(cat, 0)
            target = TARGET_SCORES.get(cat, 90)
            icon = "✅" if score >= target else "❌"
            parts.append(f"{icon} {cat}: {score:.0f}")
        return f"🔦 Lighthouse: {' | '.join(parts)} ({self.duration_ms}ms)"

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "timestamp": self.timestamp,
            "scores": self.scores,
            "metrics": self.metrics,
            "all_passing": self.all_passing,
            "failing": self.failing_categories,
            "duration_ms": self.duration_ms,
        }


class LighthouseRunner:
    """
    Runs Lighthouse audits inside the sandbox container and generates fix tasks.

    Usage:
        runner = LighthouseRunner(sandbox)
        result = await runner.run_audit(port=5173)
        if not result.all_passing:
            fix_tasks = runner.generate_fix_tasks(result)
            # Inject fix_tasks into the DAG
    """

    # Core Web Vitals to extract from Lighthouse JSON
    _METRICS_TO_EXTRACT = [
        "first-contentful-paint",
        "largest-contentful-paint",
        "total-blocking-time",
        "cumulative-layout-shift",
        "speed-index",
        "interactive",
    ]

    # Metric-specific thresholds for "good"
    _METRIC_TARGETS = {
        "first-contentful-paint": 1800,    # ms
        "largest-contentful-paint": 2500,   # ms
        "total-blocking-time": 200,        # ms
        "cumulative-layout-shift": 0.1,    # unitless
        "speed-index": 3400,               # ms
    }

    def __init__(self, sandbox, workspace: str = "/workspace"):
        self._sandbox = sandbox
        self._workspace = workspace
        self._lighthouse_installed = False
        self._history: list[dict] = []  # Score history for trend detection

    async def run_audit(
        self,
        port: int = 5173,
        url_path: str = "/",
    ) -> LighthouseResult:
        """
        Run a Lighthouse audit against the dev server.

        Args:
            port: Dev server port inside the container
            url_path: URL path to audit (default: root)

        Returns:
            LighthouseResult with scores, metrics, and diagnostics
        """
        start = time.time()
        result = LighthouseResult(
            url=f"http://localhost:{port}{url_path}",
            timestamp=time.time(),
        )

        # Ensure Lighthouse is installed
        if not await self._ensure_lighthouse():
            result.success = False
            result.error = "Could not install Lighthouse in sandbox"
            return result

        # Verify dev server is running
        if not await self._check_server(port):
            result.success = False
            result.error = f"Dev server not responding on port {port}"
            return result

        try:
            # Run Lighthouse with JSON output
            cmd = (
                f"cd {self._workspace} && "
                f"npx lighthouse http://localhost:{port}{url_path} "
                f"--output json "
                f"--output-path /tmp/lighthouse-report.json "
                f"--chrome-flags='--headless --no-sandbox --disable-gpu' "
                f"--only-categories=performance,accessibility,best-practices,seo "
                f"--quiet "
                f"2>&1 | tail -5"
            )

            audit_result = await self._sandbox.exec_command(cmd, timeout=120)

            if audit_result.exit_code != 0:
                # Check if it's a Chrome issue
                stderr = audit_result.stderr or audit_result.stdout or ""
                if "chrome" in stderr.lower() or "chromium" in stderr.lower():
                    result.success = False
                    result.error = "Chrome/Chromium not available in sandbox"
                else:
                    result.success = False
                    result.error = f"Lighthouse failed (exit {audit_result.exit_code}): {stderr[:200]}"
                return result

            # Read and parse the JSON report
            read_result = await self._sandbox.exec_command(
                "cat /tmp/lighthouse-report.json 2>/dev/null | head -c 50000",
                timeout=10,
            )

            if not read_result.stdout or read_result.exit_code != 0:
                result.success = False
                result.error = "Could not read Lighthouse report"
                return result

            self._parse_report(read_result.stdout, result)

        except Exception as exc:
            result.success = False
            result.error = str(exc)
            logger.warning("🔦  [Lighthouse] Audit error: %s", exc)

        result.duration_ms = int((time.time() - start) * 1000)

        # Log result
        if result.success:
            logger.info("🔦  %s", result.summary())
            # Track history
            self._history.append(result.to_dict())
        else:
            logger.warning("🔦  %s", result.summary())

        return result

    def generate_fix_tasks(self, result: LighthouseResult) -> list[dict]:
        """
        Generate targeted DAG fix tasks based on Lighthouse audit results.

        Returns a list of task dicts compatible with TemporalPlanner.inject_task().
        """
        if result.all_passing or not result.success:
            return []

        tasks = []
        task_num = 900  # High IDs to avoid collision with existing DAG

        # Performance fixes
        perf_score = result.scores.get("performance", 100)
        if perf_score < TARGET_SCORES["performance"]:
            desc_parts = ["[PERF] Fix Lighthouse Performance issues:"]

            # Check specific metrics
            metrics = result.metrics
            if metrics.get("first-contentful-paint", 0) > 1800:
                desc_parts.append(
                    "- FCP too slow: inline critical CSS, defer non-critical JS, "
                    "add font-display: swap, preconnect to external origins"
                )
            if metrics.get("largest-contentful-paint", 0) > 2500:
                desc_parts.append(
                    "- LCP too slow: preload LCP image, add explicit width/height "
                    "to images, avoid lazy-loading above-fold images"
                )
            if metrics.get("total-blocking-time", 0) > 200:
                desc_parts.append(
                    "- TBT too high: code-split JS bundles, defer heavy computation, "
                    "break long tasks (>50ms) into smaller chunks"
                )
            if metrics.get("cumulative-layout-shift", 0) > 0.1:
                desc_parts.append(
                    "- CLS too high: set width/height on all images/videos, "
                    "use aspect-ratio CSS, avoid injecting content above existing content"
                )

            # Add relevant diagnostics
            perf_diagnostics = [d for d in result.diagnostics if any(
                k in d.lower() for k in ("render-blocking", "image", "cache", "unused", "font")
            )]
            for diag in perf_diagnostics[:3]:
                desc_parts.append(f"- {diag}")

            tasks.append({
                "task_id": f"t{task_num}-PERF",
                "description": "\n".join(desc_parts),
                "dependencies": [],
            })
            task_num += 1

        # Accessibility fixes
        a11y_score = result.scores.get("accessibility", 100)
        if a11y_score < TARGET_SCORES["accessibility"]:
            a11y_diagnostics = [d for d in result.diagnostics if any(
                k in d.lower() for k in ("aria", "contrast", "alt", "label", "heading", "tab")
            )]
            desc = (
                f"[PERF] Fix Lighthouse Accessibility issues (score: {a11y_score:.0f}):\n"
                + "\n".join(f"- {d}" for d in a11y_diagnostics[:5])
            )
            if not a11y_diagnostics:
                desc += (
                    "- Check all buttons/links have accessible names\n"
                    "- Verify heading hierarchy (h1>h2>h3)\n"
                    "- Ensure color contrast >= 4.5:1\n"
                    "- Add aria-labels where needed"
                )
            tasks.append({
                "task_id": f"t{task_num}-PERF",
                "description": desc,
                "dependencies": [],
            })
            task_num += 1

        # Best Practices fixes
        bp_score = result.scores.get("best-practices", 100)
        if bp_score < TARGET_SCORES["best-practices"]:
            bp_diagnostics = [d for d in result.diagnostics if any(
                k in d.lower() for k in ("console", "deprecated", "https", "csp", "source-map")
            )]
            desc = (
                f"[PERF] Fix Lighthouse Best Practices issues (score: {bp_score:.0f}):\n"
                + "\n".join(f"- {d}" for d in bp_diagnostics[:5])
            )
            if not bp_diagnostics:
                desc += (
                    "- Remove console.log statements from production code\n"
                    "- Set CSP headers (script-src, style-src)\n"
                    "- Serve source maps for production JS bundles"
                )
            tasks.append({
                "task_id": f"t{task_num}-PERF",
                "description": desc,
                "dependencies": [],
            })
            task_num += 1

        # SEO fixes
        seo_score = result.scores.get("seo", 100)
        if seo_score < TARGET_SCORES["seo"]:
            seo_diagnostics = [d for d in result.diagnostics if any(
                k in d.lower() for k in ("meta", "title", "robots", "canonical", "crawl", "structured")
            )]
            desc = (
                f"[PERF] Fix Lighthouse SEO issues (score: {seo_score:.0f}):\n"
                + "\n".join(f"- {d}" for d in seo_diagnostics[:5])
            )
            if not seo_diagnostics:
                desc += (
                    "- Add/fix <title> tag (unique, under 60 chars)\n"
                    "- Add <meta name='description'> (120-155 chars)\n"
                    "- Verify robots.txt is valid plain text\n"
                    "- Add canonical URL"
                )
            tasks.append({
                "task_id": f"t{task_num}-PERF",
                "description": desc,
                "dependencies": [],
            })

        if tasks:
            logger.info(
                "🔦  [Lighthouse] Generated %d fix tasks for failing categories: %s",
                len(tasks),
                ", ".join(result.failing_categories),
            )

        return tasks

    def get_score_trend(self) -> dict[str, list[float]]:
        """
        Get score trends over time for regression detection.

        Returns dict of category -> list of scores (oldest first).
        """
        trends: dict[str, list[float]] = {}
        for entry in self._history[-20:]:  # Last 20 audits
            for cat, score in entry.get("scores", {}).items():
                if cat not in trends:
                    trends[cat] = []
                trends[cat].append(score)
        return trends

    def detect_regressions(self) -> list[str]:
        """
        Detect score regressions compared to previous audit.

        Returns list of warning strings for categories that dropped.
        """
        if len(self._history) < 2:
            return []

        prev = self._history[-2].get("scores", {})
        curr = self._history[-1].get("scores", {})
        regressions = []

        for cat in TARGET_SCORES:
            prev_score = prev.get(cat, 0)
            curr_score = curr.get(cat, 0)
            if curr_score < prev_score - 5:  # 5-point tolerance
                regressions.append(
                    f"{cat}: {prev_score:.0f} → {curr_score:.0f} (dropped {prev_score - curr_score:.0f} points)"
                )

        return regressions

    async def save_to_history(self, project_path: str | Path) -> None:
        """Append latest result to dag_history.jsonl for cross-session tracking."""
        if not self._history:
            return

        try:
            hist_path = Path(project_path) / ".ag-supervisor" / "lighthouse_history.jsonl"
            hist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(hist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._history[-1]) + "\n")
            logger.debug("🔦  [Lighthouse] Saved audit result to history")
        except Exception as exc:
            logger.debug("🔦  [Lighthouse] History save error: %s", exc)

    def _parse_report(self, json_str: str, result: LighthouseResult) -> None:
        """Parse Lighthouse JSON report into LighthouseResult."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            result.success = False
            result.error = "Invalid JSON in Lighthouse report"
            return

        # Extract category scores (0-1 scale → 0-100)
        categories = data.get("categories", {})
        for cat_key in ("performance", "accessibility", "best-practices", "seo"):
            cat_data = categories.get(cat_key, {})
            score = cat_data.get("score")
            if score is not None:
                result.scores[cat_key] = round(score * 100, 1)

        # Extract Core Web Vitals
        audits = data.get("audits", {})
        for metric_key in self._METRICS_TO_EXTRACT:
            metric_data = audits.get(metric_key, {})
            value = metric_data.get("numericValue")
            if value is not None:
                result.metrics[metric_key] = round(value, 2)

        # Extract diagnostics (failed audits with recommendations)
        for audit_id, audit_data in audits.items():
            score = audit_data.get("score")
            if score is not None and score < 1:
                title = audit_data.get("title", audit_id)
                display = audit_data.get("displayValue", "")
                if display:
                    result.diagnostics.append(f"{title}: {display}")
                elif title != audit_id:
                    result.diagnostics.append(title)

        # Cap diagnostics to keep output manageable
        result.diagnostics = result.diagnostics[:25]

    async def _ensure_lighthouse(self) -> bool:
        """Install Lighthouse in the sandbox if not already present."""
        if self._lighthouse_installed:
            return True

        try:
            # Check if already installed
            check = await self._sandbox.exec_command(
                "npx lighthouse --version 2>/dev/null | head -1",
                timeout=15,
            )
            if check.exit_code == 0 and check.stdout and check.stdout.strip():
                self._lighthouse_installed = True
                logger.debug("🔦  [Lighthouse] Already installed: v%s", check.stdout.strip())
                return True

            # Install globally
            logger.info("🔦  [Lighthouse] Installing lighthouse in sandbox...")
            install = await self._sandbox.exec_command(
                "npm install -g lighthouse 2>&1 | tail -3",
                timeout=60,
            )
            if install.exit_code == 0:
                self._lighthouse_installed = True
                logger.info("🔦  [Lighthouse] Installed successfully")
                return True
            else:
                logger.warning(
                    "🔦  [Lighthouse] Install failed: %s",
                    (install.stderr or install.stdout or "")[:200],
                )
                return False
        except Exception as exc:
            logger.warning("🔦  [Lighthouse] Install error: %s", exc)
            return False

    async def _check_server(self, port: int) -> bool:
        """Verify the dev server is responding."""
        try:
            check = await self._sandbox.exec_command(
                f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/ 2>/dev/null",
                timeout=10,
            )
            status = (check.stdout or "").strip()
            return status in ("200", "301", "302", "304")
        except Exception:
            return False
