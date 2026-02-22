"""
gemini_advisor.py — Centralized Gemini CLI Interface (V9: OpenClaw-Enhanced).

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

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
# Keys are the first 200 chars of the prompt, values are responses.
# ─────────────────────────────────────────────────────────────
_session_cache: dict[str, str] = {}
_MAX_CACHE_SIZE = 50


def _cache_key(prompt: str) -> str:
    """Create a cache key from the first 200 chars of the prompt."""
    return prompt.strip()[:200]


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from Gemini's response."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json|python|javascript|html|css|bash|sh)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


# ─────────────────────────────────────────────────────────────
# Model Resolution — delegated to ModelFailoverChain
# ─────────────────────────────────────────────────────────────

_cached_best_model: Optional[str] = None


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
        model = chain.get_active_model()

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
                model = data["model"]
                logger.info("🧠  Using cached best model: %s (%.1fh old)", model, age_hours)
                return model
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
                capture_output=True, text=True, timeout=30,
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

    # Check cache.
    if use_cache and key in _session_cache:
        logger.debug("🧠  Cache hit for prompt (%.60s…)", key)
        return _session_cache[key]

    # Glass Brain: show what we're sending.
    _glass_brain_send(prompt)

    last_error: Optional[Exception] = None
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

    # Smart model selection based on prompt complexity
    # _get_best_model already handles rate limit cooldowns internally
    model = _get_best_model(prompt)

    if not model:
        raise RuntimeError("ALL_MODELS_EXHAUSTED: Every available model is on cooldown.")

    # V31 Fix 1.3: Outer safety timeout — if the entire retry loop hangs
    # (subprocess deadlock, infinite rate-limit wait), bail out cleanly.
    outer_timeout = timeout * (attempts + 1)  # e.g. 180s * 4 = 720s

    async def _inner_retry_loop():
        nonlocal model
        last_err: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                response = await _call_gemini_async(prompt, timeout, model=model)
                if not response:
                    raise RuntimeError("Gemini CLI returned an empty response.")

                # Glass Brain: show the response.
                _glass_brain_receive(response)

                # Track context budget.
                budget.record(len(prompt), len(response), model=model)

                # Report success to failover chain.
                chain.report_success(model)

                # Feed router adaptive learning.
                router = get_router()
                tier = router.classify(prompt)
                router.record_outcome(tier, success=True)

                # Cache the response.
                if use_cache:
                    if len(_session_cache) >= _MAX_CACHE_SIZE:
                        oldest_key = next(iter(_session_cache))
                        del _session_cache[oldest_key]
                    _session_cache[key] = response

                logger.info(
                    "🧠  Gemini response (%d chars, attempt %d/%d, model=%s): %.120s…",
                    len(response), attempt + 1, attempts, model, response,
                )
                return response

            except Exception as exc:
                last_err = exc
                error_text = str(exc)
                _glass_brain_error(f"Attempt {attempt + 1}/{attempts} failed: {exc}")

                # ── Rate limit detection ──
                if RateLimitTracker.is_rate_limit_error(error_text):
                    wait_s = rate_tracker.record_rate_limit(model, error_text)
                    # Try downgrading to Flash before waiting
                    alt_model = rate_tracker.suggest_alternative_model(model)
                    if alt_model != model:
                        logger.info("⚡  Rate-limited on %s → switching to %s", model, alt_model)
                        model = alt_model
                        # Don't wait the full cooldown if we have an alternative
                        await asyncio.sleep(min(5.0, wait_s))
                        continue
                    else:
                        # Already on Flash, must wait
                        logger.info("⚡  Rate-limited on %s, waiting %ds", model, wait_s)
                        await asyncio.sleep(min(wait_s, config.RATE_LIMIT_MAX_WAIT_S))
                        continue

                # ── Normal failure handling ──
                chain.report_failure(model)
                model = chain.get_active_model()  # Get next model

                # Feed router adaptive learning.
                router = get_router()
                tier = router.classify(prompt)
                router.record_outcome(tier, success=False)

                if policy.should_retry(attempt):
                    delay = policy.delay_for(attempt)
                    logger.warning(
                        "🧠  Gemini CLI attempt %d/%d failed: %s — retrying in %.1fs (model: %s) …",
                        attempt + 1, attempts, exc, delay, model,
                    )
                    await asyncio.sleep(delay)
                else:
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


async def ask_gemini_json(
    prompt: str,
    timeout: int | None = None,
    use_cache: bool = True,
) -> Optional[dict]:
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

    last_error: Optional[Exception] = None

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
                model = chain.get_active_model()
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
            model = chain.get_active_model()

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
            model = chain.get_active_model()

            if policy.should_retry(attempt):
                delay = policy.delay_for(attempt)
                await asyncio.sleep(delay)
                continue
            else:
                break

    # Cleanup temp file
    try:
        prompt_file.unlink(missing_ok=True)
    except OSError:
        pass

    raise RuntimeError(f"Gemini CLI with file failed after {policy.max_attempts} attempts: {last_error}")


async def call_gemini_with_file_json(
    prompt: str,
    file_path: str,
    timeout: int | None = None,
) -> Optional[dict]:
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


def _extract_json_object(text: str) -> Optional[dict]:
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
    cleaned = _strip_markdown_fences(text)
    valid_objects = []
    
    for start_idx in range(len(cleaned)):
        if cleaned[start_idx] == '{':
            depth = 0
            in_string = False
            escape = False
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
                            break
                            
    if valid_objects:
        # Return the largest valid object by string length
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
    model = chain.get_active_model()

    if not model:
        raise RuntimeError("ALL_MODELS_EXHAUSTED: Every available model is on cooldown.")

    # Glass Brain: show what we're sending.
    _glass_brain_send(prompt)

    last_error: Optional[Exception] = None

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

            logger.info(
                "🧠  Gemini sync response (%d chars, attempt %d/%d): %.120s…",
                len(response), attempt + 1, attempts, response,
            )
            return response
        except Exception as exc:
            last_error = exc
            _glass_brain_error(f"Sync attempt {attempt + 1}/{attempts} failed: {exc}")
            chain.report_failure(model)
            model = chain.get_active_model()

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
) -> Optional[dict]:
    """Synchronous version of ask_gemini_json."""
    try:
        raw = ask_gemini_sync(prompt, timeout=timeout)
    except RuntimeError:
        return None

    cleaned = _strip_markdown_fences(raw)
    json_match = re.search(r"\{[^}]+\}", cleaned, re.DOTALL)
    if not json_match:
        json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)

    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ─────────────────────────────────────────────────────────────
# Low-level subprocess wrappers (V8: all include -m model flag)
# ─────────────────────────────────────────────────────────────

async def _call_gemini_async(prompt: str, timeout: int, model: Optional[str] = None) -> str:
    """Async subprocess call to the Gemini CLI with -m model flag."""
    gemini_cmd = config.get_gemini_cli_cmd()
    if not model:
        model = _get_best_model()
        if not model:
            raise RuntimeError("ALL_MODELS_EXHAUSTED")

    if config.IS_WINDOWS:
        # V32: Use --model flag (current Gemini CLI v0.29+ API)
        cmd_str = f'"{gemini_cmd}" --model "{model}"'
        proc = await asyncio.create_subprocess_shell(
            cmd_str,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            gemini_cmd, "--model", model,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(f"Gemini CLI timed out after {timeout}s")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Gemini CLI exited with code {proc.returncode}: {err[:500]}")

    return stdout.decode("utf-8", errors="replace").strip()


def _call_gemini_sync(prompt: str, timeout: int, model: Optional[str] = None) -> str:
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

async def _diagnose_gemini_error(error_text: str, original_prompt: str) -> Optional[str]:
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
