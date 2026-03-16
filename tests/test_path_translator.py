"""
V74: PathTranslator Unit Tests.

Tests host↔sandbox path mapping, edge cases, and detection methods.
Covers audit §7.1 (P1 test categories: PathTranslator).
"""

import sys
import os
import platform
import unittest

# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from supervisor.tool_server import PathTranslator

# Use OS-appropriate test paths
IS_WINDOWS = platform.system() == "Windows"
if IS_WINDOWS:
    _HOST_ROOT = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "test_project")
else:
    _HOST_ROOT = "/tmp/test_project"


class TestPathTranslator(unittest.TestCase):
    """Test bidirectional path mapping between host and sandbox."""

    def setUp(self):
        """Create a translator with a real, resolvable host path."""
        os.makedirs(_HOST_ROOT, exist_ok=True)
        self.pt = PathTranslator(_HOST_ROOT, "/workspace")
        self._resolved_root = str(os.path.realpath(_HOST_ROOT))

    def test_host_to_sandbox_basic(self):
        """Basic host → sandbox conversion."""
        host_path = os.path.join(self._resolved_root, "src", "main.py")
        result = self.pt.host_to_sandbox(host_path)
        self.assertEqual(result, "/workspace/src/main.py")

    def test_host_to_sandbox_root(self):
        """Host root maps to sandbox root."""
        result = self.pt.host_to_sandbox(self._resolved_root)
        self.assertEqual(result, "/workspace")

    def test_host_to_sandbox_nested(self):
        """Deeply nested paths convert correctly."""
        host_path = os.path.join(self._resolved_root, "a", "b", "c", "d.ts")
        result = self.pt.host_to_sandbox(host_path)
        self.assertEqual(result, "/workspace/a/b/c/d.ts")

    def test_host_to_sandbox_unrelated_path(self):
        """Unrelated paths pass through unchanged."""
        result = self.pt.host_to_sandbox("/tmp/random/file.txt")
        self.assertEqual(result, "/tmp/random/file.txt")

    def test_sandbox_to_host_basic(self):
        """Basic sandbox → host conversion."""
        result = self.pt.sandbox_to_host("/workspace/src/main.py")
        expected = os.path.join(self._resolved_root, "src", "main.py")
        self.assertEqual(os.path.normpath(result), os.path.normpath(expected))

    def test_sandbox_to_host_root(self):
        """Sandbox root maps to host root."""
        result = self.pt.sandbox_to_host("/workspace")
        self.assertEqual(os.path.normpath(result), os.path.normpath(self._resolved_root))

    def test_sandbox_to_host_unrelated(self):
        """Non-sandbox paths pass through unchanged."""
        result = self.pt.sandbox_to_host("/usr/local/bin/node")
        self.assertEqual(result, "/usr/local/bin/node")

    def test_is_sandbox_path(self):
        """Detects sandbox paths correctly."""
        self.assertTrue(self.pt.is_sandbox_path("/workspace/src/main.py"))
        self.assertTrue(self.pt.is_sandbox_path("/workspace"))
        self.assertFalse(self.pt.is_sandbox_path(os.path.join(self._resolved_root, "src")))
        self.assertFalse(self.pt.is_sandbox_path("/tmp/test"))

    def test_is_host_path(self):
        """Detects host paths correctly."""
        self.assertTrue(self.pt.is_host_path(os.path.join(self._resolved_root, "src", "main.py")))
        self.assertTrue(self.pt.is_host_path(self._resolved_root))
        self.assertFalse(self.pt.is_host_path("/workspace/src/main.py"))
        self.assertFalse(self.pt.is_host_path("/tmp/test"))

    def test_roundtrip_host_sandbox_host(self):
        """host → sandbox → host roundtrip preserves path."""
        original = os.path.join(self._resolved_root, "src", "components", "Header.tsx")
        sandbox = self.pt.host_to_sandbox(original)
        back = self.pt.sandbox_to_host(sandbox)
        self.assertEqual(os.path.normpath(back), os.path.normpath(original))

    def test_roundtrip_sandbox_host_sandbox(self):
        """sandbox → host → sandbox roundtrip preserves path."""
        original = "/workspace/src/utils/helpers.js"
        host = self.pt.sandbox_to_host(original)
        back = self.pt.host_to_sandbox(host)
        self.assertEqual(back, original)

    def test_custom_sandbox_root(self):
        """Custom sandbox workspace root works correctly."""
        pt = PathTranslator(self._resolved_root, "/app")
        host_path = os.path.join(self._resolved_root, "index.js")
        result = pt.host_to_sandbox(host_path)
        self.assertEqual(result, "/app/index.js")
        result = pt.sandbox_to_host("/app/index.js")
        self.assertEqual(os.path.normpath(result), os.path.normpath(host_path))

    def test_properties(self):
        """host_root and sandbox_root properties work."""
        self.assertEqual(os.path.normpath(self.pt.host_root), os.path.normpath(self._resolved_root))
        self.assertEqual(self.pt.sandbox_root, "/workspace")


# ── Runner ──

if __name__ == "__main__":
    print("\n🧪 Running PathTranslator Unit Tests...\n")

    # Suppress noisy logs
    import logging
    logging.disable(logging.CRITICAL)

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestPathTranslator)

    passed = 0
    failed = 0
    total = suite.countTestCases()

    for test in suite:
        result = unittest.TestResult()
        test.run(result)
        name = str(test).split()[0]
        if result.wasSuccessful():
            print(f"  ✅ {name} PASSED")
            passed += 1
        else:
            print(f"  ❌ {name} FAILED")
            failed += 1
            for _, tb in result.failures + result.errors:
                for line in tb.strip().split("\n")[-3:]:
                    print(f"     {line}")

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {total} tests")
    if failed == 0:
        print(f"\n✅ All PathTranslator tests passed!")
    else:
        print(f"\n❌ {failed} test(s) failed!")
        sys.exit(1)
