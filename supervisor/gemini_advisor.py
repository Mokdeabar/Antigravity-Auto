"""
gemini_advisor.py — Centralized Gemini CLI Interface (V60: cwd-aware subprocess).

Every module that needs Gemini's intelligence calls through here.
Provides async, sync, and JSON-parsing variants with built-in:
  • RetryPolicy (exponential backoff + jitter, configurable)
  • ModelFailoverChain (per-model cooldowns: 1m→5m→25m→1h)
  • ContextBudget (track chars/tokens sent per session)
  • Response caching (avoid asking the same question twice)
  • Glass Brain ANSI transparency
  • Error self-analysis

V9 UPGRADE — OpenClaw-Inspired:
  1. RetryPolicy replaces fixed 2s sleep with exponential backoff + jitter.
  2. ModelFailoverChain replaces simple probe-and-cache with automatic
     model rotation on failure, with escalating cooldowns.
  3. ContextBudget tracks total chars sent/received per session and
     warns when approaching configurable limits.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .retry_policy import (
    RetryPolicy,
    ModelFailoverChain,
    ContextBudget,
    RateLimitTracker,
    get_retry_policy,
    get_failover_chain,
    get_context_budget,
    get_router,
    get_rate_tracker,
)

logger = logging.getLogger("supervisor.gemini_advisor")

# ─────────────────────────────────────────────────────────────
# Session cache — avoid asking the exact same question twice.
# Keys are SHA256 hashes of the full prompt.
# Values are (response, timestamp) tuples for TTL-based eviction.
# ─────────────────────────────────────────────────────────────
_session_cache: dict[str, tuple[str, float]] = {}  # key → (response, epoch)
_MAX_CACHE_SIZE = 50
_CACHE_TTL_S = 300  # 5 minutes — stale responses are evicted after this

# V74: System prompt — establishes role and constraints to reduce
# conversational filler and placeholder code from Gemini.
GEMINI_SYSTEM_PROMPT = (
    "You are a senior full-stack engineer working inside an automated build system. "
    "Your code will be written directly to files and immediately built/tested. "
    "Always produce complete, working code — never use placeholders, TODOs, or '...' stubs. "
    "Output only the requested format (JSON, code, or markdown) — no conversational text. "
    "If you encounter an error, fix the root cause — never suppress or ignore errors."
)

# ─────────────────────────────────────────────────────────────
# V55: Status callback — lets main.py push Gemini attempt events
# to the UI (operation label + activity feed) without threading
# state through every ask_gemini call site.
# Register once at startup via set_gemini_status_callback().
# ─────────────────────────────────────────────────────────────
_gemini_status_cb: 'Callable[[str, str], None] | None' = None


def set_gemini_status_callback(fn: 'Callable[[str, str], None] | None') -> None:
    """
    Register a callback that receives (event_type, message) whenever
    ask_gemini changes state. event_type is one of:
      'attempt'    — starting an attempt
      'retry'      — attempt failed, retrying with delay
      'ratelimit'  — rate-limited, waiting or switching model
      'success'    — response received
    """
    global _gemini_status_cb
    _gemini_status_cb = fn


def _cb(event: str, msg: str) -> None:
    """Fire the status callback if registered, silently ignoring errors."""
    if _gemini_status_cb:
        try:
            _gemini_status_cb(event, msg)
        except Exception:
            pass
# ─────────────────────────────────────────────────────────────
# V55: Global stop flag — lets main.py cancel in-flight Gemini
# subprocesses immediately on safe-stop instead of waiting for
# the full per-call timeout (potentially 600s).
# ─────────────────────────────────────────────────────────────
_stop_requested: bool = False


def set_gemini_stop(value: bool) -> None:
    """Signal all in-flight _call_gemini_async calls to abort immediately."""
    global _stop_requested
    _stop_requested = value


# ─────────────────────────────────────────────────────────────
# V73: Post-call quota probe — runs /stats after every Gemini
# CLI call to keep quota data fresh for pause/resume decisions.
# ─────────────────────────────────────────────────────────────

_post_call_counter = __import__('itertools').count(1)  # V75: Thread-safe atomic counter


def _post_call_probe(model: str = "") -> None:
    """
    V73: Record usage and run /stats probe after a successful Gemini call.
    V74: Stats probe runs every 5th call to reduce ~1s overhead per call.
    V75: Uses itertools.count for thread safety (shared by async + sync paths).

    Called after every ask_gemini / stream_gemini / call_gemini_with_file.
    Runs in a background thread to avoid blocking the async event loop.
    All errors are swallowed — probe failures must never break calls.
    """
    _count = next(_post_call_counter)
    _run_stats = (_count % 5 == 1)  # Run on 1st, 6th, 11th, etc.

    import threading
    def _probe_bg():
        try:
            from .retry_policy import get_daily_budget, get_quota_probe
            _budget = get_daily_budget()
            _budget.record_request()
            _qp = get_quota_probe()
            _qp.record_usage(model)
            # V74: Only run expensive stats probe every 5th call
            if _run_stats:
                _qp.run_stats_probe()
        except Exception:
            pass
    threading.Thread(target=_probe_bg, daemon=True, name="post-call-probe").start()


def _post_call_probe_sync(model: str = "") -> None:
    """
    V73: Synchronous version of _post_call_probe for sync call paths.
    V74: Stats probe runs every 5th call (shared counter with async version).
    V75: Uses shared itertools.count — thread-safe without locks.
    Runs inline since we're already in a blocking context.
    """
    _count = next(_post_call_counter)
    _run_stats = (_count % 5 == 1)

    try:
        from .retry_policy import get_daily_budget, get_quota_probe
        _budget = get_daily_budget()
        _budget.record_request()
        _qp = get_quota_probe()
        _qp.record_usage(model)
        # V74: Only run expensive stats probe every 5th call
        if _run_stats:
            _qp.run_stats_probe()
    except Exception:
        pass



def _cache_key(prompt: str) -> str:
    """Create a collision-resistant cache key from the full prompt using SHA256."""
    return hashlib.sha256(prompt.strip().encode('utf-8')).hexdigest()


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from Gemini's response."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json|python|javascript|html|css|bash|sh)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


# ─────────────────────────────────────────────────────────────
# Model Resolution — delegated to ModelFailoverChain
# ─────────────────────────────────────────────────────────────

_cached_best_model: str | None = None


def _get_best_model(prompt: str = "") -> str:
    """
    Return the best available Gemini model name.

    V11 Strategy — Smart Routing:
      1. If prompt provided, use TaskComplexityRouter to classify.
      2. Check rate limit tracker for cooldowns.
      3. Delegate to ModelFailoverChain for failover-aware selection.
      4. On first call, probe models and initialize.
    """
    global _cached_best_model

    # Smart routing: classify prompt and pick model tier
    if prompt:
        router = get_router()
        rate_tracker = get_rate_tracker()

        # Get routed model based on task complexity
        model = router.get_model_for(prompt)

        # Check if this model is rate-limited
        wait_time = rate_tracker.should_wait(model)
        if wait_time > 0:
            alt = rate_tracker.suggest_alternative_model(model)
            if alt != model:
                logger.info("⚡  Model %s rate-limited (%.0fs). Using %s instead.", model, wait_time, alt)
                model = alt
    else:
        chain = get_failover_chain()
        _pro_only = getattr(config, "PRO_ONLY_CODING", False)
        model = chain.get_active_model(pro_only=_pro_only)

    # Still do the initial probe on first call (for Glass Brain output)
    if _cached_best_model is None:
        _cached_best_model = _probe_and_cache_model(model)
    else:
        _cached_best_model = model

    return _cached_best_model


def _probe_and_cache_model(preferred: str) -> str:
    """
    Probe models on startup. Uses the failover chain's model as preference,
    but falls back through the chain if it fails.
    """
    cache_path = config.GEMINI_MODEL_CACHE_PATH
    ttl_hours = config.GEMINI_MODEL_CACHE_TTL_HOURS

    # Try reading disk cache first.
    try:
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_time = datetime.fromisoformat(data["timestamp"])
            age_hours = (datetime.now(timezone.utc) - cached_time).total_seconds() / 3600
            if age_hours < ttl_hours and data.get("model"):
                model = data.get("model") # Define 'model' from cache
                # V73: Before returning cached model, check if the failover
                # chain has a higher-priority model available (e.g. from a
                # recovery promotion). The chain's active model takes priority.
                # V75: Reuse 'preferred' param instead of re-calling chain.
                chain = get_failover_chain()
                chain_active = preferred  # Already resolved by caller
                if chain_active and chain_active != model:
                    # Chain promoted a better model — prefer it over stale cache.
                    # chain._models is ordered by priority (0 = highest).
                    try:
                        chain_idx = chain._models.index(chain_active)
                    except ValueError:
                        chain_idx = 999
                    try:
                        cache_idx = chain._models.index(model)
                    except ValueError:
                        cache_idx = 999
                    if chain_idx < cache_idx:
                        logger.info(
                            "🧠  Disk cache has %s but failover chain promoted %s — using chain model.",
                            model, chain_active,
                        )
                        # Update disk cache to the promoted model
                        cache_path.write_text(
                            json.dumps({"model": chain_active, "timestamp": datetime.now(timezone.utc).isoformat()}),
                            encoding="utf-8",
                        )
                        return chain_active
                # V52: Check if cached model is on cooldown before returning it
                if chain._is_available(model, time.time()):
                    logger.info("🧠  Using cached best model: %s (%.1fh old)", model, age_hours)
                    return model
                else:
                    # Cached model is on cooldown — get the best available one
                    active = chain.get_active_model()
                    if active:
                        logger.info(
                            "🧠  Cached model %s is on cooldown — using %s instead",
                            model, active,
                        )
                        return active
                    logger.warning("🧠  Cached model %s on cooldown, all models exhausted", model)
    except Exception as exc:
        logger.debug("Could not read model cache: %s", exc)

    # Probe each model via the failover chain.
    gemini_cmd = config.get_gemini_cli_cmd()
    test_prompt = "Reply with OK"
    chain = get_failover_chain()

    M = config.ANSI_MAGENTA
    B = config.ANSI_BOLD
    R = config.ANSI_RESET

    for model in config.GEMINI_MODEL_PROBE_LIST:
        # V73: Skip image-gen models — they can't handle text probes
        if config.classify_model(model) == "image":
            continue
        # V73: Skip probing if stop has been requested
        if _stop_requested:
            logger.info("🧠  Stop requested — aborting model probe.")
            break
        try:
            logger.info("🧠  Probing model: %s …", model)
            print(f"  {M}🧠 Probing model: {model} …{R}", end=" ", flush=True)

            # V33: Use positional prompt (v0.29+ canonical form, -p is deprecated)
            if config.IS_WINDOWS:
                cmd = f'"{gemini_cmd}" --model "{model}" "{test_prompt}"'
            else:
                cmd = [gemini_cmd, "--model", model, test_prompt]

            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=60,
                shell=config.IS_WINDOWS, encoding='utf-8', errors='replace',
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
            )

            if result.returncode == 0 and result.stdout.strip():
                # Write cache to disk.
                cache_path.write_text(
                    json.dumps({"model": model, "timestamp": datetime.now(timezone.utc).isoformat()}),
                    encoding="utf-8",
                )
                chain.report_success(model)
                logger.info("🧠  ✅ Model %s is LIVE. Cached to disk.", model)
                print(f"{config.ANSI_GREEN}✅ LIVE{R}")
                print(f"  {B}{M}🧠 Best Gemini model: {model}{R}")
                return model
            else:
                err_snippet = (result.stderr or "")[:100]
                chain.report_failure(model)
                logger.info("🧠  ❌ Model %s failed (code %d): %s", model, result.returncode, err_snippet)
                print(f"{config.ANSI_RED}❌ unavailable{R}")

        except subprocess.TimeoutExpired:
            chain.report_timeout(model)  # V30.6: Timeout ≠ failure, short cooldown
            logger.warning("🧠  ⏱️ Model %s probe timed out.", model)
            print(f"{config.ANSI_YELLOW}⏱️ timeout{R}")
        except Exception as exc:
            logger.warning("🧠  Model %s probe error: %s", model, exc)
            print(f"{config.ANSI_RED}❌ error{R}")

    # All probes failed — use fallback.
    fallback = config.GEMINI_FALLBACK_MODEL
    logger.warning("🧠  All probes failed. Using fallback model: %s", fallback)
    print(f"  {config.ANSI_YELLOW}🧠 All probes failed. Fallback: {fallback}{R}")
    return fallback


# ─────────────────────────────────────────────────────────────
# Glass Brain — ANSI colored console output
# ─────────────────────────────────────────────────────────────

def _glass_brain_send(prompt: str) -> None:
    """Print what we're sending to Gemini in bright Cyan."""
    C = config.ANSI_CYAN
    B = config.ANSI_BOLD
    R = config.ANSI_RESET
    model = _cached_best_model or "(resolving...)"
    # Truncate prompt for display (show first 500 chars)
    display_prompt = prompt[:500]
    if len(prompt) > 500:
        display_prompt += f"\n... ({len(prompt) - 500} more chars)"
    print(f"\n{B}{C}================== 🧠 SENDING TO GEMINI [{model}] =================={R}")
    print(f"{C}[PROMPT]: {display_prompt}{R}")
    print(f"{B}{C}==========================================================={R}\n")


