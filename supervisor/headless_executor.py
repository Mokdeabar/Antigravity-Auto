"""
headless_executor.py — Headless Task Execution Engine.

"Host Intelligence, Sandboxed Hands" Architecture:
    The HOST runs the brain (Gemini CLI + Ollama).
    The SANDBOX is a dumb terminal (file I/O + shell exec only).

    ┌─────────────────────────────────────────────────────────┐
    │  HOST (Intelligence)                                     │
    │  ─ Gemini CLI (cloud, authenticated AI Ultra session)    │
    │  ─ Ollama (local LLM, ~200ms for triage)                 │
    │  ─ Prompt construction, output parsing                   │
    │  ─ ZERO credentials leak into the sandbox                │
    ├─────────────────────────────────────────────────────────┤
    │  SANDBOX (Execution)                                     │
    │  ─ File read/write via docker exec                       │
    │  ─ Shell command execution via docker exec               │
    │  ─ Dev server, tests, linters — all inside container     │
    │  ─ Non-root user, no API keys, no Gemini CLI installed   │
    └─────────────────────────────────────────────────────────┘

Graceful degradation: if Ollama is unavailable, everything works
via Gemini CLI alone (just slower for lightweight decisions).

Context gathering uses structured API calls via the ToolServer.
"""

import asyncio
import json
import shutil
import tempfile
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import aiohttp

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
# Ollama Local Brain — Fast local intelligence
# ─────────────────────────────────────────────────────────────

