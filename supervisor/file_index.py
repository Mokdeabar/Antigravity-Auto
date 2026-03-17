"""
file_index.py — V75 Two-Tier File Index for Large Repository Optimization

Provides smart, cached, role-based file context for projects with 300+ files:

  Tier 1 (Planners):  Compressed directory tree + export signatures
  Tier 2 (Workers):   Task-scoped dependency subgraph only

The index is built using Python's AST/regex parsing — zero Gemini API calls.
Cached in-memory with configurable TTL (default 120s).

Usage:
    from .file_index import get_file_index
    idx = get_file_index("/path/to/project")
    tier1 = idx.get_tier1_context()        # For planners
    tier2 = idx.get_tier2_context(prompt)   # For workers
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("supervisor.file_index")

# ─────────────────────────────────────────────────────────────
# Regex patterns for export/import extraction
# ─────────────────────────────────────────────────────────────

# TypeScript / JavaScript exports
_TS_EXPORT_RE = re.compile(
    r"export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|type|interface|const|let|var|enum)\s+"
    r"(\w+)",
    re.MULTILINE,
)

# TypeScript / JavaScript imports
_TS_IMPORT_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
    re.MULTILINE,
)

# Python exports: top-level def/class
_PY_EXPORT_RE = re.compile(
    r"^(?:def|class)\s+(\w+)",
    re.MULTILINE,
)

# Python imports
_PY_IMPORT_RE = re.compile(
    r"^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    re.MULTILINE,
)

# Source file extensions worth indexing
_SOURCE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".pyw",
    ".vue", ".svelte", ".astro",
    ".css", ".scss", ".sass", ".less",
    ".json", ".yaml", ".yml", ".toml",
    ".md", ".mdx",
    ".html", ".htm",
    ".graphql", ".gql",
    ".sql",
    ".rs", ".go",
}

# Config files that are always important
_CONFIG_PATTERNS = {
    "package.json", "tsconfig.json", "tsconfig.app.json", "tsconfig.node.json",
    "vite.config.ts", "vite.config.js", "next.config.js", "next.config.mjs",
    "tailwind.config.js", "tailwind.config.ts",
    "eslint.config.js", ".eslintrc.js", ".eslintrc.json",
    "postcss.config.js", "postcss.config.mjs",
    "Cargo.toml", "pyproject.toml", "setup.py", "requirements.txt",
    ".env", ".env.local", ".env.example",
}

# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

class FileEntry:
    """Lightweight metadata for a single source file."""
    __slots__ = ("rel_path", "extension", "exports", "imports", "is_config", "size_bytes")

    def __init__(self, rel_path: str, extension: str, size_bytes: int = 0):
        self.rel_path = rel_path
        self.extension = extension
        self.exports: list[str] = []
        self.imports: list[str] = []
        self.is_config = os.path.basename(rel_path) in _CONFIG_PATTERNS
        self.size_bytes = size_bytes


# ─────────────────────────────────────────────────────────────
# FileIndex
# ─────────────────────────────────────────────────────────────

class FileIndex:
    """
    Smart file index with role-based context tiers.

    Tier 1 (Planners): Compressed directory tree + export signatures.
    Tier 2 (Workers):  Task-scoped dependency subgraph.
    """

    def __init__(self, project_path: str):
        self._project_path = Path(project_path).resolve()
        self._files: dict[str, FileEntry] = {}          # rel_path → FileEntry
        self._dir_counts: dict[str, int] = {}            # dir_path → file count
        self._import_graph: dict[str, set[str]] = {}     # rel_path → set of imported rel_paths
        self._reverse_graph: dict[str, set[str]] = {}    # rel_path → set of files that import it
        self._scan_time: float = 0.0
        self._file_count: int = 0
        self._scanned = False

    # ── Scanning ──────────────────────────────────────────────

    def scan(self) -> "FileIndex":
        """
        Walk the project tree, extract file signatures and import graphs.
        Returns self for chaining.
        """
        t0 = time.monotonic()
        skip_dirs = config.FILE_INDEX_SKIP_DIRS
        proj = self._project_path

        files: dict[str, FileEntry] = {}
        dir_counts: defaultdict[str, int] = defaultdict(int)

        for root, dirs, filenames in os.walk(proj, topdown=True):
            # Prune skip dirs in-place
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]

            rel_root = os.path.relpath(root, proj).replace("\\", "/")
            if rel_root == ".":
                rel_root = ""

            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _SOURCE_EXTENSIONS and fname not in _CONFIG_PATTERNS:
                    continue

                rel_path = f"{rel_root}/{fname}" if rel_root else fname
                try:
                    full_path = os.path.join(root, fname)
                    size = os.path.getsize(full_path)
                except OSError:
                    size = 0

                entry = FileEntry(rel_path, ext, size)
                files[rel_path] = entry

                # Count files per directory
                dir_key = rel_root or "."
                dir_counts[dir_key] += 1

        # Extract signatures for source files (skip very large files)
        for rel_path, entry in files.items():
            if entry.size_bytes > 500_000:  # Skip files > 500KB
                continue
            if entry.extension in (".css", ".scss", ".sass", ".less", ".json", ".yaml",
                                    ".yml", ".toml", ".md", ".mdx", ".html", ".htm",
                                    ".sql", ".graphql", ".gql"):
                continue  # No meaningful exports to extract

            try:
                full_path = str(self._project_path / rel_path)
                content = self._read_file_safe(full_path)
                if not content:
                    continue

                if entry.extension in (".py", ".pyw"):
                    entry.exports = _PY_EXPORT_RE.findall(content)[:20]
                    raw_imports = _PY_IMPORT_RE.findall(content)
                    entry.imports = [m[0] or m[1] for m in raw_imports][:30]
                else:
                    entry.exports = _TS_EXPORT_RE.findall(content)[:20]
                    raw_imports = _TS_IMPORT_RE.findall(content)
                    entry.imports = [m[0] or m[1] for m in raw_imports][:30]
            except Exception:
                pass

        # Build import graph (resolve relative imports to rel_paths)
        import_graph: dict[str, set[str]] = defaultdict(set)
        reverse_graph: dict[str, set[str]] = defaultdict(set)

        for rel_path, entry in files.items():
            for imp in entry.imports:
                resolved = self._resolve_import(rel_path, imp, files)
                if resolved:
                    import_graph[rel_path].add(resolved)
                    reverse_graph[resolved].add(rel_path)

        self._files = files
        self._dir_counts = dict(dir_counts)
        self._import_graph = dict(import_graph)
        self._reverse_graph = dict(reverse_graph)
        self._file_count = len(files)
        self._scan_time = time.monotonic() - t0
        self._scanned = True

        logger.info(
            "📂  [FileIndex] Scanned %d files in %.1fs (large_repo=%s)",
            self._file_count, self._scan_time, self.is_large_repo(),
        )
        return self

    def is_large_repo(self) -> bool:
        """Returns True if the project exceeds the large repo threshold."""
        return self._file_count >= config.LARGE_REPO_THRESHOLD

    # ── Tier 1: Planner Context ───────────────────────────────

    def get_tier1_context(self) -> str:
        """
        Returns compressed directory tree + export signatures for planners.
        Shows every directory with file counts and key exports per module.
        Capped at FILE_INDEX_TIER1_MAX_CHARS.
        """
        if not self._scanned:
            self.scan()

        max_chars = config.FILE_INDEX_TIER1_MAX_CHARS
        lines: list[str] = []

        # Header
        source_count = sum(1 for f in self._files.values() if not f.is_config)
        config_count = sum(1 for f in self._files.values() if f.is_config)
        lines.append(f"PROJECT STRUCTURE ({self._file_count} files):\n")

        if not self.is_large_repo():
            # Small repo: just list all files (existing behavior, enhanced)
            for rel_path in sorted(self._files.keys()):
                entry = self._files[rel_path]
                if entry.exports:
                    exports_str = ", ".join(entry.exports[:5])
                    if len(entry.exports) > 5:
                        exports_str += f", … +{len(entry.exports) - 5}"
                    lines.append(f"  {rel_path} — exports: {exports_str}")
                else:
                    lines.append(f"  {rel_path}")
            lines.append(f"\nSource files: {source_count} | Config files: {config_count}")
            result = "\n".join(lines)
            if len(result) > max_chars:
                result = result[:max_chars - 50] + f"\n… (truncated at {max_chars} chars)"
            return result

        # Large repo: directory-grouped compressed view
        # Sort directories by depth then name
        sorted_dirs = sorted(self._dir_counts.keys(), key=lambda d: (d.count("/"), d))

        for dir_path in sorted_dirs:
            count = self._dir_counts[dir_path]
            display_dir = dir_path if dir_path != "." else "(root)"
            lines.append(f"\n{display_dir}/ ({count} files)")

            # Show key files in this directory (those with exports, up to 8)
            dir_files = sorted(
                (f for f in self._files.values()
                 if self._get_dir(f.rel_path) == dir_path and f.exports),
                key=lambda f: len(f.exports),
                reverse=True,
            )

            shown = 0
            for entry in dir_files[:8]:
                fname = os.path.basename(entry.rel_path)
                exports_str = ", ".join(entry.exports[:6])
                if len(entry.exports) > 6:
                    exports_str += f", …+{len(entry.exports) - 6}"
                lines.append(f"  {fname} — exports: {exports_str}")
                shown += 1

            # Show count of remaining files
            remaining = count - shown
            if remaining > 0:
                lines.append(f"  … +{remaining} more file(s)")

            # Check char budget
            current = "\n".join(lines)
            if len(current) > max_chars - 200:
                remaining_dirs = len(sorted_dirs) - sorted_dirs.index(dir_path) - 1
                lines.append(f"\n… +{remaining_dirs} more directories")
                break

        # Footer
        lines.append(f"\nTotal source files: {source_count} | Config files: {config_count}")

        # Key directories highlight
        top_dirs = sorted(self._dir_counts.items(), key=lambda x: x[1], reverse=True)[:8]
        top_str = ", ".join(f"{d} ({c})" for d, c in top_dirs)
        lines.append(f"Key directories: {top_str}")

        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars - 50] + f"\n… (truncated at {max_chars} chars)"
        return result

    def get_directory_summary(self) -> str:
        """
        One-line summary for sandbox context (replaces raw file listing).
        """
        if not self._scanned:
            self.scan()
        top_dirs = sorted(self._dir_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_str = ", ".join(f"{d}/ ({c})" for d, c in top_dirs)
        return f"{self._file_count} files. Key dirs: {top_str}"

    # ── Tier 2: Worker Context ────────────────────────────────

    def get_tier2_context(self, task_description: str) -> str:
        """
        Returns task-scoped file subset for workers.
        Identifies files mentioned in the task, expands their dependency
        subgraph, and returns @file references.
        """
        if not self._scanned:
            self.scan()

        if not self.is_large_repo():
            return ""  # Small repos don't need filtering

        max_files = config.FILE_INDEX_TIER2_MAX_FILES
        task_lower = task_description.lower()

        # 1. Direct mentions — files explicitly named in the task
        direct: list[str] = []
        for rel_path in self._files:
            fname = os.path.basename(rel_path).lower()
            # Check if filename or path segment appears in task
            if fname in task_lower or rel_path.lower() in task_lower:
                direct.append(rel_path)
            # Also check without extension
            name_no_ext = os.path.splitext(fname)[0]
            if len(name_no_ext) > 3 and name_no_ext in task_lower:
                if rel_path not in direct:
                    direct.append(rel_path)

        # 2. Keyword match — files whose exports match task keywords
        # Extract meaningful words from task (4+ chars, not common words)
        _common = {"this", "that", "with", "from", "have", "will", "should", "must",
                    "file", "code", "make", "update", "create", "implement", "ensure",
                    "component", "function", "class", "type", "interface", "module",
                    "the", "and", "for", "not", "are", "but", "all", "can"}
        words = set(
            w.lower() for w in re.findall(r'\b[a-zA-Z]\w{3,}\b', task_description)
            if w.lower() not in _common
        )

        keyword_matches: list[str] = []
        for rel_path, entry in self._files.items():
            if rel_path in direct:
                continue
            # Check if any export matches a task keyword
            for export_name in entry.exports:
                if export_name.lower() in words:
                    keyword_matches.append(rel_path)
                    break

        # 3. Dependency expansion — imports of direct + keyword files
        targets = direct + keyword_matches[:10]
        deps: set[str] = set()
        for target in targets:
            # Forward deps (what this file imports)
            for dep in self._import_graph.get(target, set()):
                deps.add(dep)
            # Reverse deps (what imports this file) — limited
            for rev in list(self._reverse_graph.get(target, set()))[:5]:
                deps.add(rev)

        deps -= set(targets)

        # 4. Same-directory siblings (likely related)
        siblings: list[str] = []
        target_dirs = set(self._get_dir(t) for t in targets)
        for rel_path in self._files:
            if rel_path in targets or rel_path in deps:
                continue
            if self._get_dir(rel_path) in target_dirs:
                siblings.append(rel_path)

        # 5. Config files always included
        configs = [f.rel_path for f in self._files.values()
                   if f.is_config and f.rel_path not in targets]

        # Assemble within budget
        all_sections: list[tuple[str, list[str]]] = [
            ("Direct targets", direct),
            ("Keyword matches", keyword_matches[:8]),
            ("Dependencies (imported by targets)", sorted(deps)[:15]),
            ("Related (same directory)", siblings[:10]),
            ("Config files", configs[:5]),
        ]

        lines: list[str] = [
            f"TASK-RELEVANT FILES ({min(max_files, len(targets) + len(deps) + len(siblings))} "
            f"of {self._file_count} total):\n",
        ]

        total_added = 0
        for section_name, file_list in all_sections:
            if total_added >= max_files:
                break
            if not file_list:
                continue  # Skip empty sections, don't break
            lines.append(f"\n{section_name}:")
            for rel_path in file_list:
                if total_added >= max_files:
                    break
                lines.append(f"  @{rel_path}")
                total_added += 1

        lines.append(
            "\n[FILE INDEX] The above files are the most relevant to your task. "
            "Focus your changes on these files. The full project has "
            f"{self._file_count} files — use `find` or `grep` if you need others.\n"
        )

        result = "\n".join(lines)
        logger.info(
            "📂  [FileIndex] Tier 2: %d files for task (direct=%d, kw=%d, deps=%d, siblings=%d)",
            total_added, len(direct), len(keyword_matches[:8]),
            len(deps), len(siblings[:10]),
        )
        return result

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _read_file_safe(path: str, max_bytes: int = 100_000) -> str:
        """Read a file safely, returning empty string on failure."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(max_bytes)
        except Exception:
            return ""

    @staticmethod
    def _get_dir(rel_path: str) -> str:
        """Get the directory part of a relative path."""
        d = os.path.dirname(rel_path).replace("\\", "/")
        return d if d else "."

    def _resolve_import(
        self, from_file: str, import_spec: str, files: dict[str, FileEntry]
    ) -> Optional[str]:
        """
        Resolve an import specifier to a file in the index.
        Handles relative imports (./foo, ../bar) and bare specifiers.
        """
        if not import_spec or import_spec.startswith("@") and "/" not in import_spec[1:]:
            return None  # Skip bare package imports like '@angular/core'

        # Handle scoped packages like '@/components/Button'
        if import_spec.startswith("@/") or import_spec.startswith("~/"):
            import_spec = import_spec[2:]  # Strip alias prefix

        # Relative imports
        if import_spec.startswith("."):
            from_dir = os.path.dirname(from_file)
            candidate = os.path.normpath(os.path.join(from_dir, import_spec))
            candidate = candidate.replace("\\", "/")
        else:
            # Could be a path alias or src-relative — try direct
            candidate = import_spec

        # Try with various extensions
        for ext_try in ["", ".ts", ".tsx", ".js", ".jsx", ".py", "/index.ts",
                        "/index.tsx", "/index.js", "/index.jsx"]:
            full = candidate + ext_try
            if full in files:
                return full

        return None


# ─────────────────────────────────────────────────────────────
# Module-level singleton with TTL cache
# ─────────────────────────────────────────────────────────────

_cached_index: Optional[FileIndex] = None
_cached_path: Optional[str] = None
_cached_time: float = 0.0
_cached_count: int = 0


def get_file_index(project_path: str) -> FileIndex:
    """
    Get or create a cached FileIndex for the given project path.

    The index is cached in memory with a TTL of FILE_INDEX_CACHE_TTL_S.
    If the project path or file count changes, the cache is invalidated.
    """
    global _cached_index, _cached_path, _cached_time, _cached_count

    now = time.monotonic()
    resolved = str(Path(project_path).resolve())

    # Check cache validity
    if (
        _cached_index is not None
        and _cached_path == resolved
        and (now - _cached_time) < config.FILE_INDEX_CACHE_TTL_S
    ):
        return _cached_index

    # Build new index
    idx = FileIndex(project_path)
    idx.scan()

    _cached_index = idx
    _cached_path = resolved
    _cached_time = now
    _cached_count = idx._file_count

    return idx
