"""
skills_loader.py — Token-budgeted Skills Platform for Supervisor V11.

Loads custom skills (SKILL.md files) from the project's .ag-supervisor/skills/
directory and injects them into the prompt context safely.
"""

import logging
from pathlib import Path

from . import config

logger = logging.getLogger("supervisor.skills")

def load_active_skills(max_chars: int = 4000) -> str:
    """
    Load all markdown files from the active project's skills directory.
    Concatenates them into a single string, truncating if it exceeds max_chars.
    """
    state_dir = config.get_state_dir()
    skills_dir = state_dir / "skills"

    if not skills_dir.exists():
        return ""

    skill_files = list(skills_dir.glob("**/*.md"))
    if not skill_files:
        return ""

    logger.info("📚  Found %d skill files in %s", len(skill_files), skills_dir)

    skills_text = "## ENABLED SKILLS\n\nThe following custom skills are active for this workspace:\n\n"
    current_chars = len(skills_text)

    for sf in sorted(skill_files):
        try:
            content = sf.read_text(encoding="utf-8").strip()
            if not content:
                continue

            header = f"### SKILL: {sf.name}\n"
            added_chars = len(header) + len(content) + 2

            if current_chars + added_chars > max_chars:
                logger.warning("📚  Skill budget exceeded. Truncating skills at %d chars.", max_chars)
                remaining = max_chars - current_chars
                if remaining > len(header):
                    skills_text += header + content[:remaining - len(header)] + "... [TRUNCATED]\n"
                break
            
            skills_text += header + content + "\n\n"
            current_chars += added_chars

        except Exception as exc:
            logger.warning("📚  Failed to load skill %s: %s", sf.name, exc)

    return skills_text.strip()
