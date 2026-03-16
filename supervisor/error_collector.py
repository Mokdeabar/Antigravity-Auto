"""
V74: Runtime Error Collector (Audit §4.5 — main.py split)

Extracted from main.py: all runtime error collection, console scanning,
Vite dev log analysis, error hook injection, and error-driven retry logic.

The original functions remain in main.py (user instruction: do not delete
dead code). New code should import from this module for error collection.

Integration points:
  - main.py: call ErrorCollector.start() after dev server boots
  - dev_server_manager.py: use scan_console() for server health
  - agent_council.py: use get_recent_errors() for diagnosis context
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("supervisor.error_collector")


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class RuntimeError_:
    """A runtime error captured from the dev server or browser console."""
    source: str = ""       # "console", "vite", "build", "network", "unhandled"
    level: str = "error"   # "error", "warning", "info"
    message: str = ""
    stack_trace: str = ""
    file: str = ""
    line: int = 0
    timestamp: float = 0.0
    count: int = 1         # Deduplication count

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "level": self.level,
            "message": self.message[:500],
            "file": self.file,
            "line": self.line,
            "count": self.count,
            "timestamp": self.timestamp,
        }


@dataclass
class ErrorSummary:
    """Aggregated summary of all collected errors."""
    total_errors: int = 0
    total_warnings: int = 0
    unique_errors: int = 0
    sources: dict[str, int] = field(default_factory=dict)  # source -> count
    top_errors: list[RuntimeError_] = field(default_factory=list)
    collection_duration_s: float = 0.0

    def summary(self) -> str:
        if self.total_errors == 0 and self.total_warnings == 0:
            return "✅ No runtime errors or warnings"
        parts = []
        if self.total_errors:
            parts.append(f"{self.total_errors} errors")
        if self.total_warnings:
            parts.append(f"{self.total_warnings} warnings")
        sources_str = ", ".join(f"{k}: {v}" for k, v in sorted(self.sources.items()))
        return f"⚠️ {', '.join(parts)} ({self.unique_errors} unique) from {sources_str}"

    def to_dict(self) -> dict:
        return {
            "total_errors": self.total_errors,
            "total_warnings": self.total_warnings,
            "unique_errors": self.unique_errors,
            "sources": self.sources,
            "top_errors": [e.to_dict() for e in self.top_errors[:10]],
        }


# ─────────────────────────────────────────────────────────────
# Vite log patterns
# ─────────────────────────────────────────────────────────────

VITE_ERROR_PATTERNS = [
    (r"error TS(\d+):\s*(.*)", "typescript"),
    (r"SyntaxError:\s*(.*)", "syntax"),
    (r"ReferenceError:\s*(.*)", "reference"),
    (r"TypeError:\s*(.*)", "type"),
    (r"\[vite\]\s*Internal server error:\s*(.*)", "vite_internal"),
    (r"Pre-transform error:\s*(.*)", "transform"),
    (r"ENOENT.*no such file.*'([^']+)'", "file_not_found"),
    (r"Module not found.*'([^']+)'", "module_not_found"),
    (r"Cannot find module\s+'([^']+)'", "module_not_found"),
    (r"ERR_MODULE_NOT_FOUND", "module_not_found"),
    (r"Port (\d+) is already in use", "port_conflict"),
]

VITE_WARNING_PATTERNS = [
    (r"\[vite\]\s*(.*deprecat.*)", "deprecation"),
    (r"warning.*:\s*(.*)", "general_warning"),
    (r"WARN\s+(.*)", "npm_warning"),
]


# ─────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────

class ErrorCollector:
    """
    Collects, deduplicates, and analyzes runtime errors from multiple sources:
    browser console, Vite dev server logs, build output, and network errors.

    Features:
      - Deduplication by message hash (avoids spam from hot-reload loops)
      - Source classification (console / vite / build / network)
      - Vite log pattern matching (TS errors, module not found, etc.)
      - Error trend detection (increasing error rate)
      - Fix task generation for persistent errors

    Usage:
        collector = ErrorCollector()
        collector.add_error("console", "TypeError: Cannot read undefined", file="App.tsx")
        collector.scan_vite_log(log_text)
        summary = collector.get_summary()
        if summary.total_errors > 0:
            fix_tasks = collector.generate_fix_tasks()
    """

    def __init__(self, max_errors: int = 200):
        self._errors: list[RuntimeError_] = []
        self._seen_hashes: set[str] = set()
        self._max_errors = max_errors
        self._start_time = time.time()
        self._started = False

    async def start(self, sandbox) -> bool:
        """
        Start error collection inside the sandbox.

        Deploys the console error collector script and starts it
        on port 9999.
        """
        if self._started:
            return True

        try:
            collector_path = Path(__file__).parent / "console_error_collector.js"
            if collector_path.exists():
                await sandbox.copy_file_in(str(collector_path), "/tmp/console_error_collector.js")
                await sandbox.exec_command(
                    "nohup node /tmp/console_error_collector.js > /tmp/error_collector.log 2>&1 &",
                    timeout=5,
                )
                self._started = True
                logger.info("🖥️  [ErrorCollector] Started on port 9999")
                return True
            else:
                logger.debug("🖥️  [ErrorCollector] Collector script not found")
                return False
        except Exception as exc:
            logger.warning("🖥️  [ErrorCollector] Start failed: %s", exc)
            return False

    def add_error(
        self,
        source: str,
        message: str,
        level: str = "error",
        file: str = "",
        line: int = 0,
        stack_trace: str = "",
    ) -> bool:
        """
        Add a runtime error. Returns True if it's a new unique error,
        False if it's a duplicate.
        """
        # Deduplicate by message hash
        msg_hash = hash(f"{source}:{message[:200]}:{file}")
        if msg_hash in self._seen_hashes:
            # Increment count on existing error
            for err in reversed(self._errors):
                if hash(f"{err.source}:{err.message[:200]}:{err.file}") == msg_hash:
                    err.count += 1
                    break
            return False

        self._seen_hashes.add(msg_hash)

        error = RuntimeError_(
            source=source,
            level=level,
            message=message[:1000],
            stack_trace=stack_trace[:2000],
            file=file,
            line=line,
            timestamp=time.time(),
        )
        self._errors.append(error)

        # Cap errors
        if len(self._errors) > self._max_errors:
            self._errors = self._errors[-self._max_errors:]

        return True

    def scan_vite_log(self, log_text: str) -> int:
        """
        Parse Vite dev server log output and extract errors/warnings.

        Returns the number of new errors found.
        """
        new_count = 0

        for line in log_text.splitlines():
            line = line.strip()
            if not line:
                continue

            # Check error patterns
            for pattern, error_type in VITE_ERROR_PATTERNS:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    msg = match.group(1) if match.lastindex else line
                    # Extract file path if present
                    file_match = re.search(r'(?:src/|\./)([^\s:]+)', line)
                    file_path = file_match.group(0) if file_match else ""

                    if self.add_error(
                        source="vite",
                        message=f"[{error_type}] {msg}",
                        file=file_path,
                    ):
                        new_count += 1
                    break

            # Check warning patterns
            for pattern, warn_type in VITE_WARNING_PATTERNS:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    msg = match.group(1) if match.lastindex else line
                    self.add_error(
                        source="vite",
                        message=f"[{warn_type}] {msg}",
                        level="warning",
                    )
                    break

        return new_count

    async def capture_from_sandbox(self, sandbox) -> int:
        """
        Read errors collected by the sandbox error collector script.

        The script stores errors at /tmp/console_errors.json.
        """
        try:
            result = await sandbox.exec_command(
                "cat /tmp/console_errors.json 2>/dev/null",
                timeout=5,
            )
            if not result.stdout:
                return 0

            data = json.loads(result.stdout)
            new_count = 0
            for err in data if isinstance(data, list) else data.get("errors", []):
                if self.add_error(
                    source=err.get("source", "console"),
                    message=err.get("message", ""),
                    level=err.get("level", "error"),
                    file=err.get("file", ""),
                    line=err.get("line", 0),
                    stack_trace=err.get("stack", ""),
                ):
                    new_count += 1

            return new_count

        except Exception:
            return 0

    def get_summary(self) -> ErrorSummary:
        """Get aggregated error summary."""
        summary = ErrorSummary(
            collection_duration_s=time.time() - self._start_time,
            unique_errors=len(self._errors),
        )

        for err in self._errors:
            if err.level == "error":
                summary.total_errors += err.count
            elif err.level == "warning":
                summary.total_warnings += err.count

            source = err.source
            summary.sources[source] = summary.sources.get(source, 0) + err.count

        # Top errors by count
        summary.top_errors = sorted(
            [e for e in self._errors if e.level == "error"],
            key=lambda e: e.count,
            reverse=True,
        )[:10]

        return summary

    def get_recent_errors(self, max_count: int = 10) -> list[RuntimeError_]:
        """Get most recent errors (for injection into agent prompts)."""
        errors = [e for e in reversed(self._errors) if e.level == "error"]
        return errors[:max_count]

    def get_errors_for_prompt(self, max_chars: int = 2000) -> str:
        """Format recent errors for injection into Gemini prompts."""
        errors = self.get_recent_errors(10)
        if not errors:
            return ""

        lines = ["RUNTIME ERRORS (captured from dev server):"]
        for err in errors:
            loc = f" ({err.file}:{err.line})" if err.file else ""
            lines.append(f"  [{err.source}] {err.message[:200]}{loc}")
            if err.count > 1:
                lines[-1] += f" (×{err.count})"

        result = "\n".join(lines)
        return result[:max_chars]

    def generate_fix_tasks(self) -> list[dict]:
        """Generate DAG fix tasks for persistent runtime errors."""
        summary = self.get_summary()
        if summary.total_errors == 0:
            return []

        tasks = []

        # Group errors by file
        file_errors: dict[str, list[RuntimeError_]] = {}
        for err in self._errors:
            if err.level == "error" and err.file:
                if err.file not in file_errors:
                    file_errors[err.file] = []
                file_errors[err.file].append(err)

        task_num = 850
        for filepath, errors in sorted(file_errors.items(), key=lambda x: -len(x[1]))[:5]:
            messages = [e.message[:150] for e in errors[:3]]
            desc = (
                f"[FUNC] Fix runtime errors in {filepath}:\n"
                + "\n".join(f"- {m}" for m in messages)
            )
            tasks.append({
                "task_id": f"t{task_num}-FUNC",
                "description": desc,
                "dependencies": [],
            })
            task_num += 1

        return tasks

    def clear(self) -> None:
        """Clear all collected errors."""
        self._errors.clear()
        self._seen_hashes.clear()
        self._start_time = time.time()
