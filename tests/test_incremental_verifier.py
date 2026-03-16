"""
Tests for IncrementalVerifier (V74, Audit §4.3)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def run_tests():
    passed = 0
    failed = 0
    total = 0

    def test(name, condition):
        nonlocal passed, failed, total
        total += 1
        if condition:
            passed += 1
            print(f"  ✅ {name} PASSED")
        else:
            failed += 1
            print(f"  ❌ {name} FAILED")

    print("\n🧪 Running IncrementalVerifier Unit Tests...\n")

    # ── Test 1: VerifyResult defaults ──
    from supervisor.incremental_verifier import VerifyResult
    r = VerifyResult()
    test("test_verify_result_defaults", r.success and r.error_count == 0 and r.mode == "incremental")

    # ── Test 2: VerifyResult with errors ──
    r2 = VerifyResult(
        success=False,
        ts_errors=["error TS2304: Cannot find name 'foo'"],
        lint_errors=["Expected indentation of 2 spaces"],
        test_failures=["FAIL src/App.test.tsx"],
    )
    test("test_verify_result_error_count", r2.error_count == 3)

    # ── Test 3: VerifyResult summary (success) ──
    r3 = VerifyResult(files_checked=5, duration_ms=120)
    test("test_verify_result_summary_success", "✅" in r3.summary() and "5 files" in r3.summary())

    # ── Test 4: VerifyResult summary (failure) ──
    r4 = VerifyResult(success=False, ts_errors=["e1", "e2"], duration_ms=300)
    test("test_verify_result_summary_failure", "❌" in r4.summary() and "2 TS errors" in r4.summary())

    # ── Test 5: IncrementalVerifier initialization ──
    from supervisor.incremental_verifier import IncrementalVerifier

    class MockSandbox:
        pass

    v = IncrementalVerifier(MockSandbox(), workspace="/workspace")
    test("test_verifier_init", v._workspace == "/workspace" and v._has_tsconfig is None)

    # ── Test 6: Path normalization — workspace prefix strip ──
    paths = v._normalize_paths([
        "/workspace/src/App.tsx",
        "/workspace/src/utils.ts",
        "src/main.ts",
    ])
    test("test_normalize_paths_workspace_strip", paths == ["src/App.tsx", "src/utils.ts", "src/main.ts"])

    # ── Test 7: Path normalization — skip node_modules and .git ──
    paths2 = v._normalize_paths([
        "node_modules/react/index.js",
        ".git/config",
        "src/App.tsx",
        "dist/bundle.js",
    ])
    test("test_normalize_paths_skip_patterns", paths2 == ["src/App.tsx"])

    # ── Test 8: Path normalization — absolute non-workspace paths skipped ──
    paths3 = v._normalize_paths(["/etc/hosts", "/usr/bin/node", "src/valid.ts"])
    test("test_normalize_paths_absolute_skipped", paths3 == ["src/valid.ts"])

    # ── Test 9: TS_EXTENSIONS detection ──
    ts_exts = IncrementalVerifier.TS_EXTENSIONS
    test("test_ts_extensions", ".ts" in ts_exts and ".tsx" in ts_exts and ".jsx" in ts_exts)

    # ── Test 10: TESTABLE_EXTENSIONS includes Python ──
    test("test_testable_extensions_python", ".py" in IncrementalVerifier.TESTABLE_EXTENSIONS)

    # ── Test 11: Reset cache ──
    v._has_tsconfig = True
    v._has_jest = True
    v._has_eslint = True
    v.reset_cache()
    test("test_reset_cache", v._has_tsconfig is None and v._has_jest is None and v._has_eslint is None)

    # ── Test 12: VerifyResult mode ──
    r_full = VerifyResult(mode="full", files_checked=-1)
    test("test_verify_result_mode_full", r_full.mode == "full" and "full" in r_full.summary())

    # ── Results ──
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {total} tests")
    if failed == 0:
        print("\n✅ All IncrementalVerifier tests passed!")
    else:
        print(f"\n❌ {failed} test(s) failed!")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
