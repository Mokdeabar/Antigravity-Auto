"""
headless_executor.py — Headless Task Execution Engine.

Replaces injector.py. Instead of typing prompts into a chat UI via
Playwright DOM manipulation, this module executes coding tasks by
running Gemini CLI directly inside a Docker sandbox.

Architecture:
    Python Brain → HeadlessExecutor → Gemini CLI (in sandbox) → File System

The executor:
    1. Writes structured prompts to the sandbox
    2. Invokes Gemini CLI with --yolo flag for autonomous execution
    3. Parses stdout/stderr for results
    4. Gathers structured context (files changed, errors, test results)
    5. Returns everything as typed dataclasses — no DOM, no pixels

This module also replaces context_engine.py's DOM-based context gathering
with structured API calls via the ToolServer.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from . import config
from .sandbox_manager import SandboxManager, SandboxError, CommandResult
from .tool_server import ToolServer, ShellExecResult, DevServerResult, GitStatusResult

logger = logging.getLogger("supervisor.headless_executor")


# ─────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    """Result of a coding task executed inside the sandbox."""
    status: str = "unknown"  # success, error, timeout, partial
    output: str = ""
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    exit_code: int = -1
    duration_s: float = 0.0
    prompt_used: str = ""

    @property
    def success(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["success"] = self.success
        return d


@dataclass
class ExecutionContext:
    """
    Structured snapshot of the sandbox's current state.

    Replaces context_engine.py's ContextSnapshot which gathered state
    by scraping the IDE's DOM. This gathers state via direct API calls.
    """
    # Agent output
    last_agent_output: str = ""
    agent_status: str = "unknown"  # working, idle, error, waiting

    # Workspace state
    workspace_files: list[str] = field(default_factory=list)
    recently_changed_files: list[str] = field(default_factory=list)

    # Dev server
    dev_server_running: bool = False
    dev_server_port: int = 0
    dev_server_url: str = ""

    # Code quality
    diagnostics_errors: int = 0
    diagnostics_warnings: int = 0
    diagnostic_details: list[dict] = field(default_factory=list)

    # Git state
    git_branch: str = ""
    git_modified: list[str] = field(default_factory=list)
    git_clean: bool = True

    # Project state
    project_state_content: str = ""

    # Process info
    running_processes: list[dict] = field(default_factory=list)

    # Meta
    gathered_at: float = 0.0
    confidence: float = 1.0  # Always 1.0 — API calls are deterministic

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────
# Core: HeadlessExecutor
# ─────────────────────────────────────────────────────────────

class HeadlessExecutor:
    """
    Executes coding tasks via Gemini CLI inside a Docker sandbox.

    Replaces:
        - injector.py (chat injection → API call)
        - context_engine.py (DOM scraping → structured API calls)
        - monitor.py (CDP chat monitoring → stdout/stderr parsing)
        - approver.py (button clicking → agent has full permissions)

    Usage:
        sandbox = SandboxManager()
        await sandbox.create(project_path)
        tools = ToolServer(sandbox)
        executor = HeadlessExecutor(tools, sandbox)

        # Execute a task
        result = await executor.execute_task("Create a hello.py that prints Hello World")

        # Gather context
        ctx = await executor.gather_context()
    """

    def __init__(self, tool_server: ToolServer, sandbox: SandboxManager):
        self.tools = tool_server
        self.sandbox = sandbox
        self._last_task_result: Optional[TaskResult] = None
        self._task_history: list[TaskResult] = []

    @property
    def last_result(self) -> Optional[TaskResult]:
        """Return the result of the last executed task."""
        return self._last_task_result

    # ── Task Execution ───────────────────────────────────────

    async def execute_task(
        self,
        prompt: str,
        timeout: int = 300,
        mandate: Optional[str] = None,
        use_gemini_cli: bool = True,
    ) -> TaskResult:
        """
        Execute a coding task inside the sandbox.

        Strategy:
            1. Write the prompt (with mandate) to a temp file in the sandbox
            2. Write GEMINI.md context file for Gemini CLI
            3. Run Gemini CLI with --yolo flag for autonomous execution
            4. Parse stdout/stderr for results
            5. Detect files changed via git diff

        Args:
            prompt: The coding task to execute.
            timeout: Max seconds for the entire task.
            mandate: Quality mandate to prepend. Default: config.ULTIMATE_MANDATE.
            use_gemini_cli: If True, use Gemini CLI. If False, just run the prompt as a shell command.

        Returns:
            TaskResult with status, output, files changed, errors.
        """
        result = TaskResult(prompt_used=prompt)
        start_time = time.time()

        try:
            # Get files before execution (for diff tracking)
            git_before = await self.tools.git_status()

            if use_gemini_cli:
                result = await self._execute_via_gemini_cli(prompt, timeout, mandate)
            else:
                result = await self._execute_as_shell(prompt, timeout)

            # Detect files changed
            git_after = await self.tools.git_status()
            result.files_changed = list(set(
                git_after.modified + git_after.untracked
            ) - set(
                git_before.modified + git_before.untracked
            ))

            result.duration_s = time.time() - start_time

        except SandboxError as exc:
            result.status = "error"
            result.errors.append(f"Sandbox error: {exc}")
            result.duration_s = time.time() - start_time
        except asyncio.TimeoutError:
            result.status = "timeout"
            result.errors.append(f"Task timed out after {timeout}s")
            result.duration_s = time.time() - start_time
        except Exception as exc:
            result.status = "error"
            result.errors.append(f"Unexpected error: {exc}")
            result.duration_s = time.time() - start_time

        # Record history
        self._last_task_result = result
        self._task_history.append(result)
        if len(self._task_history) > 50:
            self._task_history = self._task_history[-50:]

        logger.info(
            "Task completed: status=%s, duration=%.1fs, files_changed=%d, errors=%d",
            result.status, result.duration_s, len(result.files_changed), len(result.errors),
        )
        return result

    async def _execute_via_gemini_cli(
        self,
        prompt: str,
        timeout: int,
        mandate: Optional[str],
    ) -> TaskResult:
        """Execute a task by invoking Gemini CLI inside the sandbox."""
        result = TaskResult(prompt_used=prompt)

        # Build the full prompt with mandate
        mandate = mandate or getattr(config, "ULTIMATE_MANDATE", "")
        full_prompt = prompt
        if mandate:
            full_prompt = f"{mandate}\n\n---\n\nTASK:\n{prompt}"

        # Write the prompt file
        await self.sandbox.write_file("/tmp/_task_prompt.md", full_prompt)

        # Write GEMINI.md context file if PROJECT_STATE.md exists
        project_state_exists = await self.sandbox.file_exists("PROJECT_STATE.md")
        if project_state_exists:
            project_state = await self.sandbox.read_file("PROJECT_STATE.md")
            gemini_context = (
                "# Project Context\n\n"
                "## Current Project State\n\n"
                f"{project_state[:5000]}\n"
            )
            await self.sandbox.write_file("GEMINI.md", gemini_context)

        # Determine the Gemini CLI model
        model = getattr(config, "GEMINI_DEFAULT_FLASH", "gemini-2.5-flash")
        gemini_cmd = getattr(config, "GEMINI_CLI_CMD", "gemini")

        # Run Gemini CLI with --yolo (auto-approve all actions)
        cmd = (
            f"cd /workspace && "
            f"{gemini_cmd} --model {model} --yolo "
            f"< /tmp/_task_prompt.md 2>&1"
        )

        cmd_result = await self.sandbox.exec_command(cmd, timeout=timeout)

        result.exit_code = cmd_result.exit_code
        result.output = cmd_result.stdout

        if cmd_result.timed_out:
            result.status = "timeout"
            result.errors.append(f"Gemini CLI timed out after {timeout}s")
        elif cmd_result.exit_code != 0:
            result.status = "error"
            result.errors.append(f"Gemini CLI exited with code {cmd_result.exit_code}")
            if cmd_result.stderr:
                result.errors.append(cmd_result.stderr[:1000])
        else:
            result.status = "success"

        # Check for error patterns in output
        error_patterns = [
            r"(?i)error:",
            r"(?i)traceback \(most recent call last\)",
            r"(?i)syntaxerror:",
            r"(?i)typeerror:",
            r"(?i)referenceerror:",
        ]
        for pattern in error_patterns:
            if re.search(pattern, result.output):
                if result.status == "success":
                    result.status = "partial"  # Success with warnings
                break

        return result

    async def _execute_as_shell(self, command: str, timeout: int) -> TaskResult:
        """Execute a raw shell command (for non-Gemini tasks like npm install)."""
        result = TaskResult(prompt_used=command)

        cmd_result = await self.sandbox.exec_command(command, timeout=timeout)
        result.exit_code = cmd_result.exit_code
        result.output = cmd_result.stdout
        result.status = "success" if cmd_result.success else "error"

        if cmd_result.stderr:
            result.errors.append(cmd_result.stderr[:2000])
        if cmd_result.timed_out:
            result.status = "timeout"
            result.errors.append(f"Command timed out after {timeout}s")

        return result

    # ── Context Gathering ────────────────────────────────────

    async def gather_context(
        self,
        check_diagnostics: bool = True,
        check_dev_server: bool = True,
        max_diagnostic_files: int = 10,
    ) -> ExecutionContext:
        """
        Gather structured context from the sandbox.

        Replaces context_engine.py's gather_context() which used DOM scraping.
        This uses structured API calls that never fail due to CSS changes.

        Pipeline:
            1. Workspace file listing
            2. Git status (recently changed files)
            3. Dev server health check
            4. LSP diagnostics for changed files
            5. PROJECT_STATE.md content
            6. Running processes

        Returns:
            ExecutionContext with complete sandbox state.
        """
        ctx = ExecutionContext(gathered_at=time.time())

        # Run independent operations in parallel
        tasks = {
            "files": self.tools.file_list(".", max_depth=3),
            "git": self.tools.git_status(),
            "processes": self.sandbox.list_processes(),
        }

        if check_dev_server:
            tasks["dev_server"] = self.tools.dev_server_check()

        # Gather results concurrently
        results = {}
        gathered = await asyncio.gather(
            *[tasks[k] for k in tasks],
            return_exceptions=True,
        )
        for key, value in zip(tasks.keys(), gathered):
            if isinstance(value, Exception):
                logger.warning("Context gathering failed for %s: %s", key, value)
                results[key] = None
            else:
                results[key] = value

        # Process file listing
        file_result = results.get("files")
        if file_result and hasattr(file_result, "files"):
            ctx.workspace_files = file_result.files[:200]  # Cap at 200

        # Process git status
        git_result = results.get("git")
        if git_result and isinstance(git_result, GitStatusResult):
            ctx.git_branch = git_result.branch
            ctx.git_modified = git_result.modified
            ctx.git_clean = git_result.clean
            ctx.recently_changed_files = git_result.modified + git_result.untracked

        # Process dev server
        dev_server = results.get("dev_server")
        if dev_server and isinstance(dev_server, DevServerResult):
            ctx.dev_server_running = dev_server.running
            ctx.dev_server_port = dev_server.port
            ctx.dev_server_url = dev_server.url

        # Process running processes
        processes = results.get("processes")
        if processes and isinstance(processes, list):
            ctx.running_processes = processes[:20]  # Cap at 20

        # Run diagnostics on recently changed files
        if check_diagnostics and ctx.recently_changed_files:
            diag_files = ctx.recently_changed_files[:max_diagnostic_files]
            diag_tasks = [self.tools.lsp_diagnostics(f) for f in diag_files]
            diag_results = await asyncio.gather(*diag_tasks, return_exceptions=True)

            for diag_result in diag_results:
                if isinstance(diag_result, Exception):
                    continue
                if hasattr(diag_result, "total_errors"):
                    ctx.diagnostics_errors += diag_result.total_errors
                    ctx.diagnostics_warnings += diag_result.total_warnings
                    if diag_result.errors:
                        ctx.diagnostic_details.extend(diag_result.errors)
                    if diag_result.warnings:
                        ctx.diagnostic_details.extend(diag_result.warnings)

        # Read PROJECT_STATE.md
        try:
            state_exists = await self.sandbox.file_exists("PROJECT_STATE.md")
            if state_exists:
                ctx.project_state_content = await self.sandbox.read_file("PROJECT_STATE.md")
                ctx.project_state_content = ctx.project_state_content[:5000]  # Cap
        except Exception:
            pass

        # Determine agent status
        ctx.agent_status = self._classify_agent_status(ctx)

        # Last task output
        if self._last_task_result:
            ctx.last_agent_output = self._last_task_result.output[:3000]  # Cap

        logger.info(
            "Context gathered: files=%d, git_modified=%d, dev_server=%s, errors=%d, warnings=%d",
            len(ctx.workspace_files),
            len(ctx.git_modified),
            ctx.dev_server_running,
            ctx.diagnostics_errors,
            ctx.diagnostics_warnings,
        )
        return ctx

    def _classify_agent_status(self, ctx: ExecutionContext) -> str:
        """
        Classify the agent's current status based on context.

        Replaces context_engine.py's _classify_agent_status() which used
        DOM-based signals (chat messages, activity badges).

        Returns: working, idle, error, or waiting.
        """
        # If there are diagnostic errors, the agent needs to fix them
        if ctx.diagnostics_errors > 0:
            return "error"

        # If files were recently changed, the agent is working
        if ctx.recently_changed_files:
            return "working"

        # If a dev server is running, things are probably fine
        if ctx.dev_server_running:
            return "idle"

        # Default
        return "waiting"

    # ── Context Formatting ───────────────────────────────────

    def format_context_for_prompt(self, ctx: ExecutionContext) -> str:
        """
        Format an ExecutionContext into a string for Gemini prompt inclusion.

        Replaces context_engine.py's format_context_for_prompt().
        """
        parts = []

        parts.append(f"## Sandbox State (gathered at {time.strftime('%H:%M:%S', time.localtime(ctx.gathered_at))})")
        parts.append(f"- Agent status: **{ctx.agent_status}**")

        if ctx.git_branch:
            parts.append(f"- Git branch: `{ctx.git_branch}`")
            parts.append(f"- Git clean: {ctx.git_clean}")

        if ctx.recently_changed_files:
            parts.append(f"- Recently changed files ({len(ctx.recently_changed_files)}):")
            for f in ctx.recently_changed_files[:10]:
                parts.append(f"  - `{f}`")

        if ctx.dev_server_running:
            parts.append(f"- Dev server: ✅ running on `{ctx.dev_server_url}`")
        else:
            parts.append("- Dev server: ❌ not running")

        if ctx.diagnostics_errors > 0:
            parts.append(f"- Diagnostics: ❌ {ctx.diagnostics_errors} errors, {ctx.diagnostics_warnings} warnings")
            for diag in ctx.diagnostic_details[:5]:
                parts.append(f"  - `{diag.get('file', '?')}:{diag.get('line', '?')}` — {diag.get('message', '?')}")
        elif ctx.diagnostics_warnings > 0:
            parts.append(f"- Diagnostics: ⚠️ {ctx.diagnostics_warnings} warnings (no errors)")
        else:
            parts.append("- Diagnostics: ✅ clean")

        if ctx.project_state_content:
            parts.append("\n## PROJECT_STATE.md (excerpt)")
            parts.append(ctx.project_state_content[:2000])

        if ctx.last_agent_output:
            parts.append("\n## Last Agent Output (excerpt)")
            parts.append(f"```\n{ctx.last_agent_output[:1500]}\n```")

        return "\n".join(parts)

    # ── Convenience Methods ──────────────────────────────────

    async def install_dependencies(self, timeout: int = 180) -> TaskResult:
        """
        Auto-detect and install project dependencies.

        Checks for package.json (npm), requirements.txt (pip), etc.
        """
        # Check what package managers are needed
        has_package_json = await self.sandbox.file_exists("package.json")
        has_requirements = await self.sandbox.file_exists("requirements.txt")
        has_pyproject = await self.sandbox.file_exists("pyproject.toml")

        commands = []
        if has_package_json:
            commands.append("npm install")
        if has_requirements:
            commands.append("pip install -r requirements.txt")
        if has_pyproject:
            commands.append("pip install -e .")

        if not commands:
            return TaskResult(status="success", output="No dependencies to install")

        combined_cmd = " && ".join(commands)
        return await self.execute_task(combined_cmd, timeout=timeout, use_gemini_cli=False)

    async def run_tests(self, timeout: int = 120) -> TaskResult:
        """
        Auto-detect and run project tests.

        Checks for jest, pytest, mocha, etc.
        """
        has_package_json = await self.sandbox.file_exists("package.json")
        has_pytest = await self.sandbox.file_exists("pytest.ini") or \
                     await self.sandbox.file_exists("pyproject.toml")

        if has_package_json:
            # Check if test script exists in package.json
            pkg_content = await self.sandbox.read_file("package.json")
            try:
                pkg = json.loads(pkg_content)
                if "test" in pkg.get("scripts", {}):
                    return await self.execute_task("npm test", timeout=timeout, use_gemini_cli=False)
            except json.JSONDecodeError:
                pass

        # Try pytest
        return await self.execute_task(
            "python -m pytest -v 2>&1 || echo 'No tests found'",
            timeout=timeout,
            use_gemini_cli=False,
        )

    async def start_dev_server(self, timeout: int = 30) -> TaskResult:
        """
        Start the dev server in the background.

        Auto-detects the right command (npm run dev, python -m http.server, etc.)
        """
        has_package_json = await self.sandbox.file_exists("package.json")

        if has_package_json:
            pkg_content = await self.sandbox.read_file("package.json")
            try:
                pkg = json.loads(pkg_content)
                scripts = pkg.get("scripts", {})
                if "dev" in scripts:
                    cmd = "npm run dev &"
                elif "start" in scripts:
                    cmd = "npm start &"
                else:
                    cmd = "npx serve . -l 3000 &"
            except json.JSONDecodeError:
                cmd = "npx serve . -l 3000 &"
        else:
            cmd = "python -m http.server 3000 &"

        # Start in background
        result = await self.execute_task(cmd, timeout=timeout, use_gemini_cli=False)

        # Wait a moment for the server to start
        await asyncio.sleep(3)

        # Verify it's running
        server = await self.tools.dev_server_check()
        if server.running:
            logger.info("Dev server started on port %d", server.port)
        else:
            logger.warning("Dev server may not have started")

        return result
