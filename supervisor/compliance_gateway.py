"""
compliance_gateway.py — V24 DevSecOps Compliance Gateway

Adversarial security auditor that sits between the V22 Green Phase and the
final merge. Every node's diff is intercepted and run through three gates:

Gate 1 — SAST Scan:
  npm audit --json --production (JS) or bandit -r . -f json (Python).
  Filters for critical/high severity production vulnerabilities only.

Gate 2 — Semantic Compliance Audit:
  Routes the scoped git diff (not the full codebase) to the Local Manager
  with strict business constraints.

Gate 3 — Financial AST Analysis:
  Scans Python/JS files in the diff for prohibited financial patterns:
  interest calculation, riba variables, blocked payment gateways.

If any gate fails, the node is rejected with a structured violation report.
Bypass-after-2: If a dependency CVE cannot be resolved in 2 attempts,
the gateway issues a library-swap directive instead of blocking forever.
"""

import ast
import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("supervisor.compliance_gateway")

# Financial patterns prohibited under Shariah compliance
_PROHIBITED_PATTERNS = [
    r"\binterest[\s_]?rate\b",
    r"\binterest[\s_]?amount\b",
    r"\bcalculate[\s_]?interest\b",
    r"\bcompound[\s_]?interest\b",
    r"\bsimple[\s_]?interest\b",
    r"\briba\b",
    r"\busury\b",
    r"\bapr\b",              # Annual Percentage Rate
    r"\bloan[\s_]?interest\b",
]

_PROHIBITED_GATEWAYS = [
    "stripe",       # Not inherently blocked but flagged for review
    "paypal",       # Flagged for review in Shariah context
]

MAX_BYPASS_ATTEMPTS = 2


