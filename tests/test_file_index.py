"""
test_file_index.py — Unit tests for V75 Two-Tier File Index

Tests the FileIndex class: scanning, Tier 1/Tier 2 output, caching,
dependency graph resolution, and configuration thresholds.
"""

import os
import sys
import tempfile
import time
import types

# Add parent directory to path for imports
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

# ─────────────────────────────────────────────────────────────
# Minimal config stub (file_index imports config)
# ─────────────────────────────────────────────────────────────
class _ConfigStub:
    LARGE_REPO_THRESHOLD = 300
    FILE_INDEX_CACHE_TTL_S = 120
    FILE_INDEX_TIER2_MAX_FILES = 50
    FILE_INDEX_TIER1_MAX_CHARS = 12000
    FILE_INDEX_SKIP_DIRS = {
        "node_modules", ".git", "__pycache__", ".ag-supervisor",
        "dist", ".next", "coverage", ".cache",
    }

# Create a proper package stub so relative imports work
_supervisor_pkg = types.ModuleType("supervisor")
_supervisor_pkg.__path__ = [os.path.join(_project_root, "supervisor")]
_supervisor_pkg.__package__ = "supervisor"
_supervisor_pkg.config = _ConfigStub()
sys.modules["supervisor"] = _supervisor_pkg
sys.modules["supervisor.config"] = _ConfigStub()

# Now import from file_index
from supervisor.file_index import FileIndex, FileEntry, _TS_EXPORT_RE, _PY_EXPORT_RE

# ─────────────────────────────────────────────────────────────
# Test Helpers
# ─────────────────────────────────────────────────────────────

_results = {"passed": 0, "failed": 0, "total": 0}

def _test(name: str):
    _results["total"] += 1
    return name

def _pass(name: str):
    _results["passed"] += 1
    print(f"  ✅ {name} PASSED")

def _fail(name: str, msg: str):
    _results["failed"] += 1
    print(f"  ❌ {name} FAILED: {msg}")


def _create_small_project(tmpdir: str, file_count: int = 30):
    """Create a small test project with TypeScript-like files."""
    src = os.path.join(tmpdir, "src")
    os.makedirs(os.path.join(src, "components"), exist_ok=True)
    os.makedirs(os.path.join(src, "utils"), exist_ok=True)
    os.makedirs(os.path.join(src, "types"), exist_ok=True)

    # Components
    for i in range(min(file_count, 10)):
        path = os.path.join(src, "components", f"Component{i}.tsx")
        with open(path, "w") as f:
            f.write(f'import {{ helper }} from "../utils/helpers";\n')
            f.write(f"export function Component{i}() {{ return null; }}\n")
            f.write(f"export type Component{i}Props = {{ name: string }};\n")

    # Utils
    with open(os.path.join(src, "utils", "helpers.ts"), "w") as f:
        f.write("export function formatDate(d: Date) { return d.toString(); }\n")
        f.write("export function debounce(fn: Function) { return fn; }\n")
        f.write("export const MAX_RETRIES = 3;\n")

    # Types
    with open(os.path.join(src, "types", "index.ts"), "w") as f:
        f.write("export type User = { id: string; name: string };\n")
        f.write("export interface Session { token: string; }\n")

    # Config files
    with open(os.path.join(tmpdir, "package.json"), "w") as f:
        f.write('{"name": "test-project", "version": "1.0.0"}\n')

    with open(os.path.join(tmpdir, "tsconfig.json"), "w") as f:
        f.write('{"compilerOptions": {"strict": true}}\n')

    return tmpdir


def _create_large_project(tmpdir: str, file_count: int = 350):
    """Create a large test project exceeding the threshold."""
    dirs = ["api", "auth", "dashboard", "settings", "shared", "types", "utils", "hooks"]

    for d in dirs:
        dir_path = os.path.join(tmpdir, "src", d)
        os.makedirs(dir_path, exist_ok=True)

    created = 0
    for d in dirs:
        per_dir = file_count // len(dirs)
        for i in range(per_dir):
            if created >= file_count:
                break
            path = os.path.join(tmpdir, "src", d, f"file_{i}.ts")
            with open(path, "w") as f:
                f.write(f"export function {d}Func{i}() {{ }}\n")
                f.write(f"export class {d.title()}Class{i} {{ }}\n")
                if d != "types":
                    f.write(f'import {{ typeDef }} from "../types/file_0";\n')
            created += 1

    # Config
    with open(os.path.join(tmpdir, "package.json"), "w") as f:
        f.write('{"name": "large-project"}\n')

    return tmpdir


