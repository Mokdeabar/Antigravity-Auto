"""
Tests for main.py split modules (V74, Audit §4.5):
  - health_diagnostics.py: HealthDiagnostics, HealthReport, HealthIssue
  - error_collector.py: ErrorCollector, RuntimeError_, ErrorSummary
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

    print("\n🧪 Running Main.py Split Unit Tests...\n")

    # ── health_diagnostics.py tests ──
    from supervisor.health_diagnostics import (
        HealthIssue, HealthReport, HealthDiagnostics,
        CODE_PATTERNS,
    )

    # Test 1: HealthIssue defaults
    hi = HealthIssue()
    test("test_issue_defaults", hi.category == "" and hi.severity == "warn")

    # Test 2: HealthIssue to_dict
    hi2 = HealthIssue(category="typescript", severity="error", file="app.tsx", line=42, message="TS2304: not found")
    d = hi2.to_dict()
    test("test_issue_to_dict", d["category"] == "typescript" and d["line"] == 42)

    # Test 3: HealthReport defaults
    hr = HealthReport()
    test("test_report_defaults", hr.healthy and hr.error_count == 0)

    # Test 4: HealthReport unhealthy with TS errors
    hr2 = HealthReport(ts_error_count=3)
    test("test_report_unhealthy_ts", not hr2.healthy)

    # Test 5: HealthReport unhealthy with build errors
    hr3 = HealthReport(build_errors=["npm ERR!"])
    test("test_report_unhealthy_build", not hr3.healthy)

    # Test 6: HealthReport error_count
    hr4 = HealthReport(issues=[
        HealthIssue(severity="error"),
        HealthIssue(severity="warn"),
        HealthIssue(severity="error"),
    ])
    test("test_report_error_count", hr4.error_count == 2)

    # Test 7: HealthReport warning_count
    test("test_report_warning_count", hr4.warning_count == 1)

    # Test 8: HealthReport summary with issues
    hr5 = HealthReport(ts_error_count=5, lint_error_count=3)
    test("test_report_summary", "5 TS" in hr5.summary() and "3 lint" in hr5.summary())

    # Test 9: HealthReport summary healthy
    hr6 = HealthReport()
    test("test_report_summary_healthy", "✅" in hr6.summary())

    # Test 10: HealthReport to_dict
    d3 = hr5.to_dict()
    test("test_report_to_dict", "ts_errors" in d3 and d3["ts_errors"] == 5)

    # Test 11: HealthReport to_markdown
    hr7 = HealthReport(issues=[HealthIssue(category="typescript", severity="error", message="TS2304")])
    md = hr7.to_markdown()
    test("test_report_markdown", "# Build Issues" in md and "TS2304" in md)

    # Test 12: HealthReport markdown no issues
    hr8 = HealthReport()
    test("test_report_markdown_clean", "No issues detected" in hr8.to_markdown())

    # Test 13: CODE_PATTERNS has entries
    test("test_code_patterns", len(CODE_PATTERNS) >= 4)

    # Test 14: CODE_PATTERNS has 'as any' detection
    as_any = [p for p in CODE_PATTERNS if "as any" in p["message"].lower()]
    test("test_code_patterns_as_any", len(as_any) == 1)

    # Test 15: HealthDiagnostics init
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        diag = HealthDiagnostics(tmpdir)
        test("test_diag_init", diag.last_report is None)

    # Test 16: generate_fix_tasks no issues
    diag2 = HealthDiagnostics(".")
    tasks = diag2.generate_fix_tasks(HealthReport())
    test("test_fix_tasks_empty", len(tasks) == 0)

    # Test 17: generate_fix_tasks with errors
    hr9 = HealthReport(ts_error_count=2, issues=[
        HealthIssue(category="typescript", severity="error", file="a.ts", message="TS2304"),
        HealthIssue(category="typescript", severity="error", file="b.ts", message="TS2551"),
    ])
    tasks2 = diag2.generate_fix_tasks(hr9)
    test("test_fix_tasks_generated", len(tasks2) == 1 and "typescript" in tasks2[0]["description"].lower())

    # ── error_collector.py tests ──
    from supervisor.error_collector import (
        RuntimeError_, ErrorSummary, ErrorCollector,
        VITE_ERROR_PATTERNS, VITE_WARNING_PATTERNS,
    )

    # Test 18: RuntimeError_ defaults
    re_ = RuntimeError_()
    test("test_runtime_error_defaults", re_.source == "" and re_.count == 1)

    # Test 19: RuntimeError_ to_dict
    re2 = RuntimeError_(source="console", message="TypeError: undefined", file="App.tsx", line=10)
    d4 = re2.to_dict()
    test("test_runtime_error_to_dict", d4["source"] == "console" and d4["line"] == 10)

    # Test 20: ErrorSummary defaults
    es = ErrorSummary()
    test("test_error_summary_defaults", es.total_errors == 0)

    # Test 21: ErrorSummary no errors summary
    test("test_error_summary_clean", "✅" in es.summary())

    # Test 22: ErrorSummary with errors
    es2 = ErrorSummary(total_errors=5, total_warnings=3, unique_errors=4, sources={"console": 5})
    test("test_error_summary_with_errors", "5 errors" in es2.summary())

    # Test 23: ErrorCollector init
    ec = ErrorCollector()
    test("test_collector_init", not ec._started)

    # Test 24: add_error new
    is_new = ec.add_error("console", "TypeError: undefined")
    test("test_add_error_new", is_new)

    # Test 25: add_error duplicate
    is_dup = ec.add_error("console", "TypeError: undefined")
    test("test_add_error_dup", not is_dup)

    # Test 26: add_error count incremented
    summary = ec.get_summary()
    test("test_error_dedup_count", summary.total_errors == 2)  # 1 original + 1 dup count

    # Test 27: unique error count
    test("test_unique_count", summary.unique_errors == 1)

    # Test 28: scan_vite_log
    ec2 = ErrorCollector()
    log = "error TS2304: Cannot find name 'foo'\nSome info line\nSyntaxError: Unexpected token"
    new = ec2.scan_vite_log(log)
    test("test_scan_vite_log", new == 2)

    # Test 29: get_recent_errors
    recent = ec2.get_recent_errors(5)
    test("test_recent_errors", len(recent) == 2)

    # Test 30: get_errors_for_prompt
    prompt_ctx = ec2.get_errors_for_prompt()
    test("test_errors_for_prompt", "RUNTIME ERRORS" in prompt_ctx and "typescript" in prompt_ctx)

    # Test 31: generate_fix_tasks
    ec3 = ErrorCollector()
    ec3.add_error("console", "TypeError: undefined", file="App.tsx")
    fix_tasks = ec3.generate_fix_tasks()
    test("test_generate_fix_tasks", len(fix_tasks) == 1 and "App.tsx" in fix_tasks[0]["description"])

    # Test 32: clear
    ec3.clear()
    test("test_clear", ec3.get_summary().total_errors == 0)

    # Test 33: VITE_ERROR_PATTERNS exists
    test("test_vite_patterns", len(VITE_ERROR_PATTERNS) >= 5)

    # Test 34: VITE_WARNING_PATTERNS exists
    test("test_vite_warning_patterns", len(VITE_WARNING_PATTERNS) >= 2)

    # Test 35: ErrorCollector max cap
    ec4 = ErrorCollector(max_errors=5)
    for i in range(10):
        ec4.add_error("test", f"Error {i}")
    test("test_max_errors_cap", len(ec4._errors) == 5)

    # ── Results ──
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {total} tests")
    if failed == 0:
        print("\n✅ All Main.py Split tests passed!")
    else:
        print(f"\n❌ {failed} test(s) failed!")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