def _glass_brain_receive(response: str) -> None:
    """Print the Gemini response in bright Yellow."""
    Y = config.ANSI_YELLOW
    B = config.ANSI_BOLD
    R = config.ANSI_RESET
    # Truncate response for display (show first 800 chars)
    display_response = response[:800]
    if len(response) > 800:
        display_response += f"\n... ({len(response) - 800} more chars)"
    print(f"\n{B}{Y}================== 💡 GEMINI RESPONSE =================={R}")
    print(f"{Y}[💡 GEMINI RESPONSE]: {display_response}{R}")
    print(f"{B}{Y}========================================================={R}\n")


def _glass_brain_error(error: str) -> None:
    """Print a Gemini error in bright Red."""
    RD = config.ANSI_RED
    B = config.ANSI_BOLD
    R = config.ANSI_RESET
    print(f"\n{B}{RD}================== ❌ GEMINI ERROR =================={R}")
    print(f"{RD}[ERROR]: {error}{R}")
    print(f"{B}{RD}====================================================={R}\n")


# ─────────────────────────────────────────────────────────────
# Core async call — used by most modules
# ─────────────────────────────────────────────────────────────

async def ask_gemini(
    prompt: str,
    timeout: int | None = None,
    use_cache: bool = True,
    max_retries: int | None = None,
    all_files: bool = False,
    cwd: str | None = None,
    model: str | None = None,
) -> str:
    """
    Send a prompt to Gemini CLI and return the response.

    V10: Uses RetryPolicy for exponential backoff + jitter,
    ModelFailoverChain for automatic model rotation on failure,
    and ContextBudget for tracking context consumption.

    Args:
        prompt:      The full prompt string to send via stdin.
        timeout:     Override the default timeout (seconds).
        use_cache:   If True, check/update the session cache.
        max_retries: Override RetryPolicy max attempts.
        all_files:   If True, prepend '@./' for file expansion.
        cwd:         Working directory for the Gemini CLI subprocess.
                     CRITICAL for audit tasks: @./ file expansion reads
                     from this dir, so it MUST be the project path, not
                     the supervisor directory.
        model:       V73: Explicit model override. When set, this model
                     is used instead of auto-selection. Use for audit/
                     planning calls that must run on pro-tier models.

    Returns:
        The raw text response from Gemini CLI.

    Raises:
        RuntimeError on persistent failure.
    """
    timeout = timeout or config.GEMINI_TIMEOUT_SECONDS
    key = _cache_key(prompt)
    policy = get_retry_policy()
    chain = get_failover_chain()
    budget = get_context_budget()
    attempts = max_retries if max_retries is not None else policy.max_attempts

    # Check cache (with TTL validation).
    if use_cache and key in _session_cache:
        _cached_resp, _cached_time = _session_cache[key]
        if (time.time() - _cached_time) < _CACHE_TTL_S:
            logger.debug("🧠  Cache hit for prompt (hash=%s)", key[:16])
            return _cached_resp
        else:
            # TTL expired — evict stale entry
            del _session_cache[key]
            logger.debug("🧠  Cache expired for prompt (hash=%s)", key[:16])

    # V74: Proactive context budget diagnostic — suggests @file references
    # for large content blocks. Respects 1M token limit but NEVER truncates.
    _prompt_len = len(prompt)
    if _prompt_len > 100_000:
        _sections = prompt.split('\n\n')
        _large_sections = [(i, len(s)) for i, s in enumerate(_sections) if len(s) > 10_000]
        if _large_sections:
            logger.info(
                "📊  Large prompt detected (%d chars, ~%d tokens). "
                "%d section(s) > 10K chars could use @file references for efficiency:",
                _prompt_len, _prompt_len // 4, len(_large_sections),
            )
            for idx, size in _large_sections[:5]:
                logger.info("📊    Section %d: %d chars (~%d tokens)", idx, size, size // 4)
    elif _prompt_len > config.PROMPT_SIZE_WARN_CHARS:
        logger.warning(
            "📊  Prompt approaching limit: %d chars (%.0f%% of %d max). "
            "Consider using @file references for large code blocks.",
            _prompt_len, (_prompt_len / config.PROMPT_SIZE_MAX_CHARS) * 100,
            config.PROMPT_SIZE_MAX_CHARS,
        )
    # V74: Prepend system prompt if the caller hasn't included one.
    # headless_executor.py already provides its own system context via the
    # mandate/sections pattern, so we check to avoid double-injection.
    if not any(kw in prompt[:500].lower() for kw in ("senior", "automated build system", "you are a")):
        prompt = f"{GEMINI_SYSTEM_PROMPT}\n\n{prompt}"

    # Glass Brain: show what we're sending.
    _glass_brain_send(prompt)

    last_error: Exception | None = None
    rate_tracker = get_rate_tracker()

    # OpenClaw pattern: budget-triggered auto-pruning.
    # If context budget is hot, compact session history BEFORE calling Gemini.
    if budget.should_prune():
        logger.info("📊  Context budget hot (%.0f%%) — triggering auto-compaction.", budget.budget_pct)
        try:
            from .session_memory import SessionMemory
            _mem = SessionMemory()
            _mem.compact_history()
            logger.info("📊  Auto-compaction complete.")
        except Exception as exc:
            logger.debug("📊  Auto-compaction failed (non-critical): %s", exc)

    # V73: If caller specified an explicit model, use it directly.
    # Otherwise, auto-select the best model based on prompt + failover chain.
    if not model:
        model = _get_best_model(prompt)

    if not model:
        raise RuntimeError("ALL_MODELS_EXHAUSTED: Every available model is on cooldown.")

    # V31 Fix 1.3: Outer safety timeout — if the entire retry loop hangs
    # (subprocess deadlock, infinite rate-limit wait), bail out cleanly.
    outer_timeout = timeout * (attempts + 1)  # e.g. 180s * 4 = 720s

    async def _inner_retry_loop():
        nonlocal model
        last_err: Exception | None = None
        for attempt in range(attempts):
            _cb('attempt', f'🧠 Gemini attempt {attempt + 1}/{attempts} — sending to {model}…')
            try:
                response = await _call_gemini_async(prompt, timeout, model=model, all_files=all_files, cwd=cwd)
                if not response:
                    raise RuntimeError("Gemini CLI returned an empty response.")

                # Glass Brain: show the response.
                _glass_brain_receive(response)

                # Track context budget.
                budget.record(len(prompt), len(response), model=model)

                # Report success to failover chain.
                chain.report_success(model)

                # V73: Post-call quota probe
                _post_call_probe(model)

                # Feed router adaptive learning.
                router = get_router()
                tier = router.classify(prompt)
                router.record_outcome(tier, success=True)

                # Cache the response.
                if use_cache:
                    # Evict oldest entry if at capacity
                    if len(_session_cache) >= _MAX_CACHE_SIZE:
                        oldest_key = min(_session_cache, key=lambda k: _session_cache[k][1])
                        del _session_cache[oldest_key]
                    _session_cache[key] = (response, time.time())

                logger.info(
                    "🧠  Gemini response (%d chars, attempt %d/%d, model=%s): %.120s…",
                    len(response), attempt + 1, attempts, model, response,
                )
                return response

            except Exception as exc:
                last_err = exc
                error_text = str(exc)
                _glass_brain_error(f"Attempt {attempt + 1}/{attempts} failed: {exc}")

                # V73: Stop cancellation — NOT a model failure.
                # Break immediately without cooldowns or failure reporting.
                if "safe stop requested" in error_text.lower():
                    logger.info("🧠  Gemini call cancelled by safe stop — no model cooldown.")
                    break

                # ── Rate limit detection ──
                if RateLimitTracker.is_rate_limit_error(error_text):
                    wait_s = rate_tracker.record_rate_limit(model, error_text)
                    # Try downgrading to Flash before waiting
                    alt_model = rate_tracker.suggest_alternative_model(model)
                    if alt_model != model:
                        logger.info("⚡  Rate-limited on %s → switching to %s", model, alt_model)
                        _cb('ratelimit', f'⚡️ Rate-limited on {model} — switching to {alt_model}')
                        model = alt_model
                        # Don't wait the full cooldown if we have an alternative
                        await asyncio.sleep(min(5.0, wait_s))
                        continue
                    else:
                        # Already on Flash, must wait
                        logger.info("⚡  Rate-limited on %s, waiting %ds", model, wait_s)
                        _cb('ratelimit', f'⚡️ Rate-limited — all models exhausted, waiting {int(wait_s)}s for cooldown')
                        await asyncio.sleep(min(wait_s, config.RATE_LIMIT_MAX_WAIT_S))
                        continue

                # ── Normal failure handling ──
                chain.report_failure(model)
                _pro_only = getattr(config, "PRO_ONLY_CODING", False)
                model = chain.get_active_model(pro_only=_pro_only)  # Get next model

                # Feed router adaptive learning.
                router = get_router()
                tier = router.classify(prompt)
                router.record_outcome(tier, success=False)

                if policy.should_retry(attempt):
                    delay = policy.delay_for(attempt)
                    # Classify the error type for the UI label
                    _err_summary = 'timeout' if 'timed out' in str(exc).lower() else str(exc)[:60]
                    _cb('retry',
                        f'🧠 Gemini attempt {attempt + 1}/{attempts} failed ({_err_summary}) — '
                        f'retrying in {delay:.0f}s on {model}')
                    logger.warning(
                        "🧠  Gemini CLI attempt %d/%d failed: %s — retrying in %.1fs (model: %s) …",
                        attempt + 1, attempts, exc, delay, model,
                    )
                    await asyncio.sleep(delay)
                else:
                    _cb('retry', f'🧠 Gemini all {attempts} attempts exhausted — giving up')
                    logger.error(
                        "🧠  Gemini CLI failed after %d attempts: %s",
                        attempt + 1, exc,
                    )

        raise RuntimeError(f"Gemini CLI failed after {attempts} attempts: {last_err}")

    try:
        return await asyncio.wait_for(_inner_retry_loop(), timeout=outer_timeout)
    except asyncio.TimeoutError:
        logger.critical(
            "🧠  OUTER SAFETY TIMEOUT: ask_gemini retry loop hung for %ds. Bailing out.",
            outer_timeout,
        )
        raise RuntimeError(
            f"Gemini CLI outer safety timeout ({outer_timeout}s). "
            "The retry loop itself hung — likely a subprocess deadlock."
        )


async def stream_gemini(
    prompt: str,
    timeout: int | None = None,
    max_retries: int | None = None,
    cwd: str | None = None,
    model_override: str | None = None,
):
    """
    Async generator that streams the response from Gemini CLI.
    Wraps _stream_gemini_async with full retry, failover, and budget tracking.
    """
    timeout = timeout or config.GEMINI_TIMEOUT_SECONDS
    policy = get_retry_policy()
    chain = get_failover_chain()
    budget = get_context_budget()
    attempts = max_retries if max_retries is not None else policy.max_attempts

    _glass_brain_send(prompt)
    last_error: Exception | None = None
    rate_tracker = get_rate_tracker()

    if budget.should_prune():
        try:
            from .session_memory import SessionMemory
            SessionMemory().compact_history()
        except Exception:
            pass

    model = model_override or _get_best_model(prompt)
    if not model:
        raise RuntimeError("ALL_MODELS_EXHAUSTED")

    for attempt in range(attempts):
        _cb('attempt', f'🧠 Gemini streaming attempt {attempt + 1}/{attempts} — targeting {model}…')
        try:
            full_response = []
            async for chunk in _stream_gemini_async(prompt, timeout, model=model, cwd=cwd):
                full_response.append(chunk)
                yield chunk

            if not full_response:
                raise RuntimeError("Gemini CLI returned an empty stream.")

            response_str = "".join(full_response)
            _glass_brain_receive(response_str)
            budget.record(len(prompt), len(response_str), model=model)
            chain.report_success(model)

            # V73: Post-call quota probe
            _post_call_probe(model)

            router = get_router()
            tier = router.classify(prompt)
            router.record_outcome(tier, success=True)
            return

        except Exception as exc:
            last_error = exc
            error_text = str(exc)
            _glass_brain_error(f"Stream attempt {attempt + 1}/{attempts} failed: {exc}")

            if RateLimitTracker.is_rate_limit_error(error_text):
                wait_s = rate_tracker.record_rate_limit(model, error_text)
                alt_model = rate_tracker.suggest_alternative_model(model)
                if alt_model != model:
                    _cb('ratelimit', f'⚡️ Rate-limited on {model} — switching to {alt_model}')
                    model = alt_model
                    await asyncio.sleep(min(5.0, wait_s))
                    continue
                else:
                    _cb('ratelimit', f'⚡️ Rate-limited — waiting {int(wait_s)}s')
                    await asyncio.sleep(min(wait_s, config.RATE_LIMIT_MAX_WAIT_S))
                    continue

            chain.report_failure(model)
            _pro_only = getattr(config, "PRO_ONLY_CODING", False)
            model = chain.get_active_model(pro_only=_pro_only)

            router = get_router()
            tier = router.classify(prompt)
            router.record_outcome(tier, success=False)

            if policy.should_retry(attempt):
                delay = policy.delay_for(attempt)
                _err_summary = 'timeout' if 'timed out' in str(exc).lower() else str(exc)[:60]
                _cb('retry', f'🧠 Stream failed ({_err_summary}) — retrying in {delay:.0f}s on {model}')
                await asyncio.sleep(delay)
            else:
                _cb('retry', f'🧠 Gemini all {attempts} stream attempts exhausted')

    raise RuntimeError(f"Gemini CLI stream failed after {attempts} attempts: {last_error}")



async def ask_gemini_json(
    prompt: str,
    timeout: int | None = None,
    use_cache: bool = True,
) -> dict | None:
    """
    Call Gemini CLI and parse a JSON object from the response.
    V9: Uses balanced-brace extraction for robustness.
    Returns the parsed dict, or None if parsing fails.
    """
    try:
        raw = await ask_gemini(prompt, timeout=timeout, use_cache=use_cache)
    except RuntimeError:
        return None

    parsed = _extract_json_object(raw)
    if parsed:
        return parsed

    logger.warning("🧠  Failed to extract JSON from Gemini response: %.200s", raw[:200])
    return None


# ─────────────────────────────────────────────────────────────
# V74: Parallel Gemini Calls (Audit §4.2)
# ─────────────────────────────────────────────────────────────

@dataclass
class BatchResult:
    """Result of a single prompt within a batch call."""
    index: int
    prompt_id: str
    response: str = ""
    success: bool = True
    error: str = ""
    duration_s: float = 0.0


async def batch_ask_gemini(
    prompts: list[dict],
    max_concurrency: int | None = None,
    timeout: int | None = None,
    use_cache: bool = True,
    model: str | None = None,
) -> list[BatchResult]:
    """
    V74: Send multiple independent prompts to Gemini in parallel.

    Uses asyncio.Semaphore for concurrency control so we don't exceed
    rate limits. Each prompt gets full retry/failover handling via the
    existing ask_gemini() function.

    Args:
        prompts: List of dicts with keys:
            - "prompt" (str): The prompt text (required)
            - "id" (str): Optional identifier for the prompt
            - "cwd" (str): Optional working directory override
        max_concurrency: Max parallel calls (default: config.MAX_CONCURRENT_WORKERS)
        timeout: Per-call timeout override
        use_cache: Whether to use the session cache
        model: Model override for all calls

    Returns:
        List of BatchResult in the same order as input prompts.

    Example:
        results = await batch_ask_gemini([
            {"prompt": "Review file A", "id": "review-a"},
            {"prompt": "Review file B", "id": "review-b"},
            {"prompt": "Review file C", "id": "review-c"},
        ], max_concurrency=3)
        for r in results:
            if r.success:
                print(f"{r.prompt_id}: {r.response[:100]}")
    """
    if not prompts:
        return []

    concurrency = max_concurrency or config.MAX_CONCURRENT_WORKERS
    semaphore = asyncio.Semaphore(concurrency)
    results: list[BatchResult] = [
        BatchResult(index=i, prompt_id=p.get("id", f"batch-{i}"))
        for i, p in enumerate(prompts)
    ]

    async def _run_one(idx: int, prompt_dict: dict, result: BatchResult) -> None:
        """Execute a single prompt with semaphore-controlled concurrency."""
        async with semaphore:
            start = time.time()
            try:
                response = await ask_gemini(
                    prompt=prompt_dict["prompt"],
                    timeout=timeout,
                    use_cache=use_cache,
                    cwd=prompt_dict.get("cwd"),
                    model=model,
                )
                result.response = response
                result.success = True
            except Exception as exc:
                result.success = False
                result.error = str(exc)[:500]
                logger.warning(
                    "🧠  [Batch] Prompt %s failed: %s",
                    result.prompt_id, str(exc)[:100],
                )
            finally:
                result.duration_s = round(time.time() - start, 1)

    # Launch all tasks concurrently (semaphore controls actual parallelism)
    tasks = [
        _run_one(i, p, results[i])
        for i, p in enumerate(prompts)
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    # Summary
    succeeded = sum(1 for r in results if r.success)
    total_time = sum(r.duration_s for r in results)
    logger.info(
        "🧠  [Batch] %d/%d prompts succeeded (%.1fs total, %d concurrent)",
        succeeded, len(results), total_time, concurrency,
    )

    return results


# ─────────────────────────────────────────────────────────────
# V8: File Inclusion — @file embedded INSIDE the -p string
# ─────────────────────────────────────────────────────────────

async def call_gemini_with_file(
    prompt: str,
    file_path: str,
    timeout: int | None = None,
) -> str:
    """
    Call Gemini CLI with a file attachment for multimodal analysis.

    V14 UPGRADE: Full retry/failover/rate-limit integration.
    Previously this was a raw subprocess call with zero resilience.
    Now matches the ask_gemini() text path's infrastructure:
      - RetryPolicy for exponential backoff + jitter
      - ModelFailoverChain for automatic model rotation on failure
      - RateLimitTracker for detecting and persisting rate limit cooldowns
      - ContextBudget for tracking context consumption

    Returns the raw text response from Gemini CLI.
    Raises RuntimeError on persistent failure.
    """
    timeout = timeout or config.GEMINI_TIMEOUT_SECONDS
    gemini_cmd = config.get_gemini_cli_cmd()
    policy = get_retry_policy()
    chain = get_failover_chain()
    budget = get_context_budget()
    rate_tracker = get_rate_tracker()
    router = get_router()
    model = _get_best_model()

    if not model:
        raise RuntimeError("ALL_MODELS_EXHAUSTED: Every available model is on cooldown.")

    # Normalize file path (forward slashes work better in CLI args)
    normalized_path = str(Path(file_path).resolve()).replace("\\", "/")
    file_ref = f"@{normalized_path}"

    # Glass Brain: show what we're sending.
    _glass_brain_send(f"{prompt}\n\n[📎 FILE ATTACHED: {file_path}]")

    import uuid
    tmp_dir = Path(".ag-tmp")
    tmp_dir.mkdir(exist_ok=True)
    prompt_file = tmp_dir / f"prompt_{uuid.uuid4().hex}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    last_error: Exception | None = None

    # V37 FIX (H-2): Ensure temp file is always cleaned up, even on success.
    try:
      for attempt in range(policy.max_attempts):
        try:
            if config.IS_WINDOWS:
                cmd_str = f'"{gemini_cmd}" -m "{model}" -- "{file_ref}" < "{prompt_file.absolute()}"'
                proc = await asyncio.create_subprocess_shell(
                    cmd_str,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                )
            else:
                with open(prompt_file, "r", encoding="utf-8") as stdin_f:
                    proc = await asyncio.create_subprocess_exec(
                        gemini_cmd, "-m", model, "--", file_ref,
                        stdin=stdin_f,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                    )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()

                # ── Rate limit detection ──
                if RateLimitTracker.is_rate_limit_error(err):
                    wait_s = rate_tracker.record_rate_limit(model, err)
                    alt_model = rate_tracker.suggest_alternative_model(model)
                    if alt_model != model:
                        logger.info(
                            "⚡  File call: Rate-limited on %s → switching to %s (cooldown: %ds)",
                            model, alt_model, wait_s,
                        )
                        chain.report_failure(model)
                        model = alt_model
                        await asyncio.sleep(min(5.0, wait_s))
                        continue
                    else:
                        logger.info("⚡  File call: Rate-limited on %s, waiting %ds", model, wait_s)
                        await asyncio.sleep(min(wait_s, config.RATE_LIMIT_MAX_WAIT_S))
                        continue

                # ── Normal failure ──
                chain.report_failure(model)
                _pro_only = getattr(config, "PRO_ONLY_CODING", False)
                model = chain.get_active_model(pro_only=_pro_only)
                tier = router.classify(prompt)
                router.record_outcome(tier, success=False)

                _glass_brain_error(f"Gemini CLI exited with code {proc.returncode}: {err[:500]}")

                if policy.should_retry(attempt):
                    delay = policy.delay_for(attempt)
                    logger.warning(
                        "🧠  File call attempt %d/%d failed: code %d — retrying in %.1fs (model: %s)",
                        attempt + 1, policy.max_attempts, proc.returncode, delay, model,
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    raise RuntimeError(f"Gemini CLI exited with code {proc.returncode}: {err[:500]}")

            # ── Success ──
            response = stdout.decode("utf-8", errors="replace").strip()
            if not response:
                raise RuntimeError("Gemini CLI with file returned an empty response.")

            # Glass Brain: show the response.
            _glass_brain_receive(response)

            # Track context budget.
            budget.record(len(prompt), len(response), model=model)

            # Report success to failover chain.
            chain.report_success(model)

            # V73: Post-call quota probe
            _post_call_probe(model)

            # Feed router adaptive learning.
            tier = router.classify(prompt)
            router.record_outcome(tier, success=True)

            logger.info(
                "🧠  Gemini file response (%d chars, attempt %d/%d, model=%s): %.120s…",
                len(response), attempt + 1, policy.max_attempts, model, response,
            )
            return response

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            last_error = RuntimeError(f"Gemini CLI with file timed out after {timeout}s")
            _glass_brain_error(f"Gemini CLI with file timed out after {timeout}s")

            chain.report_timeout(model)  # V30.6: Timeout ≠ failure, short cooldown
            _pro_only = getattr(config, "PRO_ONLY_CODING", False)
            model = chain.get_active_model(pro_only=_pro_only)

            if policy.should_retry(attempt):
                delay = policy.delay_for(attempt)
                logger.warning(
                    "🧠  File call attempt %d/%d timed out — retrying in %.1fs (model: %s)",
                    attempt + 1, policy.max_attempts, delay, model,
                )
                await asyncio.sleep(delay)
                continue
            else:
                break

        except Exception as exc:
            last_error = exc
            _glass_brain_error(f"File call attempt {attempt + 1}/{policy.max_attempts} failed: {exc}")

            chain.report_failure(model)
            _pro_only = getattr(config, "PRO_ONLY_CODING", False)
            model = chain.get_active_model(pro_only=_pro_only)

            if policy.should_retry(attempt):
                delay = policy.delay_for(attempt)
                await asyncio.sleep(delay)
                continue
            else:
                break

      raise RuntimeError(f"Gemini CLI with file failed after {policy.max_attempts} attempts: {last_error}")
    finally:
        # V37 FIX (H-2): Always clean up temp file, including on success paths.
        try:
            prompt_file.unlink(missing_ok=True)
        except OSError:
            pass


async def call_gemini_with_file_json(
    prompt: str,
    file_path: str,
    timeout: int | None = None,
) -> dict | None:
    """
    Call Gemini CLI with file and parse a JSON object from the response.

    V9 FIX: Two-stage extraction:
      Stage 1: Improved regex with balanced-brace matching.
      Stage 2: If regex fails, feed the raw prose back to Gemini (text-only)
               with a focused extraction prompt. This creates a self-correcting
               loop where Gemini fixes its own output.

    Returns the parsed dict, or None if both stages fail.
    """
    try:
        raw = await call_gemini_with_file(prompt, file_path, timeout=timeout)
    except RuntimeError:
        return None

    # ── Stage 1: Direct JSON extraction ──────────────────────
    parsed = _extract_json_object(raw)
    if parsed:
        return parsed

    # ── Stage 2: Self-correction — feed prose back for extraction ──
    logger.warning(
        "🧠  Stage 1 JSON extraction failed. Running self-correction …"
    )
    print(f"  {config.ANSI_YELLOW}🧠 Gemini returned prose — running self-correction …{config.ANSI_RESET}")

    correction_prompt = (
        "The previous Gemini response below was supposed to be a JSON object "
        "but was returned as prose instead. Extract the information and "
        "return ONLY a valid JSON object.\n\n"
        "PREVIOUS RESPONSE:\n"
        f"{raw[:2000]}\n\n"
        "Based on that analysis, reply with ONLY this JSON (nothing else):\n"
        '{"state": "WORKING" or "WAITING" or "CRASHED", '
        '"reason": "one sentence summary of what you see"}\n\n'
        "Rules:\n"
        "- WORKING = AI agent is actively generating code/text (visible streaming, spinner, Stop button)\n"
        "- WAITING = chat area is empty or idle, input shows placeholder, no spinner\n"
        "- CRASHED = error dialogs, blank screen, extension host terminated\n"
        "- 'Claude Opus 4.6 (Thinking)' is a MODEL NAME, NOT a status\n"
        "Reply with ONLY the JSON. No other text."
    )

    try:
        correction_raw = await ask_gemini(
            correction_prompt, timeout=60, use_cache=False
        )
        parsed = _extract_json_object(correction_raw)
        if parsed:
            logger.info("🧠  ✅ Self-correction succeeded: %s", parsed)
            print(f"  {config.ANSI_GREEN}🧠 Self-correction recovered JSON: {parsed.get('state', '?')}{config.ANSI_RESET}")
            return parsed
    except Exception as exc:
        logger.warning("🧠  Self-correction failed: %s", exc)

    logger.warning("🧠  Both JSON extraction stages failed.")
    return None


def _extract_json_object(text: str) -> dict | None:
    """
    Extract a JSON object from text with extreme robustness.
    
    V10 OpenClaw Upgrade:
      1. Parses explicit ```json ``` blocks.
      2. String-aware balanced brace extraction (handles braces inside strings).
      3. Returns the largest valid JSON dictionary found.
    """
    # 1. Try to find explicit code blocks
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    for block in blocks:
        try:
            obj = json.loads(block)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    # 2. String-aware balanced brace extraction
    # V37 FIX (H-3): O(n) instead of O(n²) — walk from the first '{' only,
    # and extract each top-level object sequentially.
    cleaned = _strip_markdown_fences(text)
    valid_objects = []
    pos = 0

    while pos < len(cleaned):
        start_idx = cleaned.find('{', pos)
        if start_idx == -1:
            break
        depth = 0
        in_string = False
        escape = False
        found_end = False
        for i in range(start_idx, len(cleaned)):
            char = cleaned[i]
            if not escape and char == '"':
                in_string = not in_string
            elif char == '\\' and not escape:
                escape = True
                continue
            else:
                escape = False

            if not in_string:
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = cleaned[start_idx:i+1]
                        try:
                            obj = json.loads(candidate)
                            if isinstance(obj, dict):
                                valid_objects.append(obj)
                        except json.JSONDecodeError:
                            pass
                        pos = i + 1
                        found_end = True
                        break
        if not found_end:
            break  # Unclosed brace, stop searching

    if valid_objects:
        # Return the largest valid object by key count
        return max(valid_objects, key=lambda x: len(str(x)))

    # 3. Strategy 3: Regex fallback for simpler unnested cases
    matches = re.finditer(r'\{[^{}]*\}', cleaned)
    for match in matches:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    return None


# ─────────────────────────────────────────────────────────────
# Synchronous call — for exception handlers where the event
# loop may not be available (e.g., self_evolver).
# ─────────────────────────────────────────────────────────────

def ask_gemini_sync(
    prompt: str,
    timeout: int | None = None,
    max_retries: int | None = None,
) -> str:
    """
    Synchronous Gemini CLI call. Used in exception handlers
    where the async event loop may be unavailable.

    V10: Uses RetryPolicy for exponential backoff + jitter.

    Returns the response text.
    Raises RuntimeError on failure.
    """
    timeout = timeout or config.GEMINI_TIMEOUT_SECONDS
    policy = get_retry_policy()
    chain = get_failover_chain()
    budget = get_context_budget()
    attempts = max_retries if max_retries is not None else policy.max_attempts
    _pro_only = getattr(config, "PRO_ONLY_CODING", False)
    model = chain.get_active_model(pro_only=_pro_only)

    if not model:
        raise RuntimeError("ALL_MODELS_EXHAUSTED: Every available model is on cooldown.")

    # Glass Brain: show what we're sending.
    _glass_brain_send(prompt)

    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            response = _call_gemini_sync(prompt, timeout)
            if not response:
                raise RuntimeError("Gemini CLI returned an empty response.")

            # Glass Brain: show the response.
            _glass_brain_receive(response)

            # Track context budget.
            budget.record(len(prompt), len(response), model=model)
            chain.report_success(model)

            # V73: Post-call quota probe (sync)
            _post_call_probe_sync(model)

            logger.info(
                "🧠  Gemini sync response (%d chars, attempt %d/%d): %.120s…",
                len(response), attempt + 1, attempts, response,
            )
            return response
        except Exception as exc:
            last_error = exc
            _glass_brain_error(f"Sync attempt {attempt + 1}/{attempts} failed: {exc}")
            chain.report_failure(model)
            _pro_only = getattr(config, "PRO_ONLY_CODING", False)
            model = chain.get_active_model(pro_only=_pro_only)

            if policy.should_retry(attempt):
                delay = policy.delay_for(attempt)
                logger.warning(
                    "🧠  Gemini sync attempt %d/%d failed: %s — retrying in %.1fs …",
                    attempt + 1, attempts, exc, delay,
                )
                time.sleep(delay)

    raise RuntimeError(f"Gemini sync failed after {attempts} attempts: {last_error}")


def ask_gemini_sync_json(
    prompt: str,
    timeout: int | None = None,
) -> dict | None:
    """Synchronous version of ask_gemini_json."""
    try:
        raw = ask_gemini_sync(prompt, timeout=timeout)
    except RuntimeError:
        return None

    # V37 FIX (H-4): Use the same robust extraction as the async variant.
    parsed = _extract_json_object(raw)
    if parsed:
        return parsed

    logger.warning("🧠  Failed to extract JSON from sync Gemini response: %.200s", raw[:200])
    return None


# ─────────────────────────────────────────────────────────────
# Low-level subprocess wrappers (V8: all include -m model flag)
# ─────────────────────────────────────────────────────────────

async def _call_gemini_async(
    prompt: str,
    timeout: int,
    model: str | None = None,
    all_files: bool = False,
    cwd: str | None = None,
) -> str:
    """Async subprocess call to the Gemini CLI with stop-aware streaming read."""
    gemini_cmd = config.get_gemini_cli_cmd()
    if not model:
        model = _get_best_model()
        if not model:
            raise RuntimeError("ALL_MODELS_EXHAUSTED")

    # V56: --all_files was deprecated in Gemini CLI v0.11.0 (Oct 2025).
    # The canonical mechanism is the '@' prefix in the prompt body.
    # When all_files=True we prepend '@./\n\n' so the CLI's built-in
    # file-expansion tool loads all project files from the working dir.
    if all_files:
        prompt = "@./\n\n" + prompt

    if config.IS_WINDOWS:
        cmd_str = f'"{gemini_cmd}" --model "{model}"'
        proc = await asyncio.create_subprocess_shell(
            cmd_str,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            gemini_cmd, "--model", model,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

    if proc.stdin:
        try:
            proc.stdin.write(prompt.encode('utf-8'))
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            pass

    # Stream stdout in chunks, checking stop flag and timeout every 0.5s.
    # V73: Activity-aware timeout — large prompts can take 90-120s of
    # "thinking" before any output starts. We use a generous initial
    # wait (5× timeout) and then reset the deadline on each chunk received.
    # A stall MID-stream (no data for `timeout` seconds) still triggers timeout.
    chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    bytes_received = 0
    _PROGRESS_EVERY = 30_000   # report to UI every 30 KB
    _last_report_at = 0
    _initial_patience = timeout * 5   # 600s for thinking phase (5× 120s)
    _stream_patience = timeout         # 120s stall timeout once data flows
    _loop = asyncio.get_event_loop()
    deadline = _loop.time() + _initial_patience  # generous first-byte wait

    try:
        while True:
            # Check global stop flag — kill immediately and propagate
            if _stop_requested:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise RuntimeError("Gemini CLI cancelled (safe stop requested)")

            # Check timeout
            now_t = _loop.time()
            if now_t > deadline:
                _phase = "thinking" if bytes_received == 0 else "streaming"
                try:
                    proc.kill()
                except Exception:
                    pass
                raise RuntimeError(
                    f"Gemini CLI timed out after {int(now_t - (deadline - (_initial_patience if bytes_received == 0 else _stream_patience)))}s "
                    f"({_phase} phase, {bytes_received} bytes received)"
                )

            # Non-blocking read (0.5s window)
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(8192), timeout=0.5)
            except asyncio.TimeoutError:
                # Nothing yet — check if proc finished
                if proc.returncode is not None:
                    break
                continue

            if chunk == b'':
                # EOF
                break

            chunks.append(chunk)
            bytes_received += len(chunk)

            # V73: Activity-aware — reset deadline on each chunk.
            # Once we're receiving data, any silence > timeout = stall.
            deadline = _loop.time() + _stream_patience

            # Progress callback every 30 KB
            if bytes_received - _last_report_at >= _PROGRESS_EVERY:
                _last_report_at = bytes_received
                _cb('progress', f'🧠 Gemini receiving response… {bytes_received // 1024} KB received')

        # Drain stderr quickly
        try:
            stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=2)
            if stderr_data:
                stderr_chunks.append(stderr_data)
        except asyncio.TimeoutError:
            pass

        await asyncio.wait_for(proc.wait(), timeout=5)

    except RuntimeError:
        # Re-raise stop/timeout errors
        raise
    except Exception as _exc:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(f"Gemini CLI stream error: {_exc}") from _exc

    stdout = b''.join(chunks)
    stderr = b''.join(stderr_chunks)

    if proc.returncode != 0:
        err = stderr.decode('utf-8', errors='replace').strip()

        # V65: MCP extension discovery failures are non-fatal.
        # The Gemini CLI loads MCP extensions at startup (e.g. 'nanobanana').
        # If a server isn't running in the subprocess context, the CLI may
        # exit with code 1 even though the model responded correctly.
        # If stdout has content AND the only errors are MCP-related, accept it.
        _is_mcp_only_error = (
            proc.returncode == 1
            and "MCP error" in err
            and not any(k in err for k in ("PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
                                            "INVALID_ARGUMENT", "API key", "quota"))
        )
        _stdout_has_content = bool(stdout.strip())

        if _is_mcp_only_error and _stdout_has_content:
            logger.debug(
                "⚡  [Gemini] MCP extension error on exit (non-fatal) — model responded OK. "
                "stderr: %s", err[:200]
            )
            # Fall through to return stdout below — don't raise
        elif _is_mcp_only_error and not _stdout_has_content:
            # MCP only, but no output — model never ran. Raise for tier fallback.
            logger.warning(
                "⚡  [Gemini] MCP extension error caused empty response (code %d). "
                "Tip: ensure MCP servers are running or remove unused extensions. err: %s",
                proc.returncode, err[:200]
            )
            raise RuntimeError(f"Gemini CLI exited with code {proc.returncode}: {err[:500]}")
        else:
            raise RuntimeError(f"Gemini CLI exited with code {proc.returncode}: {err[:500]}")


    # Fire final bytes-received callback
        _cb('success', f'🧠 Gemini responded ({bytes_received // 1024} KB received)')

    return stdout.decode("utf-8", errors="replace").strip()


async def _stream_gemini_async(
    prompt: str,
    timeout: int,
    model: str | None = None,
    all_files: bool = False,
    cwd: str | None = None,
):
    """Async generator yielding chunks from the Gemini CLI subprocess."""
    gemini_cmd = config.get_gemini_cli_cmd()
    if not model:
        model = _get_best_model()
        if not model:
            raise RuntimeError("ALL_MODELS_EXHAUSTED")

    if all_files:
        prompt = "@./\n\n" + prompt

    if config.IS_WINDOWS:
        cmd_str = f'"{gemini_cmd}" --model "{model}"'
        proc = await asyncio.create_subprocess_shell(
            cmd_str,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            gemini_cmd, "--model", model,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

    if proc.stdin:
        try:
            proc.stdin.write(prompt.encode('utf-8'))
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            pass

    stderr_chunks: list[bytes] = []
    bytes_received = 0
    _PROGRESS_EVERY = 30_000
    _last_report_at = 0
    # V73: Activity-aware timeout (same pattern as _call_gemini_async).
    # Chat prompts are smaller so initial patience is 3× (vs 5× for audits).
    _initial_patience = timeout * 3
    _stream_patience = timeout
    _loop = asyncio.get_event_loop()
    deadline = _loop.time() + _initial_patience
    has_yielded = False

    try:
        while True:
            if _stop_requested:
                try: proc.kill()
                except Exception: pass
                raise RuntimeError("Gemini CLI cancelled (safe stop requested)")

            now_t = _loop.time()
            if now_t > deadline:
                _phase = "thinking" if bytes_received == 0 else "streaming"
                try: proc.kill()
                except Exception: pass
                raise RuntimeError(
                    f"Gemini CLI timed out after {int(now_t - (deadline - (_initial_patience if bytes_received == 0 else _stream_patience)))}s "
                    f"({_phase} phase, {bytes_received} bytes received)"
                )

            try:
                # Need smaller chunks and faster timeout for smooth streaming
                chunk = await asyncio.wait_for(proc.stdout.read(1024), timeout=0.1)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue

            if chunk == b'':
                break

            bytes_received += len(chunk)
            decoded_chunk = chunk.decode("utf-8", errors="replace")
            if decoded_chunk:
                has_yielded = True
                yield decoded_chunk

            # V73: Reset deadline — data is flowing, not stalled.
            deadline = _loop.time() + _stream_patience

            if bytes_received - _last_report_at >= _PROGRESS_EVERY:
                _last_report_at = bytes_received
                _cb('progress', f'🧠 Gemini streaming… {bytes_received // 1024} KB received')

        try:
            stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=1)
            if stderr_data:
                stderr_chunks.append(stderr_data)
        except asyncio.TimeoutError:
            pass

        await asyncio.wait_for(proc.wait(), timeout=5)

    except RuntimeError:
        raise
    except Exception as _exc:
        try: proc.kill()
        except Exception: pass
        raise RuntimeError(f"Gemini CLI stream error: {_exc}") from _exc

    if proc.returncode != 0:
        stderr = b''.join(stderr_chunks)
        err = stderr.decode('utf-8', errors='replace').strip()

        _is_mcp_only_error = (
            proc.returncode == 1
            and "MCP error" in err
            and not any(k in err for k in ("PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
                                            "INVALID_ARGUMENT", "API key", "quota"))
        )
        
        if _is_mcp_only_error and has_yielded:
            logger.debug("⚡  [Gemini] Stream MCP extension error on exit (non-fatal)")
        elif _is_mcp_only_error and not has_yielded:
            raise RuntimeError(f"Gemini CLI exited with code {proc.returncode}: {err[:500]}")
        else:
            raise RuntimeError(f"Gemini CLI exited with code {proc.returncode}: {err[:500]}")


def _call_gemini_sync(prompt: str, timeout: int, model: str | None = None) -> str:
    """Synchronous subprocess call to the Gemini CLI with -m model flag."""
    gemini_cmd = config.get_gemini_cli_cmd()
    if not model:
        model = _get_best_model()
        if not model:
            raise RuntimeError("ALL_MODELS_EXHAUSTED")

    # V8: Build command with -m flag.
    if config.IS_WINDOWS:
        cmd = f'"{gemini_cmd}" -m "{model}"'
    else:
        cmd = [gemini_cmd, "-m", model]

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=config.IS_WINDOWS,
            encoding='utf-8',
            errors='replace',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Gemini CLI timed out after {timeout}s")

    if result.returncode != 0:
        raise RuntimeError(
            f"Gemini CLI exited with code {result.returncode}: {result.stderr[:500]}"
        )

    return result.stdout.strip()


# ─────────────────────────────────────────────────────────────
# V8: Error Self-Analysis
# ─────────────────────────────────────────────────────────────

async def _diagnose_gemini_error(error_text: str, original_prompt: str) -> str | None:
    """
    When Gemini CLI fails, feed the error back through Gemini to diagnose it.

    This uses the standard stdin-piped call (which works), not the file call
    (which may be the one that just failed).

    Returns diagnosis text, or None on failure.
    """
    try:
        diag_prompt = (
            f"The Gemini CLI just failed with this error:\n\n"
            f"{error_text}\n\n"
            f"The original prompt I was trying to send (first 200 chars):\n"
            f"{original_prompt[:200]}\n\n"
            f"What is the most likely cause? How should I fix the CLI invocation?\n"
            f"Keep your answer under 3 bullet points."
        )
        _glass_brain_send(f"[🔬 ERROR SELF-ANALYSIS]\n{diag_prompt[:300]}")

        response = await _call_gemini_async(diag_prompt, timeout=60)
        if response:
            _glass_brain_receive(f"[🔬 DIAGNOSIS]: {response[:500]}")
            return response
    except Exception as exc:
        logger.debug("Error self-analysis failed (non-critical): %s", exc)

    return None


# ─────────────────────────────────────────────────────────────
# Cache management
# ─────────────────────────────────────────────────────────────

def clear_cache() -> None:
    """Clear the session cache."""
    _session_cache.clear()
    logger.debug("🧠  Session cache cleared.")


def cache_stats() -> dict:
    """Return cache statistics."""
    return {
        "entries": len(_session_cache),
        "max_size": _MAX_CACHE_SIZE,
    }


# ─────────────────────────────────────────────────────────────
# V11: Self-Healing Architecture
# ─────────────────────────────────────────────────────────────

async def self_diagnose(error: str, context: str = "") -> str:
    """
    Feed a CLI error back to Gemini (using Flash) for diagnosis.

    Uses Flash model to conserve Pro quota — diagnosis doesn't need
    the most powerful model, just fast pattern recognition.

    V12 FIX: Pipes prompt via stdin instead of -p flag to avoid
    shell injection from special chars in error messages.

    Args:
        error: The error message/text to diagnose.
        context: Optional context about what was being attempted.

    Returns:
        Diagnosis and suggested fix, or empty string on failure.
    """
    try:
        diag_prompt = (
            "You are a CLI error diagnostician. Analyze this error concisely.\n\n"
            f"ERROR:\n{error[:1000]}\n\n"
        )
        if context:
            diag_prompt += f"CONTEXT: {context[:500]}\n\n"
        diag_prompt += (
            "Respond with:\n"
            "1. ROOT CAUSE (one line)\n"
            "2. FIX (one actionable line)\n"
            "3. PREVENTION (one line)\n"
            "Keep it under 5 lines total."
        )

        # Use Flash for diagnosis — pipe via stdin for safety
        gemini_cmd = config.get_gemini_cli_cmd()
        flash_model = config.GEMINI_DEFAULT_FLASH

        if config.IS_WINDOWS:
            cmd = f'"{gemini_cmd}" -m "{flash_model}"'
        else:
            cmd = [gemini_cmd, "-m", flash_model]

        result = subprocess.run(
            cmd,
            input=diag_prompt,
            capture_output=True, text=True, timeout=30,
            shell=config.IS_WINDOWS, encoding='utf-8', errors='replace',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

        if result.returncode == 0 and result.stdout.strip():
            diagnosis = result.stdout.strip()
            logger.info("🔬  Self-diagnosis result: %s", diagnosis[:200])
            _glass_brain_receive(f"[🔬 SELF-DIAGNOSIS]: {diagnosis[:500]}")
            return diagnosis

    except Exception as exc:
        logger.debug("🔬  Self-diagnosis failed (non-critical): %s", exc)

    return ""


async def request_self_improvement(issue_summary: str = "") -> str:
    """
    Ask Gemini to review recent errors and suggest supervisor improvements.

    V12 FIX: Pipes prompt via stdin instead of -p flag to avoid
    shell injection from special characters in error summaries.

    This function:
      1. Collects recent error patterns from session memory
      2. Asks Gemini Flash to analyze and suggest optimizations
      3. Logs suggestions to disk (NEVER auto-applies)

    Returns:
        Improvement suggestions, or empty string on failure.
    """
    try:
        prompt = (
            "You are reviewing a Python supervisor system. "
            "Analyze these recent issues and suggest specific improvements.\n\n"
        )
        if issue_summary:
            prompt += f"RECENT ISSUES:\n{issue_summary[:2000]}\n\n"
        else:
            prompt += "No specific issues reported. Suggest general optimizations.\n\n"

        prompt += (
            "Respond with EXACTLY 3 bullet points:\n"
            "- [EFFICIENCY] One suggestion to improve speed/performance\n"
            "- [RELIABILITY] One suggestion to improve error handling\n"
            "- [INTELLIGENCE] One suggestion to improve decision-making\n"
            "Keep each bullet under 50 words."
        )

        # Use Flash — pipe via stdin for safety
        gemini_cmd = config.get_gemini_cli_cmd()
        flash_model = config.GEMINI_DEFAULT_FLASH

        if config.IS_WINDOWS:
            cmd = f'"{gemini_cmd}" -m "{flash_model}"'
        else:
            cmd = [gemini_cmd, "-m", flash_model]

        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True, text=True, timeout=45,
            shell=config.IS_WINDOWS, encoding='utf-8', errors='replace',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

        if result.returncode == 0 and result.stdout.strip():
            suggestions = result.stdout.strip()
            logger.info("🔧  Self-improvement suggestions: %s", suggestions[:300])

            # Log to disk (never auto-apply)
            try:
                log_path = config.SELF_IMPROVEMENT_LOG_PATH
                with open(log_path, "a", encoding="utf-8") as f:
                    ts = datetime.now(timezone.utc).isoformat()
                    f.write(f"\n--- {ts} ---\n")
                    f.write(f"Issues: {issue_summary[:500]}\n")
                    f.write(f"Suggestions:\n{suggestions}\n")
            except Exception:
                pass

            return suggestions

    except Exception as exc:
        logger.debug("🔧  Self-improvement request failed (non-critical): %s", exc)

    return ""