# ─────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────

def test_scan_small_project():
    name = _test("test_scan_small_project")
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_small_project(tmpdir)
        idx = FileIndex(tmpdir)
        idx.scan()

        if idx._file_count < 5:
            return _fail(name, f"Expected ≥5 files, got {idx._file_count}")
        if idx._scanned is not True:
            return _fail(name, "Index not marked as scanned")
        _pass(name)


def test_scan_large_project():
    name = _test("test_scan_large_project")
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_large_project(tmpdir, 350)
        idx = FileIndex(tmpdir)
        idx.scan()

        if idx._file_count < 300:
            return _fail(name, f"Expected ≥300 files, got {idx._file_count}")
        if not idx.is_large_repo():
            return _fail(name, "Should be classified as large repo")
        _pass(name)


def test_is_large_repo():
    name = _test("test_is_large_repo")
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_small_project(tmpdir, 10)
        idx = FileIndex(tmpdir)
        idx.scan()

        if idx.is_large_repo():
            return _fail(name, "Small project should NOT be large repo")

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_large_project(tmpdir, 350)
        idx = FileIndex(tmpdir)
        idx.scan()

        if not idx.is_large_repo():
            return _fail(name, "350-file project SHOULD be large repo")
        _pass(name)


def test_tier1_output_format():
    name = _test("test_tier1_output_format")
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_small_project(tmpdir)
        idx = FileIndex(tmpdir)
        idx.scan()
        t1 = idx.get_tier1_context()

        if "PROJECT STRUCTURE" not in t1:
            return _fail(name, "Missing 'PROJECT STRUCTURE' header")
        if "files)" not in t1:
            return _fail(name, "Missing file count")
        # Small repo should list actual files, not just directories
        if "helpers.ts" not in t1:
            return _fail(name, "Missing helpers.ts in output")
        _pass(name)


def test_tier1_shows_all_small_repo_files():
    name = _test("test_tier1_shows_all_small_repo_files")
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_small_project(tmpdir, 10)
        idx = FileIndex(tmpdir)
        idx.scan()
        t1 = idx.get_tier1_context()

        # Should show ALL files, not cap at old 100 limit
        for i in range(10):
            if f"Component{i}.tsx" not in t1:
                return _fail(name, f"Missing Component{i}.tsx — small repos must show all files")
        _pass(name)


def test_tier1_char_cap():
    name = _test("test_tier1_char_cap")
    # Override config for this test
    old_max = _ConfigStub.FILE_INDEX_TIER1_MAX_CHARS
    _ConfigStub.FILE_INDEX_TIER1_MAX_CHARS = 500  # Very small cap

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_large_project(tmpdir, 350)
            idx = FileIndex(tmpdir)
            idx.scan()
            t1 = idx.get_tier1_context()

            if len(t1) > 600:  # Allow some margin for truncation message
                return _fail(name, f"Tier 1 output exceeds cap: {len(t1)} chars")
            if "truncated" not in t1.lower() and "more directories" not in t1.lower():
                return _fail(name, "Missing truncation indicator")
            _pass(name)
    finally:
        _ConfigStub.FILE_INDEX_TIER1_MAX_CHARS = old_max


def test_tier2_task_scope():
    name = _test("test_tier2_task_scope")
    old_threshold = _ConfigStub.LARGE_REPO_THRESHOLD
    _ConfigStub.LARGE_REPO_THRESHOLD = 5  # Force large repo detection

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_small_project(tmpdir, 10)
            idx = FileIndex(tmpdir)
            idx.scan()
            t2 = idx.get_tier2_context("Fix the Component3 rendering bug")

            if not t2:
                return _fail(name, "Tier 2 returned empty for large repo")
            if "TASK-RELEVANT FILES" not in t2:
                return _fail(name, "Missing TASK-RELEVANT FILES header")
            if "Component3" not in t2:
                return _fail(name, "Missing Component3 — should be identified from task")
            _pass(name)
    finally:
        _ConfigStub.LARGE_REPO_THRESHOLD = old_threshold


