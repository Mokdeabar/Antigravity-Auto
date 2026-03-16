"""
test_skills_loader.py — Unit tests for the Smart Skills Engine.

Tests YAML frontmatter parsing, tag-based selection, category inference,
and token budget enforcement.
"""

import sys
import os
import tempfile
import shutil
from pathlib import Path

# Add parent directory so we can import the supervisor package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────

def _create_skill(skills_dir: Path, filename: str, content: str):
    """Helper to create a skill file."""
    (skills_dir / filename).write_text(content, encoding="utf-8")


def _make_skills_dir():
    """Create a temporary skills directory structure."""
    tmpdir = Path(tempfile.mkdtemp())
    skills_dir = tmpdir / ".ag-supervisor" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return tmpdir, skills_dir


# ─────────────────────────────────────────────────────────────
# Test: YAML Frontmatter Parsing
# ─────────────────────────────────────────────────────────────

def test_parse_valid_frontmatter():
    """Parse a skill file with valid YAML frontmatter."""
    from supervisor.skills_loader import parse_skill_frontmatter

    tmpdir, skills_dir = _make_skills_dir()
    try:
        _create_skill(skills_dir, "test-skill.md", """---
name: My Test Skill
tags: [coding, testing, always]
priority: 9
---
# Test Skill Content

This is the skill body.
""")
        result = parse_skill_frontmatter(skills_dir / "test-skill.md")
        assert result["name"] == "My Test Skill", f"Expected 'My Test Skill', got '{result['name']}'"
        assert result["tags"] == ["coding", "testing", "always"], f"Tags: {result['tags']}"
        assert result["priority"] == 9, f"Priority: {result['priority']}"
        assert "# Test Skill Content" in result["content"], f"Content: {result['content'][:100]}"
        print("  ✅ test_parse_valid_frontmatter PASSED")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_parse_missing_frontmatter():
    """A file with no frontmatter should still load with defaults."""
    from supervisor.skills_loader import parse_skill_frontmatter

    tmpdir, skills_dir = _make_skills_dir()
    try:
        _create_skill(skills_dir, "bare-skill.md", "# Just content\n\nNo frontmatter here.")
        result = parse_skill_frontmatter(skills_dir / "bare-skill.md")
        assert result["name"] == "Bare Skill", f"Name: {result['name']}"
        assert result["tags"] == [], f"Tags should be empty: {result['tags']}"
        assert result["priority"] == 5, f"Priority should be default 5: {result['priority']}"
        assert "# Just content" in result["content"]
        print("  ✅ test_parse_missing_frontmatter PASSED")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_parse_malformed_frontmatter():
    """Malformed frontmatter should fall back to defaults gracefully."""
    from supervisor.skills_loader import parse_skill_frontmatter

    tmpdir, skills_dir = _make_skills_dir()
    try:
        _create_skill(skills_dir, "bad-yaml.md", """---
name: Good Name
tags: [not closed
priority: notanumber
---
# Body content
""")
        result = parse_skill_frontmatter(skills_dir / "bad-yaml.md")
        assert result["name"] == "Good Name"
        # tags should parse what it can
        assert isinstance(result["tags"], list)
        # priority should stay default since 'notanumber' isn't int
        assert result["priority"] == 5, f"Bad priority should default: {result['priority']}"
        print("  ✅ test_parse_malformed_frontmatter PASSED")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# Test: Category Inference
# ─────────────────────────────────────────────────────────────

def test_infer_category():
    """Test keyword-based category inference."""
    from supervisor.skills_loader import infer_category

    assert infer_category("Run lighthouse audit and fix performance") == "testing"
    assert infer_category("Create a new Vite project and scaffold it") == "setup"
    assert infer_category("Implement the payment API endpoint") == "coding"
    assert infer_category("Review the code and analyze errors") == "analysis"
    assert infer_category("Update the CSS animation and color palette for the hero section") == "frontend"
    assert infer_category("Do something unrelated") == "coding"  # default
    print("  \u2705 test_infer_category PASSED")


# ─────────────────────────────────────────────────────────────
# Test: Tag-Based Selection
# ─────────────────────────────────────────────────────────────

