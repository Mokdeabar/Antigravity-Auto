"""
sandbox_manager.py — Docker Sandbox Lifecycle Manager.

Replaces the entire Playwright/CDP boot sequence with ephemeral Docker
containers. Each supervisor session spins up an isolated sandbox where
the coding agent (Gemini CLI) can read/write files, execute shell commands,
install packages, and run dev servers — without touching the host OS.

Architecture:
    Python Brain → SandboxManager → Docker CLI → Container → File System & Shell

Key properties:
    - Containers are ephemeral: created per-session, destroyed on shutdown.
    - Project workspace is bind-mounted at /workspace.
    - All commands execute inside the container, never on the host.
    - Timeout enforcement via asyncio wrapper.
    - Automatic cleanup on manager destruction.
"""

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("supervisor.sandbox_manager")


# ─────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────

class SandboxError(Exception):
    """Raised when a sandbox operation fails."""
    pass


class SandboxTimeoutError(SandboxError):
    """Raised when a sandbox command exceeds its timeout."""
    pass


class DockerNotAvailableError(SandboxError):
    """Raised when Docker CLI is not found or Docker daemon is not running."""
    pass


# ─────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    """Result of a command executed inside the sandbox."""
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    command: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def __str__(self) -> str:
        status = "OK" if self.success else f"FAIL(exit={self.exit_code})"
        if self.timed_out:
            status = "TIMEOUT"
        return f"[{status}] {self.command[:80]}"


@dataclass
class SandboxInfo:
    """Metadata about a running sandbox container."""
    container_id: str = ""
    container_name: str = ""
    image: str = ""
    project_path: str = ""
    workspace_path: str = "/workspace"
    created_at: float = 0.0
    status: str = "unknown"  # created, running, stopped, destroyed


# ─────────────────────────────────────────────────────────────
# Core: SandboxManager
# ─────────────────────────────────────────────────────────────

