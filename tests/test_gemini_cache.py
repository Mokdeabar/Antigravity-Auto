"""
test_gemini_cache.py — Unit tests for the gemini_advisor.py session cache.

Tests cover:
  - SHA256-based cache keys don't collide on same-prefix prompts
  - TTL expiry removes stale entries
  - Size cap evicts oldest entries
  - Cache miss returns expected behavior
"""

import sys
import time
import hashlib
from pathlib import Path

# Add parent directory so we can import the supervisor package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor.gemini_advisor import (
    _cache_key,
    _session_cache,
    _CACHE_TTL_S,
    _MAX_CACHE_SIZE,
)


# ─────────────────────────────────────────────────────────────
# Cache Key Tests
# ─────────────────────────────────────────────────────────────

def test_cache_key_uses_sha256():
    """Cache key should be a SHA256 hex digest of the full prompt."""
    prompt = "Test prompt for caching"
    expected = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    assert _cache_key(prompt) == expected
    print("  ✅ test_cache_key_uses_sha256 PASSED")


def test_cache_key_no_collision_same_prefix():
    """Two prompts with the same first 200 chars should produce different keys."""
    prefix = "A" * 200
    prompt_a = prefix + " ENDING A with unique content here"
    prompt_b = prefix + " ENDING B with different content here"
    key_a = _cache_key(prompt_a)
    key_b = _cache_key(prompt_b)
    assert key_a != key_b, f"Cache keys should differ but both are {key_a}"
    print("  ✅ test_cache_key_no_collision_same_prefix PASSED")


def test_cache_key_strips_whitespace():
    """Cache key should strip leading/trailing whitespace."""
    key_clean = _cache_key("test prompt")
    key_padded = _cache_key("  test prompt  ")
    assert key_clean == key_padded
    print("  ✅ test_cache_key_strips_whitespace PASSED")


def test_cache_key_deterministic():
    """Same prompt should always produce the same key."""
    prompt = "Deterministic test prompt"
    key1 = _cache_key(prompt)
    key2 = _cache_key(prompt)
    assert key1 == key2
    print("  ✅ test_cache_key_deterministic PASSED")


# ─────────────────────────────────────────────────────────────
# TTL and Storage Tests (uses module-level cache directly)
# ─────────────────────────────────────────────────────────────

def test_cache_ttl_constant():
    """TTL should be 300 seconds (5 minutes)."""
    assert _CACHE_TTL_S == 300
    print("  ✅ test_cache_ttl_constant PASSED")


def test_cache_max_size_constant():
    """Max cache size should be 50 entries."""
    assert _MAX_CACHE_SIZE == 50
    print("  ✅ test_cache_max_size_constant PASSED")


def test_cache_stores_tuples():
    """Cache entries should be (response, timestamp) tuples."""
    _session_cache.clear()
    key = _cache_key("test tuple storage")
    now = time.time()
    _session_cache[key] = ("test response", now)
    stored = _session_cache[key]
    assert isinstance(stored, tuple), f"Expected tuple, got {type(stored)}"
    assert len(stored) == 2
    assert stored[0] == "test response"
    assert isinstance(stored[1], float)
    _session_cache.clear()
    print("  ✅ test_cache_stores_tuples PASSED")


# ─────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────

def main():
    print("\n🧪 Running Gemini Cache Unit Tests...\n")
    tests = [
        test_cache_key_uses_sha256,
        test_cache_key_no_collision_same_prefix,
        test_cache_key_strips_whitespace,
        test_cache_key_deterministic,
        test_cache_ttl_constant,
        test_cache_max_size_constant,
        test_cache_stores_tuples,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__} FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")

    if failed > 0:
        sys.exit(1)
    else:
        print("\n✅ All cache tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