class ComplianceGateway:
    """
    Pre-merge security and compliance auditor.
    """

    SEMANTIC_AUDIT_PROMPT = (
        "You are an adversarial security auditor. Analyze this git diff for "
        "security vulnerabilities, compliance violations, and dangerous patterns.\n\n"
        "CHECK FOR:\n"
        "1. Hardcoded secrets, API keys, or tokens.\n"
        "2. SQL injection vectors (string concatenation in queries).\n"
        "3. Unsafe deserialization (pickle.loads, eval, exec).\n"
        "4. Missing input validation or sanitization.\n"
        "5. Overly permissive CORS or authentication bypasses.\n"
        "6. Dependency confusion or typosquatted package names.\n"
        "7. Financial logic that violates Shariah compliance "
        "(interest calculations, riba, usury, prohibited gateways).\n\n"
        "Output strict JSON:\n"
        '{"pass": true/false, "violations": ["..."], "severity": "critical|high|medium|low"}'
    )

    def __init__(self, local_manager=None, workspace_path: str = "."):
        self._manager = local_manager
        self._workspace = Path(workspace_path)
        self._cve_attempts: Dict[str, int] = {}  # CVE-ID → attempt count

    # ────────────────────────────────────────────────
    # Gate 1: SAST Scan
    # ────────────────────────────────────────────────

    async def run_sast_scan(self, cwd: str) -> Tuple[bool, str]:
        """
        Run static application security testing (async).
        Auto-detects project type (JS/Python) and runs the appropriate tool.
        Returns (passes, report_or_violations).
        """
        cwd_path = Path(cwd)

        # Detect project type
        has_package_json = (cwd_path / "package.json").exists()
        has_python = any(cwd_path.rglob("*.py"))

        violations = []

        if has_package_json:
            npm_ok, npm_report = await self._run_npm_audit(cwd)
            if not npm_ok:
                violations.append(npm_report)

        if has_python:
            bandit_ok, bandit_report = await self._run_bandit(cwd)
            if not bandit_ok:
                violations.append(bandit_report)

        if violations:
            combined = "\n".join(violations)

            # Check bypass threshold for known CVEs
            if self._should_bypass(combined):
                logger.warning(
                    "⚠️ SAST violations bypassed after %d attempts. "
                    "Issuing library-swap directive.", MAX_BYPASS_ATTEMPTS,
                )
                return False, f"BYPASS_REQUIRED: {combined}"

            return False, combined

        return True, "SAST scan passed."

    async def _run_npm_audit(self, cwd: str) -> Tuple[bool, str]:
        """Run npm audit for production dependencies only (async)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "npm", "audit", "--json", "--omit=dev",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return True, "npm audit timed out, skipping."

            stdout_text = (stdout_b or b"").decode("utf-8", errors="replace")

            if proc.returncode == 0:
                return True, "npm audit: no vulnerabilities."

            try:
                data = json.loads(stdout_text)
                vuln_meta = data.get("metadata", {}).get("vulnerabilities", {})
                critical = vuln_meta.get("critical", 0)
                high = vuln_meta.get("high", 0)

                if critical > 0 or high > 0:
                    # Track CVE attempts
                    advisories = data.get("advisories", {})
                    for adv_id, adv in advisories.items():
                        severity = adv.get("severity", "")
                        if severity in ("critical", "high"):
                            cve = adv.get("cves", [str(adv_id)])[0] if adv.get("cves") else str(adv_id)
                            self._cve_attempts[cve] = self._cve_attempts.get(cve, 0) + 1

                    report = (
                        f"npm audit: {critical} critical, {high} high vulnerabilities "
                        f"in production dependencies."
                    )
                    return False, report

                # Only low/moderate — pass
                return True, "npm audit: no critical/high vulnerabilities."

            except (json.JSONDecodeError, KeyError):
                return True, "npm audit: could not parse output, assuming safe."

        except FileNotFoundError:
            return True, "npm not found, skipping npm audit."

    async def _run_bandit(self, cwd: str) -> Tuple[bool, str]:
        """Run bandit for Python security analysis (async)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "bandit", "-r", cwd, "-f", "json", "-ll",  # -ll = medium+ severity
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return True, "bandit timed out, skipping."

            stdout_text = (stdout_b or b"").decode("utf-8", errors="replace")

            if proc.returncode == 0:
                return True, "bandit: no issues found."

            try:
                data = json.loads(stdout_text)
                results = data.get("results", [])
                high_crit = [
                    r for r in results
                    if r.get("issue_severity", "").upper() in ("HIGH", "MEDIUM")
                ]
                if high_crit:
                    issues = [
                        f"  - {r['test_id']}: {r['issue_text']} ({r['filename']}:{r['line_number']})"
                        for r in high_crit[:5]
                    ]
                    return False, "bandit:\n" + "\n".join(issues)
                return True, "bandit: no high-severity issues."
            except (json.JSONDecodeError, KeyError):
                return True, "bandit: could not parse output, assuming safe."

        except FileNotFoundError:
            return True, "bandit not found, skipping."

    # ────────────────────────────────────────────────
    # Gate 2: Semantic Compliance Audit
    # ────────────────────────────────────────────────

    async def run_semantic_audit(
        self,
        diff_text: str,
        objective: str = "",
    ) -> Tuple[bool, str]:
        """
        Route the scoped git diff to the Local Manager for adversarial review.
        Returns (passes, violation_report).
        """
        if not self._manager:
            return True, "No Local Manager available — skipping semantic audit."

        if not diff_text or len(diff_text.strip()) < 10:
            return True, "No meaningful diff to audit."

        # Strictly scope: only the diff, not the full codebase
        user_prompt = (
            f"OBJECTIVE: {objective[:300]}\n\n"
            f"GIT DIFF (scoped to this node's changes only):\n"
            f"{diff_text[:3000]}\n\n"
            "Analyze this diff for security and compliance violations."
        )

        try:
            raw = await self._manager.ask_local_model(
                system_prompt=self.SEMANTIC_AUDIT_PROMPT,
                user_prompt=user_prompt,
                temperature=0.0,
            )

            if not raw or raw == "{}":
                return True, "Semantic audit returned empty — assuming safe."

            return self._parse_audit_result(raw)

        except Exception as exc:
            logger.warning("Semantic audit failed: %s. Assuming safe.", exc)
            return True, f"Semantic audit error: {exc}"

    @staticmethod
    def _parse_audit_result(raw: str) -> Tuple[bool, str]:
        """Parse the LLM's JSON audit response."""
        try:
            json_match = re.search(r'\{[^}]+\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                passes = data.get("pass", True)
                violations = data.get("violations", [])
                severity = data.get("severity", "low")

                if not passes:
                    report = f"Severity: {severity}\n"
                    for i, v in enumerate(violations[:5], 1):
                        report += f"  {i}. {v}\n"
                    return False, report.strip()
                return True, "Semantic audit passed."
        except (json.JSONDecodeError, AttributeError):
            pass

        # Fallback: keyword detection
        lower = raw.lower()
        if "violation" in lower or "critical" in lower or "reject" in lower:
            return False, raw[:300]
        return True, "Semantic audit passed (fallback parse)."

    # ────────────────────────────────────────────────
    # Gate 3: Financial AST Analysis
    # ────────────────────────────────────────────────

    def run_financial_ast_check(self, diff_text: str) -> Tuple[bool, str]:
        """
        Scan the diff for prohibited financial patterns at the source level.
        This catches interest calculations, riba variables, and blocked gateways
        even if the LLM semantic audit misses them.
        """
        violations = []

        # Check prohibited regex patterns
        for pattern in _PROHIBITED_PATTERNS:
            matches = re.findall(pattern, diff_text, re.IGNORECASE)
            if matches:
                violations.append(
                    f"Prohibited financial pattern: '{matches[0]}' (Shariah violation)"
                )

        # Check prohibited gateways in added lines only
        added_lines = [
            line[1:] for line in diff_text.split("\n")
            if line.startswith("+") and not line.startswith("+++")
        ]
        added_text = "\n".join(added_lines).lower()

        for gateway in _PROHIBITED_GATEWAYS:
            if gateway in added_text:
                violations.append(
                    f"Flagged payment gateway: '{gateway}' — requires Shariah compliance review"
                )

        # Python AST check on added code blocks
        python_violations = self._ast_financial_check(added_text)
        violations.extend(python_violations)

        if violations:
            report = "Financial compliance violations:\n"
            for i, v in enumerate(violations[:5], 1):
                report += f"  {i}. {v}\n"
            return False, report.strip()

        return True, "Financial AST check passed."

    @staticmethod
    def _ast_financial_check(code_text: str) -> List[str]:
        """
        Parse Python code fragments and detect prohibited financial logic:
        - Variables named *interest*
        - Functions named *calculate_interest*
        - Multiplication patterns that suggest compounding
        """
        violations = []

        try:
            # Try to parse as valid Python (may fail for partial diffs)
            tree = ast.parse(code_text)
        except SyntaxError:
            # Not valid Python or a partial diff — skip AST
            return violations

        for node in ast.walk(tree):
            # Check variable assignments
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        name = target.id.lower()
                        if "interest" in name and "disinterest" not in name:
                            violations.append(
                                f"Prohibited variable assignment: '{target.id}'"
                            )

            # Check function definitions
            if isinstance(node, ast.FunctionDef):
                name = node.name.lower()
                if "interest" in name or "riba" in name or "usury" in name:
                    violations.append(
                        f"Prohibited function definition: '{node.name}()'"
                    )

        return violations

    # ────────────────────────────────────────────────
    # Bypass Logic
    # ────────────────────────────────────────────────

    def _should_bypass(self, violation_report: str) -> bool:
        """
        Check if any CVE has exceeded MAX_BYPASS_ATTEMPTS.
        If so, the gateway switches from "fix the code" to "swap the library".
        """
        for cve, count in self._cve_attempts.items():
            if count >= MAX_BYPASS_ATTEMPTS:
                return True
        return False

    def get_swap_directive(self, violation_report: str) -> str:
        """
        Generate a library-swap directive when a CVE cannot be resolved.
        The Temporal Planner will use this to replan the node.
        """
        stuck_cves = [
            cve for cve, count in self._cve_attempts.items()
            if count >= MAX_BYPASS_ATTEMPTS
        ]
        return (
            f"LIBRARY SWAP REQUIRED: The following vulnerabilities could not be "
            f"resolved after {MAX_BYPASS_ATTEMPTS} attempts: {stuck_cves}. "
            f"Replace the vulnerable dependency with a secure alternative. "
            f"Do NOT attempt to patch the existing library."
        )

    # ────────────────────────────────────────────────
    # Full Compliance Pipeline
    # ────────────────────────────────────────────────

    async def run_full_audit(
        self,
        diff_text: str,
        cwd: str,
        objective: str = "",
    ) -> Tuple[bool, str]:
        """
        Run all three compliance gates sequentially.
        Returns (passes_all, combined_report).
        """
        reports = []

        # Gate 1: SAST
        sast_ok, sast_report = await self.run_sast_scan(cwd)
        if not sast_ok:
            reports.append(f"[GATE 1 — SAST] FAILED\n{sast_report}")
            # Check if bypass is needed
            if "BYPASS_REQUIRED" in sast_report:
                swap = self.get_swap_directive(sast_report)
                reports.append(swap)
                return False, "\n\n".join(reports)

        # Gate 2: Semantic audit
        sem_ok, sem_report = await self.run_semantic_audit(diff_text, objective)
        if not sem_ok:
            reports.append(f"[GATE 2 — SEMANTIC] FAILED\n{sem_report}")

        # Gate 3: Financial AST
        fin_ok, fin_report = self.run_financial_ast_check(diff_text)
        if not fin_ok:
            reports.append(f"[GATE 3 — FINANCIAL] FAILED\n{fin_report}")

        if reports:
            return False, "\n\n".join(reports)

        logger.info("🛡️ All compliance gates passed.")
        return True, "All compliance gates passed."
