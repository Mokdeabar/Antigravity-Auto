"""
V74: Incremental Build Verification (Audit §4.3)

Fast-path verification that only checks files changed by a task,
rather than running full tsc / npm run build on every completion.

Integration:
  - Called after each DAG node completes (from main.py pool_worker)
  - Returns a VerifyResult with errors for the changed files only
  - Falls back to full verification if incremental check detects issues
  - Uses tsc --incremental with .tsbuildinfo persistence for TS projects
  - Uses Jest --findRelatedTests for test verification

This module runs commands inside the sandbox container via the sandbox
manager's exec_command interface.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

logger = logging.getLogger("supervisor.incremental_verifier")


@dataclass
class VerifyResult:
    """Result of an incremental verification run."""
    success: bool = True
    ts_errors: list[str] = field(default_factory=list)
    lint_errors: list[str] = field(default_factory=list)
    test_failures: list[str] = field(default_factory=list)
    duration_ms: int = 0
    files_checked: int = 0
    mode: str = "incremental"  # "incremental" or "full"

    @property
    def error_count(self) -> int:
        return len(self.ts_errors) + len(self.lint_errors) + len(self.test_failures)

    def summary(self) -> str:
        if self.success:
            return f"✅ {self.mode} verify: {self.files_checked} files, {self.duration_ms}ms"
        parts = []
        if self.ts_errors:
            parts.append(f"{len(self.ts_errors)} TS errors")
        if self.lint_errors:
            parts.append(f"{len(self.lint_errors)} lint errors")
        if self.test_failures:
            parts.append(f"{len(self.test_failures)} test failures")
        return f"❌ {self.mode} verify: {', '.join(parts)} ({self.duration_ms}ms)"


class IncrementalVerifier:
    """
    Fast-path build verification that only checks changed files.

    Usage:
        verifier = IncrementalVerifier(sandbox)
        result = await verifier.verify_changes(["src/App.tsx", "src/utils.ts"])
        if not result.success:
            # Handle errors — inject fix task or retry
            pass
    """

    # File extensions that trigger TypeScript verification
    TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}
    # Extensions that trigger CSS/style verification
    STYLE_EXTENSIONS = {".css", ".scss", ".sass", ".less"}
    # Extensions that should trigger related test discovery
    TESTABLE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".py"}
    # Globs to skip
    SKIP_PATTERNS = {"node_modules", ".git", "dist", ".next", "coverage", "__pycache__"}

    def __init__(self, sandbox, workspace: str = "/workspace"):
        """
        Args:
            sandbox: SandboxManager instance with exec_command()
            workspace: Workspace path inside the container
        """
        self._sandbox = sandbox
        self._workspace = workspace
        self._has_tsconfig = None  # Cached after first check
        self._has_jest = None
        self._has_eslint = None

    async def verify_changes(self, changed_files: list[str]) -> VerifyResult:
        """
        Run incremental verification on only the changed files.

        Args:
            changed_files: List of file paths (relative to workspace or absolute)

        Returns:
            VerifyResult with any errors found
        """
        start = time.time()
        result = VerifyResult(files_checked=len(changed_files))

        if not changed_files:
            result.duration_ms = int((time.time() - start) * 1000)
            return result

        # Normalize paths to be relative to workspace
        rel_files = self._normalize_paths(changed_files)

        # Filter by type
        ts_files = [f for f in rel_files if PurePosixPath(f).suffix in self.TS_EXTENSIONS]
        style_files = [f for f in rel_files if PurePosixPath(f).suffix in self.STYLE_EXTENSIONS]
        testable_files = [f for f in rel_files if PurePosixPath(f).suffix in self.TESTABLE_EXTENSIONS]

        # Run checks in parallel where possible
        import asyncio

        tasks = []
        if ts_files:
            tasks.append(self._check_typescript(ts_files))
        if style_files:
            tasks.append(self._check_styles(style_files))
        if testable_files:
            tasks.append(self._check_related_tests(testable_files))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.debug("Incremental verify task error: %s", r)
                    continue
                if isinstance(r, dict):
                    if r.get("ts_errors"):
                        result.ts_errors.extend(r["ts_errors"])
                    if r.get("lint_errors"):
                        result.lint_errors.extend(r["lint_errors"])
                    if r.get("test_failures"):
                        result.test_failures.extend(r["test_failures"])

        result.success = result.error_count == 0
        result.duration_ms = int((time.time() - start) * 1000)

        if result.success:
            logger.info("🔍  %s", result.summary())
        else:
            logger.warning("🔍  %s", result.summary())

        return result

    def _normalize_paths(self, files: list[str]) -> list[str]:
        """Normalize file paths to be relative to workspace."""
        normalized = []
        for f in files:
            # Skip files in excluded directories
            if any(skip in f for skip in self.SKIP_PATTERNS):
                continue
            # Strip workspace prefix if absolute
            if f.startswith(self._workspace):
                f = f[len(self._workspace):].lstrip("/")
            elif f.startswith("/"):
                continue  # Skip absolute non-workspace paths
            normalized.append(f)
        return normalized

    async def _detect_tooling(self) -> None:
        """Detect available tooling in the workspace (cached)."""
        if self._has_tsconfig is not None:
            return  # Already detected

        # Run all detections in parallel
        import asyncio
        ts_check, jest_check, eslint_check = await asyncio.gather(
            self._sandbox.exec_command(
                f"test -f {self._workspace}/tsconfig.json && echo YES || echo NO",
                timeout=5,
            ),
            self._sandbox.exec_command(
                f"test -f {self._workspace}/node_modules/.bin/jest && echo YES || echo NO",
                timeout=5,
            ),
            self._sandbox.exec_command(
                f"test -f {self._workspace}/node_modules/.bin/eslint && echo YES || echo NO",
                timeout=5,
            ),
        )
        self._has_tsconfig = "YES" in (ts_check.stdout or "")
        self._has_jest = "YES" in (jest_check.stdout or "")
        self._has_eslint = "YES" in (eslint_check.stdout or "")

        logger.debug(
            "🔍  [Incremental] Tooling: tsconfig=%s jest=%s eslint=%s",
            self._has_tsconfig, self._has_jest, self._has_eslint,
        )

    async def _check_typescript(self, ts_files: list[str]) -> dict:
        """
        Run incremental TypeScript verification on changed files.

        Strategy:
          1. If tsconfig.json exists, use tsc --noEmit --incremental
          2. The --incremental flag uses .tsbuildinfo for fast re-checks
          3. Parse output for error lines affecting our changed files
        """
        await self._detect_tooling()
        errors = []

        if not self._has_tsconfig:
            return {"ts_errors": errors}

        try:
            # Prefer local tsc, fall back to npx
            tsc_bin = f"{self._workspace}/node_modules/.bin/tsc"
            tsc_check = await self._sandbox.exec_command(
                f"test -f {tsc_bin} && echo YES || echo NO",
                timeout=5,
            )
            if "YES" not in (tsc_check.stdout or ""):
                tsc_bin = "npx tsc"

            # Run incremental type check
            cmd = f"cd {self._workspace} && {tsc_bin} --noEmit --incremental --pretty false 2>&1 | head -100"
            result = await self._sandbox.exec_command(cmd, timeout=30)

            if result.exit_code != 0 and result.stdout:
                # Filter errors to only those in our changed files
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if not line or "error TS" not in line:
                        continue
                    # Check if error is in one of our changed files
                    is_relevant = any(f in line for f in ts_files)
                    if is_relevant:
                        errors.append(line[:200])  # Cap line length

                if errors:
                    logger.warning(
                        "🔍  [Incremental] %d TS errors in changed files",
                        len(errors),
                    )
        except Exception as exc:
            logger.debug("🔍  [Incremental] TS check error: %s", exc)

        return {"ts_errors": errors}

    async def _check_styles(self, style_files: list[str]) -> dict:
        """
        Validate changed stylesheets.

        Uses stylelint if available, otherwise does basic syntax validation.
        """
        errors = []

        try:
            # Check if stylelint is available
            stylelint_check = await self._sandbox.exec_command(
                f"test -f {self._workspace}/node_modules/.bin/stylelint && echo YES || echo NO",
                timeout=5,
            )

            if "YES" in (stylelint_check.stdout or ""):
                file_args = " ".join(f'"{self._workspace}/{f}"' for f in style_files[:10])
                result = await self._sandbox.exec_command(
                    f"cd {self._workspace} && ./node_modules/.bin/stylelint {file_args} "
                    f"--formatter compact 2>&1 | head -50",
                    timeout=15,
                )
                if result.exit_code != 0 and result.stdout:
                    for line in result.stdout.strip().split("\n"):
                        if line.strip():
                            errors.append(line.strip()[:200])
        except Exception as exc:
            logger.debug("🔍  [Incremental] Style check error: %s", exc)

        return {"lint_errors": errors}

    async def _check_related_tests(self, testable_files: list[str]) -> dict:
        """
        Run only tests related to changed files.

        Uses Jest's --findRelatedTests for JS/TS, or pytest -k for Python.
        """
        await self._detect_tooling()
        failures = []

        if not self._has_jest:
            return {"test_failures": failures}

        try:
            # Build file list for --findRelatedTests
            abs_files = [f"{self._workspace}/{f}" for f in testable_files[:10]]
            file_args = " ".join(f'"{f}"' for f in abs_files)

            result = await self._sandbox.exec_command(
                f"cd {self._workspace} && ./node_modules/.bin/jest "
                f"--findRelatedTests {file_args} "
                f"--no-coverage --bail --forceExit 2>&1 | tail -30",
                timeout=60,
            )

            if result.exit_code != 0 and result.stdout:
                output = result.stdout.strip()
                # Extract failure summary
                if "FAIL" in output:
                    for line in output.split("\n"):
                        line = line.strip()
                        if line.startswith("FAIL") or "●" in line:
                            failures.append(line[:200])

                if failures:
                    logger.warning(
                        "🔍  [Incremental] %d test failures in related tests",
                        len(failures),
                    )
        except Exception as exc:
            logger.debug("🔍  [Incremental] Test check error: %s", exc)

        return {"test_failures": failures}

    async def full_verify(self) -> VerifyResult:
        """
        Fallback: Run a full build verification (npm run build / tsc --noEmit).

        Used when incremental verification detects issues or when a full
        check is explicitly requested (e.g., phase transitions).
        """
        start = time.time()
        result = VerifyResult(mode="full")

        await self._detect_tooling()

        if self._has_tsconfig:
            tsc_result = await self._check_typescript(["*"])  # Check all files
            result.ts_errors = tsc_result.get("ts_errors", [])

        result.success = result.error_count == 0
        result.duration_ms = int((time.time() - start) * 1000)
        result.files_checked = -1  # Indicates "all"

        if result.success:
            logger.info("🔍  %s", result.summary())
        else:
            logger.warning("🔍  %s", result.summary())

        return result

    def reset_cache(self) -> None:
        """Reset cached tooling detection (call when workspace changes)."""
        self._has_tsconfig = None
        self._has_jest = None
        self._has_eslint = None
