"""
deployment_engine.py — V25 Autonomous Deployment Engine (OpsEngineer)

Closes the loop from code to production. After an Epic completes with all
DAG nodes merged, the OpsEngineer:

  1. Validates no secrets are committed to tracked files
  2. Provisions a staging environment via hosting CLI (Vercel/Railway/AWS)
  3. Deploys to staging and runs live health checks
  4. If healthy: promotes to production
  5. If production health fails: instant rollback to previous release
  6. If database migrations detected: pause for human approval

Safety Constraints:
  - MAX_ENVIRONMENTS = 3 per epic (prevents cost overruns)
  - Secret guard blocks .env files in tracked git files
  - Migration approval gate requires explicit human input
  - Rollback on any production health failure
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("supervisor.deployment_engine")

MAX_ENVIRONMENTS = 3
HEALTH_CHECK_TIMEOUT = 30
HEALTH_CHECK_RETRIES = 3
HEALTH_CHECK_INTERVAL = 5

# File patterns that must NEVER contain secrets
_SECRET_PATTERNS = [
    r"(?:api[_-]?key|secret[_-]?key|password|token|auth)\s*[:=]\s*['\"][^'\"]{8,}['\"]",
    r"sk_live_[a-zA-Z0-9]+",       # Stripe live keys
    r"AKIA[A-Z0-9]{16}",           # AWS access keys
    r"ghp_[a-zA-Z0-9]{36}",        # GitHub PATs
]

# Migration file indicators
_MIGRATION_INDICATORS = [
    "migration", "migrate", "alembic", "knex", "prisma/migrations",
    "schema.sql", "ALTER TABLE", "CREATE TABLE", "DROP TABLE",
]


class DeploymentEngine:
    """
    OpsEngineer persona. Handles staging provisioning, health checks,
    production promotion, and rollback.
    """

    def __init__(self, workspace_path: str):
        self._workspace = Path(workspace_path)
        self._envs_created: int = 0
        self._staging_url: str = ""
        self._production_url: str = ""
        self._previous_deployment_id: str = ""
        self._deploy_provider = os.getenv("DEPLOY_PROVIDER", "vercel")
        self._deploy_token = os.getenv("DEPLOY_TOKEN", "")

    # ────────────────────────────────────────────────
    # Interactive Deployment Confirmation (10s timer)
    # ────────────────────────────────────────────────

    def request_deploy_confirmation(self) -> Tuple[bool, str]:
        """
        Present a 10-second countdown asking the user if they want to deploy.
        If DEPLOY_TOKEN is not set, inform the user what they need.
        Returns (should_deploy, message).
        """
        print("\n" + "=" * 60)
        print("🚀  DEPLOYMENT AVAILABLE")
        print("=" * 60)

        if not self._deploy_token:
            print(f"Provider: {self._deploy_provider}")
            print("No DEPLOY_TOKEN is set.")
            print("To enable deployment, set these environment variables:")
            print(f"  DEPLOY_TOKEN=<your {self._deploy_provider} token>")
            print(f"  DEPLOY_PROVIDER={self._deploy_provider}  (vercel|railway|generic)")
            print("")

        print("Type 'yes' within 10 seconds to deploy to production.")
        print("Press Enter or wait to skip (keep local only).")
        print("=" * 60)

        # Use threading to implement a non-blocking timeout input
        result = [None]

        def _read_input():
            try:
                result[0] = input("> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                result[0] = ""

        input_thread = threading.Thread(target=_read_input, daemon=True)
        input_thread.start()

        # Countdown
        for remaining in range(10, 0, -1):
            if result[0] is not None:
                break
            if remaining % 3 == 0 or remaining <= 3:
                sys.stdout.write(f"\r  ⏳ {remaining}s remaining...  ")
                sys.stdout.flush()
            time.sleep(1)

        print()  # Clear the countdown line

        response = result[0] or ""
        if response in ("yes", "y", "deploy"):
            if not self._deploy_token:
                print("⚠️  Cannot deploy: DEPLOY_TOKEN is not set.")
                return False, "Deployment requested but DEPLOY_TOKEN is missing."
            logger.info("✅ Deployment confirmed by user.")
            return True, "User confirmed deployment."
        else:
            logger.info("⏭️ Deployment skipped — keeping local only.")
            return False, "User skipped deployment (local only)."

    # ────────────────────────────────────────────────
    # Secret Guard
    # ────────────────────────────────────────────────

    def scan_for_secrets(self) -> Tuple[bool, str]:
        """
        Scan tracked files for hardcoded secrets.
        Returns (clean, report). Blocks deployment if secrets found.
        """
        violations = []

        try:
            proc = subprocess.run(
                ["git", "ls-files"],
                cwd=str(self._workspace),
                capture_output=True,
                text=True,
                timeout=10,
            )
            tracked_files = proc.stdout.strip().split("\n") if proc.returncode == 0 else []
        except Exception:
            tracked_files = []

        for fpath in tracked_files:
            full = self._workspace / fpath

            # Block .env files in tracked files
            if fpath.endswith(".env") or "/.env" in fpath or fpath == ".env":
                violations.append(f"CRITICAL: .env file is tracked by git: {fpath}")
                continue

            # Scan source files for secret patterns
            if not full.exists() or full.stat().st_size > 500_000:
                continue
            if not any(fpath.endswith(ext) for ext in [".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml"]):
                continue

            try:
                content = full.read_text(encoding="utf-8", errors="replace")
                for pattern in _SECRET_PATTERNS:
                    matches = re.findall(pattern, content, re.IGNORECASE)
                    if matches:
                        violations.append(
                            f"Hardcoded secret in {fpath}: pattern '{matches[0][:20]}...'"
                        )
            except Exception:
                continue

        if violations:
            report = "SECRET GUARD FAILED:\n" + "\n".join(f"  {i+1}. {v}" for i, v in enumerate(violations[:5]))
            logger.error("🔒 %s", report)
            return False, report

        logger.info("🔒 Secret scan passed — no hardcoded secrets in tracked files.")
        return True, "Secret scan passed."

    # ────────────────────────────────────────────────
    # Migration Approval Gate
    # ────────────────────────────────────────────────

    def detect_migrations(self, diff_text: str = "") -> bool:
        """Check if the epic's changes include database migrations."""
        check_text = diff_text.lower()
        if not check_text:
            # Fall back to checking recent commit messages
            try:
                proc = subprocess.run(
                    ["git", "log", "--oneline", "-10"],
                    cwd=str(self._workspace),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                check_text = proc.stdout.lower() if proc.returncode == 0 else ""
            except Exception:
                return False

        return any(indicator in check_text for indicator in _MIGRATION_INDICATORS)

    def request_migration_approval(self) -> bool:
        """
        Pause and request explicit human approval for database migrations.
        Returns True if approved, False if rejected.
        """
        print("\n" + "=" * 60)
        print("⚠️  DATABASE MIGRATION DETECTED")
        print("=" * 60)
        print("The deployment includes database schema changes.")
        print("Production migrations require explicit human approval.")
        print("=" * 60)

        try:
            response = input("Approve production migration? (yes/no): ").strip().lower()
            approved = response in ("yes", "y", "approve")
            if approved:
                logger.info("✅ Migration approved by human operator.")
            else:
                logger.warning("❌ Migration rejected by human operator.")
            return approved
        except (EOFError, KeyboardInterrupt):
            logger.warning("❌ Migration approval interrupted — defaulting to reject.")
            return False

    # ────────────────────────────────────────────────
    # Environment Provisioning
    # ────────────────────────────────────────────────

    async def deploy_to_staging(self) -> Tuple[bool, str]:
        """
        Provision a staging environment and deploy the code.
        Returns (success, staging_url_or_error).
        """
        if self._envs_created >= MAX_ENVIRONMENTS:
            return False, (
                f"MAX_ENVIRONMENTS ({MAX_ENVIRONMENTS}) reached. "
                "Cannot provision more environments for this epic."
            )

        provider = self._deploy_provider

        if provider == "vercel":
            return await self._deploy_vercel(production=False)
        elif provider == "railway":
            return await self._deploy_railway(production=False)
        else:
            return await self._deploy_generic(production=False)

    async def promote_to_production(self) -> Tuple[bool, str]:
        """
        Promote the staging build to production.
        """
        provider = self._deploy_provider

        if provider == "vercel":
            return await self._deploy_vercel(production=True)
        elif provider == "railway":
            return await self._deploy_railway(production=True)
        else:
            return await self._deploy_generic(production=True)

    async def _deploy_vercel(self, production: bool = False) -> Tuple[bool, str]:
        """Deploy via Vercel CLI."""
        cmd = ["vercel", "--yes"]
        if production:
            cmd.append("--prod")
        if self._deploy_token:
            cmd.extend(["--token", self._deploy_token])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self._workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                # Extract URL from Vercel output
                url = self._extract_url(output)
                self._envs_created += 1
                if production:
                    self._production_url = url
                else:
                    self._staging_url = url
                env_type = "production" if production else "staging"
                logger.info("🚀 Vercel %s deployed: %s", env_type, url)
                return True, url
            else:
                err = stderr.decode("utf-8", errors="replace")[:300]
                return False, f"Vercel deploy failed: {err}"

        except FileNotFoundError:
            return False, "Vercel CLI not found. Install with: npm i -g vercel"
        except asyncio.TimeoutError:
            return False, "Vercel deploy timed out (120s)."
        except Exception as exc:
            return False, f"Vercel deploy error: {exc}"

    async def _deploy_railway(self, production: bool = False) -> Tuple[bool, str]:
        """Deploy via Railway CLI."""
        cmd = ["railway", "up"]
        if production:
            cmd.extend(["--environment", "production"])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self._workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                url = self._extract_url(output) or "https://staging.railway.app"
                self._envs_created += 1
                return True, url
            return False, f"Railway deploy failed: {output[:300]}"

        except FileNotFoundError:
            return False, "Railway CLI not found. Install with: npm i -g @railway/cli"
        except Exception as exc:
            return False, f"Railway deploy error: {exc}"

    async def _deploy_generic(self, production: bool = False) -> Tuple[bool, str]:
        """Generic deployment via custom script."""
        script = os.getenv("DEPLOY_SCRIPT", "")
        if not script:
            return False, "No DEPLOY_SCRIPT configured and provider is not vercel/railway."

        try:
            proc = await asyncio.create_subprocess_exec(
                *script.split(),
                cwd=str(self._workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                url = self._extract_url(output) or "http://localhost"
                self._envs_created += 1
                return True, url
            return False, f"Deploy script failed: {output[:300]}"
        except Exception as exc:
            return False, f"Deploy error: {exc}"

    @staticmethod
    def _extract_url(text: str) -> str:
        """Extract the first URL from deployment output."""
        match = re.search(r'https?://[^\s\'"]+', text)
        return match.group(0).rstrip("/") if match else ""

    # ────────────────────────────────────────────────
    # Health Checks
    # ────────────────────────────────────────────────

    async def health_check(self, url: str) -> Tuple[bool, str]:
        """
        Ping the deployed URL and verify 200 OK.
        Retries HEALTH_CHECK_RETRIES times with HEALTH_CHECK_INTERVAL delays.
        """
        import urllib.request
        import urllib.error

        for attempt in range(1, HEALTH_CHECK_RETRIES + 1):
            try:
                resp = urllib.request.urlopen(url, timeout=HEALTH_CHECK_TIMEOUT)
                if resp.status == 200:
                    logger.info("💚 Health check PASSED (%s, attempt %d)", url, attempt)
                    return True, f"Health check passed (HTTP {resp.status})."
                else:
                    logger.warning(
                        "⚠️ Health check: HTTP %d (attempt %d/%d)",
                        resp.status, attempt, HEALTH_CHECK_RETRIES,
                    )
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                logger.warning(
                    "⚠️ Health check failed (attempt %d/%d): %s",
                    attempt, HEALTH_CHECK_RETRIES, exc,
                )

            if attempt < HEALTH_CHECK_RETRIES:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

        return False, f"Health check failed after {HEALTH_CHECK_RETRIES} attempts."

    # ────────────────────────────────────────────────
    # Rollback
    # ────────────────────────────────────────────────

    async def rollback(self) -> Tuple[bool, str]:
        """
        Instantly rollback to the previous stable production release.
        """
        provider = self._deploy_provider

        if provider == "vercel":
            return await self._rollback_vercel()
        elif provider == "railway":
            return await self._rollback_railway()
        else:
            return False, "Rollback not implemented for this provider."

    async def _rollback_vercel(self) -> Tuple[bool, str]:
        """Rollback Vercel to the previous deployment."""
        try:
            # List recent deployments
            cmd = ["vercel", "ls", "--json"]
            if self._deploy_token:
                cmd.extend(["--token", self._deploy_token])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self._workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace")

            try:
                deployments = json.loads(output)
                if len(deployments) >= 2:
                    prev_url = deployments[1].get("url", "")
                    # Promote previous deployment to production
                    promote_cmd = ["vercel", "promote", prev_url, "--yes"]
                    if self._deploy_token:
                        promote_cmd.extend(["--token", self._deploy_token])

                    proc2 = await asyncio.create_subprocess_exec(
                        *promote_cmd,
                        cwd=str(self._workspace),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(proc2.communicate(), timeout=60)

                    if proc2.returncode == 0:
                        logger.info("🔄 Rolled back to previous Vercel deployment: %s", prev_url)
                        return True, f"Rolled back to: {prev_url}"
            except (json.JSONDecodeError, IndexError, KeyError):
                pass

            return False, "Could not determine previous deployment for rollback."

        except Exception as exc:
            return False, f"Vercel rollback failed: {exc}"

    async def _rollback_railway(self) -> Tuple[bool, str]:
        """Rollback Railway to the previous deployment."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "railway", "rollback",
                cwd=str(self._workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0:
                return True, "Railway rollback successful."
            return False, f"Railway rollback failed: {stdout.decode()[:200]}"
        except Exception as exc:
            return False, f"Railway rollback error: {exc}"

    # ────────────────────────────────────────────────
    # Full Deployment Pipeline
    # ────────────────────────────────────────────────

    async def deploy_epic(self, diff_text: str = "") -> Tuple[bool, str]:
        """
        Complete deployment pipeline:
        1. Secret scan
        2. Migration approval (if needed)
        3. Deploy to staging
        4. Health check staging
        5. Promote to production
        6. Health check production
        7. Rollback on failure
        """
        reports = []

        # Step 1: Secret scan
        clean, secret_report = self.scan_for_secrets()
        if not clean:
            return False, secret_report

        # Step 2: Migration check
        if self.detect_migrations(diff_text):
            approved = self.request_migration_approval()
            if not approved:
                return False, "Deployment blocked: migration approval denied."
            reports.append("Migration approved by human operator.")

        # Step 3: Deploy to staging
        print("🚀 Deploying to staging...")
        staging_ok, staging_result = await self.deploy_to_staging()
        if not staging_ok:
            return False, f"Staging deployment failed: {staging_result}"
        staging_url = staging_result
        reports.append(f"Staging deployed: {staging_url}")
        print(f"🌐 Staging URL: {staging_url}")

        # Step 4: Health check staging
        print("💚 Running staging health check...")
        health_ok, health_msg = await self.health_check(staging_url)
        if not health_ok:
            reports.append(f"Staging health failed: {health_msg}")
            return False, "\n".join(reports)
        reports.append("Staging health check passed.")

        # Step 5: Promote to production
        print("🚀 Promoting to production...")
        prod_ok, prod_result = await self.promote_to_production()
        if not prod_ok:
            return False, f"Production promotion failed: {prod_result}"
        prod_url = prod_result
        reports.append(f"Production deployed: {prod_url}")
        print(f"🌐 Production URL: {prod_url}")

        # Step 6: Health check production
        print("💚 Running production health check...")
        prod_health_ok, prod_health_msg = await self.health_check(prod_url)
        if not prod_health_ok:
            # Step 7: ROLLBACK on production failure
            print("🚨 PRODUCTION HEALTH FAILED — INITIATING ROLLBACK...")
            rb_ok, rb_msg = await self.rollback()
            reports.append(f"Production health failed. Rollback: {rb_msg}")
            return False, "\n".join(reports)

        reports.append("Production health check passed.")
        logger.info("🎉 Full deployment pipeline complete.")
        return True, "\n".join(reports)

    def get_status(self) -> Dict:
        """Return current deployment status."""
        return {
            "provider": self._deploy_provider,
            "staging_url": self._staging_url,
            "production_url": self._production_url,
            "environments_created": self._envs_created,
            "max_environments": MAX_ENVIRONMENTS,
        }
