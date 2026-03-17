"""
V74: Task Result Learning / Feedback Loop (Audit §4.1)

Tracks task success/failure patterns across sessions and feeds insights
back into task generation prompts. The system gets smarter over time by:

1. Recording outcomes by task category (FUNC/UIUX/PERF), file type, and complexity
2. Detecting patterns: which task types consistently fail? Which files break most?
3. Generating context for DECOMPOSITION_PROMPT: "CSS tasks have 40% failure rate"
4. Auto-suggesting granularity adjustments for problematic categories

Persistence: .ag-supervisor/task_intelligence.json

Integration points:
  - main.py pool_worker: call record_result() after each task completion
  - temporal_planner.py: call get_insights() to inject into decomposition prompts
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

logger = logging.getLogger("supervisor.task_intelligence")


@dataclass
class CategoryStats:
    """Aggregated stats for a task category."""
    total: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    avg_duration_s: float = 0.0
    total_duration_s: float = 0.0
    common_errors: dict = field(default_factory=dict)  # error_pattern -> count

    @property
    def success_rate(self) -> float:
        return (self.successes / max(1, self.total)) * 100

    @property
    def failure_rate(self) -> float:
        return (self.failures / max(1, self.total)) * 100

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "successes": self.successes,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "avg_duration_s": round(self.avg_duration_s, 1),
            "success_rate_pct": round(self.success_rate, 1),
            "common_errors": dict(sorted(
                self.common_errors.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:10]),  # Top 10 errors
        }


class TaskIntelligence:
    """
    Learning system that tracks task outcomes and generates actionable insights.

    Usage:
        intel = TaskIntelligence(project_path)

        # After each task completion
        intel.record_result(
            task_id="t5-FUNC",
            category="FUNC",
            files_changed=["src/App.tsx", "src/utils.ts"],
            success=True,
            errors=[],
            duration_s=45.2,
        )

        # Before decomposition — inject insights into prompt
        insights = intel.get_insights()
        # Returns: "TASK INTELLIGENCE: FUNC tasks have 85% success rate. CSS files
        #           have 40% failure rate — add explicit validation steps..."

        # Suggest granularity for a category
        granularity = intel.suggest_granularity("UIUX")
        # Returns: "fine" if failure rate > 30%, "normal" otherwise
    """

    # Error pattern normalization — map raw errors to categories
    _ERROR_PATTERNS = [
        ("TS2304", "missing_type_or_name"),
        ("TS2345", "type_mismatch"),
        ("TS2307", "module_not_found"),
        ("Cannot find module", "module_not_found"),
        ("SyntaxError", "syntax_error"),
        ("ReferenceError", "reference_error"),
        ("TypeError", "type_error"),
        ("ENOENT", "file_not_found"),
        ("npm ERR!", "npm_error"),
        ("ESLint", "lint_error"),
        ("timed out", "timeout"),
        ("SIGTERM", "timeout"),
    ]

    def __init__(self, project_path: str | Path):
        self._project_path = Path(project_path)
        self._state_dir = self._project_path / ".ag-supervisor"
        self._data_path = self._state_dir / "task_intelligence.json"

        # In-memory stats
        self._category_stats: dict[str, CategoryStats] = {}
        self._file_stats: dict[str, dict] = {}  # ext -> {total, failures}
        self._session_start = time.time()
        self._total_recorded = 0

        # Load persisted data
        self._load()

    def record_result(
        self,
        task_id: str,
        category: str,
        files_changed: list[str],
        success: bool,
        errors: list[str] | None = None,
        duration_s: float = 0.0,
    ) -> None:
        """
        Record a task completion result.

        Args:
            task_id: DAG node ID (e.g., "t5-FUNC")
            category: Task category — FUNC, UIUX, or PERF
            files_changed: List of files modified by the task
            success: Whether the task succeeded
            errors: Error messages (if failed)
            duration_s: Execution duration in seconds
        """
        # Normalize category
        cat = self._normalize_category(category, task_id)
        errors = errors or []

        # Update category stats
        if cat not in self._category_stats:
            self._category_stats[cat] = CategoryStats()
        stats = self._category_stats[cat]

        stats.total += 1
        stats.total_duration_s += duration_s
        stats.avg_duration_s = stats.total_duration_s / stats.total

        if success:
            stats.successes += 1
        else:
            stats.failures += 1
            # Classify errors
            for err in errors[:5]:
                pattern = self._classify_error(err)
                stats.common_errors[pattern] = stats.common_errors.get(pattern, 0) + 1

        # Check for timeout
        if any("timeout" in str(e).lower() or "SIGTERM" in str(e) for e in errors):
            stats.timeouts += 1

        # Update file extension stats
        for f in files_changed:
            ext = PurePosixPath(f).suffix.lower()
            if not ext:
                continue
            if ext not in self._file_stats:
                self._file_stats[ext] = {"total": 0, "failures": 0}
            self._file_stats[ext]["total"] += 1
            if not success:
                self._file_stats[ext]["failures"] += 1

        self._total_recorded += 1

        # Persist every 5 recordings
        if self._total_recorded % 5 == 0:
            self._persist()

        logger.debug(
            "📈  [Intelligence] Recorded %s %s: %s (%s files, %.1fs)",
            task_id, cat, "✅" if success else "❌",
            len(files_changed), duration_s,
        )

    def get_insights(self, max_chars: int = 2000) -> str:
        """
        Generate actionable insights for injection into decomposition prompts.

        Returns a compact string summarizing:
          - Overall success rates by category
          - Problematic file types
          - Common error patterns
          - Granularity recommendations
        """
        if self._total_recorded < 3:
            return ""  # Not enough data for meaningful insights

        lines = ["TASK INTELLIGENCE (learned from previous executions):\n"]

        # Category insights
        for cat, stats in sorted(self._category_stats.items()):
            if stats.total < 2:
                continue

            line = f"  {cat}: {stats.success_rate:.0f}% success ({stats.total} tasks, avg {stats.avg_duration_s:.0f}s)"

            # Flag problematic categories
            if stats.failure_rate > 30:
                line += f" ⚠️ HIGH FAILURE RATE — break into smaller sub-tasks"
            elif stats.failure_rate > 15:
                line += f" — consider adding validation steps"

            # Top error pattern
            if stats.common_errors:
                top_err = max(stats.common_errors, key=stats.common_errors.get)
                top_count = stats.common_errors[top_err]
                if top_count >= 2:
                    line += f" (common error: {top_err}, {top_count}x)"

            lines.append(line)

        # File type insights
        problem_exts = []
        for ext, fstats in sorted(self._file_stats.items()):
            if fstats["total"] < 3:
                continue
            fail_rate = (fstats["failures"] / fstats["total"]) * 100
            if fail_rate > 25:
                problem_exts.append(f"{ext} ({fail_rate:.0f}% fail rate)")

        if problem_exts:
            lines.append(f"\n  Problematic file types: {', '.join(problem_exts)}")
            lines.append(f"  → Include explicit validation/testing steps for these file types")

        # Timeout warning
        total_timeouts = sum(s.timeouts for s in self._category_stats.values())
        if total_timeouts > 2:
            lines.append(f"\n  ⚠️ {total_timeouts} tasks timed out — keep task scope narrow")

        result = "\n".join(lines)
        return result[:max_chars]

    def suggest_granularity(self, category: str) -> str:
        """
        Suggest task granularity based on historical failure rates.

        Returns:
          "fine" — break tasks into very small, atomic steps (>30% failure rate)
          "normal" — standard granularity (15-30% failure rate)
          "coarse" — can use larger tasks (<15% failure rate, high success)
        """
        cat = category.upper()
        if cat not in self._category_stats:
            return "normal"

        stats = self._category_stats[cat]
        if stats.total < 5:
            return "normal"  # Not enough data

        if stats.failure_rate > 30:
            return "fine"
        elif stats.failure_rate < 15 and stats.total > 10:
            return "coarse"
        return "normal"

    def get_file_risk_score(self, filepath: str) -> float:
        """
        Get a 0-1 risk score for a file based on its extension's failure history.

        Returns 0.5 (neutral) if insufficient data.
        """
        ext = PurePosixPath(filepath).suffix.lower()
        if ext not in self._file_stats:
            return 0.5

        fstats = self._file_stats[ext]
        if fstats["total"] < 3:
            return 0.5

        return fstats["failures"] / fstats["total"]

    def get_summary(self) -> dict:
        """Get a structured summary of all intelligence data."""
        return {
            "total_tasks_recorded": self._total_recorded,
            "session_duration_min": round((time.time() - self._session_start) / 60, 1),
            "categories": {
                cat: stats.to_dict()
                for cat, stats in sorted(self._category_stats.items())
            },
            "file_type_stats": dict(sorted(
                self._file_stats.items(),
                key=lambda x: x[1].get("total", 0),
                reverse=True,
            )),
        }

    def _normalize_category(self, category: str, task_id: str) -> str:
        """Normalize category from task description or ID."""
        cat = category.upper().strip()
        if cat in ("FUNC", "UIUX", "PERF"):
            return cat

        # Try to extract from task_id suffix
        if "-FUNC" in task_id.upper():
            return "FUNC"
        if "-UIUX" in task_id.upper():
            return "UIUX"
        if "-PERF" in task_id.upper():
            return "PERF"

        # Fallback: classify by keywords in category
        cat_lower = category.lower()
        if any(k in cat_lower for k in ("styl", "ui", "design", "css", "layout", "animation")):
            return "UIUX"
        if any(k in cat_lower for k in ("perf", "lighthouse", "speed", "optim", "cache")):
            return "PERF"
        return "FUNC"

    def _classify_error(self, error: str) -> str:
        """Classify an error string into a pattern category."""
        for pattern, label in self._ERROR_PATTERNS:
            if pattern in error:
                return label
        return "other"

    def _persist(self) -> None:
        """Save intelligence data to disk."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "total_recorded": self._total_recorded,
                "last_updated": time.time(),
                "categories": {
                    cat: stats.to_dict()
                    for cat, stats in self._category_stats.items()
                },
                "file_stats": self._file_stats,
            }
            self._data_path.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
            logger.debug("📈  [Intelligence] Persisted %d task records", self._total_recorded)
        except Exception as exc:
            logger.debug("📈  [Intelligence] Persist error: %s", exc)

    def save(self) -> None:
        """Public API to persist intelligence data to disk.

        Called by main.py at the end of DAG execution to ensure
        all recorded results are flushed to task_intelligence.json.
        """
        self._persist()

    def _load(self) -> None:
        """Load persisted intelligence data from disk."""
        if not self._data_path.exists():
            return

        try:
            data = json.loads(self._data_path.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                return

            self._total_recorded = data.get("total_recorded", 0)

            # Restore category stats
            for cat, sdata in data.get("categories", {}).items():
                stats = CategoryStats(
                    total=sdata.get("total", 0),
                    successes=sdata.get("successes", 0),
                    failures=sdata.get("failures", 0),
                    timeouts=sdata.get("timeouts", 0),
                    avg_duration_s=sdata.get("avg_duration_s", 0),
                    common_errors=sdata.get("common_errors", {}),
                )
                stats.total_duration_s = stats.avg_duration_s * stats.total
                self._category_stats[cat] = stats

            # Restore file stats
            self._file_stats = data.get("file_stats", {})

            logger.info(
                "📈  [Intelligence] Loaded %d historical task records (%d categories)",
                self._total_recorded, len(self._category_stats),
            )
        except Exception as exc:
            logger.debug("📈  [Intelligence] Load error: %s", exc)

    def get_retry_guidance(self, category: str) -> str:
        """
        Return per-task remediation guidance for a category based on failure history.

        Instead of giving up on failing categories, this provides increasingly
        specific instructions to make tasks more likely to succeed:
          - >50% failure: Very aggressive atomic step guidance
          - >30% failure: Detailed validation guidance
          - <=30% failure: No special guidance needed

        Returns empty string if insufficient data or category is healthy.
        """
        cat = category.upper().strip()
        # Also try extracting from task-id-style strings (e.g., "t5-UIUX")
        if cat not in self._category_stats:
            for suffix in ("FUNC", "UIUX", "PERF"):
                if suffix in cat:
                    cat = suffix
                    break
        if cat not in self._category_stats:
            return ""

        stats = self._category_stats[cat]
        if stats.total < 5:
            return ""  # Not enough data

        # Identify the most common error pattern for targeted advice
        top_err = ""
        if stats.common_errors:
            top_err = max(stats.common_errors, key=stats.common_errors.get)

        if stats.failure_rate > 50:
            guidance = (
                f"⚠️ CRITICAL: {cat} tasks have a {stats.failure_rate:.0f}% historical failure rate. "
                "To maximize success:\n"
                "1. Before writing ANY code, first READ the existing file contents carefully.\n"
                "2. Make ONE small change at a time.\n"
                "3. After each change, run the relevant test/build command to verify.\n"
                "4. If the change breaks something, REVERT it and try a different approach.\n"
                "5. Do NOT batch multiple changes — each must be independently verified.\n"
            )
            if top_err:
                guidance += f"6. Most common error pattern: '{top_err}' — watch for this specifically.\n"
            return guidance

        if stats.failure_rate > 30:
            guidance = (
                f"⚠️ {cat} tasks have a {stats.failure_rate:.0f}% failure rate. "
                "Break this into VERY small atomic steps. "
                "Add an explicit verification command after each file modification. "
                "Do NOT skip validation.\n"
            )
            if top_err:
                guidance += f"Common error to watch for: '{top_err}'.\n"
            return guidance

        return ""

    def get_file_risk_warnings(self, files: list[str]) -> str:
        """
        Return warnings for files whose extension has historically high failure rates.

        Args:
            files: List of file paths mentioned in a task description.

        Returns:
            Warning string for problematic file types, or empty string.
        """
        if not files or not self._file_stats:
            return ""

        warnings = []
        seen_exts: set[str] = set()
        for f in files:
            ext = PurePosixPath(f).suffix.lower()
            if not ext or ext in seen_exts:
                continue
            seen_exts.add(ext)

            fstats = self._file_stats.get(ext)
            if not fstats or fstats.get("total", 0) < 3:
                continue

            fail_rate = (fstats["failures"] / fstats["total"]) * 100
            if fail_rate > 25:
                warnings.append(
                    f"{ext} files have a {fail_rate:.0f}% historical failure rate"
                )

        if not warnings:
            return ""

        return (
            "⚠️ FILE RISK: " + "; ".join(warnings) + ". "
            "Pay extra attention to syntax and imports when modifying these files.\n"
        )

    def save(self) -> None:
        """Public save — call on shutdown to ensure final data is persisted."""
        self._persist()

