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

from __future__ import annotations

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
    host_preview_port: int = 0  # V44: Actual host-mapped port (resolved via docker port)
    volume_name: str = ""  # V45: Named volume for copy-mode (cleaned up in destroy)
    nm_volume_name: str = ""  # V62: Named volume for node_modules isolation in bind mode


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
        self._active: SandboxInfo | None = None
        self._verified: bool = False

    @property
    def active_sandbox(self) -> SandboxInfo | None:
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

        # On Windows, ensure WSL service is configured to start on demand before checking daemon
        if sys.platform == "win32":
            try:
                await asyncio.create_subprocess_shell("sc.exe config wslservice start= demand", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            except Exception as e:
                logger.debug("Failed to set wslservice to demand start: %s", e)

        # Check daemon is responsive (retry up to 3 times for freshly-launched Docker)
        _last_err = None
        for _attempt in range(1, 4):
            try:
                result = await self._run_docker(["info", "--format", "{{.ServerVersion}}"], timeout=20)
                if result.exit_code == 0:
                    logger.info("Docker verified: version %s", result.stdout.strip())
                    self._verified = True
                    return True
                _last_err = result.stderr[:200]
                logger.info(
                    "Docker daemon not ready (attempt %d/3): %s",
                    _attempt, _last_err,
                )
            except asyncio.TimeoutError:
                _last_err = f"timeout on attempt {_attempt}/3"
                logger.info("Docker daemon timeout (attempt %d/3)", _attempt)

            if _attempt < 3:
                logger.info("Waiting 10s before retrying Docker check …")
                await asyncio.sleep(10)

        raise DockerNotAvailableError(
            "Docker is installed but the daemon is not responding.\n"
            "Start Docker Desktop manually, then try again.\n"
            f"Last error: {_last_err}"
        )


    # ── Container Lifecycle ──────────────────────────────────

    def _find_available_port(self, start: int = 3000, end: int = 3020) -> int:
        """
        V37 FIX (M-1): Return a preferred port for container-side binding.
        The actual host port is auto-allocated by Docker via `-p 0:<port>`
        to avoid the TOCTOU race between socket.close() and Docker bind.
        The host-side port is resolved after container start via `docker port`.
        """
        # Just return the start of the range as the container-internal port.
        # Docker will handle host-port allocation with -p 0:<port>.
        return start

    async def create(
        self,
        project_path: str,
        image: str | None = None,
        memory_limit: str | None = None,
        extra_env: dict[str, str] | None = None,
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
            # V37 SECURITY FIX (C-3): Replace --network host with explicit port
            # forwarding. Uses Docker's default bridge network instead of host.
            # NOTE: --network none is NOT used here because it silently disables
            # -p port publishing and --add-host, breaking the preview server and
            # Ollama communication. Shadow containers use --network none instead.
            "--add-host", "host.docker.internal:host-gateway",
        ]

        # Smart mounting strategy
        volume_name = ""  # V45: copy-mode workspace volume
        nm_volume_name = ""  # V62: node_modules isolation volume (bind mode only)
        if mount_mode == "bind":
            # Bind-mount: fast, changes reflect immediately on both sides
            cmd.extend(["-v", f"{host_path}:{workspace_path}"])
            # V62: Overlay node_modules with a named Docker volume.
            # This masks the host's Windows-built node_modules from the Linux
            # container, preventing cross-platform binary corruption (the root
            # cause of 'Cannot find module .../vite/dist/node/chunks/...' errors).
            # The container gets its own clean node_modules built via npm install.
            nm_volume_name = f"supervisor-nm-{uuid.uuid4().hex[:8]}"
            cmd.extend(["-v", f"{nm_volume_name}:{workspace_path}/node_modules"])
            logger.info("📦  [Sandbox] node_modules isolated via volume: %s", nm_volume_name)
        elif mount_mode == "copy":
            # Copy-in: fully isolated, no risk to host workspace
            # We create a named volume and copy files in after container starts
            volume_name = f"supervisor-vol-{uuid.uuid4().hex[:8]}"
            cmd.extend(["-v", f"{volume_name}:{workspace_path}"])
        else:
            raise SandboxError(f"Invalid mount_mode: {mount_mode}. Use 'bind' or 'copy'.")

        # Add environment variables
        env_vars = extra_env or {}

        # V37/V74 SECURITY FIX (M-2): Credential blocklist enforcement.
        # GEMINI_API_KEY and other secrets are NEVER passed into the container.
        # The Gemini CLI runs on the HOST using the user's authenticated session.

        # Exact-match blocklist for known high-value keys
        _ENV_BLOCKLIST = {
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
            "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN",
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_KEY",
            "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN",
            # V74: Connection strings and service tokens (regex won't catch these)
            "DATABASE_URL", "REDIS_URL", "MONGODB_URI",
            "NPM_TOKEN", "NPM_AUTH",
            "DOCKER_PASSWORD", "DOCKER_AUTH_TOKEN",
        }

        # V74: Regex patterns for credential-like env var names.
        # Catches variations like DB_PASSWORD, SECRET_KEY_BASE, MY_API_TOKEN, etc.
        import re as _re
        _CREDENTIAL_PATTERNS = [
            _re.compile(r"(?:^|_)(PASSWORD|PASSWD|SECRET|PRIVATE_KEY)(?:$|_)", _re.IGNORECASE),
            _re.compile(r"(?:^|_)(API_KEY|API_TOKEN|AUTH_TOKEN|ACCESS_TOKEN)(?:$|_)", _re.IGNORECASE),
            _re.compile(r"(?:^|_)(CREDENTIALS?|CLIENT_SECRET)(?:$|_)", _re.IGNORECASE),
        ]

        blocked_keys = []
        for env_key in list(env_vars.keys()):
            if env_key in _ENV_BLOCKLIST:
                blocked_keys.append(env_key)
                continue
            for pattern in _CREDENTIAL_PATTERNS:
                if pattern.search(env_key):
                    blocked_keys.append(env_key)
                    break

        for blocked_key in blocked_keys:
            logger.warning("🛡️  Blocked credential '%s' from entering sandbox.", blocked_key)
            del env_vars[blocked_key]

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
        # V37 FIX (M-1): Let Docker auto-pick the host port to avoid TOCTOU race.
        # Use `-p 0:{port}` so Docker allocates a free host port dynamically.
        cmd.extend(["-p", f"0:{preview_port}"])
        cmd.extend(["-e", f"DEV_SERVER_PORT={preview_port}"])
        logger.info("Preview port (container-side): %d (host auto-assigned)", preview_port)

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
            volume_name=volume_name if mount_mode == "copy" else "",
            nm_volume_name=nm_volume_name,
        )

        # If copy mode, copy the workspace into the container
        if mount_mode == "copy":
            await self._copy_workspace_in(project_path, workspace_path)

        # V62: In bind mode, the node_modules volume starts empty.
        # Run npm install inside the container to build Linux-native dependencies.
        # This reuses the battle-tested _container_npm_install() from copy mode.
        if mount_mode == "bind":
            logger.info("📦  [Sandbox] Installing node_modules inside container (bind-mode isolation) …")
            await self._container_npm_install(container_name, workspace_path)

        logger.info(
            "Sandbox created: id=%s, name=%s, image=%s, mount=%s",
            container_id, container_name, image, mount_mode,
        )

        # V44 FIX: Resolve the actual host-mapped port via `docker port`.
        # Docker used `-p 0:<port>` so it auto-assigned a random host port.
        # The iframe needs the HOST port, not the container-side port.
        host_port = await self.resolve_host_port(preview_port)
        if host_port:
            self._active.host_preview_port = host_port
            logger.info("Preview port resolved: container=%d → host=%d", preview_port, host_port)
        else:
            self._active.host_preview_port = 0
            logger.warning("Could not resolve host preview port for container port %d", preview_port)

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
            # V45: Remove the named volume if it was used (copy mode)
            if self._active and self._active.volume_name:
                try:
                    vol_result = await self._run_docker(
                        ["volume", "rm", "-f", self._active.volume_name], timeout=15
                    )
                    if vol_result.exit_code == 0:
                        logger.info("🗑️  Removed volume: %s", self._active.volume_name)
                    else:
                        logger.debug("Volume removal warning: %s", vol_result.stderr[:100])
                except Exception as vol_exc:
                    logger.debug("Volume cleanup error: %s", vol_exc)
            # V62: Remove the node_modules isolation volume (bind mode)
            if self._active and self._active.nm_volume_name:
                try:
                    nm_result = await self._run_docker(
                        ["volume", "rm", "-f", self._active.nm_volume_name], timeout=15
                    )
                    if nm_result.exit_code == 0:
                        logger.info("🗑️  Removed nm volume: %s", self._active.nm_volume_name)
                    else:
                        logger.debug("nm volume removal warning: %s", nm_result.stderr[:100])
                except Exception as nm_exc:
                    logger.debug("nm volume cleanup error: %s", nm_exc)
            self._active.status = "destroyed"
            self._active = None

    async def cleanup_stale_volumes(self) -> int:
        """
        V54: Remove all orphaned supervisor-vol-* named volumes in parallel.
        Called at boot and shutdown to reclaim disk from crashed sessions.
        Returns the number of volumes removed.
        """
        try:
            # Clean up both copy-mode volumes (supervisor-vol-*) and
            # V62 node_modules isolation volumes (supervisor-nm-*)
            result = await self._run_docker(
                ["volume", "ls", "-q", "--filter", "name=supervisor-"], timeout=10
            )
            if result.exit_code != 0 or not result.stdout.strip():
                return 0

            volumes = [v.strip() for v in result.stdout.strip().splitlines() if v.strip()]
            if not volumes:
                return 0

            async def _rm_one(vol: str) -> bool:
                try:
                    rm_result = await self._run_docker(
                        ["volume", "rm", "-f", vol], timeout=10
                    )
                    return rm_result.exit_code == 0
                except Exception:
                    return False  # Volume still in use by active session

            results = await asyncio.gather(*[_rm_one(v) for v in volumes], return_exceptions=True)
            removed = sum(1 for r in results if r is True)

            skipped = len(volumes) - removed
            if removed:
                logger.info("🗑️  Cleaned up %d stale volume(s) (%d skipped — still mounted)",
                            removed, skipped)
            elif skipped:
                logger.debug("🗑️  Volume cleanup: all %d supervisor volume(s) still in use", skipped)
            return removed
        except Exception as exc:
            logger.debug("Volume cleanup error: %s", exc)
            return 0

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

    # ── File Sync (copy mode) ────────────────────────────────

    async def sync_files_to_sandbox(self, host_project_path: str, auto_install: bool = False) -> bool:
        """
        Re-copy host project files into the sandbox container.

        Only needed in 'copy' mount mode — in 'bind' mode the filesystem
        is shared and changes are immediately visible.

        V54 FIX: Excludes node_modules, .git, dist, .next, __pycache__, and
        other heavy/auto-generated dirs from the copy. This prevents the host's
        (potentially corrupted or Windows-built) node_modules from being copied
        into the Linux container — which was the root cause of vite chunk errors.

        If auto_install=True (set on first sandbox creation), runs a fresh
        npm install inside the container after the copy so node_modules is always
        built from scratch in the correct Linux environment.

        Returns:
            True if sync succeeded, False otherwise.
        """
        if not self.is_running:
            return False
        if self._active.mount_mode == "bind":
            return True  # No sync needed

        container = self._active.container_name or self._active.container_id
        workspace = self._active.workspace_path or "/workspace"

        # Directories that should NEVER be copied from host → container.
        # node_modules must be built fresh inside the Linux container.
        _EXCLUDED = [
            "node_modules", ".git", "dist", "build", "out", ".next", ".nuxt",
            "__pycache__", ".venv", "venv", ".cache", ".turbo",
            ".vite",          # Top-level Vite cache (separate from node_modules/.vite)
            "coverage",       # Test coverage output
            "storybook-static",  # Storybook build output
            ".expo",          # Expo / React Native
            ".svelte-kit",    # SvelteKit
            ".parcel-cache",  # Parcel bundler cache
            "*.pyc", ".DS_Store",
        ]

        try:
            host_path = str(Path(host_project_path).resolve())

            # Use tar (available on both Linux host and in WSL/Git Bash) piped through
            # docker exec to stream files without copying excluded dirs.
            # Fall back to docker cp if tar is unavailable.
            exclude_args = " ".join(f"--exclude=./{d}" for d in _EXCLUDED)

            if os.name == "nt":
                # On Windows we can't pipe tar → docker exec natively in the same way.
                # Strategy: docker cp everything, then remove excluded dirs inside container.
                cp_cmd = ["cp", f"{host_path}{os.sep}.", f"{container}:{workspace}/"]
                result = await self._run_docker(cp_cmd, timeout=120)
                if result.exit_code != 0:
                    logger.warning("File sync failed: %s", result.stderr[:200])
                    return False

                # Remove ALL volatile/auto-generated dirs that shouldn't be in the container.
                # Comprehensively listed here to close the contamination window where
                # docker cp copies everything first and then we clean up.
                _VOLATILE = [
                    "node_modules", ".git", "dist", "build", "out",
                    ".next", ".nuxt", "__pycache__", ".venv", "venv",
                    ".cache", ".turbo", ".vite", "coverage",
                    "storybook-static", ".expo", ".svelte-kit", ".parcel-cache",
                ]
                _rm_dirs = " ".join(f'"{workspace}/{d}"' for d in _VOLATILE)
                await self._run_docker(
                    ["exec", "-u", "root", container, "bash", "-c",
                     f"rm -rf {_rm_dirs} 2>/dev/null; true"],
                    timeout=30,
                )
            else:
                # Linux/Mac: stream tar excluding heavy dirs via stdin pipe
                tar_cmd = (
                    f"tar -C {host_path!r} {exclude_args} -cf - . "
                    f"| docker exec -i {container} tar -C {workspace} -xf -"
                )
                import subprocess as _sp
                proc = await asyncio.create_subprocess_shell(
                    tar_cmd,
                    stdout=_sp.PIPE, stderr=_sp.PIPE,
                )
                _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode != 0:
                    logger.warning("File sync (tar) failed: %s", err.decode()[:200])
                    # Fall back to docker cp + cleanup
                    await self._run_docker(
                        ["cp", f"{host_path}/.", f"{container}:{workspace}/"], timeout=120
                    )

            # Fix ownership
            chown_cmd = ["exec", "-u", "root", container, "chown", "-R", "sandbox:sandbox", workspace]
            await self._run_docker(chown_cmd, timeout=30)

            logger.info("📦  [Sync] Files synced to sandbox (%s) — node_modules excluded", container[:12])

            # ── Post-sync npm install ——————————————————————————————─
            # V55: On Windows, node_modules is always wiped after docker cp.
            # Trigger a fast --prefer-offline reinstall immediately rather than
            # waiting for _build_health_check to notice it's missing (30-60s later).
            # The npm cache (~/.npm) survives the workspace wipe so this is ~5-10s.
            if auto_install:
                await self._container_npm_install(container, workspace)
            elif os.name == "nt":
                # Re-sync (not first boot): npm cache is warm — restore node_modules fast.
                pkg_chk = await self._run_docker(
                    ["exec", container, "test", "-f", f"{workspace}/package.json"],
                    timeout=5,
                )
                if pkg_chk.exit_code == 0:
                    logger.info("📦  [Sync] Re-installing node_modules after sync wipe (prefer-offline) …")
                    self._nm_install_in_progress = True
                    try:
                        _nm_res = await self._run_docker(
                            ["exec", "-w", workspace, "-u", "sandbox",
                             "-e", "NO_UPDATE_NOTIFIER=1",
                             container, "sh", "-c",
                             "npm install --prefer-offline --no-audit --no-fund "
                             "--legacy-peer-deps --loglevel=error 2>&1 | tail -5"],
                            timeout=120,
                        )
                        if _nm_res.exit_code == 0:
                            logger.info("📦  [Sync] node_modules restored via prefer-offline ✅")
                        else:
                            # Cache was cold — fall back to full install
                            logger.info("📦  [Sync] prefer-offline failed — falling back to full install …")
                            await self._run_docker(
                                ["exec", "-w", workspace, "-u", "sandbox",
                                 "-e", "NO_UPDATE_NOTIFIER=1",
                                 container, "sh", "-c",
                                 "npm install --no-audit --no-fund "
                                 "--legacy-peer-deps --loglevel=error 2>&1 | tail -5"],
                                timeout=300,
                            )
                    finally:
                        self._nm_install_in_progress = False

            return True

        except Exception as exc:
            logger.warning("File sync error: %s", exc)
            return False

    async def _container_npm_install(self, container: str, workspace: str) -> None:
        """
        Run a clean npm install inside the container.
        Called after initial copy-in to ensure node_modules is built for Linux.
        Also called as recovery when vite chunk corruption is detected.

        npm version strategy (3 layers):
          1. Proactive/persistent — Dockerfile bakes `npm install -g npm@latest`
          2. Boot-time           — this function upgrades npm before each project install
          3. Reactive            — detects version-notice in output and upgrades + retries
        """
        pkg_check = await self._run_docker(
            ["exec", container, "test", "-f", f"{workspace}/package.json"],
            timeout=5,
        )
        if pkg_check.exit_code != 0:
            return  # No package.json — not a Node project

        # ── Layer 2: Boot-time npm self-upgrade (conditional) ─────────────────────
        # V68: Skip upgrade if npm is already v10+ (saves ~17s per boot).
        # The Docker image ships with modern npm; only upgrade if truly outdated.
        # V74: Version check and npmrc patch run in parallel (§3.3 optimization)
        async def _check_npm_version():
            return await self._run_docker(
                ["exec", container, "npm", "--version"],
                timeout=5,
            )

        async def _patch_npmrc():
            return await self._run_docker(
                ["exec", "-u", "root", container, "sh", "-c",
                 "printf 'update-notifier=false\\nloglevel=error\\n' >> /home/sandbox/.npmrc"],
                timeout=5,
            )

        _current_ver, _ = await asyncio.gather(_check_npm_version(), _patch_npmrc())

        _npm_ver = (_current_ver.stdout or "").strip()
        _npm_major = 0
        try:
            _npm_major = int(_npm_ver.split(".")[0])
        except (ValueError, IndexError):
            pass

        if _npm_major < 10:
            logger.info("📦  [Sandbox] Upgrading npm (v%s) to latest version …", _npm_ver)
            npm_upg = await self._run_docker(
                ["exec", "-u", "root",
                 "-e", "NO_UPDATE_NOTIFIER=1",
                 container,
                 "npm", "install", "-g", "npm@latest", "--loglevel=error"],
                timeout=60,
            )
            _upg_ver = await self._run_docker(
                ["exec", container, "npm", "--version"],
                timeout=5,
            )
            _npm_ver = (_upg_ver.stdout or "").strip()
            if npm_upg.exit_code == 0:
                logger.info("📦  [Sandbox] npm upgraded → v%s", _npm_ver)
            else:
                logger.warning("📦  [Sandbox] npm self-upgrade had issues (non-fatal): %s",
                               (npm_upg.stderr or npm_upg.stdout or "")[-100:])
        else:
            logger.info("📦  [Sandbox] npm v%s is recent enough — skipping upgrade", _npm_ver)

        logger.info("📦  [Sandbox] Running npm install inside container (clean Linux build) …")
        # Wipe any stale node_modules first (prevents chunk corruption)
        await self._run_docker(
            ["exec", "-u", "root", container, "rm", "-rf", f"{workspace}/node_modules"],
            timeout=60,
        )

        _npm_install_cmd = [
            "exec", "-w", workspace, "-u", "sandbox",
            "-e", "NO_UPDATE_NOTIFIER=1",
            "-e", "NPM_CONFIG_UPDATE_NOTIFIER=false",
            container,
            "npm", "install",
            "--no-audit", "--no-fund", "--no-update-notifier",
            "--legacy-peer-deps",
            "--loglevel=error",
        ]
        install_result = await self._run_docker(_npm_install_cmd, timeout=300)

        # ── Layer 3: Reactive — detect version notice and upgrade + retry ──────
        _install_out = (install_result.stdout or "") + (install_result.stderr or "")
        if "npm notice" in _install_out.lower() and (
            "update available" in _install_out.lower()
            or "new major version" in _install_out.lower()
        ):
            logger.info("📦  [Sandbox] Detected npm version nag — upgrading npm and retrying …")
            await self._run_docker(
                ["exec", "-u", "root", "-e", "NO_UPDATE_NOTIFIER=1",
                 container, "npm", "install", "-g", "npm@latest", "--loglevel=error"],
                timeout=60,
            )
            # Retry project install with freshly upgraded npm
            install_result = await self._run_docker(_npm_install_cmd, timeout=300)

        if install_result.exit_code == 0:
            logger.info("📦  [Sandbox] npm install completed successfully")

            # ── Vite chunk integrity probe ────────────────────────────────────
            # The classic error: "Cannot find module .../chunks/dep-XXXXX.js"
            # means npm linked vite but individual chunk files are missing/broken
            # (happens when a partial Windows-built node_modules leaks in, or
            # when install is interrupted). Detect it NOW before the dev server
            # tries to start and fails several minutes deep into execution.
            vite_check = await self._run_docker(
                ["exec", container, "sh", "-c",
                 f"cd {workspace} && node -e \"require('vite')\" 2>&1 | head -5"],
                timeout=15,
            )
            _vc_out = (vite_check.stdout or "").lower()
            if "cannot find module" in _vc_out and "chunk" in _vc_out:
                logger.warning(
                    "📦  [Sandbox] ⚠️  Vite chunk corruption detected after install — "
                    "wiping node_modules and reinstalling …"
                )
                await self._run_docker(
                    ["exec", "-u", "root", container, "rm", "-rf", f"{workspace}/node_modules"],
                    timeout=60,
                )
                reinstall = await self._run_docker(
                    ["exec", "-w", workspace, "-u", "sandbox",
                     "-e", "NO_UPDATE_NOTIFIER=1",
                     "-e", "NPM_CONFIG_UPDATE_NOTIFIER=false",
                     container,
                     "npm", "install",
                     "--no-audit", "--no-fund", "--no-update-notifier",
                     "--legacy-peer-deps", "--loglevel=error"],
                    timeout=300,
                )
                if reinstall.exit_code == 0:
                    logger.info("📦  [Sandbox] ✅ Vite chunk repair: clean reinstall succeeded")
                else:
                    logger.warning(
                        "📦  [Sandbox] Vite chunk repair reinstall had issues: %s",
                        (reinstall.stdout or reinstall.stderr or "")[-200:],
                    )
            elif vite_check.exit_code == 0:
                logger.info("📦  [Sandbox] ✅ Vite chunk integrity probe passed")
            # else: vite not installed in this project — fine, skip silently

        else:
            logger.warning(
                "📦  [Sandbox] npm install had issues: %s",
                (install_result.stdout or install_result.stderr or "")[-300:],
            )


    async def sync_changed_files(
        self,
        host_project_path: str,
        changed_files: list[str],
    ) -> bool:
        """
        V53: Sync only the specific files changed by a task into the container.

        Much faster than a full copy — only copies the files that were actually
        modified. After copying, touches file timestamps inside the container so
        Vite/Next.js HMR inotify watchers detect the changes and hot-reload.

        Falls back to full sync_files_to_sandbox() if changed_files is empty.

        Args:
            host_project_path: Absolute path to the project root on the host.
            changed_files: List of relative or absolute file paths that changed.

        Returns:
            True if sync succeeded, False otherwise.
        """
        if not self.is_running:
            return False
        if self._active.mount_mode == "bind":
            return True  # Bind mount: host and container share the same FS

        if not changed_files:
            return await self.sync_files_to_sandbox(host_project_path)

        container = self._active.container_name or self._active.container_id
        workspace = self._active.workspace_path or "/workspace"
        project_root = Path(host_project_path).resolve()

        # Binary/media extensions that are safe to skip — they can't cause
        # syntax errors and their tar-pipe errors abort the whole copy batch.
        _SKIP_EXTENSIONS = {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg",
            ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg",
            ".woff", ".woff2", ".ttf", ".eot", ".otf",
            ".pdf", ".zip", ".tar", ".gz", ".ico",
        }
        _MAX_BINARY_BYTES = 5 * 1024 * 1024  # skip binaries > 5 MB

        # Directory segments that must NEVER be synced — same list as full sync.
        # This catches .git objects, node_modules stragglers, build artefacts, etc.
        # that Gemini occasionally reports as "changed files".
        _EXCLUDED_DIRS = {
            ".git", "node_modules", "dist", "build", "out", ".next", ".nuxt",
            "__pycache__", ".venv", "venv", ".cache", ".turbo",
            ".vite", "coverage", "storybook-static", ".expo", ".svelte-kit", ".parcel-cache",
            ".ag-supervisor", ".ag-brain",
        }

        succeeded = 0
        failed = 0
        skipped_count = 0
        touched: list[str] = []
        failed_paths: list[str] = []

        for rel_or_abs in changed_files:
            try:
                # Normalise to an absolute host path
                p = Path(rel_or_abs)
                if not p.is_absolute():
                    p = project_root / p
                p = p.resolve()

                if not p.exists():
                    continue  # Deleted files — nothing to copy

                # Compute the path inside the container
                try:
                    rel = p.relative_to(project_root)
                except ValueError:
                    continue  # Outside project root — skip

                # Skip files inside excluded directories (.git, node_modules, etc.).
                # Gemini occasionally reports these as changed files; they must never
                # be synced and their pipe errors abort the whole copy batch.
                if any(part in _EXCLUDED_DIRS for part in rel.parts):
                    logger.debug("📦  [Sync] Skipping excluded path: %s", rel)
                    skipped_count += 1
                    continue

                # Skip large binary files that frequently cause tar pipe errors.
                # These files aren't needed for dev server correctness.
                if p.suffix.lower() in _SKIP_EXTENSIONS and p.stat().st_size > _MAX_BINARY_BYTES:
                    logger.debug("📦  [Sync] Skipping large binary: %s (%d KB)", rel, p.stat().st_size // 1024)
                    skipped_count += 1
                    continue

                container_path = f"{workspace}/{rel.as_posix()}"
                host_str = str(p)
                if os.name == "nt":
                    host_str = host_str.replace("\\", "/")

                # Ensure parent directory exists in container
                parent = container_path.rsplit("/", 1)[0]
                await self._run_docker(
                    ["exec", "-u", "root", container, "mkdir", "-p", parent],
                    timeout=5,
                )

                # Copy this single file (15s timeout; retry once with 30s if it fails)
                cp_result = await self._run_docker(
                    ["cp", host_str, f"{container}:{container_path}"],
                    timeout=15,
                )
                if cp_result.exit_code != 0:
                    logger.debug(
                        "📦  [Sync] First attempt failed for %s (%s) — retrying …",
                        rel, (cp_result.stderr or "").strip()[:80],
                    )
                    # Retry once with a longer timeout (helps with locked files)
                    await asyncio.sleep(0.5)
                    cp_result = await self._run_docker(
                        ["cp", host_str, f"{container}:{container_path}"],
                        timeout=30,
                    )

                if cp_result.exit_code != 0:
                    logger.warning("📦  [Sync] File sync failed for %s: %s", rel, cp_result.stderr[:100])
                    failed += 1
                    failed_paths.append(host_str)
                    continue

                # Fix ownership
                await self._run_docker(
                    ["exec", "-u", "root", container, "chown", "sandbox:sandbox", container_path],
                    timeout=5,
                )
                touched.append(container_path)
                succeeded += 1

            except Exception as exc:
                logger.debug("sync_changed_files error for %s: %s", rel_or_abs, exc)
                failed += 1

        # If every file failed (e.g. a batch poisoned by one locked binary),
        # fall back to a full workspace sync which uses docker cp on the whole dir.
        if failed > 0 and succeeded == 0:
            logger.warning(
                "📦  [Sync] All %d file(s) failed to sync — falling back to full workspace sync",
                failed,
            )
            return await self.sync_files_to_sandbox(host_project_path)

        if not touched:
            if skipped_count:
                logger.info("📦  [Sync] %d file(s) skipped (excluded dirs / large binaries)", skipped_count)
            return succeeded > 0 or failed == 0

        # Touch all successfully synced files inside the container so that
        # Vite / Next.js / webpack HMR inotify watchers see updated mtimes.
        touch_cmd = "touch " + " ".join(f'"{f}"' for f in touched)
        await self._run_docker(
            ["exec", "-u", "root", container, "sh", "-c", touch_cmd],
            timeout=10,
        )

        logger.info(
            "📦  [Sync] Synced %d file(s) to sandbox (touched for HMR) — %d failed, %d skipped",
            succeeded, failed, skipped_count,
        )
        return succeeded > 0

    # ── Command Execution ────────────────────────────────────

    async def exec_command(
        self,
        command: str,
        timeout: int | None = None,
        workdir: str | None = None,
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
            f"| head -5000"
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

    async def resolve_host_port(self, container_port: int) -> int | None:
        """
        V44: Resolve the host-mapped port for a container port.

        Docker's `-p 0:<container_port>` auto-assigns a random host port.
        This method calls `docker port` to discover the actual mapping.

        Returns:
            The host port number, or None if resolution fails.
        """
        if not self._active:
            return None
        container = self._active.container_name or self._active.container_id
        try:
            result = await self._run_docker(
                ["port", container, str(container_port)],
                timeout=5,
            )
            if result.exit_code == 0 and result.stdout.strip():
                # Output format: "0.0.0.0:49152" or ":::49152"
                port_str = result.stdout.strip().split(":")[-1]
                return int(port_str)
        except Exception as exc:
            logger.debug("Failed to resolve host port: %s", exc)
        return None

    # ── Async Context Manager ────────────────────────────────

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.destroy()
        return False

    # ── Smart Mounting ───────────────────────────────────────

    async def _copy_workspace_in(self, host_path: str, workspace_path: str) -> None:
        """
        Copy the host workspace into the container (for 'copy' mount mode).
        V54: Delegates to sync_files_to_sandbox(auto_install=True) which:
          - Excludes node_modules/.git/dist/etc. from the copy
          - Runs a clean `npm install` inside the Linux container after copy
        This prevents host-built (Windows) node_modules from corrupting the container.
        """
        logger.info("📦  [Sandbox] Copying workspace into container (copy-in mode, node_modules excluded) …")
        await self.sync_files_to_sandbox(host_path, auto_install=True)

    async def copy_workspace_out(self, host_dest: str | None = None) -> None:
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

        # V44: Remove node_modules entirely before copy-out on Windows.
        # docker cp recreates symlinks on the host, but Windows requires
        # SeCreateSymbolicLinkPrivilege which normal users don't have —
        # and the privilege error fires on ANY symlink inside node_modules,
        # not just .bin. node_modules is auto-regenerated by npm install.
        if os.name == "nt":
            await self._run_docker(
                ["exec", container, "rm", "-rf",
                 f"{self._active.workspace_path}/node_modules"],
                timeout=30,
            )

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
                try:
                    proc.kill()
                except Exception:
                    pass
                await proc.wait()
                result.timed_out = True
                result.stderr = f"Command timed out after {timeout}s"
            except asyncio.CancelledError:
                try:
                    proc.kill()
                except Exception:
                    pass
                await proc.wait()
                raise

        except FileNotFoundError:
            raise DockerNotAvailableError(
                f"Docker binary not found: {self._docker_cmd}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result.exit_code = -1
            result.stderr = str(exc)

        return result

    # ─────────────────────────────────────────────────────────────
    # V41: Shadow Containers — Per-Worker Runtime Isolation
    # ─────────────────────────────────────────────────────────────

    async def create_shadow(self, task_id: str, project_path: str) -> str | None:
        """
        Spin up an ephemeral shadow container for pre-merge validation.

        Shadow containers share the same image as the primary sandbox but
        have strict resource limits and no port bindings.  They exist only
        long enough to validate changed files (typically 5-15 seconds).

        Returns:
            Container ID string, or None if Docker is unavailable.
        """
        try:
            image = await self._resolve_image()
        except Exception:
            logger.debug("🐳  [Shadow] Cannot resolve image — falling back to host validation")
            return None

        container_name = f"shadow-{task_id.replace('/', '-')[:20]}-{uuid.uuid4().hex[:6]}"

        # Normalize project path for Docker
        host_path = str(Path(project_path).resolve())
        if os.name == "nt":
            host_path = host_path.replace("\\", "/")

        cmd = [
            "run", "-d",
            "--name", container_name,
            "--memory", "512m",
            "--stop-timeout", "1",
            "--network", "none",  # Headless validation without external network
            "-w", "/workspace",
            # Bind-mount the project read-write so we can copy changed files
            "-v", f"{host_path}:/workspace",
            image, "sleep", "infinity",
        ]

        try:
            result = await self._run_docker(cmd, timeout=30)
            if result.exit_code != 0:
                logger.debug("🐳  [Shadow] Failed to create: %s", result.stderr[:200])
                return None
            container_id = result.stdout.strip()[:12]
            logger.info("🐳  [Shadow] Created %s for task %s", container_name, task_id)
            return container_name
        except Exception as exc:
            logger.debug("🐳  [Shadow] Creation error: %s", exc)
            return None

    async def validate_in_shadow(
        self,
        container_name: str,
        changed_files: list[str],
    ) -> tuple[bool, list[str]]:
        """
        Run validation inside a shadow container.

        Checks:
          1. Python files: syntax (ast.parse) + import check
          2. JS/TS files: node --check
          3. If pytest/jest detected: run test suite

        Returns:
            (is_valid, error_messages)
        """
        errors: list[str] = []
        TIMEOUT = 15  # Hard timeout per validation command

        for rel_path in changed_files:
            if rel_path.endswith(".py"):
                # Python syntax + import check
                safe_path = _shell_quote(rel_path)
                cmd = ["exec", container_name, "sh", "-c",
                       f"timeout {TIMEOUT} python3 -c \"import ast; ast.parse(open({safe_path}).read())\" 2>&1"]
                result = await self._run_docker(cmd, timeout=TIMEOUT + 5)
                if result.exit_code != 0:
                    errors.append(f"{rel_path}: {result.stdout.strip() or result.stderr.strip()}")

            elif rel_path.endswith((".js", ".ts", ".jsx", ".tsx")):
                # Node syntax check (if node available)
                safe_path = _shell_quote(rel_path)
                cmd = ["exec", container_name, "sh", "-c",
                       f"timeout {TIMEOUT} node --check {safe_path} 2>&1 || true"]
                result = await self._run_docker(cmd, timeout=TIMEOUT + 5)
                if result.exit_code != 0 and "SyntaxError" in (result.stdout + result.stderr):
                    errors.append(f"{rel_path}: {result.stdout.strip() or result.stderr.strip()}")

        # If no file-level errors, try a broader test run (quick)
        if not errors:
            # Check for pytest
            test_cmd = ["exec", container_name, "sh", "-c",
                        f"timeout 30 python3 -m pytest --co -q 2>/dev/null && timeout 30 python3 -m pytest -x -q 2>&1 || true"]
            test_result = await self._run_docker(test_cmd, timeout=45)
            if test_result.exit_code != 0 and "FAILED" in test_result.stdout:
                errors.append(f"pytest: {test_result.stdout.strip()[-500:]}")

        is_valid = len(errors) == 0
        if is_valid:
            logger.info("🐳  [Shadow] Validation passed for %d files", len(changed_files))
        else:
            logger.warning("🐳  [Shadow] Validation FAILED: %s", errors[:3])

        return (is_valid, errors)

    async def destroy_shadow(self, container_name: str) -> None:
        """Tear down a shadow container immediately — no grace period."""
        try:
            await self._run_docker(["rm", "-f", container_name], timeout=10)
            logger.info("🐳  [Shadow] Destroyed %s", container_name)
        except Exception as exc:
            logger.debug("🐳  [Shadow] Cleanup error for %s: %s", container_name, exc)



# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _shell_quote(s: str) -> str:
    """Simple shell quoting for paths passed to sh -c inside the container."""
    return "'" + s.replace("'", "'\\''") + "'"