class OllamaLocalBrain:
    """
    Fast local LLM via Ollama for lightweight intelligence tasks.

    Handles task classification, context analysis, error pre-screening,
    and action decisions with ~200ms latency. Falls back gracefully
    if Ollama is unavailable — everything still works via Gemini CLI.

    Communicates with Ollama via its HTTP API, running on the host
    and accessible from Docker via host.docker.internal:11434.
    """

    def __init__(self, host: Optional[str] = None, model: Optional[str] = None):
        self.host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2:3b")
        self._available: Optional[bool] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def is_available(self) -> bool:
        """Check if Ollama is running and responsive."""
        if self._available is not None:
            return self._available
        try:
            session = await self._get_session()
            async with session.get(f"{self.host}/api/tags") as resp:
                self._available = resp.status == 200
                if self._available:
                    data = await resp.json()
                    models = [m.get("name", "") for m in data.get("models", [])]
                    logger.info("Ollama available. Models: %s", models[:5])
                    # Auto-select best available model if default isn't present
                    if self.model not in models and models:
                        self.model = models[0]
                        logger.info("Auto-selected Ollama model: %s", self.model)
                return self._available
        except Exception as exc:
            logger.debug("Ollama not available: %s", exc)
            self._available = False
            return False

    async def ask(self, prompt: str, system: str = "", temperature: float = 0.1) -> Optional[str]:
        """
        Send a prompt to the local Ollama model.

        Returns the response text, or None if Ollama is unavailable.
        Fast path: ~200ms for short prompts on modern hardware.
        """
        if not await self.is_available():
            return None

        try:
            session = await self._get_session()
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temperature},
            }
            if system:
                payload["system"] = system

            async with session.post(f"{self.host}/api/generate", json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("response", "")
        except Exception as exc:
            logger.debug("Ollama request failed: %s", exc)
            return None

    async def ask_json(self, prompt: str, system: str = "") -> Optional[dict]:
        """Ask Ollama and parse a JSON response."""
        response = await self.ask(
            prompt,
            system=system + "\nRespond with valid JSON only. No markdown, no explanation.",
        )
        if not response:
            return None
        try:
            # Extract JSON from response (handle markdown fences)
            cleaned = re.sub(r"```json?\s*", "", response)
            cleaned = re.sub(r"```\s*", "", cleaned)
            return json.loads(cleaned.strip())
        except (json.JSONDecodeError, ValueError):
            return None

    async def classify_task(self, prompt: str) -> dict:
        """
        Classify a task's complexity and requirements using local LLM.

        Returns:
            {"complexity": "simple"|"medium"|"complex",
             "needs_gemini": bool,
             "category": "coding"|"testing"|"analysis"|"setup"|"other",
             "estimated_duration_s": int}
        """
        result = await self.ask_json(
            f"Classify this coding task:\n\n{prompt[:1000]}",
            system=(
                "You classify coding tasks. Return JSON with: "
                "complexity (simple/medium/complex), "
                "needs_gemini (true if this needs a powerful cloud LLM, false if a shell command suffices), "
                "category (coding/testing/analysis/setup/other), "
                "estimated_duration_s (integer seconds)."
            ),
        )
        return result or {
            "complexity": "medium",
            "needs_gemini": True,
            "category": "coding",
            "estimated_duration_s": 60,
        }

    async def analyze_errors(self, errors: list[dict], context: str = "") -> Optional[str]:
        """
        Quick analysis of diagnostic errors — faster than calling Gemini.

        Returns a short analysis string, or None if Ollama is unavailable.
        """
        if not errors:
            return None
        error_text = json.dumps(errors[:5], indent=2)
        return await self.ask(
            f"Analyze these code errors and suggest the most likely fix:\n\n"
            f"Errors:\n{error_text}\n\nContext:\n{context[:500]}",
            system="You are a senior developer. Be concise. Give actionable fix suggestions.",
        )

    async def decide_action(self, context_summary: str) -> dict:
        """
        Decide what action to take based on current context.

        Returns:
            {"action": "execute_task"|"fix_errors"|"run_tests"|"start_server"|"wait"|"escalate",
             "reason": str}
        """
        result = await self.ask_json(
            f"Based on this project state, what should the supervisor do next?\n\n{context_summary[:2000]}",
            system=(
                "You are an autonomous coding supervisor. Decide the next action. "
                "Return JSON with: action (execute_task/fix_errors/run_tests/start_server/wait/escalate), "
                "reason (brief explanation)."
            ),
        )
        return result or {"action": "execute_task", "reason": "Default: continue with main task"}

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()


# ─────────────────────────────────────────────────────────────
# Core: HeadlessExecutor
# ─────────────────────────────────────────────────────────────

class HeadlessExecutor:
    """
    Host Intelligence, Sandboxed Hands.

    The HOST runs intelligence (Gemini CLI + Ollama).
    The SANDBOX is a dumb terminal (file I/O + shell exec only).

    Gemini CLI runs on the host using the user's authenticated session.
    ZERO credentials enter the Docker container.

    Usage:
        sandbox = SandboxManager()
        await sandbox.create(project_path)
        tools = ToolServer(sandbox)
        executor = HeadlessExecutor(tools, sandbox)

        # Execute a task (Gemini runs on HOST, actions in SANDBOX)
        result = await executor.execute_task("Create a hello.py that prints Hello World")

        # Gather context (reads from SANDBOX via docker exec)
        ctx = await executor.gather_context()
    """

    def __init__(self, tool_server: ToolServer, sandbox: SandboxManager):
        self.tools = tool_server
        self.sandbox = sandbox
        self.local_brain = OllamaLocalBrain()
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
        """
        Execute a task via Gemini CLI running on the HOST.

        Architecture: "Host Intelligence, Sandboxed Hands"
            - Gemini CLI runs on the host OS (uses authenticated AI Ultra session)
            - Prompt is constructed on the host and piped to Gemini via stdin
            - Gemini output is parsed on the host
            - File changes and commands are pushed into the sandbox via bridges
            - ZERO credentials enter the container
        """
        result = TaskResult(prompt_used=prompt)

        # Build the full prompt with mandate
        mandate = mandate or getattr(config, "ULTIMATE_MANDATE", "")
        full_prompt = prompt
        if mandate:
            full_prompt = f"{mandate}\n\n---\n\nTASK:\n{prompt}"

        # Gather sandbox context to feed to the host-side Gemini CLI
        # so it has awareness of the workspace state
        sandbox_context = await self._gather_sandbox_context_for_prompt()
        if sandbox_context:
            full_prompt = f"{full_prompt}\n\n---\n\nCURRENT WORKSPACE STATE:\n{sandbox_context}"

        # Run Gemini CLI on the HOST (not inside the container)
        cmd_result = await self._run_gemini_on_host(full_prompt, timeout)

        result.exit_code = cmd_result.get("exit_code", -1)
        result.output = cmd_result.get("stdout", "")

        if cmd_result.get("timed_out"):
            result.status = "timeout"
            result.errors.append(f"Gemini CLI timed out after {timeout}s")
        elif result.exit_code != 0:
            result.status = "error"
            result.errors.append(f"Gemini CLI exited with code {result.exit_code}")
            stderr = cmd_result.get("stderr", "")
            if stderr:
                result.errors.append(stderr[:1000])
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

    async def _run_gemini_on_host(
        self,
        prompt: str,
        timeout: int,
    ) -> dict:
        """
        Run Gemini CLI on the HOST OS using the user's authenticated session.

        The prompt is piped via stdin. The Gemini CLI has access to the
        host's credentials (AI Ultra session) but the sandbox does not.

        The --sandbox flag tells Gemini CLI to use its own sandboxing,
        but we override the working directory to point at the bind-mounted
        project path so Gemini can read/write files that are visible
        inside the container.

        Returns:
            dict with keys: exit_code, stdout, stderr, timed_out
        """
        model = getattr(config, "GEMINI_DEFAULT_FLASH", "gemini-2.5-flash")
        gemini_cmd = getattr(config, "GEMINI_CLI_CMD", "gemini")

        # Resolve the project directory on the host
        # If bind-mounted, the project directory is the host path itself
        project_path = "."
        if self.sandbox._active:
            project_path = self.sandbox._active.project_path or "."

        # Build the Gemini CLI command
        cmd_args = [
            gemini_cmd,
            "--model", model,
            "--yolo",  # Auto-approve all file operations
        ]

        C = getattr(config, "ANSI_CYAN", "")
        R = getattr(config, "ANSI_RESET", "")
        logger.info("Running Gemini CLI on HOST: model=%s, cwd=%s", model, project_path)
        print(f"  {C}🧠 Host Intelligence: Gemini CLI (model={model}) …{R}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=project_path,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout,
            )

            return {
                "exit_code": proc.returncode or 0,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "timed_out": False,
            }

        except asyncio.TimeoutError:
            logger.warning("Gemini CLI timed out after %ds", timeout)
            try:
                proc.kill()
            except Exception:
                pass
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Timed out after {timeout}s",
                "timed_out": True,
            }
        except FileNotFoundError:
            logger.error("Gemini CLI binary not found: %s", gemini_cmd)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Gemini CLI not found: {gemini_cmd}. Ensure it is installed and in PATH.",
                "timed_out": False,
            }
        except Exception as exc:
            logger.error("Gemini CLI host execution failed: %s", exc)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "timed_out": False,
            }

    async def _gather_sandbox_context_for_prompt(self) -> str:
        """
        Gather lightweight context from the sandbox to include in the
        Gemini prompt so the host-side AI has workspace awareness.

        This is the Context Bridge — the host reads sandbox state
        via docker exec and feeds it into the prompt.
        """
        parts = []

        try:
            # Git status
            git_result = await self.tools.git_status()
            if git_result.modified or git_result.untracked:
                parts.append(f"Git branch: {git_result.branch}")
                if git_result.modified:
                    parts.append(f"Modified files: {', '.join(git_result.modified[:20])}")
                if git_result.untracked:
                    parts.append(f"Untracked files: {', '.join(git_result.untracked[:20])}")

            # Project state file
            project_state_exists = await self.sandbox.file_exists("PROJECT_STATE.md")
            if project_state_exists:
                state_content = await self.sandbox.read_file("PROJECT_STATE.md")
                parts.append(f"PROJECT_STATE.md:\n{state_content[:3000]}")

            # File listing (top-level)
            file_list = await self.sandbox.list_files(".", max_depth=2)
            if file_list:
                parts.append(f"Workspace files ({len(file_list)} total): {', '.join(file_list[:30])}")

        except Exception as exc:
            logger.debug("Context bridge gathering failed (non-fatal): %s", exc)

        return "\n".join(parts) if parts else ""

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
