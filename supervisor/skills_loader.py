"""
skills_loader.py — Smart Skills Engine for Supervisor V50.

Task-aware skill injection: loads SKILL.md files from the project's
.ag-supervisor/skills/ directory, parses YAML frontmatter for metadata
(name, tags, priority), and selects only the skills relevant to the
current task based on Ollama's classification.

Skills are injected into the Gemini CLI prompt alongside the mandate,
keeping context focused and token-efficient.

SKILL.md Format:
    ---
    name: Visual Design 2026
    tags: [frontend, design]
    priority: 10
    ---
    (markdown content here)

Tag matching: a skill is selected if ANY of its tags match the task
category. The "frontend" tag matches tasks with UI/design keywords.
Skills with no tags are included in all tasks (backward-compatible).
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("supervisor.skills")

# ─────────────────────────────────────────────────────────────
# YAML Frontmatter Parser (stdlib-only, no PyYAML dependency)
# ─────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(
    r"\A\s*---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)


def parse_skill_frontmatter(path: Path) -> dict:
    """
    Parse a SKILL.md file into structured metadata + content.

    Returns:
        {
            "name": str,       # Skill display name
            "tags": list[str], # Tag list for matching
            "priority": int,   # 1-10, higher = more important
            "content": str,    # Markdown body (after frontmatter)
            "path": Path,      # Source file path
        }
    """
    raw = path.read_text(encoding="utf-8").strip()
    result = {
        "name": path.stem.replace("-", " ").replace("_", " ").title(),
        "tags": [],
        "priority": 5,
        "content": raw,
        "path": path,
    }

    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return result

    frontmatter_block = match.group(1)
    result["content"] = match.group(2).strip()

    # Parse YAML-like key: value pairs (no PyYAML needed)
    for line in frontmatter_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        colon_idx = line.find(":")
        if colon_idx == -1:
            continue

        key = line[:colon_idx].strip().lower()
        value = line[colon_idx + 1:].strip()

        if key == "name":
            result["name"] = value.strip("\"'")
        elif key == "tags":
            # Parse [tag1, tag2, tag3] or tag1, tag2, tag3
            value = value.strip("[]")
            result["tags"] = [
                t.strip().strip("\"'").lower()
                for t in value.split(",")
                if t.strip()
            ]
        elif key == "priority":
            try:
                result["priority"] = max(1, min(10, int(value)))
            except ValueError:
                pass

    return result


# ─────────────────────────────────────────────────────────────
# In-Memory Skill Cache (mtime-based invalidation)
# ─────────────────────────────────────────────────────────────

_cache: dict = {
    "skills": [],       # list[dict] of parsed skills
    "dir_path": None,   # Path of the cached directory
    "dir_mtime": 0.0,   # mtime of the directory itself
    "file_mtimes": {},   # {filename: mtime} for invalidation
}


def _cache_is_valid(skills_dir: Path) -> bool:
    """Check if the cached skills are still valid (no file changes)."""
    if _cache["dir_path"] != skills_dir:
        return False

    try:
        current_dir_mtime = skills_dir.stat().st_mtime
    except OSError:
        return False

    if current_dir_mtime != _cache["dir_mtime"]:
        return False

    # Spot-check: verify file count hasn't changed
    current_files = set(sf.name for sf in skills_dir.glob("**/*.md"))
    if current_files != set(_cache["file_mtimes"].keys()):
        return False

    return True


def invalidate_cache():
    """Force cache invalidation (useful for testing)."""
    _cache["skills"] = []
    _cache["dir_path"] = None
    _cache["dir_mtime"] = 0.0
    _cache["file_mtimes"] = {}


# ─────────────────────────────────────────────────────────────
# Skill Discovery (cached)
# ─────────────────────────────────────────────────────────────

def _discover_skills() -> list[dict]:
    """
    Scan the project's skills directory and return parsed metadata.
    Results are cached in memory and invalidated when files change.
    """
    state_dir = config.get_state_dir()
    skills_dir = state_dir / "skills"

    if not skills_dir.exists():
        return []

    # Return cached if valid
    if _cache_is_valid(skills_dir):
        return _cache["skills"]

    # Cache miss — rescan
    skills = []
    seen_names = set()
    file_mtimes = {}

    for sf in sorted(skills_dir.glob("**/*.md")):
        try:
            parsed = parse_skill_frontmatter(sf)
            if not parsed["content"]:
                continue
            if parsed["name"] in seen_names:
                continue
            seen_names.add(parsed["name"])
            skills.append(parsed)
            file_mtimes[sf.name] = sf.stat().st_mtime
        except Exception as exc:
            logger.warning("Failed to parse skill %s: %s", sf.name, exc)

    # Update cache
    _cache["skills"] = skills
    _cache["dir_path"] = skills_dir
    _cache["dir_mtime"] = skills_dir.stat().st_mtime
    _cache["file_mtimes"] = file_mtimes

    logger.info(
        "Cached %d skills from %s",
        len(skills), skills_dir,
    )

    return skills


# ─────────────────────────────────────────────────────────────
# Category Heuristic (zero Ollama calls)
# ─────────────────────────────────────────────────────────────

_CATEGORY_KEYWORDS = {
    "testing": [
        "test", "playwright", "lighthouse", "audit", "verify",
        "coverage", "a11y", "accessibility", "performance",
    ],
    "setup": [
        "scaffold", "create", "init", "install", "bootstrap",
        "new project", "vite", "npm create",
    ],
    "analysis": [
        "analyze", "review", "report", "audit", "investigate",
        "debug", "diagnose", "inspect",
    ],
    "frontend": [
        "design", "ui", "ux", "visual", "css", "style",
        "animation", "color", "typography", "layout",
        "landing page", "dashboard", "component", "responsive",
        "html", "page", "button", "card", "modal", "navbar",
        "form", "hero", "footer", "header", "sidebar",
    ],
    "coding": [
        "implement", "build", "fix", "add", "update",
        "refactor", "feature", "function", "api", "backend",
        "endpoint", "database", "server", "logic",
    ],
}


def infer_category(prompt: str) -> str:
    """
    Infer the task category from prompt keywords.
    Fast heuristic — zero LLM calls.
    Returns one of: testing, setup, analysis, frontend, coding
    """
    prompt_lower = prompt[:2000].lower()
    scores = {}

    for category, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in prompt_lower)
        if score > 0:
            scores[category] = score

    if not scores:
        return "coding"  # Default

    return max(scores, key=scores.get)


# ─────────────────────────────────────────────────────────────
# Smart Skill Selection
# ─────────────────────────────────────────────────────────────

def select_skills(
    category: str = "",
    complexity: str = "",
    max_chars: int = 0,
) -> str:
    """
    Select and format skills relevant to the current task.

    Args:
        category: Task category from Ollama classification or heuristic
                  (coding/testing/analysis/setup/frontend/other)
        complexity: Task complexity (simple/medium/complex)
        max_chars: Token budget. 0 = use config default.

    Returns:
        Formatted string with selected skill content, ready for prompt injection.
        Empty string if no skills found or no directory exists.
    """
    if max_chars <= 0:
        max_chars = getattr(config, "SKILLS_TOKEN_BUDGET", 6000)

    all_skills = _discover_skills()
    if not all_skills:
        return ""

    category = (category or "coding").lower()

    # Filter: skill matches if any tag matches category
    # No tags = include for all tasks (backward compat)
    matched = []
    for skill in all_skills:
        tags = skill["tags"]
        if not tags:
            matched.append(skill)
        elif category in tags:
            matched.append(skill)

    if not matched:
        logger.info(
            "No skills matched category '%s' (available: %s)",
            category,
            ", ".join(s["name"] for s in all_skills),
        )
        return ""

    # Sort by priority (highest first) so important skills get budget first
    matched.sort(key=lambda s: s["priority"], reverse=True)

    # Build output with budget enforcement
    header = "## ACTIVE SKILLS\n\nThe following skills are loaded for this task:\n\n"
    parts = [header]
    current_chars = len(header)
    loaded = []
    skipped = []

    for skill in matched:
        skill_header = f"### SKILL: {skill['name']}\n"
        skill_block = skill_header + skill["content"] + "\n\n"
        block_chars = len(skill_block)

        if current_chars + block_chars > max_chars:
            # Try truncating this skill to fit remaining budget
            remaining = max_chars - current_chars
            if remaining > len(skill_header) + 100:  # Worth including partial
                truncated = skill["content"][:remaining - len(skill_header) - 30]
                parts.append(skill_header + truncated + "\n... [TRUNCATED]\n\n")
                loaded.append(f"{skill['name']}(partial)")
            else:
                skipped.append(skill["name"])
            break

        parts.append(skill_block)
        current_chars += block_chars
        loaded.append(skill["name"])

    # Log what was selected
    if loaded:
        logger.info(
            "Selected skills for '%s': %s (%d chars)",
            category,
            ", ".join(loaded),
            current_chars,
        )
    if skipped:
        logger.info(
            "Skipped skills (budget): %s",
            ", ".join(skipped),
        )

    return "".join(parts).strip()


# ─────────────────────────────────────────────────────────────
# Backward-Compatible API
# ─────────────────────────────────────────────────────────────

def load_active_skills(max_chars: int = 6000) -> str:
    """
    Load all skills regardless of category (backward-compatible).
    This is the original API — now delegates to select_skills with no filter.
    """
    return select_skills(category="", max_chars=max_chars)
