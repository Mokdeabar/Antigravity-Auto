"""
Tests for headless_executor split modules (V74, Audit §4.5):
  - environment_setup.py: EnvironmentSetup, BackendInfo, ServiceInfo, EnvironmentSnapshot
  - dev_server_manager.py: DevServerManager, DevServerState
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

    print("\n🧪 Running Headless Executor Split Unit Tests...\n")

    # ── environment_setup.py tests ──
    from supervisor.environment_setup import (
        BackendInfo, ServiceInfo, EnvironmentSnapshot, EnvironmentSetup,
        BACKEND_PATTERNS, SERVICE_PATTERNS, ENV_SAFE_DEFAULTS,
    )

    # Test 1: BackendInfo defaults
    bi = BackendInfo()
    test("test_backend_info_defaults", bi.name == "" and bi.port == 0)

    # Test 2: BackendInfo to_dict
    bi2 = BackendInfo(name="express", language="node", port=3001)
    d = bi2.to_dict()
    test("test_backend_info_to_dict", d["name"] == "express" and d["port"] == 3001)

    # Test 3: ServiceInfo defaults
    si = ServiceInfo()
    test("test_service_info_defaults", si.name == "" and si.service_type == "")

    # Test 4: ServiceInfo to_dict
    si2 = ServiceInfo(name="postgres", service_type="database", port=5432)
    test("test_service_info_to_dict", si2.to_dict()["type"] == "database")

    # Test 5: EnvironmentSnapshot defaults
    snap = EnvironmentSnapshot()
    test("test_snapshot_defaults", not snap.healthy and snap.deps_installed == False)

    # Test 6: EnvironmentSnapshot healthy
    snap2 = EnvironmentSnapshot(deps_installed=True)
    test("test_snapshot_healthy", snap2.healthy)

    # Test 7: EnvironmentSnapshot summary
    snap3 = EnvironmentSnapshot(
        node_version="v20.1.0", npm_version="10.0.0",
        deps_installed=True,
        backends=[BackendInfo(name="express")],
    )
    test("test_snapshot_summary", "v20.1.0" in snap3.summary() and "1 backend" in snap3.summary())

    # Test 8: EnvironmentSnapshot to_dict
    d3 = snap3.to_dict()
    test("test_snapshot_to_dict",
         "node_version" in d3 and "backends" in d3 and d3["healthy"])

    # Test 9: EnvironmentSnapshot unhealthy with errors
    snap4 = EnvironmentSnapshot(deps_installed=True, errors=["npm install failed"])
    test("test_snapshot_unhealthy_errors", not snap4.healthy)

    # Test 10: BACKEND_PATTERNS has express
    test("test_backend_patterns_express",
         "express" in BACKEND_PATTERNS and BACKEND_PATTERNS["express"]["default_port"] == 3001)

    # Test 11: Backend patterns cover key frameworks
    test("test_backend_patterns_coverage",
         all(k in BACKEND_PATTERNS for k in ("express", "fastapi", "flask", "django")))

    # Test 12: SERVICE_PATTERNS covers databases
    test("test_service_patterns",
         all(k in SERVICE_PATTERNS for k in ("postgres", "redis", "mongodb")))

    # Test 13: ENV_SAFE_DEFAULTS has NODE_ENV
    test("test_env_defaults", "NODE_ENV" in ENV_SAFE_DEFAULTS)

    # Test 14: EnvironmentSetup init
    class MockSandbox:
        pass
    setup = EnvironmentSetup(MockSandbox())
    test("test_setup_init", setup._snapshot is None and not setup._tooling_upgraded)

    # ── dev_server_manager.py tests ──
    from supervisor.dev_server_manager import (
        DevServerState, DevServerManager,
        DEFAULT_DEV_PORTS, DEV_COMMANDS, MAX_STARTUP_WAIT_S,
    )

    # Test 15: DevServerState defaults
    ds = DevServerState()
    test("test_dev_state_defaults", not ds.running and ds.port == 0 and not ds.healthy)

    # Test 16: DevServerState healthy
    ds2 = DevServerState(running=True, port=5173)
    test("test_dev_state_healthy", ds2.healthy)

    # Test 17: DevServerState unhealthy with errors
    ds3 = DevServerState(running=True, port=5173, console_errors=["ERR! not found"])
    test("test_dev_state_unhealthy_errors", not ds3.healthy)

    # Test 18: DevServerState summary running
    test("test_dev_state_summary", "5173" in ds2.summary() and "✅" in ds2.summary())

    # Test 19: DevServerState summary not running
    test("test_dev_state_summary_stopped", "not running" in ds.summary())

    # Test 20: DevServerState to_dict
    d4 = ds2.to_dict()
    test("test_dev_state_to_dict",
         d4["running"] and d4["port"] == 5173 and d4["healthy"])

    # Test 21: DEFAULT_DEV_PORTS includes 5173
    test("test_default_ports", 5173 in DEFAULT_DEV_PORTS and 3000 in DEFAULT_DEV_PORTS)

    # Test 22: DEV_COMMANDS has vite
    test("test_dev_commands", "vite" in DEV_COMMANDS and "next" in DEV_COMMANDS)

    # Test 23: DevServerManager init
    mgr = DevServerManager(MockSandbox())
    test("test_mgr_init", mgr.get_active_port() == 0)

    # Test 24: DevServerManager state property
    test("test_mgr_state", not mgr.state.running)

    # Test 25: MAX_STARTUP_WAIT reasonable
    test("test_startup_wait", MAX_STARTUP_WAIT_S >= 30)

    # ── Results ──
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {total} tests")
    if failed == 0:
        print("\n✅ All Headless Executor Split tests passed!")
    else:
        print(f"\n❌ {failed} test(s) failed!")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
