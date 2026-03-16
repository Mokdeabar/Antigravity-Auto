"""
Tests for LighthouseRunner (V74, Audit §4.6)
"""

import sys
import os
import json
import tempfile
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

    print("\n🧪 Running LighthouseRunner Unit Tests...\n")

    from supervisor.lighthouse_runner import LighthouseResult, LighthouseRunner, TARGET_SCORES

    # ── Test 1: LighthouseResult defaults ──
    r = LighthouseResult()
    test("test_result_defaults", r.success and not r.all_passing and r.url == "")

    # ── Test 2: Result with passing scores ──
    r2 = LighthouseResult(scores={
        "performance": 95, "accessibility": 98,
        "best-practices": 92, "seo": 95,
    })
    test("test_result_all_passing", r2.all_passing)

    # ── Test 3: Result with failing scores ──
    r3 = LighthouseResult(scores={
        "performance": 45, "accessibility": 98,
        "best-practices": 92, "seo": 95,
    })
    test("test_result_failing", not r3.all_passing)

    # ── Test 4: Failing categories ──
    test("test_failing_categories", len(r3.failing_categories) == 1 and "performance" in r3.failing_categories[0])

    # ── Test 5: Summary format ──
    test("test_summary_format", "🔦" in r3.summary() and "performance" in r3.summary())

    # ── Test 6: Error summary ──
    r_err = LighthouseResult(success=False, error="Chrome not found")
    test("test_error_summary", "❌" in r_err.summary() and "Chrome" in r_err.summary())

    # ── Test 7: to_dict ──
    d = r3.to_dict()
    test("test_to_dict", "scores" in d and "metrics" in d and "failing" in d and d["all_passing"] == False)

    # ── Test 8: TARGET_SCORES exist for all 4 categories ──
    test("test_target_scores",
         all(k in TARGET_SCORES for k in ("performance", "accessibility", "best-practices", "seo")))

    # ── Test 9: _parse_report with mock data ──
    class MockSandbox:
        pass

    runner = LighthouseRunner(MockSandbox())
    mock_report = json.dumps({
        "categories": {
            "performance": {"score": 0.72},
            "accessibility": {"score": 0.95},
            "best-practices": {"score": 0.88},
            "seo": {"score": 1.0},
        },
        "audits": {
            "first-contentful-paint": {"numericValue": 2100, "score": 0.5, "title": "FCP", "displayValue": "2.1s"},
            "largest-contentful-paint": {"numericValue": 3200, "score": 0.3, "title": "LCP", "displayValue": "3.2s"},
            "total-blocking-time": {"numericValue": 350, "score": 0.4, "title": "TBT", "displayValue": "350ms"},
            "cumulative-layout-shift": {"numericValue": 0.05, "score": 0.9, "title": "CLS", "displayValue": "0.05"},
            "speed-index": {"numericValue": 3800, "score": 0.5, "title": "Speed Index", "displayValue": "3.8s"},
            "render-blocking-resources": {"score": 0, "title": "Eliminate render-blocking resources", "displayValue": "2 resources"},
        },
    })

    parsed = LighthouseResult()
    runner._parse_report(mock_report, parsed)
    test("test_parse_scores", parsed.scores.get("performance") == 72.0 and parsed.scores.get("seo") == 100.0)

    # ── Test 10: Parse metrics ──
    test("test_parse_metrics",
         parsed.metrics.get("first-contentful-paint") == 2100 and
         parsed.metrics.get("cumulative-layout-shift") == 0.05)

    # ── Test 11: Parse diagnostics ──
    test("test_parse_diagnostics",
         any("render-blocking" in d.lower() for d in parsed.diagnostics))

    # ── Test 12: generate_fix_tasks for failing result ──
    parsed.scores = {
        "performance": 45, "accessibility": 70,
        "best-practices": 80, "seo": 60,
    }
    parsed.metrics = {
        "first-contentful-paint": 3000,
        "largest-contentful-paint": 4000,
        "total-blocking-time": 500,
        "cumulative-layout-shift": 0.2,
    }
    parsed.diagnostics = ["Eliminate render-blocking resources: 3 resources"]
    tasks = runner.generate_fix_tasks(parsed)
    test("test_generate_fix_tasks_count", len(tasks) >= 3)  # perf, a11y, seo at minimum

    # ── Test 13: Fix tasks have valid structure ──
    if tasks:
        test("test_fix_task_structure",
             all("task_id" in t and "description" in t and "dependencies" in t for t in tasks))
    else:
        test("test_fix_task_structure", False)

    # ── Test 14: Fix tasks include specific metrics ──
    perf_task = next((t for t in tasks if "Performance" in t["description"]), None)
    test("test_fix_task_perf_details",
         perf_task is not None and ("FCP" in perf_task["description"] or "LCP" in perf_task["description"]))

    # ── Test 15: No fix tasks when all passing ──
    passing_result = LighthouseResult(scores={
        "performance": 100, "accessibility": 100,
        "best-practices": 100, "seo": 100,
    })
    no_tasks = runner.generate_fix_tasks(passing_result)
    test("test_no_fix_tasks_when_passing", len(no_tasks) == 0)

    # ── Test 16: No fix tasks on error result ──
    no_tasks2 = runner.generate_fix_tasks(LighthouseResult(success=False))
    test("test_no_fix_tasks_on_error", len(no_tasks2) == 0)

    # ── Test 17: Score trend tracking ──
    runner._history = [
        {"scores": {"performance": 60}},
        {"scores": {"performance": 75}},
        {"scores": {"performance": 90}},
    ]
    trends = runner.get_score_trend()
    test("test_score_trends", trends.get("performance") == [60, 75, 90])

    # ── Test 18: Regression detection ──
    runner._history = [
        {"scores": {"performance": 90, "seo": 95}},
        {"scores": {"performance": 75, "seo": 95}},
    ]
    regressions = runner.detect_regressions()
    test("test_regression_detection",
         len(regressions) == 1 and "performance" in regressions[0])

    # ── Test 19: No regression when scores improve ──
    runner._history = [
        {"scores": {"performance": 60}},
        {"scores": {"performance": 80}},
    ]
    no_reg = runner.detect_regressions()
    test("test_no_regression_on_improvement", len(no_reg) == 0)

    # ── Test 20: Invalid JSON parse ──
    bad_result = LighthouseResult()
    runner._parse_report("not valid json", bad_result)
    test("test_invalid_json_parse", not bad_result.success and "Invalid JSON" in bad_result.error)

    # ── Results ──
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {total} tests")
    if failed == 0:
        print("\n✅ All LighthouseRunner tests passed!")
    else:
        print(f"\n❌ {failed} test(s) failed!")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
