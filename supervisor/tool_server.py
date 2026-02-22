"""
tool_server.py — MCP-Compatible Tool Interface.

Provides structured JSON tool calls for the "Host Intelligence, Sandboxed Hands"
architecture. The HOST reads from / writes to the sandbox via docker exec bridges.
Every tool returns a well-defined dictionary — deterministic, no DOM parsing.

Architecture:
    Host Brain → ToolServer → SandboxManager → Docker exec → Result (JSON)
                                   ↕
                             PathTranslator (host paths ↔ container paths)

Tool categories:
    - File operations:  file_read, file_write, file_list, file_delete
    - Shell execution:  shell_exec
    - Code intelligence: lsp_diagnostics, syntax_check
    - Git operations:   git_status, git_diff
    - Dev server:       dev_server_check
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path, PurePosixPath
from typing import Optional

from .sandbox_manager import SandboxManager, SandboxError

logger = logging.getLogger("supervisor.tool_server")


# ─────────────────────────────────────────────────────────────
# Path Translator — host ↔ sandbox path mapping
# ─────────────────────────────────────────────────────────────

class PathTranslator:
    """
    Bidirectional path mapping between the host filesystem and the
    sandbox container workspace.

    Host:      C:\\Users\\user\\project\\src\\main.py   (or /home/user/project/src/main.py)
    Sandbox:   /workspace/src/main.py

    This prevents confusion when the AI references container paths
    that don't exist on the host, or vice versa.
    """

    def __init__(self, host_project_path: str, sandbox_workspace: str = "/workspace"):
        self._host_root = str(Path(host_project_path).resolve())
        self._sandbox_root = sandbox_workspace

    @property
    def host_root(self) -> str:
        return self._host_root

    @property
    def sandbox_root(self) -> str:
        return self._sandbox_root

    def host_to_sandbox(self, host_path: str) -> str:
        """Convert a host filesystem path to a sandbox container path."""
        resolved = str(Path(host_path).resolve())
        # Normalize separators for comparison
        norm_host = self._host_root.replace("\\", "/")
        norm_path = resolved.replace("\\", "/")
        if norm_path.startswith(norm_host):
            relative = norm_path[len(norm_host):].lstrip("/")
            return f"{self._sandbox_root}/{relative}" if relative else self._sandbox_root
        # Already a sandbox-style path or unrelated — return as-is
        return host_path

    def sandbox_to_host(self, sandbox_path: str) -> str:
        """Convert a sandbox container path to a host filesystem path."""
        if sandbox_path.startswith(self._sandbox_root):
            relative = sandbox_path[len(self._sandbox_root):].lstrip("/")
            return str(Path(self._host_root) / relative) if relative else self._host_root
        # Not a sandbox path — return as-is
        return sandbox_path

    def is_sandbox_path(self, path: str) -> bool:
        """Check if a path looks like a sandbox container path."""
        return path.startswith(self._sandbox_root) or path.startswith("/workspace")

    def is_host_path(self, path: str) -> bool:
        """Check if a path looks like a host filesystem path."""
        norm = path.replace("\\", "/")
        return norm.startswith(self._host_root.replace("\\", "/"))


# ─────────────────────────────────────────────────────────────
# Data Classes for structured tool results
# ─────────────────────────────────────────────────────────────

@dataclass
class FileReadResult:
    path: str = ""
    content: str = ""
    exists: bool = True
    size_bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FileWriteResult:
    path: str = ""
    bytes_written: int = 0
    success: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FileListResult:
    directory: str = ""
    files: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    total_files: int = 0
    total_dirs: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ShellExecResult:
    command: str = ""
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["success"] = self.success
        return d


@dataclass
class DiagnosticItem:
    file: str = ""
    line: int = 0
    column: int = 0
    severity: str = "error"  # error, warning, info, hint
    message: str = ""
    code: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiagnosticsResult:
    file: str = ""
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    total_errors: int = 0
    total_warnings: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GitStatusResult:
    branch: str = ""
    modified: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    staged: list[str] = field(default_factory=list)
    clean: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DevServerResult:
    running: bool = False
    port: int = 0
    url: str = ""
    process_name: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────
# Core: ToolServer
# ─────────────────────────────────────────────────────────────

class ToolServer:
    """
    Structured tool interface for the coding agent workspace.

    All tools return typed dataclass results with .to_dict() for JSON serialization.
    Every tool is deterministic — same input always produces same output.
    Every tool handles its own errors — callers never see raw exceptions.
    """

    def __init__(self, sandbox: SandboxManager):
        self.sandbox = sandbox

    # ── File Operations ──────────────────────────────────────

    async def file_read(self, path: str) -> FileReadResult:
        """
        Read file contents from the sandbox workspace.

        Args:
            path: Path relative to /workspace, or absolute path.

        Returns:
            FileReadResult with content and metadata.
        """
        result = FileReadResult(path=path)
        try:
            exists = await self.sandbox.file_exists(path)
            if not exists:
                result.exists = False
                result.error = f"File not found: {path}"
                return result

            content = await self.sandbox.read_file(path)
            result.content = content
            result.size_bytes = len(content.encode("utf-8"))
        except SandboxError as exc:
            result.exists = False
            result.error = str(exc)
        except Exception as exc:
            result.error = f"Unexpected error reading {path}: {exc}"
        return result

    async def file_write(self, path: str, content: str) -> FileWriteResult:
        """
        Write content to a file in the sandbox workspace.

        Creates parent directories if they don't exist.

        Args:
            path: Path relative to /workspace, or absolute path.
            content: File content to write.

        Returns:
            FileWriteResult with bytes written.
        """
        result = FileWriteResult(path=path)
        try:
            bytes_written = await self.sandbox.write_file(path, content)
            result.bytes_written = bytes_written
            result.success = True
        except SandboxError as exc:
            result.success = False
            result.error = str(exc)
        except Exception as exc:
            result.success = False
            result.error = f"Unexpected error writing {path}: {exc}"
        return result

    async def file_list(self, directory: str = ".", max_depth: int = 3) -> FileListResult:
        """
        List files and directories in the sandbox workspace.

        Args:
            directory: Directory to list (relative to /workspace or absolute).
            max_depth: Maximum recursion depth for file listing.

        Returns:
            FileListResult with files and dirs.
        """
        result = FileListResult(directory=directory)
        try:
            # Get files
            files = await self.sandbox.list_files(directory, max_depth=max_depth)
            result.files = files
            result.total_files = len(files)

            # Get directories
            full_path = directory
            if not full_path.startswith("/"):
                workspace = self.sandbox.active_sandbox.workspace_path if self.sandbox.active_sandbox else "/workspace"
                full_path = f"{workspace}/{directory}"

            dir_result = await self.sandbox.exec_command(
                f"find {_sq(full_path)} -maxdepth {max_depth} -type d "
                f"! -path '*/node_modules/*' ! -path '*/.git/*' | head -100"
            )
            if dir_result.exit_code == 0:
                result.dirs = [d.strip() for d in dir_result.stdout.strip().split("\n") if d.strip()]
                result.total_dirs = len(result.dirs)

        except Exception as exc:
            result.error = f"Error listing {directory}: {exc}"
        return result

    async def file_delete(self, path: str) -> dict:
        """
        Delete a file from the sandbox workspace.

        Returns:
            {"path": str, "deleted": bool, "error": str}
        """
        try:
            cmd_result = await self.sandbox.exec_command(f"rm -f {_sq(path)}")
            return {
                "path": path,
                "deleted": cmd_result.exit_code == 0,
                "error": cmd_result.stderr[:200] if cmd_result.exit_code != 0 else "",
            }
        except Exception as exc:
            return {"path": path, "deleted": False, "error": str(exc)}

    # ── Shell Execution ──────────────────────────────────────

    async def shell_exec(
        self,
        command: str,
        timeout: int = 120,
        workdir: Optional[str] = None,
    ) -> ShellExecResult:
        """
        Execute a shell command inside the sandbox.

        This is the primary way to run build tools, test suites,
        package managers, and dev servers.

        Args:
            command: Shell command to execute.
            timeout: Max seconds to wait.
            workdir: Working directory inside the container.

        Returns:
            ShellExecResult with exit code, stdout, stderr.
        """
        result = ShellExecResult(command=command)
        try:
            cmd_result = await self.sandbox.exec_command(
                command, timeout=timeout, workdir=workdir
            )
            result.exit_code = cmd_result.exit_code
            result.stdout = cmd_result.stdout
            result.stderr = cmd_result.stderr
            result.timed_out = cmd_result.timed_out
        except SandboxError as exc:
            result.error = str(exc)
        except Exception as exc:
            result.error = f"Unexpected error: {exc}"
        return result

    # ── Code Intelligence ────────────────────────────────────

    async def lsp_diagnostics(self, file_path: str) -> DiagnosticsResult:
        """
        Get diagnostic information (errors, warnings) for a file.

        Uses language-specific tools available in the sandbox:
        - Python: pyflakes, flake8, or python -m py_compile
        - JavaScript/TypeScript: eslint or tsc --noEmit
        - Generic: file existence and syntax check

        Args:
            file_path: Path to the file to check.

        Returns:
            DiagnosticsResult with errors and warnings.
        """
        result = DiagnosticsResult(file=file_path)
        try:
            ext = PurePosixPath(file_path).suffix.lower()

            if ext == ".py":
                result = await self._python_diagnostics(file_path)
            elif ext in (".js", ".jsx", ".ts", ".tsx"):
                result = await self._js_diagnostics(file_path)
            elif ext in (".html", ".css"):
                result = await self._check_file_exists(file_path)
            else:
                result = await self._check_file_exists(file_path)

        except Exception as exc:
            result.error = f"Diagnostic error: {exc}"
        return result

    async def syntax_check(self, file_path: str, content: str) -> dict:
        """
        Check syntax of code content without writing to disk.

        Args:
            file_path: Filename (used to detect language).
            content: Code content to check.

        Returns:
            {"valid": bool, "errors": [...], "language": str}
        """
        ext = PurePosixPath(file_path).suffix.lower()
        temp_path = f"/tmp/_syntax_check{ext}"

        try:
            await self.sandbox.write_file(temp_path, content)

            if ext == ".py":
                cmd_result = await self.sandbox.exec_command(
                    f"python -m py_compile {temp_path} 2>&1"
                )
                errors = []
                if cmd_result.exit_code != 0:
                    errors = [cmd_result.stderr.strip() or cmd_result.stdout.strip()]
                return {"valid": cmd_result.exit_code == 0, "errors": errors, "language": "python"}

            elif ext in (".js", ".jsx"):
                cmd_result = await self.sandbox.exec_command(
                    f"node --check {temp_path} 2>&1"
                )
                errors = []
                if cmd_result.exit_code != 0:
                    errors = [cmd_result.stderr.strip() or cmd_result.stdout.strip()]
                return {"valid": cmd_result.exit_code == 0, "errors": errors, "language": "javascript"}

            elif ext == ".json":
                cmd_result = await self.sandbox.exec_command(
                    f"python -c \"import json; json.load(open('{temp_path}'))\" 2>&1"
                )
                errors = []
                if cmd_result.exit_code != 0:
                    errors = [cmd_result.stderr.strip()]
                return {"valid": cmd_result.exit_code == 0, "errors": errors, "language": "json"}

            return {"valid": True, "errors": [], "language": "unknown"}

        except Exception as exc:
            return {"valid": False, "errors": [str(exc)], "language": "unknown"}
        finally:
            await self.sandbox.exec_command(f"rm -f {temp_path}")

    # ── Git Operations ───────────────────────────────────────

    async def git_status(self) -> GitStatusResult:
        """
        Get the git status of the workspace.

        Returns:
            GitStatusResult with branch, modified, untracked, staged files.
        """
        result = GitStatusResult()
        try:
            # Get branch
            branch_result = await self.sandbox.exec_command(
                "git rev-parse --abbrev-ref HEAD 2>/dev/null"
            )
            if branch_result.exit_code == 0:
                result.branch = branch_result.stdout.strip()
            else:
                result.error = "Not a git repository"
                return result

            # Get status in porcelain format
            status_result = await self.sandbox.exec_command(
                "git status --porcelain 2>/dev/null"
            )
            if status_result.exit_code == 0:
                for line in status_result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    status_code = line[:2]
                    filepath = line[3:].strip()

                    if status_code[0] in ("M", "A", "D", "R"):
                        result.staged.append(filepath)
                    if status_code[1] == "M":
                        result.modified.append(filepath)
                    elif status_code == "??":
                        result.untracked.append(filepath)

                result.clean = not (result.modified or result.untracked or result.staged)

        except Exception as exc:
            result.error = f"Git status error: {exc}"
        return result

    async def git_diff(self, file_path: Optional[str] = None) -> dict:
        """
        Get git diff for the workspace or a specific file.

        Returns:
            {"diff": str, "files_changed": int, "insertions": int, "deletions": int}
        """
        try:
            cmd = "git diff"
            if file_path:
                cmd += f" -- {_sq(file_path)}"

            diff_result = await self.sandbox.exec_command(cmd)

            # Get stat summary
            stat_cmd = "git diff --stat"
            if file_path:
                stat_cmd += f" -- {_sq(file_path)}"
            stat_result = await self.sandbox.exec_command(stat_cmd)

            return {
                "diff": diff_result.stdout[:10000],  # Cap at 10K chars
                "stat": stat_result.stdout,
                "error": diff_result.stderr[:200] if diff_result.exit_code != 0 else "",
            }
        except Exception as exc:
            return {"diff": "", "stat": "", "error": str(exc)}

    # ── Dev Server ───────────────────────────────────────────

    async def dev_server_check(self, ports: Optional[list[int]] = None) -> DevServerResult:
        """
        Check if a dev server is running inside the sandbox.

        Scans common ports for listening processes.

        Args:
            ports: Specific ports to check. Default: common dev server ports.

        Returns:
            DevServerResult with first found running server.
        """
        ports = ports or [3000, 3001, 4200, 5000, 5173, 8000, 8080]
        result = DevServerResult()

        try:
            for port in ports:
                is_listening = await self.sandbox.check_port(port)
                if is_listening:
                    result.running = True
                    result.port = port
                    result.url = f"http://localhost:{port}"

                    # Try to identify the process
                    proc_result = await self.sandbox.exec_command(
                        f"ss -tlnp 2>/dev/null | grep ':{port} ' | head -1"
                    )
                    if proc_result.exit_code == 0 and proc_result.stdout.strip():
                        result.process_name = proc_result.stdout.strip()[:100]

                    logger.info("Dev server found on port %d", port)
                    return result

        except Exception as exc:
            result.error = f"Dev server check error: {exc}"
        return result

    # ── Internal Helpers ─────────────────────────────────────

    async def _python_diagnostics(self, file_path: str) -> DiagnosticsResult:
        """Run Python syntax checking and basic linting."""
        result = DiagnosticsResult(file=file_path)

        # Try py_compile first (always available)
        cmd_result = await self.sandbox.exec_command(
            f"python -m py_compile {_sq(file_path)} 2>&1"
        )
        if cmd_result.exit_code != 0:
            error_text = cmd_result.stderr.strip() or cmd_result.stdout.strip()
            diag = DiagnosticItem(
                file=file_path,
                severity="error",
                message=error_text,
            )
            # Try to parse line number from error
            line_match = re.search(r"line (\d+)", error_text)
            if line_match:
                diag.line = int(line_match.group(1))
            result.errors.append(diag.to_dict())
            result.total_errors = 1

        # Try pyflakes if available (more detailed diagnostics)
        pyflakes_result = await self.sandbox.exec_command(
            f"python -m pyflakes {_sq(file_path)} 2>&1"
        )
        if pyflakes_result.exit_code == 0 and pyflakes_result.stdout.strip():
            for line in pyflakes_result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                # pyflakes format: filename:line: message
                match = re.match(r"(.+?):(\d+):\s*(.*)", line)
                if match:
                    diag = DiagnosticItem(
                        file=match.group(1),
                        line=int(match.group(2)),
                        severity="warning",
                        message=match.group(3),
                    )
                    result.warnings.append(diag.to_dict())
            result.total_warnings = len(result.warnings)

        return result

    async def _js_diagnostics(self, file_path: str) -> DiagnosticsResult:
        """Run JavaScript/TypeScript syntax checking."""
        result = DiagnosticsResult(file=file_path)

        # Try node --check for basic syntax
        ext = PurePosixPath(file_path).suffix.lower()
        if ext in (".js", ".jsx"):
            cmd_result = await self.sandbox.exec_command(
                f"node --check {_sq(file_path)} 2>&1"
            )
            if cmd_result.exit_code != 0:
                error_text = cmd_result.stderr.strip() or cmd_result.stdout.strip()
                diag = DiagnosticItem(
                    file=file_path,
                    severity="error",
                    message=error_text,
                )
                result.errors.append(diag.to_dict())
                result.total_errors = 1

        elif ext in (".ts", ".tsx"):
            # Try tsc --noEmit if available
            cmd_result = await self.sandbox.exec_command(
                f"npx tsc --noEmit {_sq(file_path)} 2>&1",
                timeout=60,
            )
            if cmd_result.exit_code != 0:
                for line in (cmd_result.stdout + cmd_result.stderr).strip().split("\n"):
                    match = re.match(r"(.+?)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.*)", line)
                    if match:
                        diag = DiagnosticItem(
                            file=match.group(1),
                            line=int(match.group(2)),
                            column=int(match.group(3)),
                            severity="error",
                            code=match.group(4),
                            message=match.group(5),
                        )
                        result.errors.append(diag.to_dict())
                result.total_errors = len(result.errors)

        return result

    async def _check_file_exists(self, file_path: str) -> DiagnosticsResult:
        """Basic existence check for a file."""
        result = DiagnosticsResult(file=file_path)
        exists = await self.sandbox.file_exists(file_path)
        if not exists:
            result.errors.append(DiagnosticItem(
                file=file_path,
                severity="error",
                message=f"File does not exist: {file_path}",
            ).to_dict())
            result.total_errors = 1
        return result


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _sq(s: str) -> str:
    """Shell-quote a string for safe use in sh -c commands."""
    return "'" + s.replace("'", "'\\''") + "'"
