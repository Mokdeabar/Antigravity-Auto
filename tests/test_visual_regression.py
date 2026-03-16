"""
Tests for VisualRegressionDetector (V74, Audit §4.4)
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

    print("\n🧪 Running VisualRegressionDetector Unit Tests...\n")

    from supervisor.visual_qa_engine import (
        RegressionResult,
        VisualRegressionDetector,
        VisualQAEngine,
        UI_KEYWORDS,
        FREEZE_CSS,
        SCREENSHOT_WIDTH,
    )

    # ── Test 1: RegressionResult defaults ──
    r = RegressionResult()
    test("test_regression_defaults", not r.has_regression and r.score_before == 0)

    # ── Test 2: RegressionResult summary no regression ──
    r2 = RegressionResult(has_regression=False, score_after=85)
    test("test_no_regression_summary", "✅" in r2.summary() and "85" in r2.summary())

    # ── Test 3: RegressionResult with regression ──
    r3 = RegressionResult(
        has_regression=True,
        score_before=90,
        score_after=60,
        diff_details=["text clipping", "broken layout"],
    )
    test("test_regression_summary", "❌" in r3.summary() and "90" in r3.summary())

    # ── Test 4: Diff details in summary ──
    test("test_regression_details_in_summary", "text clipping" in r3.summary())

    # ── Test 5: generate_fix_tasks with regression ──
    engine = VisualQAEngine()  # No sandbox — just for init
    detector = VisualRegressionDetector(engine)
    tasks = detector.generate_fix_tasks(r3, "homepage")
    test("test_fix_tasks_generated", len(tasks) == 1 and "[UIUX]" in tasks[0]["description"])

    # ── Test 6: Fix task has correct structure ──
    if tasks:
        test("test_fix_task_structure",
             "task_id" in tasks[0] and "description" in tasks[0] and "dependencies" in tasks[0])
    else:
        test("test_fix_task_structure", False)

    # ── Test 7: Fix task includes score info ──
    if tasks:
        test("test_fix_task_scores",
             "90" in tasks[0]["description"] and "60" in tasks[0]["description"])
    else:
        test("test_fix_task_scores", False)

    # ── Test 8: No fix tasks when no regression ──
    no_tasks = detector.generate_fix_tasks(RegressionResult(), "test")
    test("test_no_fix_tasks", len(no_tasks) == 0)

    # ── Test 9: _extract_score from critique ──
    score = VisualRegressionDetector._extract_score("PASS (score: 92): looks good")
    test("test_extract_score", score == 92)

    # ── Test 10: _extract_score no match ──
    score2 = VisualRegressionDetector._extract_score("no score here")
    test("test_extract_score_no_match", score2 == 0)

    # ── Test 11: UI_KEYWORDS constant ──
    test("test_ui_keywords", "css" in UI_KEYWORDS and "react" in UI_KEYWORDS)

    # ── Test 12: is_ui_node ──
    test("test_is_ui_node", VisualQAEngine.is_ui_node("Build the dashboard layout"))

    # ── Test 13: is_ui_node negative ──
    test("test_not_ui_node", not VisualQAEngine.is_ui_node("Set up database connection"))

    # ── Test 14: FREEZE_CSS contains animation freeze ──
    test("test_freeze_css", "animation-duration: 0s" in FREEZE_CSS)

    # ── Test 15: SCREENSHOT_WIDTH constant ──
    test("test_screenshot_width", SCREENSHOT_WIDTH == 1280)

    # ── Test 16: Detector init with no project path ──
    det2 = VisualRegressionDetector(engine, "")
    test("test_detector_no_path", det2._baselines_dir is None)

    # ── Test 17: RegressionResult with empty diff_details ──
    r4 = RegressionResult(has_regression=True, diff_details=[])
    test("test_empty_diff_details", "❌" in r4.summary())

    # ── Test 18: Fix task dependencies are empty ──
    if tasks:
        test("test_fix_task_no_deps", tasks[0]["dependencies"] == [])
    else:
        test("test_fix_task_no_deps", False)

    # ── Results ──
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {total} tests")
    if failed == 0:
        print("\n✅ All VisualRegressionDetector tests passed!")
    else:
        print(f"\n❌ {failed} test(s) failed!")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
