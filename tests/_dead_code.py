"""
Comprehensive dead code analysis for supervisor modules.
"""
import ast
import os
import sys
import re

SUPERVISOR_DIR = r"c:\Users\mokde\Desktop\Experiments\Antigravity Auto\supervisor"

py_files = sorted([
    f for f in os.listdir(SUPERVISOR_DIR)
    if f.endswith(".py") and not f.startswith("__")
])

print("=" * 70)
print("DEAD CODE ANALYSIS")
print("=" * 70)

# 1. Import graph
imported_by = {f: set() for f in py_files}

for f in py_files:
    path = os.path.join(SUPERVISOR_DIR, f)
    with open(path, "r", encoding="utf-8") as fh:
        try:
            tree = ast.parse(fh.read())
        except SyntaxError:
            print(f"  SYNTAX ERROR: {f}")
            continue

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("supervisor."):
                target = node.module.replace("supervisor.", "") + ".py"
                if target in imported_by:
                    imported_by[target].add(f)
            elif "." not in node.module:
                target = node.module + ".py"
                if target in imported_by:
                    imported_by[target].add(f)

orphaned = [f for f, importers in imported_by.items() if len(importers) == 0]
print(f"\nOrphaned files (not imported by any supervisor module): {len(orphaned)}")
for f in sorted(orphaned):
    print(f"  {f}")

# 2. Unused imports in NEW modules
NEW_MODULES = [
    "a2a_protocol.py", "environment_setup.py", "dev_server_manager.py",
    "health_diagnostics.py", "error_collector.py", "incremental_verifier.py",
    "task_intelligence.py", "lighthouse_runner.py",
]

print(f"\n{'='*70}\nUNUSED IMPORTS IN NEW MODULES\n{'='*70}")

for f in NEW_MODULES:
    path = os.path.join(SUPERVISOR_DIR, f)
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)

    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imported_names.append((name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                imported_names.append((name, node.lineno))

    unused = []
    for name, lineno in imported_names:
        occurrences = len(re.findall(r'\b' + re.escape(name) + r'\b', source))
        if occurrences <= 1:
            unused.append((name, lineno))

    if unused:
        print(f"  {f}:")
        for name, lineno in unused:
            print(f"    L{lineno}: '{name}' unused")
    else:
        print(f"  {f}: clean")

# 3. Unused private symbols
print(f"\n{'='*70}\nUNUSED PRIVATE SYMBOLS IN NEW MODULES\n{'='*70}")

for f in NEW_MODULES:
    path = os.path.join(SUPERVISOR_DIR, f)
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)

    private_defs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and not node.name.startswith("__"):
                private_defs.append((node.name, node.lineno))

    unused_privates = []
    for name, lineno in private_defs:
        occurrences = len(re.findall(r'\b' + re.escape(name) + r'\b', source))
        if occurrences <= 1:
            unused_privates.append((name, lineno))

    if unused_privates:
        print(f"  {f}:")
        for name, lineno in unused_privates:
            print(f"    L{lineno}: '{name}' unreferenced")
    else:
        print(f"  {f}: clean")

# 4. Module sizes
print(f"\n{'='*70}\nMODULE SIZES\n{'='*70}")
total = 0
for f in sorted(py_files):
    path = os.path.join(SUPERVISOR_DIR, f)
    with open(path, "r", encoding="utf-8") as fh:
        lines = len(fh.read().split("\n"))
    total += lines
    print(f"  {lines:>6}  {f}")
print(f"  {total:>6}  TOTAL")