class SandboxManager:
    """
    Ephemeral Docker sandbox for coding agent execution.

    Lifecycle:
        1. verify_docker()       — Check Docker is available
        2. create(project_path)  — Spin up container with workspace mounted
        3. exec_command(cmd)     — Execute shell commands inside the sandbox
        4. read_file(path)       — Read files from inside the sandbox
        5. write_file(path, content) — Write files inside the sandbox
        6. destroy()             — Tear down the container

    The manager tracks one active container at a time. Call destroy()
    before creating a new one, or use the async context manager.
    """

    def __init__(self):
        self._docker_cmd: str = "docker"
        self._active: Optional[SandboxInfo] = None
        self._verified: bool = False

    @property
    def active_sandbox(self) -> Optional[SandboxInfo]:
        """Return info about the currently active sandbox, or None."""
        return self._active

    @property
    def is_running(self) -> bool:
        """Check if there's an active sandbox."""
        return self._active is not None and self._active.status == "running"

    # ── Docker Verification ──────────────────────────────────

    async def verify_docker(self) -> bool:
        """
        Verify Docker CLI is available and the daemon is running.
        Raises DockerNotAvailableError if not.
        """
        if self._verified:
            return True

        # Check docker binary exists
        docker_path = shutil.which("docker")
        if not docker_path:
            raise DockerNotAvailableError(
                "Docker CLI not found on PATH. Install Docker Desktop: "
                "https://docs.docker.com/get-docker/"
            )

        # Check daemon is responsive
        try:
            result = await self._run_docker(["info", "--format", "{{.ServerVersion}}"], timeout=10)
            if result.exit_code != 0:
                raise DockerNotAvailableError(
                    f"Docker daemon not running. Start Docker Desktop.\n"
                    f"stderr: {result.stderr[:200]}"
                )
            logger.info("Docker verified: version %s", result.stdout.strip())
            self._verified = True
            return True
        except asyncio.TimeoutError:
            raise DockerNotAvailableError("Docker daemon not responding (timeout 10s)")

    # ── Container Lifecycle ──────────────────────────────────

    async def create(
        self,
        project_path: str,
        image: Optional[str] = None,
        memory_limit: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> SandboxInfo:
        """
        Spin up an ephemeral Docker container with the project workspace mounted.

        Args:
            project_path: Absolute path to the project directory on the host.
            image: Docker image to use (default: config.SANDBOX_IMAGE).
            memory_limit: Memory limit (e.g., "2g"). Default: config.SANDBOX_MEMORY_LIMIT.
            extra_env: Additional environment variables to pass into the container.

        Returns:
            SandboxInfo with container details.
        """
        await self.verify_docker()

        # Destroy any existing sandbox first
        if self._active and self._active.status == "running":
            logger.warning("Destroying existing sandbox before creating a new one")
            await self.destroy()

        image = image or getattr(config, "SANDBOX_IMAGE", "python:3.11-slim")
        memory_limit = memory_limit or getattr(config, "SANDBOX_MEMORY_LIMIT", "2g")
        workspace_path = getattr(config, "SANDBOX_WORKSPACE_PATH", "/workspace")

        container_name = f"supervisor-sandbox-{uuid.uuid4().hex[:8]}"

        # Normalize project path for Docker (Windows → forward slashes)
        host_path = str(Path(project_path).resolve())
        if os.name == "nt":
            # Docker Desktop on Windows needs forward-slash paths
            host_path = host_path.replace("\\", "/")

        # Build docker run command
        cmd = [
            "run", "-d",
            "--name", container_name,
            "--memory", memory_limit,
            "-v", f"{host_path}:{workspace_path}",
            "-w", workspace_path,
            "--network", "host",  # Allow container to access host services
        ]

        # Add environment variables
        env_vars = extra_env or {}
        # Pass through Gemini API key if available
        gemini_key = os.getenv("GEMINI_API_KEY", "")
        if gemini_key:
            env_vars["GEMINI_API_KEY"] = gemini_key

        for key, val in env_vars.items():
            cmd.extend(["-e", f"{key}={val}"])

        # Use the image with an infinite sleep to keep the container alive
        cmd.extend([image, "sleep", "infinity"])

        logger.info("Creating sandbox: image=%s, project=%s", image, project_path)
        result = await self._run_docker(cmd, timeout=60)

        if result.exit_code != 0:
            raise SandboxError(
                f"Failed to create sandbox container.\n"
                f"Command: docker {' '.join(cmd)}\n"
                f"stderr: {result.stderr[:500]}"
            )

        container_id = result.stdout.strip()[:12]

        import time
        self._active = SandboxInfo(
            container_id=container_id,
            container_name=container_name,
            image=image,
            project_path=project_path,
            workspace_path=workspace_path,
            created_at=time.time(),
            status="running",
        )

        logger.info(
            "Sandbox created: id=%s, name=%s, image=%s",
            container_id, container_name, image,
        )
        return self._active

    async def destroy(self) -> None:
        """Tear down the active sandbox container."""
        if not self._active:
            return

        container = self._active.container_name or self._active.container_id
        logger.info("Destroying sandbox: %s", container)

        try:
            result = await self._run_docker(
                ["rm", "-f", container], timeout=30
            )
            if result.exit_code != 0:
                logger.warning(
                    "Failed to destroy sandbox %s: %s",
                    container, result.stderr[:200]
                )
        except Exception as exc:
            logger.warning("Error destroying sandbox %s: %s", container, exc)
        finally:
            self._active.status = "destroyed"
            self._active = None

    async def health_check(self) -> bool:
        """Verify the active sandbox container is still running."""
        if not self._active:
            return False

        container = self._active.container_name or self._active.container_id
        try:
            result = await self._run_docker(
                ["inspect", "--format", "{{.State.Running}}", container],
                timeout=5,
            )
            is_running = result.stdout.strip().lower() == "true"
            if not is_running:
                logger.warning("Sandbox %s is no longer running", container)
                self._active.status = "stopped"
            return is_running
        except Exception as exc:
            logger.warning("Sandbox health check failed: %s", exc)
            return False

    # ── Command Execution ────────────────────────────────────

    async def exec_command(
        self,
        command: str,
        timeout: Optional[int] = None,
        workdir: Optional[str] = None,
    ) -> CommandResult:
        """
        Execute a shell command inside the active sandbox.

        Args:
            command: Shell command to execute (passed to sh -c).
            timeout: Max seconds to wait. Default: config.MCP_TOOL_TIMEOUT_S.
            workdir: Working directory inside the container (default: /workspace).

        Returns:
            CommandResult with exit code, stdout, stderr.
        """
        if not self.is_running:
            raise SandboxError("No active sandbox. Call create() first.")

        timeout = timeout or getattr(config, "MCP_TOOL_TIMEOUT_S", 120)
        container = self._active.container_name or self._active.container_id

        cmd = ["exec"]
        if workdir:
            cmd.extend(["-w", workdir])
        cmd.extend([container, "sh", "-c", command])

        result = await self._run_docker(cmd, timeout=timeout)
        result.command = command
        return result

    # ── File Operations ──────────────────────────────────────

    async def read_file(self, path: str) -> str:
        """
        Read a file from inside the active sandbox.

        Args:
            path: Path relative to /workspace, or absolute path inside container.

        Returns:
            File contents as a string.

        Raises:
            SandboxError if file doesn't exist or can't be read.
        """
        full_path = self._resolve_path(path)
        result = await self.exec_command(f"cat {_shell_quote(full_path)}")
        if result.exit_code != 0:
            raise SandboxError(f"Failed to read file {path}: {result.stderr[:200]}")
        return result.stdout

    async def write_file(self, path: str, content: str) -> int:
        """
        Write content to a file inside the active sandbox.

        Args:
            path: Path relative to /workspace, or absolute path inside container.
            content: File content to write.

        Returns:
            Number of bytes written.
        """
        full_path = self._resolve_path(path)

        # Ensure parent directory exists
        parent_dir = str(Path(full_path).parent)
        await self.exec_command(f"mkdir -p {_shell_quote(parent_dir)}")

        # Write via stdin pipe to handle arbitrary content safely
        if not self.is_running:
            raise SandboxError("No active sandbox. Call create() first.")

        container = self._active.container_name or self._active.container_id
        timeout = getattr(config, "MCP_TOOL_TIMEOUT_S", 120)

        proc = await asyncio.create_subprocess_exec(
            self._docker_cmd, "exec", "-i", container,
            "tee", full_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(content.encode("utf-8")),
                timeout=timeout,
            )
            if proc.returncode != 0:
                raise SandboxError(
                    f"Failed to write file {path}: {stderr.decode('utf-8', errors='replace')[:200]}"
                )
            return len(content.encode("utf-8"))
        except asyncio.TimeoutError:
            proc.kill()
            raise SandboxTimeoutError(f"Timeout writing file {path}")

    async def file_exists(self, path: str) -> bool:
        """Check if a file exists inside the sandbox."""
        full_path = self._resolve_path(path)
        result = await self.exec_command(f"test -f {_shell_quote(full_path)} && echo yes || echo no")
        return result.stdout.strip() == "yes"

    async def list_files(self, directory: str = ".", max_depth: int = 3) -> list[str]:
        """
        List files in a directory inside the sandbox.

        Returns a list of relative file paths.
        """
        full_path = self._resolve_path(directory)
        result = await self.exec_command(
            f"find {_shell_quote(full_path)} -maxdepth {max_depth} -type f "
            f"! -path '*/node_modules/*' ! -path '*/.git/*' ! -path '*/__pycache__/*' "
            f"| head -500"
        )
        if result.exit_code != 0:
            return []
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

    # ── Process Management ───────────────────────────────────

    async def list_processes(self) -> list[dict]:
        """List running processes inside the sandbox."""
        result = await self.exec_command("ps aux --no-headers 2>/dev/null || ps aux")
        if result.exit_code != 0:
            return []

        processes = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split(None, 10)
            if len(parts) >= 11:
                processes.append({
                    "user": parts[0],
                    "pid": parts[1],
                    "cpu": parts[2],
                    "mem": parts[3],
                    "command": parts[10],
                })
        return processes

    async def check_port(self, port: int) -> bool:
        """Check if a port is listening inside the sandbox."""
        # Try multiple methods since containers may not have all tools
        result = await self.exec_command(
            f"(ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | grep ':{port} '"
        )
        return result.exit_code == 0

    # ── Async Context Manager ────────────────────────────────

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.destroy()
        return False

    # ── Internal Helpers ─────────────────────────────────────

    def _resolve_path(self, path: str) -> str:
        """Resolve a path relative to the sandbox workspace."""
        if path.startswith("/"):
            return path
        workspace = self._active.workspace_path if self._active else "/workspace"
        return f"{workspace}/{path}"

    async def _run_docker(self, args: list[str], timeout: int = 30) -> CommandResult:
        """
        Run a docker CLI command and capture output.

        Returns CommandResult with exit code, stdout, stderr.
        """
        full_cmd = [self._docker_cmd] + args
        result = CommandResult()

        try:
            proc = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                result.exit_code = proc.returncode or 0
                result.stdout = stdout.decode("utf-8", errors="replace")
                result.stderr = stderr.decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                result.timed_out = True
                result.stderr = f"Command timed out after {timeout}s"

        except FileNotFoundError:
            raise DockerNotAvailableError(
                f"Docker binary not found: {self._docker_cmd}"
            )
        except Exception as exc:
            result.exit_code = -1
            result.stderr = str(exc)

        return result


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _shell_quote(s: str) -> str:
    """Simple shell quoting for paths passed to sh -c inside the container."""
    return "'" + s.replace("'", "'\\''") + "'"