def test_select_skills_tag_matching():
    """Skills with matching tags should be selected."""
    from supervisor.skills_loader import parse_skill_frontmatter, _discover_skills, select_skills, invalidate_cache
    from supervisor import config

    tmpdir, skills_dir = _make_skills_dir()
    try:
        # Point config to our temp dir
        old_path = config._ACTIVE_PROJECT_PATH
        config._ACTIVE_PROJECT_PATH = str(tmpdir)

        invalidate_cache()

        _create_skill(skills_dir, "frontend-skill.md", """---
name: Frontend Skill
tags: [frontend, coding]
priority: 10
---
Frontend content.""")

        _create_skill(skills_dir, "testing-only.md", """---
name: Testing Only
tags: [testing]
priority: 7
---
Only for tests.""")

        _create_skill(skills_dir, "coding-only.md", """---
name: Coding Only
tags: [coding]
priority: 5
---
Only for coding.""")

        # Select for "coding" — should get "Frontend Skill" + "Coding Only"
        result = select_skills(category="coding", max_chars=10000)
        assert "Frontend Skill" in result, f"'Frontend Skill' should be selected for coding"
        assert "Coding Only" in result, f"'Coding Only' should be selected for coding"
        assert "Testing Only" not in result, f"'Testing Only' should NOT be selected for coding"

        # Select for "testing" — should get only "Testing Only"
        invalidate_cache()
        result = select_skills(category="testing", max_chars=10000)
        assert "Testing Only" in result
        assert "Coding Only" not in result
        assert "Frontend Skill" not in result

        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        print("  \u2705 test_select_skills_tag_matching PASSED")
    finally:
        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_select_skills_priority_ordering():
    """Higher priority skills should be loaded first."""
    from supervisor.skills_loader import select_skills, invalidate_cache
    from supervisor import config

    tmpdir, skills_dir = _make_skills_dir()
    try:
        old_path = config._ACTIVE_PROJECT_PATH
        config._ACTIVE_PROJECT_PATH = str(tmpdir)
        invalidate_cache()

        _create_skill(skills_dir, "low-pri.md", """---
name: Low Priority
tags: [coding]
priority: 1
---
Low priority content.""")

        _create_skill(skills_dir, "high-pri.md", """---
name: High Priority
tags: [coding]
priority: 10
---
High priority content.""")

        result = select_skills(category="coding", max_chars=10000)
        high_pos = result.find("High Priority")
        low_pos = result.find("Low Priority")
        assert high_pos < low_pos, "High priority skill should appear before low priority"

        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        print("  \u2705 test_select_skills_priority_ordering PASSED")
    finally:
        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_token_budget_enforcement():
    """Skills should be truncated when budget is exceeded."""
    from supervisor.skills_loader import select_skills, invalidate_cache
    from supervisor import config

    tmpdir, skills_dir = _make_skills_dir()
    try:
        old_path = config._ACTIVE_PROJECT_PATH
        config._ACTIVE_PROJECT_PATH = str(tmpdir)
        invalidate_cache()

        # Create a skill with a lot of content
        big_content = "X" * 5000
        _create_skill(skills_dir, "big-skill.md", f"""---
name: Big Skill
tags: [coding]
priority: 10
---
{big_content}""")

        _create_skill(skills_dir, "small-skill.md", """---
name: Small Skill
tags: [coding]
priority: 5
---
Tiny content.""")

        # Budget of 500 chars — Big Skill should be truncated, Small Skill skipped
        result = select_skills(category="coding", max_chars=500)
        assert len(result) <= 600, f"Result too long: {len(result)} chars"  # Allow some header overhead
        assert "TRUNCATED" in result or "Small Skill" not in result, \
            "Budget should cause truncation or skip"

        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        print("  \u2705 test_token_budget_enforcement PASSED")
    finally:
        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_no_skills_dir():
    """Missing skills directory should return empty string."""
    from supervisor.skills_loader import select_skills, invalidate_cache
    from supervisor import config

    tmpdir = Path(tempfile.mkdtemp())
    try:
        old_path = config._ACTIVE_PROJECT_PATH
        config._ACTIVE_PROJECT_PATH = str(tmpdir)
        invalidate_cache()

        result = select_skills(category="coding")
        assert result == "", f"Expected empty string, got: {result}"

        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        print("  \u2705 test_no_skills_dir PASSED")
    finally:
        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_cache_hit():
    """Second call should use cached skills (no disk re-read)."""
    from supervisor.skills_loader import select_skills, _discover_skills, invalidate_cache, _cache
    from supervisor import config

    tmpdir, skills_dir = _make_skills_dir()
    try:
        old_path = config._ACTIVE_PROJECT_PATH
        config._ACTIVE_PROJECT_PATH = str(tmpdir)
        invalidate_cache()

        _create_skill(skills_dir, "cached.md", """---\nname: Cached Skill\ntags: [coding]\npriority: 5\n---\nCacheable.""")

        # First call: cache miss
        s1 = _discover_skills()
        assert len(s1) == 1
        assert _cache["dir_path"] == skills_dir, "Cache should be populated"

        # Second call: cache hit (same list object)
        s2 = _discover_skills()
        assert s1 is s2, "Second call should return same cached list"

        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        print("  \u2705 test_cache_hit PASSED")
    finally:
        config._ACTIVE_PROJECT_PATH = old_path
        invalidate_cache()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────

def main():
    print("\\n🧪 Running Smart Skills Engine Tests...\\n")
    tests = [
        test_parse_valid_frontmatter,
        test_parse_missing_frontmatter,
        test_parse_malformed_frontmatter,
        test_infer_category,
        test_select_skills_tag_matching,
        test_select_skills_priority_ordering,
        test_token_budget_enforcement,
        test_no_skills_dir,
        test_cache_hit,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__} FAILED: {e}")
            failed += 1

    print(f"\\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")

    if failed > 0:
        sys.exit(1)
    else:
        print("\\n✅ All tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
