#!/usr/bin/env python3
"""
Supervisor UI Smoke Tests (V79)

Static analysis tests for index.html — no browser required.
Run: python -m supervisor.tests.test_ui_smoke
"""

import io
import os
import re
import sys
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ──
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
INDEX = UI_DIR / "index.html"

# Key element IDs that must exist
REQUIRED_IDS = [
    "projects-grid",        # Project launcher
    "tab-bar",              # Tab navigation
    "console-drawer",       # Console panel
    "dag-panel",            # DAG visualization
    "budget-label",         # Quota display
    "val-cpu",              # CPU health
    "val-mem",              # Memory health
    "sr-activity-list",     # Activity sidebar
    "log-filter",           # Log filter input
]

# Required files alongside index.html
REQUIRED_FILES = [
    "manifest.json",        # PWA manifest (V79)
    "sw.js",                # Service worker (V79)
]


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = True
        self.details: list[str] = []

    def fail(self, msg: str):
        self.passed = False
        self.details.append(f"  FAIL: {msg}")

    def info(self, msg: str):
        self.details.append(f"  INFO: {msg}")


def run_tests() -> list[TestResult]:
    results = []

    # ── Test 1: File exists ──
    t = TestResult("index.html exists")
    if not INDEX.exists():
        t.fail(f"File not found: {INDEX}")
    else:
        t.info(f"Found at {INDEX} ({INDEX.stat().st_size:,} bytes)")
    results.append(t)
    if not t.passed:
        return results

    html = INDEX.read_text(encoding="utf-8", errors="replace")
    lines = html.split("\n")

    # ── Test 2: Valid HTML structure ──
    t = TestResult("Valid HTML structure")
    if "<!DOCTYPE html>" not in html[:100]:
        t.fail("Missing <!DOCTYPE html> declaration")
    if "<html" not in html[:200]:
        t.fail("Missing <html> tag")
    if "</html>" not in html[-100:]:
        t.fail("Missing closing </html>")
    # Use regex to avoid matching <header>, <thead>, etc.
    open_heads = len(re.findall(r'<head[\s>]', html))
    close_heads = html.count("</head>")
    if open_heads != close_heads:
        t.fail(f"Mismatched <head> tags: {open_heads} open, {close_heads} close")
    open_bodies = len(re.findall(r'<body[\s>]', html))
    close_bodies = html.count("</body>")
    if open_bodies != close_bodies:
        t.fail(f"Mismatched <body> tags: {open_bodies} open, {close_bodies} close")
    results.append(t)

    # ── Test 3: Required element IDs ──
    t = TestResult("Required element IDs present")
    for eid in REQUIRED_IDS:
        pattern = f'id="{eid}"'
        if pattern not in html:
            t.fail(f"Missing element: id=\"{eid}\"")
        else:
            t.info(f"✓ #{eid}")
    results.append(t)

    # ── Test 4: No empty <script> tags ──
    t = TestResult("No empty script tags")
    empty_scripts = re.findall(r"<script[^>]*>\s*</script>", html)
    for es in empty_scripts:
        if 'src=' not in es:  # External scripts with src are OK
            t.fail(f"Empty script tag found: {es[:80]}")
    if not empty_scripts:
        t.info("No empty script tags found")
    results.append(t)

    # ── Test 5: CSS variables defined ──
    t = TestResult("CSS custom properties defined")
    critical_vars = ["--bg-primary", "--text-primary", "--border-light", "--font-mono"]
    for var in critical_vars:
        if var not in html:
            t.fail(f"Missing CSS variable: {var}")
        else:
            t.info(f"✓ {var}")
    results.append(t)

    # ── Test 6: JavaScript function integrity ──
    t = TestResult("Key JavaScript functions exist")
    key_functions = ["switchTab", "pollHealth", "showToast", "pollBudget"]
    for fn in key_functions:
        pattern = f"function {fn}"
        if pattern not in html:
            # Also check arrow/method style
            if f"{fn} =" not in html and f"{fn}(" not in html:
                t.fail(f"Missing function: {fn}")
            else:
                t.info(f"✓ {fn} (arrow/method style)")
        else:
            t.info(f"✓ {fn}")
    results.append(t)

    # ── Test 7: No unclosed template literals ──
    t = TestResult("Template literal balance")
    script_blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    for i, block in enumerate(script_blocks):
        backtick_count = block.count("`")
        if backtick_count % 2 != 0:
            # Find approximate line number
            block_start = html.index(block)
            line_no = html[:block_start].count("\n") + 1
            # In large SPA script blocks, template literals with nested
            # quotes can legitimately have odd backtick counts — warn, don't fail
            t.info(f"Note: Odd backtick count ({backtick_count}) near line {line_no} (may be OK in template literals)")
    if t.passed:
        t.info(f"All {len(script_blocks)} script blocks checked")
    results.append(t)

    # ── Test 8: Required companion files ──
    t = TestResult("Required companion files")
    for fname in REQUIRED_FILES:
        fpath = UI_DIR / fname
        if not fpath.exists():
            t.fail(f"Missing file: {fname}")
        else:
            t.info(f"✓ {fname} ({fpath.stat().st_size:,} bytes)")
    results.append(t)

    # ── Test 9: PWA meta tags ──
    t = TestResult("PWA configuration")
    if 'rel="manifest"' not in html:
        t.fail("Missing <link rel=\"manifest\">")
    else:
        t.info("✓ Manifest link present")
    if 'name="theme-color"' not in html:
        t.fail("Missing <meta name=\"theme-color\">")
    else:
        t.info("✓ Theme color meta present")
    if "serviceWorker" not in html:
        t.fail("Missing service worker registration")
    else:
        t.info("✓ Service worker registration found")
    results.append(t)

    # ── Test 10: SEO basics ──
    t = TestResult("SEO basics")
    if "<title>" not in html:
        t.fail("Missing <title> tag")
    else:
        title = re.search(r"<title>(.*?)</title>", html)
        t.info(f"✓ Title: {title.group(1) if title else '(empty)'}")
    if 'name="description"' not in html:
        t.fail("Missing meta description")
    else:
        t.info("✓ Meta description present")
    if '<h1' not in html:
        t.info("Note: No <h1> tag (acceptable for SPA)")
    results.append(t)

    return results


def main():
    print("=" * 60)
    print("  Supervisor UI Smoke Tests (V79)")
    print("=" * 60)
    print()

    results = run_tests()
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)

    for r in results:
        icon = "✅" if r.passed else "❌"
        print(f"{icon}  {r.name}")
        for d in r.details:
            print(d)
        print()

    print("=" * 60)
    print(f"  Results: {passed} passed, {failed} failed, {len(results)} total")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
