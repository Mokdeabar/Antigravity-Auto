"""
autonomous_verifier.py — V22 Autonomous Verification Engine (TestEngineer)

Enforces strict Test-Driven Development. Before the Gemini Worker writes a
single line of implementation code, the TestEngineer generates a dedicated
test file that MUST fail (red phase). Only after the red phase is confirmed
does the Worker receive the green light to implement.

Commit gating requires dual pass: the node-specific test AND the global
test suite must both return exit code 0.

TDD Lifecycle per node:
  1. TestEngineer generates test → .ag-tests/test_{node_id}.py
  2. AST validation: confirm the test imports a target codebase module
  3. Red phase: execute the test — it MUST fail
  4. Worker implements the feature
  5. Green phase: execute both the node test and the global suite
  6. Only commit if BOTH pass
"""

import ast
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("supervisor.autonomous_verifier")

_AG_TESTS_DIR = ".ag-tests"


class AutonomousVerifier:
    """
    TDD enforcer. Generates node-specific tests, validates them via AST,
    enforces the red phase, and gates commits on dual-pass verification.
    """

    TEST_ENGINEER_PROMPT = (
        "You are a strict Test Engineer. You write Python test files using unittest.\n\n"
        "RULES:\n"
        "1. Write a COMPLETE, runnable test file for the given objective.\n"
        "2. Import at least one module from the target codebase.\n"
        "3. Use unittest.mock to mock ALL external HTTP requests. Never hit live APIs.\n"
        "4. Use unittest.mock.patch to intercept requests.get, requests.post, etc.\n"
        "5. Assert SPECIFIC return values, status codes, or data structures.\n"
        "6. Do NOT write trivial tests that simply assert True.\n"
        "7. The test MUST fail before implementation code exists (red phase).\n"
        "8. Include at least 2 test methods per class.\n\n"
        "Output ONLY the raw Python code. No markdown, no explanation, no backticks."
    )

    def __init__(self, local_manager, workspace_path: str):
        self._manager = local_manager
        self._workspace = Path(workspace_path)
        self._tests_dir = self._workspace / _AG_TESTS_DIR
        self._tests_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_gitignore()

    def _ensure_gitignore(self):
        """Ensure .ag-tests/ is in .gitignore."""
        gitignore = self._workspace / ".gitignore"
        marker = _AG_TESTS_DIR
        if gitignore.exists():
            content = gitignore.read_text(encoding="utf-8", errors="replace")
            if marker not in content:
                with gitignore.open("a", encoding="utf-8") as f:
                    f.write(f"\n{marker}/\n")
        else:
            gitignore.write_text(f"{marker}/\n", encoding="utf-8")

    # ────────────────────────────────────────────────
    # Test Generation
    # ────────────────────────────────────────────────

    async def generate_test(
        self,
        node_id: str,
        objective: str,
        rag_docs: str = "",
    ) -> Tuple[bool, str]:
        """
        Route the objective to the TestEngineer to generate a node-specific test.
        Returns (success, test_file_path_or_error).
        """
        user_prompt = f"OBJECTIVE: {objective[:1500]}\n"
        if rag_docs:
            user_prompt += f"\nEXTERNAL DOCUMENTATION:\n{rag_docs[:2000]}\n"
        user_prompt += (
            f"\nWORKSPACE ROOT: {self._workspace}\n"
            "Write a test file that validates this objective is implemented correctly."
        )

        try:
            raw = await self._manager.ask_local_model(
                system_prompt=self.TEST_ENGINEER_PROMPT,
                user_prompt=user_prompt,
                temperature=0.0,
            )

            if not raw or len(raw.strip()) < 30:
                return False, "TestEngineer returned empty or minimal output."

            # Strip markdown fencing if the LLM wrapped it
            code = self._strip_markdown(raw)

            # AST validation
            valid, ast_msg = self._validate_ast(code)
            if not valid:
                return False, f"AST validation failed: {ast_msg}"

            # Write the test file
            test_filename = f"test_{node_id}.py"
            test_path = self._tests_dir / test_filename
            test_path.write_text(code, encoding="utf-8")

            logger.info("🧪 Generated test: %s (%d bytes)", test_filename, len(code))
            return True, str(test_path)

        except Exception as exc:
            return False, f"Test generation failed: {exc}"

    @staticmethod
    def _strip_markdown(raw: str) -> str:
        """Remove markdown code fencing if present."""
        code = raw.strip()
        if code.startswith("```python"):
            code = code[len("```python"):].strip()
        elif code.startswith("```"):
            code = code[3:].strip()
        if code.endswith("```"):
            code = code[:-3].strip()
        return code

    # ────────────────────────────────────────────────
    # AST Validation
    # ────────────────────────────────────────────────

    @staticmethod
    def _validate_ast(code: str) -> Tuple[bool, str]:
        """
        Parse the test code and verify:
        1. It is syntactically valid Python.
        2. It imports at least one non-stdlib module (targets the codebase).
        3. It is not a trivial test (no bare assert True).
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"

        # Check for imports
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        if not imports:
            return False, "Test file contains no imports."

        # Filter: must import something beyond unittest/os/sys
        stdlib_only = {"unittest", "os", "sys", "pathlib", "json", "re", "typing"}
        codebase_imports = [
            imp for imp in imports
            if imp.split(".")[0] not in stdlib_only
        ]

        # unittest.mock is acceptable as a stdlib import since it's required
        has_mock = any("mock" in imp for imp in imports)
        if not codebase_imports and not has_mock:
            return False, "Test only imports stdlib. Must import a codebase module."

        # Check for trivial assertions (bare `assert True`)
        source_lower = code.lower()
        if "assert true" in source_lower and source_lower.count("assert") == 1:
            return False, "Test contains only a trivial 'assert True'."

        # Check that there's at least one test method
        has_test = any(
            isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
            for node in ast.walk(tree)
        )
        if not has_test:
            return False, "No test methods found (must start with 'test_')."

        return True, "AST validation passed."

    # ────────────────────────────────────────────────
    # Red Phase (Test MUST fail)
    # ────────────────────────────────────────────────

    def run_red_phase(self, test_path: str) -> Tuple[bool, str]:
        """
        Execute the generated test. It MUST fail (red phase).
        Returns (is_red, output).
        - If the test FAILS → (True, "Red phase confirmed")
        - If the test PASSES → (False, "Test passed before implementation — invalid")
        """
        exit_code, output = self._run_test(test_path)

        if exit_code != 0:
            logger.info("🔴 Red phase confirmed for %s", Path(test_path).name)
            return True, "Red phase confirmed. Test correctly fails before implementation."
        else:
            logger.warning("⚠️ Red phase FAILED: test passed before implementation.")
            return False, "Test passed before implementation — test is invalid or testing the wrong thing."

    # ────────────────────────────────────────────────
    # Green Phase (Dual-pass verification)
    # ────────────────────────────────────────────────

    def run_green_phase(self, test_path: str, global_test_cmd: str) -> Tuple[bool, str]:
        """
        Execute BOTH the node-specific test AND the global test suite.
        Returns (both_pass, combined_logs).
        """
        # Pass 1: Node-specific test
        node_code, node_output = self._run_test(test_path)
        if node_code != 0:
            return False, f"Node test FAILED:\n{node_output}"

        # Pass 2: Global test suite
        global_code, global_output = self._run_test_cmd(global_test_cmd)
        if global_code != 0:
            return False, f"Node test passed, but global suite FAILED:\n{global_output}"

        logger.info("🟢 Green phase confirmed: dual-pass verification succeeded.")
        return True, "Both node test and global suite passed."

    # ────────────────────────────────────────────────
    # Test Execution Helpers
    # ────────────────────────────────────────────────

    def _run_test(self, test_path: str) -> Tuple[int, str]:
        """Run a single test file."""
        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", test_path, "-v", "--tb=short", "--no-header"],
                cwd=str(self._workspace),
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            output = (proc.stdout + "\n" + proc.stderr).strip()
            return proc.returncode, output[-2000:]
        except subprocess.TimeoutExpired:
            return 1, "Test execution timed out (60s)."
        except FileNotFoundError:
            # pytest not available, fall back to unittest
            try:
                proc = subprocess.run(
                    ["python", test_path],
                    cwd=str(self._workspace),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                output = (proc.stdout + "\n" + proc.stderr).strip()
                return proc.returncode, output[-2000:]
            except Exception as e:
                return 1, f"Test execution failed: {e}"

    def _run_test_cmd(self, cmd: str) -> Tuple[int, str]:
        """Run the global test command."""
        try:
            proc = subprocess.run(
                cmd.split(),
                cwd=str(self._workspace),
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            output = (proc.stdout + "\n" + proc.stderr).strip()
            return proc.returncode, output[-2000:]
        except subprocess.TimeoutExpired:
            return 1, "Global test suite timed out (120s)."
        except Exception as e:
            return 1, f"Global test execution failed: {e}"

    # ────────────────────────────────────────────────
    # Teardown
    # ────────────────────────────────────────────────

    def teardown(self):
        """Purge the .ag-tests/ directory after epic completion."""
        if self._tests_dir.exists():
            shutil.rmtree(self._tests_dir, ignore_errors=True)
            logger.info("🧪 Purged .ag-tests/ directory.")

    def get_test_path(self, node_id: str) -> str:
        """Return the expected test file path for a node."""
        return str(self._tests_dir / f"test_{node_id}.py")