def test_tier2_includes_dependencies():
    name = _test("test_tier2_includes_dependencies")
    old_threshold = _ConfigStub.LARGE_REPO_THRESHOLD
    _ConfigStub.LARGE_REPO_THRESHOLD = 5

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_small_project(tmpdir, 10)
            idx = FileIndex(tmpdir)
            idx.scan()
            t2 = idx.get_tier2_context("Update Component0 props")

            # Write debug to file if test would fail
            with open(os.path.join(tempfile.gettempdir(), "t2_debug.txt"), "w", encoding="utf-8") as dbg:
                dbg.write(f"t2 output:\n{t2}\n\n")
                dbg.write(f"import_graph: {dict(idx._import_graph)}\n")
                dbg.write(f"reverse_graph: {dict(idx._reverse_graph)}\n")
                dbg.write(f"file_count: {idx._file_count}\n")
                dbg.write(f"is_large: {idx.is_large_repo()}\n")

            # Component0 imports from helpers — should include helpers in deps or related
            if "helpers" not in t2.lower() and "utils" not in t2.lower():
                return _fail(name, f"Missing dependency 'helpers' in Tier 2 output")
            _pass(name)
    finally:
        _ConfigStub.LARGE_REPO_THRESHOLD = old_threshold


def test_tier2_small_repo_returns_empty():
    name = _test("test_tier2_small_repo_returns_empty")
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_small_project(tmpdir, 10)
        idx = FileIndex(tmpdir)
        idx.scan()
        t2 = idx.get_tier2_context("Fix Component0")

        if t2:
            return _fail(name, "Tier 2 should return empty for small repos (CLI handles it)")
        _pass(name)


def test_skip_dirs_respected():
    name = _test("test_skip_dirs_respected")
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create files in skip dirs
        os.makedirs(os.path.join(tmpdir, "node_modules", "react"), exist_ok=True)
        with open(os.path.join(tmpdir, "node_modules", "react", "index.js"), "w") as f:
            f.write("module.exports = {};\n")

        os.makedirs(os.path.join(tmpdir, ".git", "objects"), exist_ok=True)
        with open(os.path.join(tmpdir, ".git", "objects", "abc.pack"), "w") as f:
            f.write("binary data\n")

        # Create source file
        with open(os.path.join(tmpdir, "index.ts"), "w") as f:
            f.write("export const app = 'hello';\n")

        idx = FileIndex(tmpdir)
        idx.scan()

        if idx._file_count != 1:
            return _fail(name, f"Expected 1 file (index.ts only), got {idx._file_count}")
        if "node_modules" in str(list(idx._files.keys())):
            return _fail(name, "node_modules files should be skipped")
        _pass(name)


def test_export_extraction_ts():
    name = _test("test_export_extraction_ts")
    matches = _TS_EXPORT_RE.findall(
        "export function Button() {}\n"
        "export default class Modal {}\n"
        "export type ButtonProps = {};\n"
        "export interface ModalConfig {}\n"
        "export const MAX_SIZE = 10;\n"
        "export async function fetchData() {}\n"
    )
    expected = {"Button", "Modal", "ButtonProps", "ModalConfig", "MAX_SIZE", "fetchData"}
    found = set(matches)
    missing = expected - found
    if missing:
        return _fail(name, f"Missing exports: {missing}")
    _pass(name)


def test_export_extraction_py():
    name = _test("test_export_extraction_py")
    matches = _PY_EXPORT_RE.findall(
        "def helper_function():\n    pass\n\n"
        "class DataProcessor:\n    pass\n\n"
        "def _private():\n    pass\n"
    )
    expected = {"helper_function", "DataProcessor", "_private"}
    found = set(matches)
    missing = expected - found
    if missing:
        return _fail(name, f"Missing exports: {missing}")
    _pass(name)


def test_directory_summary():
    name = _test("test_directory_summary")
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_small_project(tmpdir, 10)
        idx = FileIndex(tmpdir)
        idx.scan()
        summary = idx.get_directory_summary()

        if "files" not in summary:
            return _fail(name, "Missing file count in summary")
        if "Key dirs" not in summary:
            return _fail(name, "Missing 'Key dirs' in summary")
        _pass(name)


# ─────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🧪 Running File Index Unit Tests...\n")

    test_scan_small_project()
    test_scan_large_project()
    test_is_large_repo()
    test_tier1_output_format()
    test_tier1_shows_all_small_repo_files()
    test_tier1_char_cap()
    test_tier2_task_scope()
    test_tier2_includes_dependencies()
    test_tier2_small_repo_returns_empty()
    test_skip_dirs_respected()
    test_export_extraction_ts()
    test_export_extraction_py()
    test_directory_summary()

    print(f"\n{'=' * 50}")
    print(f"Results: {_results['passed']} passed, {_results['failed']} failed out of {_results['total']} tests\n")

    if _results["failed"] == 0:
        print("✅ All file index tests passed!\n")
    else:
        print("❌ Some tests failed!\n")
        sys.exit(1)
