"""
episodic_memory.py — V17 Episodic Memory Bank (Semantic Compression Edition)

A lightweight SQLite-backed store that persists semantically compressed failure
lessons outside the Git blast radius.

V17 Change: Raw diffs are NO LONGER stored. The ReflectionEngine compresses
each failure into a concise natural language lesson BEFORE recording.
Only these compact semantic lessons are injected into the LLM prompt.

Staleness Invalidation: memories are tagged with the pre-execution base_sha.
If the developer manually changes HEAD, all prior memories are ignored.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import List

logger = logging.getLogger("supervisor.episodic_memory")

_MEMORY_DIR = Path(__file__).resolve().parent.parent / ".ag-supervisor"
_DB_PATH = _MEMORY_DIR / "failures.db"


class EpisodicMemory:
    """SQLite-backed failure memory for the Omni-Brain execution loop."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_gitignore()
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")  # V20: concurrent write safety
        self._conn.row_factory = sqlite3.Row
        self._migrate_schema()

    def _migrate_schema(self):
        """Create or migrate the failures table to V17 schema."""
        # Check if old schema exists (has diff_text column)
        cursor = self._conn.execute("PRAGMA table_info(failures)")
        columns = {row[1] for row in cursor.fetchall()}

        if not columns:
            # Fresh install — create V17 schema directly
            self._conn.execute("""
                CREATE TABLE failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    objective_hash TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    semantic_lesson TEXT NOT NULL,
                    test_error TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_obj_sha
                ON failures (objective_hash, base_sha)
            """)
        elif "diff_text" in columns and "semantic_lesson" not in columns:
            # V16 -> V17 migration: drop old table and recreate
            logger.info("🧠 Migrating episodic memory from V16 to V17 schema...")
            self._conn.execute("DROP TABLE failures")
            self._conn.execute("""
                CREATE TABLE failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    objective_hash TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    semantic_lesson TEXT NOT NULL,
                    test_error TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_obj_sha
                ON failures (objective_hash, base_sha)
            """)
        # else: already V17 schema, no migration needed

        self._conn.commit()

    def _ensure_gitignore(self):
        """Ensure .ag-supervisor is gitignored so it survives hard resets."""
        gitignore = self._db_path.parent.parent / ".gitignore"
        marker = ".ag-supervisor/"
        try:
            if gitignore.exists():
                content = gitignore.read_text(encoding="utf-8")
                if marker not in content:
                    with open(gitignore, "a", encoding="utf-8") as f:
                        f.write(f"\n{marker}\n")
            else:
                gitignore.write_text(f"{marker}\n", encoding="utf-8")
        except Exception as e:
            logger.warning("Could not update .gitignore: %s", e)

    # ────────────────────────────────────────────────
    # Write
    # ────────────────────────────────────────────────

    @staticmethod
    def hash_objective(objective: str) -> str:
        """SHA-256 hash of the objective string to group related failures."""
        return hashlib.sha256(objective.encode("utf-8")).hexdigest()[:16]

    def record_failure(
        self,
        objective: str,
        base_sha: str,
        semantic_lesson: str,
        test_error: str,
    ) -> None:
        """Persist a semantically compressed failure lesson."""
        obj_hash = self.hash_objective(objective)
        self._conn.execute(
            "INSERT INTO failures (objective_hash, base_sha, semantic_lesson, test_error, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (obj_hash, base_sha, semantic_lesson[:500], test_error[:500], time.time()),
        )
        self._conn.commit()
        logger.info(
            "🧠 Recorded semantic lesson for objective %s: %s",
            obj_hash, semantic_lesson[:80],
        )

    # ────────────────────────────────────────────────
    # Read (SHA-bound)
    # ────────────────────────────────────────────────

    def query_failures(
        self,
        objective: str,
        base_sha: str,
        limit: int = 5,
    ) -> List[dict]:
        """
        Retrieve prior failures for this objective + base SHA.
        If the base SHA doesn't match, the memory is stale and returns empty.
        """
        obj_hash = self.hash_objective(objective)
        rows = self._conn.execute(
            "SELECT semantic_lesson, test_error, timestamp FROM failures "
            "WHERE objective_hash = ? AND base_sha = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (obj_hash, base_sha, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ────────────────────────────────────────────────
    # Compress into anti-pattern block
    # ────────────────────────────────────────────────

    def compress_anti_patterns(
        self,
        objective: str,
        base_sha: str,
    ) -> str:
        """
        Query prior failures and format them as a strict
        [AVOID THESE APPROACHES] block for LLM prompt injection.

        V17: Now injects concise semantic lessons instead of raw diffs.
        Returns empty string if no relevant memories exist.
        """
        failures = self.query_failures(objective, base_sha)
        if not failures:
            return ""

        lines = ["[AVOID THESE APPROACHES — THESE WERE ALREADY TRIED AND FAILED]"]

        for i, f in enumerate(failures, 1):
            lines.append(f"  {i}. {f['semantic_lesson']}")

        block = "\n".join(lines)
        logger.info(
            "🧠 Injecting %d semantic lessons (%d chars) into prompt.",
            len(failures), len(block),
        )
        return block

    # ────────────────────────────────────────────────
    # Maintenance
    # ────────────────────────────────────────────────

    def prune_old(self, max_age_hours: int = 48) -> int:
        """Delete failures older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        cursor = self._conn.execute(
            "DELETE FROM failures WHERE timestamp < ?", (cutoff,)
        )
        self._conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info("🧠 Pruned %d stale episodic memories.", deleted)
        return deleted

    def close(self):
        self._conn.close()
