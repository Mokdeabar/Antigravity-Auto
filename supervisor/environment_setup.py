"""
V74: Environment Setup Manager (Audit §4.5 — headless_executor split)

Extracted from headless_executor.py: all environment detection, backend
discovery, service startup, dependency installation, and configuration
management logic. This module provides a clean, focused API for the
sandbox environment lifecycle.

The original HeadlessExecutor methods remain in headless_executor.py
(user instruction: do not delete dead code). New code should import
from this module for environment setup operations.

Integration points:
  - main.py: call EnvironmentSetup.bootstrap() after sandbox boot
  - headless_executor.py: can delegate to this module for env operations
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("supervisor.environment_setup")


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class BackendInfo:
    """Detected backend framework information."""
    name: str = ""               # e.g., "express", "fastapi", "flask"
    language: str = ""           # "node", "python", "ruby"
    entry_point: str = ""        # e.g., "server.js", "app.py"
    port: int = 0                # Detected or default port
    start_command: str = ""      # e.g., "node server.js"
    package_manager: str = "npm" # "npm", "yarn", "pnpm", "pip"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "language": self.language,
            "entry_point": self.entry_point,
            "port": self.port,
            "start_command": self.start_command,
            "package_manager": self.package_manager,
        }


@dataclass
class ServiceInfo:
    """Detected auxiliary service (database, cache, queue, etc.)."""
    name: str = ""          # e.g., "postgres", "redis", "mongodb"
    service_type: str = ""  # "database", "cache", "queue", "search"
    connection_url: str = ""
    port: int = 0
    detected_from: str = "" # File/config where detected

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.service_type,
            "port": self.port,
            "detected_from": self.detected_from,
        }


@dataclass
class EnvironmentSnapshot:
    """Complete snapshot of the sandbox environment state."""
    timestamp: float = 0.0
    node_version: str = ""
    npm_version: str = ""
    vite_version: str = ""
    backends: list[BackendInfo] = field(default_factory=list)
    services: list[ServiceInfo] = field(default_factory=list)
    env_vars_set: list[str] = field(default_factory=list)
    deps_installed: bool = False
    tooling_upgraded: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.deps_installed and not self.errors

    def summary(self) -> str:
        parts = [f"Node {self.node_version} | npm {self.npm_version}"]
        if self.vite_version:
            parts.append(f"Vite {self.vite_version}")
        if self.backends:
            parts.append(f"{len(self.backends)} backend(s)")
        if self.services:
            parts.append(f"{len(self.services)} service(s)")
        if self.errors:
            parts.append(f"⚠️ {len(self.errors)} error(s)")
        return " | ".join(parts)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "node_version": self.node_version,
            "npm_version": self.npm_version,
            "vite_version": self.vite_version,
            "backends": [b.to_dict() for b in self.backends],
            "services": [s.to_dict() for s in self.services],
            "deps_installed": self.deps_installed,
            "tooling_upgraded": self.tooling_upgraded,
            "errors": self.errors,
            "healthy": self.healthy,
        }


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Backend detection patterns
BACKEND_PATTERNS = {
    "express": {
        "files": ["server.js", "app.js", "index.js"],
        "deps": ["express"],
        "language": "node",
        "default_port": 3001,
    },
    "fastapi": {
        "files": ["main.py", "app.py"],
        "deps": ["fastapi", "uvicorn"],
        "language": "python",
        "default_port": 8000,
    },
    "flask": {
        "files": ["app.py", "wsgi.py"],
        "deps": ["flask"],
        "language": "python",
        "default_port": 5000,
    },
    "django": {
        "files": ["manage.py"],
        "deps": ["django"],
        "language": "python",
        "default_port": 8000,
    },
    "nestjs": {
        "files": ["src/main.ts"],
        "deps": ["@nestjs/core"],
        "language": "node",
        "default_port": 3000,
    },
}

# Service detection patterns
SERVICE_PATTERNS = {
    "postgres": {
        "env_keys": ["DATABASE_URL", "POSTGRES_URL", "PG_CONNECTION"],
        "deps": ["pg", "prisma", "sequelize", "psycopg2"],
        "type": "database",
        "default_port": 5432,
    },
    "mysql": {
        "env_keys": ["MYSQL_URL", "MYSQL_HOST"],
        "deps": ["mysql2", "mysql", "pymysql"],
        "type": "database",
        "default_port": 3306,
    },
    "mongodb": {
        "env_keys": ["MONGODB_URI", "MONGO_URL"],
        "deps": ["mongoose", "mongodb", "pymongo"],
        "type": "database",
        "default_port": 27017,
    },
    "redis": {
        "env_keys": ["REDIS_URL", "REDIS_HOST"],
        "deps": ["redis", "ioredis", "bull"],
        "type": "cache",
        "default_port": 6379,
    },
}

# Env var patterns to auto-generate safe defaults
ENV_SAFE_DEFAULTS = {
    "NODE_ENV": "development",
    "PORT": "3000",
    "DATABASE_URL": "postgresql://postgres:password@localhost:5432/dev",
    "REDIS_URL": "redis://localhost:6379",
    "JWT_SECRET": "dev-only-secret-not-for-production",
    "SESSION_SECRET": "dev-only-session-secret",
    "NEXT_PUBLIC_API_URL": "http://localhost:3001",
    "VITE_API_URL": "http://localhost:3001",
}


# ─────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────

class EnvironmentSetup:
    """
    Manages sandbox environment setup: tooling upgrades, backend detection,
    service discovery, dependency installation, and env file management.

    Usage:
        setup = EnvironmentSetup(sandbox)
        snapshot = await setup.bootstrap(workspace="/workspace")
        if snapshot.healthy:
            print("Environment ready!")
    """

    def __init__(self, sandbox):
        """
        Args:
            sandbox: SandboxManager instance with exec_command() method
        """
        self._sandbox = sandbox
        self._tooling_upgraded = False
        self._snapshot: EnvironmentSnapshot | None = None

    async def bootstrap(
        self,
        workspace: str = "/workspace",
        upgrade_tooling: bool = True,
        install_deps: bool = True,
    ) -> EnvironmentSnapshot:
        """
        Full environment bootstrap sequence.

        1. Upgrade sandbox tooling (npm, vite, typescript)
        2. Detect backends and services
        3. Ensure .env file exists
        4. Install dependencies
        5. Run DB migrations if needed

        Returns EnvironmentSnapshot with full environment state.
        """
        snapshot = EnvironmentSnapshot(timestamp=time.time())
        logger.info("🔧  [Setup] Starting environment bootstrap...")

        # Step 1: Upgrade tooling
        if upgrade_tooling:
            await self._upgrade_tooling(snapshot, workspace)

        # Step 2: Detect versions
        await self._detect_versions(snapshot)

        # Step 3: Detect backends
        snapshot.backends = await self.detect_backends(workspace)
        if snapshot.backends:
            logger.info(
                "🔧  [Setup] Detected %d backend(s): %s",
                len(snapshot.backends),
                ", ".join(b.name for b in snapshot.backends),
            )

        # Step 4: Detect services
        snapshot.services = await self.detect_services(workspace)
        if snapshot.services:
            logger.info(
                "🔧  [Setup] Detected %d service(s): %s",
                len(snapshot.services),
                ", ".join(s.name for s in snapshot.services),
            )

        # Step 5: Ensure .env
        env_vars = await self.ensure_env_file(workspace)
        snapshot.env_vars_set = env_vars

        # Step 6: Install dependencies
        if install_deps:
            success = await self.install_dependencies(workspace)
            snapshot.deps_installed = success
            if not success:
                snapshot.errors.append("Dependency installation failed")

        self._snapshot = snapshot
        logger.info("🔧  [Setup] Bootstrap complete: %s", snapshot.summary())
        return snapshot

    async def _upgrade_tooling(
        self, snapshot: EnvironmentSnapshot, workspace: str,
    ) -> None:
        """Upgrade npm, vite, typescript to latest versions."""
        if self._tooling_upgraded:
            snapshot.tooling_upgraded = True
            return

        stamp = "/tmp/.tooling_upgraded"
        try:
            check = await self._sandbox.exec_command(
                f"test -f {stamp} && cat {stamp} || echo 'MISSING'",
                timeout=5,
            )
            if (check.stdout or "").strip() not in ("", "MISSING"):
                self._tooling_upgraded = True
                snapshot.tooling_upgraded = True
                return
        except Exception:
            pass

        try:
            await self._sandbox.exec_command(
                "npm install -g npm@latest vite@latest typescript@latest "
                "--legacy-peer-deps --no-fund --no-audit 2>&1"
                " && date -u '+%Y-%m-%dT%H:%M:%SZ' > " + stamp + " || true",
                timeout=90,
            )
            self._tooling_upgraded = True
            snapshot.tooling_upgraded = True
        except Exception as exc:
            snapshot.errors.append(f"Tooling upgrade failed: {exc}")

    async def _detect_versions(self, snapshot: EnvironmentSnapshot) -> None:
        """Detect Node, npm, and Vite versions in the sandbox."""
        try:
            ver = await self._sandbox.exec_command(
                "node --version && npm --version && npx --yes vite --version 2>/dev/null || true",
                timeout=20,
            )
            lines = (ver.stdout or "").strip().splitlines()
            snapshot.node_version = lines[0] if len(lines) > 0 else "?"
            snapshot.npm_version = lines[1] if len(lines) > 1 else "?"
            snapshot.vite_version = lines[2] if len(lines) > 2 else "?"
        except Exception:
            pass

    async def detect_backends(self, workspace: str) -> list[BackendInfo]:
        """
        Scan workspace for backend frameworks.

        Checks package.json deps, requirements.txt, and common entry point files.
        """
        backends = []

        # Read package.json for Node backends
        pkg_deps = await self._read_package_deps(workspace)

        # Read requirements.txt for Python backends
        py_deps = await self._read_python_deps(workspace)

        all_deps = pkg_deps | py_deps

        for name, pattern in BACKEND_PATTERNS.items():
            # Check dependencies
            if any(dep in all_deps for dep in pattern["deps"]):
                info = BackendInfo(
                    name=name,
                    language=pattern["language"],
                    port=pattern["default_port"],
                    package_manager="pip" if pattern["language"] == "python" else "npm",
                )
                # Find entry point
                for entry in pattern["files"]:
                    try:
                        check = await self._sandbox.exec_command(
                            f"test -f {workspace}/{entry} && echo 'EXISTS'",
                            timeout=5,
                        )
                        if "EXISTS" in (check.stdout or ""):
                            info.entry_point = entry
                            break
                    except Exception:
                        pass

                # Build start command
                if info.language == "node":
                    info.start_command = f"node {info.entry_point}" if info.entry_point else f"npm start"
                elif info.language == "python":
                    if name == "fastapi":
                        info.start_command = f"uvicorn {info.entry_point.replace('.py','')}:app --host 0.0.0.0"
                    else:
                        info.start_command = f"python {info.entry_point}" if info.entry_point else "python app.py"

                backends.append(info)

        return backends

    async def detect_services(self, workspace: str) -> list[ServiceInfo]:
        """Detect auxiliary services (databases, caches, queues)."""
        services = []
        pkg_deps = await self._read_package_deps(workspace)
        py_deps = await self._read_python_deps(workspace)
        all_deps = pkg_deps | py_deps

        # Also check .env for service indicators
        env_keys = await self._read_env_keys(workspace)

        for name, pattern in SERVICE_PATTERNS.items():
            detected = False
            detected_from = ""

            # Check deps
            for dep in pattern["deps"]:
                if dep in all_deps:
                    detected = True
                    detected_from = f"dependency: {dep}"
                    break

            # Check env vars
            if not detected:
                for key in pattern["env_keys"]:
                    if key in env_keys:
                        detected = True
                        detected_from = f"env var: {key}"
                        break

            if detected:
                services.append(ServiceInfo(
                    name=name,
                    service_type=pattern["type"],
                    port=pattern["default_port"],
                    detected_from=detected_from,
                ))

        return services

    async def ensure_env_file(self, workspace: str) -> list[str]:
        """
        Ensure .env file exists with safe development defaults.

        Does NOT overwrite existing values — only fills in missing keys.
        Returns list of keys that were set.
        """
        set_keys = []

        try:
            # Read existing .env
            existing = await self._sandbox.exec_command(
                f"cat {workspace}/.env 2>/dev/null || echo ''",
                timeout=5,
            )
            existing_content = existing.stdout or ""
            existing_keys = set()
            for line in existing_content.splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    existing_keys.add(line.split("=")[0].strip())

            # Add missing safe defaults
            additions = []
            for key, default in ENV_SAFE_DEFAULTS.items():
                if key not in existing_keys:
                    additions.append(f"{key}={default}")
                    set_keys.append(key)

            if additions:
                env_block = "\n".join(additions) + "\n"
                await self._sandbox.exec_command(
                    f"echo '{env_block}' >> {workspace}/.env",
                    timeout=5,
                )
                logger.info("🔧  [Setup] Added %d env vars: %s", len(set_keys), ", ".join(set_keys))

        except Exception as exc:
            logger.debug("🔧  [Setup] Env file management error: %s", exc)

        return set_keys

    async def install_dependencies(self, workspace: str) -> bool:
        """Install project dependencies (npm/yarn/pnpm/pip)."""
        try:
            # Detect package manager
            pm = await self._detect_package_manager(workspace)

            if pm in ("npm", "yarn", "pnpm"):
                cmd = f"cd {workspace} && {pm} install --legacy-peer-deps --no-fund --no-audit 2>&1 | tail -10"
                result = await self._sandbox.exec_command(cmd, timeout=120)
                success = result.exit_code == 0
                if not success:
                    logger.warning(
                        "🔧  [Setup] %s install failed: %s",
                        pm, (result.stderr or result.stdout or "")[:200],
                    )
                return success

            elif pm == "pip":
                req_file = f"{workspace}/requirements.txt"
                cmd = f"pip install -r {req_file} 2>&1 | tail -5"
                result = await self._sandbox.exec_command(cmd, timeout=120)
                return result.exit_code == 0

            else:
                logger.info("🔧  [Setup] No package manager detected — skipping install")
                return True

        except Exception as exc:
            logger.warning("🔧  [Setup] Dependency install error: %s", exc)
            return False

    async def _detect_package_manager(self, workspace: str) -> str:
        """Detect which package manager the project uses."""
        checks = [
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("package-lock.json", "npm"),
            ("package.json", "npm"),
            ("requirements.txt", "pip"),
            ("Pipfile", "pip"),
        ]
        for filename, pm in checks:
            try:
                check = await self._sandbox.exec_command(
                    f"test -f {workspace}/{filename} && echo 'EXISTS'",
                    timeout=5,
                )
                if "EXISTS" in (check.stdout or ""):
                    return pm
            except Exception:
                pass
        return ""

    async def _read_package_deps(self, workspace: str) -> set[str]:
        """Read dependency names from package.json."""
        try:
            result = await self._sandbox.exec_command(
                f"cat {workspace}/package.json 2>/dev/null | head -c 10000",
                timeout=5,
            )
            if result.stdout:
                pkg = json.loads(result.stdout)
                deps = set(pkg.get("dependencies", {}).keys())
                deps |= set(pkg.get("devDependencies", {}).keys())
                return deps
        except Exception:
            pass
        return set()

    async def _read_python_deps(self, workspace: str) -> set[str]:
        """Read dependency names from requirements.txt."""
        try:
            result = await self._sandbox.exec_command(
                f"cat {workspace}/requirements.txt 2>/dev/null | head -100",
                timeout=5,
            )
            if result.stdout:
                deps = set()
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        # Extract package name (before ==, >=, etc.)
                        name = re.split(r'[><=!~]', line)[0].strip()
                        if name:
                            deps.add(name.lower())
                return deps
        except Exception:
            pass
        return set()

    async def _read_env_keys(self, workspace: str) -> set[str]:
        """Read existing .env variable names."""
        try:
            result = await self._sandbox.exec_command(
                f"cat {workspace}/.env 2>/dev/null | head -50",
                timeout=5,
            )
            keys = set()
            for line in (result.stdout or "").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    keys.add(line.split("=")[0].strip())
            return keys
        except Exception:
            return set()

    @property
    def snapshot(self) -> EnvironmentSnapshot | None:
        """Get the latest environment snapshot."""
        return self._snapshot
