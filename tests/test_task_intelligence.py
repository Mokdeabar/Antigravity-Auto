"""
Tests for TaskIntelligence (V74, Audit §4.1)
"""

import sys
import os
import tempfile
import json
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

    print("\n🧪 Running TaskIntelligence Unit Tests...\n")

    from supervisor.task_intelligence import TaskIntelligence, CategoryStats

    # ── Test 1: CategoryStats defaults ──
    cs = CategoryStats()
    test("test_category_stats_defaults", cs.total == 0 and cs.success_rate == 0.0)

    # ── Test 2: CategoryStats success rate ──
    cs2 = CategoryStats(total=10, successes=8, failures=2)
    test("test_category_stats_success_rate", cs2.success_rate == 80.0)

    # ── Test 3: CategoryStats failure rate ──
    test("test_category_stats_failure_rate", cs2.failure_rate == 20.0)

    # ── Test 4: CategoryStats to_dict ──
    d = cs2.to_dict()
    test("test_category_stats_to_dict", d["total"] == 10 and d["success_rate_pct"] == 80.0)

    # ── Test 5: TaskIntelligence init with temp dir ──
    with tempfile.TemporaryDirectory() as tmp:
        intel = TaskIntelligence(tmp)
        test("test_intel_init", intel._total_recorded == 0 and len(intel._category_stats) == 0)

        # ── Test 6: Record a successful result ──
        intel.record_result(
            task_id="t1-FUNC",
            category="FUNC",
            files_changed=["src/App.tsx", "src/utils.ts"],
            success=True,
            duration_s=45.0,
        )
        test("test_record_success", intel._category_stats["FUNC"].successes == 1)

        # ── Test 7: Record a failed result ──
        intel.record_result(
            task_id="t2-FUNC",
            category="FUNC",
            files_changed=["src/App.tsx"],
            success=False,
            errors=["error TS2304: Cannot find name 'foo'"],
            duration_s=30.0,
        )
        test("test_record_failure", intel._category_stats["FUNC"].failures == 1)

        # ── Test 8: Error classification ──
        test("test_error_classification",
             "missing_type_or_name" in intel._category_stats["FUNC"].common_errors)

        # ── Test 9: File stats tracking ──
        test("test_file_stats_tsx",
             intel._file_stats.get(".tsx", {}).get("total", 0) == 2 and
             intel._file_stats.get(".tsx", {}).get("failures", 0) == 1)

        # ── Test 10: Category normalization from task_id ──
        intel.record_result(
            task_id="t3-UIUX",
            category="unknown",
            files_changed=["src/styles.css"],
            success=True,
            duration_s=20.0,
        )
        test("test_category_from_task_id", "UIUX" in intel._category_stats)

        # ── Test 11: get_insights with sufficient data ──
        # Add more data to hit threshold
        for i in range(5):
            intel.record_result(
                task_id=f"t{10+i}-FUNC",
                category="FUNC",
                files_changed=[f"src/file{i}.ts"],
                success=i != 3,  # 1 fail out of 5
                errors=["TypeError: cannot read" ] if i == 3 else [],
                duration_s=25.0 + i,
            )
        insights = intel.get_insights()
        test("test_get_insights_content",
             "FUNC" in insights and "success" in insights.lower() or "%" in insights)

        # ── Test 12: suggest_granularity ──
        g = intel.suggest_granularity("FUNC")
        test("test_suggest_granularity", g in ("fine", "normal", "coarse"))

        # ── Test 13: suggest_granularity unknown category ──
        g2 = intel.suggest_granularity("UNKNOWN")
        test("test_suggest_granularity_unknown", g2 == "normal")

        # ── Test 14: get_file_risk_score ──
        score = intel.get_file_risk_score("src/unknown.xyz")
        test("test_file_risk_unknown_ext", score == 0.5)

        # ── Test 15: Persistence ──
        intel.save()
        data_path = os.path.join(tmp, ".ag-supervisor", "task_intelligence.json")
        test("test_persistence_file_exists", os.path.exists(data_path))

        # ── Test 16: Load from persisted data ──
        intel2 = TaskIntelligence(tmp)
        test("test_load_from_persistence",
             intel2._total_recorded == intel._total_recorded and
             "FUNC" in intel2._category_stats)

        # ── Test 17: get_summary ──
        summary = intel.get_summary()
        test("test_get_summary",
             "total_tasks_recorded" in summary and
             "categories" in summary and
             "file_type_stats" in summary)

        # ── Test 18: Category normalization by keywords ──
        cat = intel._normalize_category("styling work", "t99")
        test("test_category_normalize_keywords", cat == "UIUX")

        # ── Test 19: Perf category detection ──
        cat2 = intel._normalize_category("performance optimization", "t100")
        test("test_category_normalize_perf", cat2 == "PERF")

        # ── Test 20: Empty insights when <3 recordings ──
        with tempfile.TemporaryDirectory() as tmp2:
            fresh = TaskIntelligence(tmp2)
            fresh.record_result("t1", "FUNC", [], True, duration_s=1)
            test("test_empty_insights_few_records", fresh.get_insights() == "")

    # ── Results ──
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {total} tests")
    if failed == 0:
        print("\n✅ All TaskIntelligence tests passed!")
    else:
        print(f"\n❌ {failed} test(s) failed!")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
