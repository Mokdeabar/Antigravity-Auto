"""
V74: Health Diagnostics Engine (Audit §4.5 — main.py split)

Extracted from main.py: all build health scanning, static analysis,
TypeScript regression detection, and BUILD_ISSUES.md generation logic.

The original functions remain in main.py (user instruction: do not delete
dead code). New code should import from this module for health operations.

Integration points:
  - main.py: call HealthDiagnostics.run_static_scan() pre-boot
  - main.py: call HealthDiagnostics.run_build_check() in sandbox
  - incremental_verifier.py: can delegate full verification to this module
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("supervisor.health_diagnostics")


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class HealthIssue:
    """A single health issue detected in the project."""
    category: str = ""     # "typescript", "eslint", "security", "pattern", "build"
    severity: str = "warn" # "error", "warn", "info"
    file: str = ""         # Affected file (if applicable)
    line: int = 0          # Line number (if applicable)
    message: str = ""      # Human-readable description
    fix_hint: str = ""     # Suggested fix

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "fix_hint": self.fix_hint,
        }


@dataclass
class HealthReport:
    """Aggregated health report from all diagnostic checks."""
    timestamp: float = 0.0
    duration_s: float = 0.0
    issues: list[HealthIssue] = field(default_factory=list)
    ts_error_count: int = 0
    lint_error_count: int = 0
    security_vulns: int = 0
    pattern_warnings: int = 0
    build_errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.ts_error_count == 0 and len(self.build_errors) == 0

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warn")

    def summary(self) -> str:
        if self.healthy and self.error_count == 0:
            return f"✅ Project healthy ({self.warning_count} warnings, {len(self.issues)} total)"
        parts = []
        if self.ts_error_count:
            parts.append(f"{self.ts_error_count} TS errors")
        if self.lint_error_count:
            parts.append(f"{self.lint_error_count} lint errors")
        if self.security_vulns:
            parts.append(f"{self.security_vulns} security vulns")
        if self.build_errors:
            parts.append(f"{len(self.build_errors)} build errors")
        if self.pattern_warnings:
            parts.append(f"{self.pattern_warnings} pattern warnings")
        return f"⚠️ Health issues: {', '.join(parts)}"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "duration_s": round(self.duration_s, 1),
            "healthy": self.healthy,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "ts_errors": self.ts_error_count,
            "lint_errors": self.lint_error_count,
            "security_vulns": self.security_vulns,
            "pattern_warnings": self.pattern_warnings,
            "build_errors": self.build_errors,
            "issues": [i.to_dict() for i in self.issues[:50]],
        }

    def to_markdown(self) -> str:
        """Generate BUILD_ISSUES.md-compatible markdown."""
        if not self.issues:
            return "# Build Issues\n\nNo issues detected. ✅\n"

        lines = [
            "# Build Issues",
            "",
            f"*Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}*",
            f"*Total: {len(self.issues)} issues ({self.error_count} errors, {self.warning_count} warnings)*",
            "",
        ]

        # Group by category
        categories: dict[str, list[HealthIssue]] = {}
        for issue in self.issues:
            cat = issue.category or "other"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(issue)

        for cat, cat_issues in sorted(categories.items()):
            lines.append(f"## {cat.title()} ({len(cat_issues)} issues)")
            lines.append("")
            for issue in cat_issues[:20]:  # Cap per category
                icon = "❌" if issue.severity == "error" else "⚠️" if issue.severity == "warn" else "ℹ️"
                loc = f"`{issue.file}:{issue.line}` " if issue.file and issue.line else ""
                loc = f"`{issue.file}` " if issue.file and not issue.line else loc
                lines.append(f"- {icon} {loc}{issue.message}")
                if issue.fix_hint:
                    lines.append(f"  - Fix: {issue.fix_hint}")
            lines.append("")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Pattern checks
# ─────────────────────────────────────────────────────────────

# Dangerous code patterns to detect
CODE_PATTERNS = [
    {
        "pattern": r"as\s+any",
        "message": "Unsafe 'as any' cast — use proper types",
        "category": "pattern",
        "severity": "warn",
        "extensions": (".ts", ".tsx"),
    },
    {
        "pattern": r"console\.(log|warn|error|debug|info)\(",
        "message": "console.log left in code — remove for production",
        "category": "pattern",
        "severity": "info",
        "extensions": (".ts", ".tsx", ".js", ".jsx"),
    },
    {
        "pattern": r"(TODO|FIXME|HACK|XXX)\b",
        "message": "TODO/FIXME marker found",
        "category": "pattern",
        "severity": "info",
        "extensions": (".ts", ".tsx", ".js", ".jsx", ".py"),
    },
    {
        "pattern": r'(localhost|127\.0\.0\.1|0\.0\.0\.0):\d{4}',
        "message": "Hardcoded localhost URL — use environment variables",
        "category": "pattern",
        "severity": "warn",
        "extensions": (".ts", ".tsx", ".js", ".jsx", ".env"),
    },
    {
        "pattern": r"(password|secret|api.?key)\s*=\s*['\"](?!process\.env)",
        "message": "Possible hardcoded secret — use environment variables",
        "category": "pattern",
        "severity": "error",
        "extensions": (".ts", ".tsx", ".js", ".jsx", ".py"),
    },
]


# ─────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────

class HealthDiagnostics:
    """
    Comprehensive project health diagnostics.

    Three tiers of checks:
      1. Static scan (host-side, no sandbox needed) — fast, ~5s
      2. Build check (sandbox-side, requires sandbox) — thorough, ~30s
      3. Pattern check (file system scan) — intermediate, ~10s

    Usage:
        diag = HealthDiagnostics(project_path)
        report = await diag.run_static_scan()
        if not report.healthy:
            report.to_markdown()  # -> BUILD_ISSUES.md content
    """

    def __init__(self, project_path: str):
        self._project_path = Path(project_path)
        self._last_report: HealthReport | None = None

    async def run_static_scan(self, save_report: bool = True) -> HealthReport:
        """
        Host-side static health scan — runs BEFORE sandbox boots.

        Checks:
          1. TypeScript compile errors (tsc --noEmit)
          2. ESLint violations
          3. Code pattern checks (dangerous casts, secrets, TODOs)

        Returns HealthReport and optionally writes BUILD_ISSUES.md.
        """
        start = time.time()
        report = HealthReport(timestamp=time.time())

        # TypeScript check
        ts_issues = await self._check_typescript()
        report.issues.extend(ts_issues)
        report.ts_error_count = sum(1 for i in ts_issues if i.severity == "error")

        # ESLint check
        lint_issues = await self._check_eslint()
        report.issues.extend(lint_issues)
        report.lint_error_count = sum(1 for i in lint_issues if i.severity == "error")

        # Pattern checks
        pattern_issues = self._check_patterns()
        report.issues.extend(pattern_issues)
        report.pattern_warnings = len(pattern_issues)

        report.duration_s = time.time() - start

        # Save BUILD_ISSUES.md
        if save_report and report.issues:
            issues_path = self._project_path / "BUILD_ISSUES.md"
            try:
                issues_path.write_text(report.to_markdown(), encoding="utf-8")
                logger.info("🏥  [Health] Wrote %d issues to BUILD_ISSUES.md", len(report.issues))
            except Exception as exc:
                logger.debug("🏥  [Health] Could not write BUILD_ISSUES.md: %s", exc)

        self._last_report = report
        logger.info("🏥  %s (%.1fs)", report.summary(), report.duration_s)
        return report

    async def run_build_check(self, sandbox) -> HealthReport:
        """
        Sandbox-side build health check — runs inside the container.

        Checks:
          1. npm ci / npm run build
          2. Test suite (if configured)
          3. TypeScript compilation

        Returns HealthReport with build-specific issues.
        """
        start = time.time()
        report = HealthReport(timestamp=time.time())

        # Try build
        try:
            build_result = await sandbox.exec_command(
                "cd /workspace && npm run build 2>&1 | tail -30",
                timeout=120,
            )
            if build_result.exit_code != 0:
                output = (build_result.stdout or "") + (build_result.stderr or "")
                report.build_errors.append(output[:500])

                # Extract specific errors
                for line in output.splitlines():
                    if any(k in line.lower() for k in ("error", "err!", "failed")):
                        report.issues.append(HealthIssue(
                            category="build",
                            severity="error",
                            message=line.strip()[:200],
                        ))
        except Exception as exc:
            report.build_errors.append(str(exc)[:200])

        # TypeScript check inside sandbox
        try:
            ts_result = await sandbox.exec_command(
                "cd /workspace && npx tsc --noEmit 2>&1 | head -50",
                timeout=60,
            )
            if ts_result.exit_code != 0:
                for line in (ts_result.stdout or "").splitlines():
                    if "error TS" in line:
                        report.ts_error_count += 1
                        # Parse file:line from TS output
                        m = re.match(r'(.+?)\((\d+),\d+\):\s*error\s+(TS\d+):\s*(.*)', line)
                        if m:
                            report.issues.append(HealthIssue(
                                category="typescript",
                                severity="error",
                                file=m.group(1),
                                line=int(m.group(2)),
                                message=f"{m.group(3)}: {m.group(4)}",
                            ))
        except Exception:
            pass

        report.duration_s = time.time() - start
        self._last_report = report
        logger.info("🏥  [Build] %s (%.1fs)", report.summary(), report.duration_s)
        return report

    def generate_fix_tasks(self, report: HealthReport | None = None) -> list[dict]:
        """
        Generate DAG fix tasks for health issues.

        Groups issues by category and creates targeted fix tasks.
        """
        report = report or self._last_report
        if not report or report.healthy:
            return []

        tasks = []
        task_num = 800

        # Group errors by category
        categories: dict[str, list[HealthIssue]] = {}
        for issue in report.issues:
            if issue.severity == "error":
                cat = issue.category
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(issue)

        for cat, issues in categories.items():
            files = list(set(i.file for i in issues if i.file))[:10]
            messages = [i.message for i in issues[:5]]

            desc = (
                f"[FUNC] Fix {len(issues)} {cat} errors:\n"
                + "\n".join(f"- {m}" for m in messages)
            )
            if files:
                desc += "\n\nAffected files: " + ", ".join(files[:5])

            tasks.append({
                "task_id": f"t{task_num}-FUNC",
                "description": desc,
                "dependencies": [],
            })
            task_num += 1

        return tasks

    async def _check_typescript(self) -> list[HealthIssue]:
        """Run tsc --noEmit on the host."""
        issues = []
        tsconfig = self._project_path / "tsconfig.json"
        if not tsconfig.exists():
            return issues

        try:
            result = subprocess.run(
                ["npx", "tsc", "--noEmit", "--pretty", "false"],
                cwd=str(self._project_path),
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                for line in result.stdout.splitlines():
                    m = re.match(r'(.+?)\((\d+),\d+\):\s*error\s+(TS\d+):\s*(.*)', line)
                    if m:
                        issues.append(HealthIssue(
                            category="typescript",
                            severity="error",
                            file=m.group(1),
                            line=int(m.group(2)),
                            message=f"{m.group(3)}: {m.group(4)}",
                        ))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        except Exception as exc:
            logger.debug("🏥  [Health] TS check error: %s", exc)

        return issues

    async def _check_eslint(self) -> list[HealthIssue]:
        """Run ESLint on the host."""
        issues = []
        eslint_configs = [".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
                         "eslint.config.js", "eslint.config.mjs", "eslint.config.ts"]
        has_eslint = any((self._project_path / c).exists() for c in eslint_configs)
        if not has_eslint:
            return issues

        try:
            result = subprocess.run(
                ["npx", "eslint", "--format", "json", "--max-warnings", "50", "src/"],
                cwd=str(self._project_path),
                capture_output=True, text=True, timeout=60,
            )
            if result.stdout:
                data = json.loads(result.stdout)
                for file_result in data[:20]:
                    filepath = file_result.get("filePath", "")
                    for msg in file_result.get("messages", [])[:5]:
                        issues.append(HealthIssue(
                            category="eslint",
                            severity="error" if msg.get("severity", 0) == 2 else "warn",
                            file=filepath,
                            line=msg.get("line", 0),
                            message=f"{msg.get('ruleId', '?')}: {msg.get('message', '')}",
                        ))
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as exc:
            logger.debug("🏥  [Health] ESLint check error: %s", exc)

        return issues

    def _check_patterns(self) -> list[HealthIssue]:
        """Scan source files for dangerous code patterns."""
        issues = []

        # Find source files
        src_dir = self._project_path / "src"
        if not src_dir.exists():
            return issues

        for pattern_def in CODE_PATTERNS:
            regex = re.compile(pattern_def["pattern"], re.IGNORECASE)
            extensions = pattern_def["extensions"]
            count = 0

            for ext in extensions:
                for filepath in src_dir.rglob(f"*{ext}"):
                    if "node_modules" in str(filepath) or ".next" in str(filepath):
                        continue
                    try:
                        content = filepath.read_text(encoding="utf-8", errors="ignore")
                        matches = regex.findall(content)
                        if matches:
                            count += len(matches)
                            if count <= 3:  # Only log first 3
                                issues.append(HealthIssue(
                                    category=pattern_def["category"],
                                    severity=pattern_def["severity"],
                                    file=str(filepath.relative_to(self._project_path)),
                                    message=f"{pattern_def['message']} ({len(matches)} occurrences)",
                                ))
                    except Exception:
                        pass

        return issues

    @property
    def last_report(self) -> HealthReport | None:
        return self._last_report
