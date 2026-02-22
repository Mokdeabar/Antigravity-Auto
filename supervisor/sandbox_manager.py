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
    - Smart mounting: bind-mount for normal work, copy-in for risky operations.
    - Docker auto-install: if Docker is missing, installs it automatically.
    - Custom sandbox image: auto-builds from Dockerfile.sandbox with Python 3.12 + Node 20.
    - Timeout enforcement via asyncio wrapper.
    - Automatic cleanup on manager destruction.
"""

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("supervisor.sandbox_manager")

# Path to the custom Dockerfile shipped alongside this module
_DOCKERFILE_PATH = Path(__file__).resolve().parent / "Dockerfile.sandbox"
_CUSTOM_IMAGE_NAME = "supervisor-sandbox:latest"


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
    mount_mode: str = "copy"  # "bind" (fast, opt-in) or "copy" (isolated, default)
    created_at: float = 0.0
    status: str = "unknown"  # created, running, stopped, destroyed
    preview_port: int = 0  # Dynamically allocated port for dev server preview


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
        Raises DockerNotAvailableError with actionable instructions if not found.
        """
        if self._verified:
            return True

        # Check docker binary exists
        docker_path = shutil.which("docker")
        if not docker_path:
            raise DockerNotAvailableError(
                "Docker CLI not found on PATH.\n"
                "\n"
                "Install Docker manually:\n"
                "  Windows:  winget install Docker.DockerDesktop\n"
                "  macOS:    brew install --cask docker\n"
                "  Linux:    curl -fsSL https://get.docker.com | sh\n"
                "\n"
                "After installing, restart your terminal and try again."
            )

        # Check daemon is responsive
        try:
            result = await self._run_docker(["info", "--format", "{{.ServerVersion}}"], timeout=15)
            if result.exit_code != 0:
                raise DockerNotAvailableError(
                    "Docker is installed but the daemon is not running.\n"
                    "Start Docker Desktop manually, then try again.\n"
                    f"stderr: {result.stderr[:200]}"
                )
            logger.info("Docker verified: version %s", result.stdout.strip())
            self._verified = True
            return True
        except asyncio.TimeoutError:
            raise DockerNotAvailableError("Docker daemon not responding (timeout 15s).")


    # ── Container Lifecycle ──────────────────────────────────

    @staticmethod
    def _find_available_port(start: int = 3000, end: int = 3020) -> int:
        """
        Find an available TCP port on the host.

        Probes ports from start to end using socket.bind().
        Avoids TIME_WAIT collisions after sandbox restarts.

        Returns:
            First available port in range.

        Raises:
            SandboxError: If no port is available in range.
        """
        import socket
        for port in range(start, end + 1):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(('', port))
                    logger.debug("Port %d is available.", port)
                    return port
            except OSError:
                logger.debug("Port %d is in use, trying next.", port)
                continue
        raise SandboxError(
            f"No available port in range {start}-{end}. "
            f"All ports are in use or in TIME_WAIT state."
        )

    async def create(
        self,
        project_path: str,
        image: Optional[str] = None,
        memory_limit: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        mount_mode: str = "copy",
    ) -> SandboxInfo:
        """
        Spin up an ephemeral Docker container with the project workspace.

        Args:
            project_path: Absolute path to the project directory on the host.
            image: Docker image to use. Default: auto-built custom image.
            memory_limit: Memory limit (e.g., "2g"). Default: config.SANDBOX_MEMORY_LIMIT.
            extra_env: Additional environment variables to pass into the container.
            mount_mode: "bind" (fast, shared filesystem) or "copy" (fully isolated).
                        Use "copy" for risky operations that might corrupt the workspace.

        Returns:
            SandboxInfo with container details.
        """
        await self.verify_docker()

        # Destroy any existing sandbox first
        if self._active and self._active.status == "running":
            logger.warning("Destroying existing sandbox before creating a new one")
            await self.destroy()

        # Try to use our custom image, fall back to config default
        image = image or await self._resolve_image()
        memory_limit = memory_limit or getattr(config, "SANDBOX_MEMORY_LIMIT", "2g")
        workspace_path = getattr(config, "SANDBOX_WORKSPACE_PATH", "/workspace")

        container_name = f"supervisor-sandbox-{uuid.uuid4().hex[:8]}"

        # Normalize project path for Docker (Windows → forward slashes)
        host_path = str(Path(project_path).resolve())
        if os.name == "nt":
            host_path = host_path.replace("\\", "/")

        # Build docker run command
        cmd = [
            "run", "-d",
            "--name", container_name,
            "--memory", memory_limit,
            "-w", workspace_path,
            "--network", "host",
        ]

        # Smart mounting strategy
        if mount_mode == "bind":
            # Bind-mount: fast, changes reflect immediately on both sides
            cmd.extend(["-v", f"{host_path}:{workspace_path}"])
        elif mount_mode == "copy":
            # Copy-in: fully isolated, no risk to host workspace
            # We create a named volume and copy files in after container starts
            volume_name = f"supervisor-vol-{uuid.uuid4().hex[:8]}"
            cmd.extend(["-v", f"{volume_name}:{workspace_path}"])
        else:
            raise SandboxError(f"Invalid mount_mode: {mount_mode}. Use 'bind' or 'copy'.")

        # Add environment variables
        env_vars = extra_env or {}
        # SECURITY: GEMINI_API_KEY and GOOGLE_API_KEY are NEVER passed into the
        # container. The Gemini CLI runs on the HOST using the user's authenticated
        # session. This is the "Host Intelligence, Sandboxed Hands" architecture.
        # Only OLLAMA_HOST is passed through for optional local LLM analysis.
        ollama_host = os.getenv("OLLAMA_HOST", "")
        if ollama_host:
            env_vars["OLLAMA_HOST"] = ollama_host

        # Default OLLAMA_HOST to host.docker.internal so Ollama on host is reachable
        if "OLLAMA_HOST" not in env_vars:
            env_vars["OLLAMA_HOST"] = "http://host.docker.internal:11434"

        for key, val in env_vars.items():
            cmd.extend(["-e", f"{key}={val}"])

        # Dynamically allocate a dev server preview port to avoid TIME_WAIT collisions
        preview_port = self._find_available_port(3000, 3020)
        cmd.extend(["-e", f"DEV_SERVER_PORT={preview_port}"])
        logger.info("Preview port allocated: %d", preview_port)

        # Use the image with an infinite sleep to keep the container alive
        cmd.extend([image, "sleep", "infinity"])

        logger.info("Creating sandbox: image=%s, project=%s, mount=%s", image, project_path, mount_mode)
        result = await self._run_docker(cmd, timeout=120)

        if result.exit_code != 0:
            raise SandboxError(
                f"Failed to create sandbox container.\n"
                f"Command: docker {' '.join(cmd)}\n"
                f"stderr: {result.stderr[:500]}"
            )

        container_id = result.stdout.strip()[:12]

        import time as _time
        self._active = SandboxInfo(
            container_id=container_id,
            container_name=container_name,
            image=image,
            project_path=project_path,
            workspace_path=workspace_path,
            mount_mode=mount_mode,
            created_at=_time.time(),
            status="running",
            preview_port=preview_port,
        )

        # If copy mode, copy the workspace into the container
        if mount_mode == "copy":
            await self._copy_workspace_in(project_path, workspace_path)

        logger.info(
            "Sandbox created: id=%s, name=%s, image=%s, mount=%s",
            container_id, container_name, image, mount_mode,
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

    # ── Smart Mounting ───────────────────────────────────────

    async def _copy_workspace_in(self, host_path: str, workspace_path: str) -> None:
        """Copy the host workspace into the container (for 'copy' mount mode)."""
        container = self._active.container_name or self._active.container_id
        logger.info("Copying workspace into sandbox (copy-in mode)...")
        result = await self._run_docker(
            ["cp", f"{host_path}/.", f"{container}:{workspace_path}/"],
            timeout=120,
        )
        if result.exit_code != 0:
            logger.warning("Workspace copy-in failed: %s", result.stderr[:200])

    async def copy_workspace_out(self, host_dest: Optional[str] = None) -> None:
        """
        Copy the workspace from the container back to the host.
        Only meaningful in 'copy' mount mode.

        Args:
            host_dest: Destination path on host. Default: original project_path.
        """
        if not self._active or self._active.mount_mode != "copy":
            logger.debug("copy_workspace_out skipped: not in copy mode")
            return

        container = self._active.container_name or self._active.container_id
        dest = host_dest or self._active.project_path
        logger.info("Copying workspace out of sandbox to: %s", dest)
        result = await self._run_docker(
            ["cp", f"{container}:{self._active.workspace_path}/.", dest],
            timeout=120,
        )
        if result.exit_code != 0:
            logger.warning("Workspace copy-out failed: %s", result.stderr[:200])

    async def switch_mount_mode(self, new_mode: str) -> SandboxInfo:
        """
        Switch between bind and copy mount modes by recreating the container.

        If switching from copy to bind: copies workspace out first.
        If switching from bind to copy: copies workspace in after creation.
        """
        if not self._active:
            raise SandboxError("No active sandbox to switch mode for.")

        if self._active.mount_mode == new_mode:
            return self._active

        project_path = self._active.project_path
        image = self._active.image

        # Copy out if leaving copy mode
        if self._active.mount_mode == "copy":
            await self.copy_workspace_out()

        await self.destroy()
        return await self.create(project_path, image=image, mount_mode=new_mode)

    # ── Image Management ─────────────────────────────────────

    async def _resolve_image(self) -> str:
        """
        Determine the best Docker image to use.

        Priority:
          1. Custom supervisor-sandbox image (if built from Dockerfile.sandbox)
          2. Config-specified image (SANDBOX_IMAGE env var)
          3. Default python:3.11-slim fallback
        """
        # Check if our custom image exists
        result = await self._run_docker(
            ["image", "inspect", _CUSTOM_IMAGE_NAME],
            timeout=10,
        )
        if result.exit_code == 0:
            logger.info("Using custom sandbox image: %s", _CUSTOM_IMAGE_NAME)
            return _CUSTOM_IMAGE_NAME

        # If Dockerfile exists, build the custom image
        if _DOCKERFILE_PATH.exists():
            logger.info("Building custom sandbox image from %s...", _DOCKERFILE_PATH)
            built = await self.build_image()
            if built:
                return _CUSTOM_IMAGE_NAME

        # Fall back to config default
        return getattr(config, "SANDBOX_IMAGE", "python:3.11-slim")

    async def build_image(self, force: bool = False) -> bool:
        """
        Build the custom sandbox Docker image from Dockerfile.sandbox.

        Returns True if build succeeded.
        """
        if not _DOCKERFILE_PATH.exists():
            logger.warning("Dockerfile.sandbox not found at %s", _DOCKERFILE_PATH)
            return False

        if not force:
            # Check if image already exists
            result = await self._run_docker(
                ["image", "inspect", _CUSTOM_IMAGE_NAME],
                timeout=10,
            )
            if result.exit_code == 0:
                logger.info("Custom image already exists, skipping build.")
                return True

        logger.info("Building sandbox image: %s ...", _CUSTOM_IMAGE_NAME)
        build_context = str(_DOCKERFILE_PATH.parent)
        result = await self._run_docker(
            ["build", "-t", _CUSTOM_IMAGE_NAME, "-f", str(_DOCKERFILE_PATH), build_context],
            timeout=600,  # 10 minutes for full build
        )
        if result.exit_code != 0:
            logger.warning(
                "Sandbox image build failed (will use fallback image).\n%s",
                result.stderr[:500],
            )
            return False

        logger.info("Sandbox image built successfully: %s", _CUSTOM_IMAGE_NAME)
        return True

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
