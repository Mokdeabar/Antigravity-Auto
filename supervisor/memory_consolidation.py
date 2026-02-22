"""
memory_consolidation.py — V18 Long-Term Semantic Memory

Scans the episodic failures.db for recurring semantic lessons that appear
across multiple distinct objective_hashes. Promotes them into universal
environmental rules written to `.ag-memory/global_axioms.md`.

Safeguards:
- Hard cap of 10 axioms to prevent lost-in-the-middle syndrome.
- Dependency-hash staleness: hashes requirements.txt / package.json.
  If the hash changes, global_axioms.md is wiped and rebuilt from scratch.
- Runs as a background cron task during extended idle periods.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("supervisor.memory_consolidation")

_MEMORY_DIR = Path(__file__).resolve().parent.parent / ".ag-memory"
_AXIOMS_PATH = _MEMORY_DIR / "global_axioms.md"
_DEP_HASH_PATH = _MEMORY_DIR / ".dep_hash"
MAX_AXIOMS = 10


class MemoryConsolidator:
    """Promotes recurring episodic lessons into global environmental axioms."""

    CONSOLIDATION_PROMPT = (
        "You are a strict memory consolidation engine. You will receive a list of "
        "failure lessons from different coding tasks. Your job is to identify "
        "recurring patterns and abstract them into universal environmental rules.\n\n"
        "RULES:\n"
        "1. Only promote a lesson if it appears in at least 2 DIFFERENT tasks.\n"
        "2. Each axiom must be a single, actionable sentence (max 30 words).\n"
        "3. Include specific library names, file types, or environment constraints.\n"
        "4. Do NOT include task-specific details (variable names, line numbers).\n"
        "5. Output strict JSON: {\"axioms\": [\"axiom1\", \"axiom2\", ...]}\n"
        "6. Maximum 10 axioms. Rank by frequency. Prune the least frequent.\n"
    )

    def __init__(self, local_manager, workspace_path: Optional[str] = None):
        self._manager = local_manager
        self._workspace = Path(workspace_path) if workspace_path else _MEMORY_DIR.parent
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # ────────────────────────────────────────────────
    # Dependency Hash Staleness
    # ────────────────────────────────────────────────

    def _compute_dep_hash(self) -> str:
        """Hash requirements.txt, package.json, or pyproject.toml."""
        dep_files = [
            self._workspace / "requirements.txt",
            self._workspace / "package.json",
            self._workspace / "pyproject.toml",
            self._workspace / "Pipfile",
        ]
        hasher = hashlib.sha256()
        found = False
        for dep_file in dep_files:
            if dep_file.exists():
                try:
                    hasher.update(dep_file.read_bytes())
                    found = True
                except Exception:
                    pass
        return hasher.hexdigest()[:16] if found else "no_deps"

    def _check_staleness(self) -> bool:
        """
        Check if the dependency hash has changed since the last consolidation.
        Returns True if axioms are stale and must be wiped.
        """
        current_hash = self._compute_dep_hash()

        if _DEP_HASH_PATH.exists():
            stored_hash = _DEP_HASH_PATH.read_text(encoding="utf-8").strip()
            if stored_hash != current_hash:
                logger.warning(
                    "📜 Dependency hash changed (%s → %s). Wiping global axioms.",
                    stored_hash[:7], current_hash[:7],
                )
                self._wipe_axioms()
                _DEP_HASH_PATH.write_text(current_hash, encoding="utf-8")
                return True
        else:
            _DEP_HASH_PATH.write_text(current_hash, encoding="utf-8")

        return False

    def _wipe_axioms(self):
        """Delete global_axioms.md to force a fresh rebuild."""
        if _AXIOMS_PATH.exists():
            _AXIOMS_PATH.unlink()
            logger.info("📜 global_axioms.md wiped due to dependency change.")

    # ────────────────────────────────────────────────
    # Consolidation
    # ────────────────────────────────────────────────

    async def consolidate(self) -> str:
        """
        Main entry point. Scans failures.db, clusters lessons across
        different objectives, promotes recurring ones to global axioms.
        Returns status string for the scheduler.
        """
        # Step 1: Check staleness
        self._check_staleness()

        # Step 2: Query all unique lessons grouped by objective
        from .episodic_memory import EpisodicMemory
        memory = EpisodicMemory()

        try:
            rows = memory._conn.execute(
                "SELECT DISTINCT objective_hash, semantic_lesson FROM failures "
                "ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
        except Exception as e:
            memory.close()
            return f"Failed to query failures: {e}"

        if len(rows) < 2:
            memory.close()
            return "Not enough failures to consolidate."

        # Build a grouped representation
        lessons_by_obj = {}
        for r in rows:
            obj_hash = r["objective_hash"]
            lesson = r["semantic_lesson"]
            lessons_by_obj.setdefault(obj_hash, []).append(lesson)

        if len(lessons_by_obj) < 2:
            memory.close()
            return "Lessons only from one objective. Need cross-task patterns."

        # Step 3: Format for the Local Manager
        lesson_block = []
        for obj_hash, lessons in lessons_by_obj.items():
            lesson_block.append(f"Task {obj_hash}:")
            for l in lessons:
                lesson_block.append(f"  - {l}")

        user_prompt = "\n".join(lesson_block)

        # Step 4: Ask the Local Manager to consolidate
        try:
            raw = await self._manager.ask_local_model(
                system_prompt=self.CONSOLIDATION_PROMPT,
                user_prompt=user_prompt,
                temperature=0.0,
            )

            if not raw or raw == "{}":
                memory.close()
                return "Local Manager returned empty consolidation response."

            data = json.loads(raw)
            axioms = data.get("axioms", [])

            if not axioms:
                memory.close()
                return "No axioms promoted."

        except Exception as exc:
            memory.close()
            return f"Consolidation failed: {exc}"

        # Step 5: Enforce the 10-axiom hard cap
        axioms = axioms[:MAX_AXIOMS]

        # Step 6: Write global_axioms.md
        self._write_axioms(axioms)

        # Step 7: Prune old episodic memory
        memory.prune_old(max_age_hours=72)
        memory.close()

        logger.info("📜 Consolidated %d global axioms.", len(axioms))
        return f"Promoted {len(axioms)} axioms to global_axioms.md."

    def _write_axioms(self, axioms: List[str]):
        """Write the ranked axiom list to global_axioms.md."""
        lines = [
            "# Global Environmental Axioms",
            "",
            "> Auto-generated by V18 Memory Consolidation.",
            "> These rules apply to ALL tasks. Wiped if dependencies change.",
            "",
        ]
        for i, axiom in enumerate(axioms, 1):
            lines.append(f"{i}. {axiom}")

        _AXIOMS_PATH.write_text("\n".join(lines), encoding="utf-8")
        logger.info("📜 Wrote %d axioms to global_axioms.md", len(axioms))

    # ────────────────────────────────────────────────
    # Read (for injection into prompts)
    # ────────────────────────────────────────────────

    @staticmethod
    def load_axioms() -> str:
        """
        Load global_axioms.md as a string for prompt injection.
        Returns empty string if no axioms exist.
        """
        if not _AXIOMS_PATH.exists():
            return ""
        try:
            content = _AXIOMS_PATH.read_text(encoding="utf-8").strip()
            return content if content else ""
        except Exception:
            return ""
