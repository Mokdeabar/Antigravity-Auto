"""
retry_policy.py — OpenClaw-Inspired Retry, Failover & Budget Systems.

Three production-grade systems inspired by OpenClaw's architecture:

  1. RetryPolicy       — Exponential backoff with jitter (replaces fixed 2s sleep)
  2. ModelFailoverChain — Model fallback with per-model cooldowns (1m→5m→25m→1h)
  3. ContextBudget      — Track chars/tokens sent to Gemini per session

These classes are used by gemini_advisor.py and main.py to provide
resilient, efficient communication with the Gemini CLI.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


def _get_pacific_tz():
    """Get Pacific timezone, auto-installing tzdata if missing on Windows."""
    try:
        return ZoneInfo("America/Los_Angeles")
    except Exception:
        import subprocess, sys
        logger = logging.getLogger("supervisor.retry_policy")
        logger.warning("⚠️  tzdata missing — auto-installing…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "tzdata"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return ZoneInfo("America/Los_Angeles")

from . import config

logger = logging.getLogger("supervisor.retry_policy")


# ─────────────────────────────────────────────────────────────
# 1. RetryPolicy — Exponential backoff with jitter
# ─────────────────────────────────────────────────────────────

class RetryPolicy:
    """
    Configurable retry policy with exponential backoff and jitter.

    Inspired by OpenClaw's retry policy:
      - 3 max attempts (default)
      - 30s delay cap
      - 10% jitter to prevent thundering herd
      - Never retries non-idempotent operations

    Usage:
        policy = RetryPolicy()
        for attempt in range(policy.max_attempts):
            try:
                result = do_thing()
                break
            except Exception:
                if attempt < policy.max_attempts - 1:
                    await asyncio.sleep(policy.delay_for(attempt))
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay_s: float = 2.0,
        max_delay_s: float = 30.0,
        jitter_pct: float = 0.10,
    ):
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.jitter_pct = jitter_pct

    def delay_for(self, attempt: int) -> float:
        """
        Calculate delay for the given attempt number (0-indexed).
        Returns seconds to sleep before the next retry.

        Formula: min(base * 2^attempt, max_delay) ± jitter
        """
        base = min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
        jitter = base * self.jitter_pct * random.uniform(-1, 1)
        return max(0, base + jitter)

    def should_retry(self, attempt: int) -> bool:
        """Return True if another attempt should be made."""
        return attempt < self.max_attempts - 1

    def __repr__(self) -> str:
        return (
            f"RetryPolicy(max_attempts={self.max_attempts}, "
            f"base_delay={self.base_delay_s}s, "
            f"max_delay={self.max_delay_s}s, "
            f"jitter={self.jitter_pct:.0%})"
        )


# ─────────────────────────────────────────────────────────────
# 2. ModelFailoverChain — Cooldown-based model fallback
# ─────────────────────────────────────────────────────────────

class ModelFailoverChain:
    """
    Model failover chain with per-model cooldowns.

    Inspired by OpenClaw's model failover system:
      - Ordered list of models (primary → fallbacks)
      - On failure: report_failure() → cooldown the model
      - Cooldown escalation: 1m → 5m → 25m → 1h (cap)
      - Billing disable: exponential backoff up to 24h
      - Session stickiness: once a fallback is used, stick with it
      - State persisted to disk alongside the model cache

    Usage:
        chain = ModelFailoverChain()
        model = chain.get_active_model()
        try:
            result = call_gemini(model)
            chain.report_success(model)
        except Exception:
            chain.report_failure(model)
            # Next call to get_active_model() returns the next model
    """

    # V40: Read from config (optimized for AI Ultra: 30s→2m→10m→30m)
    COOLDOWN_DELAYS = config.GEMINI_COOLDOWN_DELAYS

    def __init__(
        self,
        models: list[str] | None = None,
        state_path: Path | None = None,
    ):
        # V73: Filter out image-bucket models — they're for image generation
        # only and time out on text/code tasks, cascading cooldowns everywhere.
        _all = models or list(config.GEMINI_MODEL_PROBE_LIST)
        self._models = [m for m in _all if config.classify_model(m) != "image"]
        self._state_path = state_path or (
            config.get_state_dir() / "_failover_state.json"
        )

        # Per-model state
        self._cooldown_expiry: dict[str, float] = {}   # model → epoch when cooldown ends
        self._failure_count: dict[str, int] = {}        # model → consecutive failures
        self._success_count: dict[str, int] = {}        # model → total successes
        self._sticky_model: str | None = None        # session-sticky model

        self._load_state()

    def _load_state(self) -> None:
        """Load failover state from disk."""
        try:
            # V52: Try project-specific path first, then global fallback
            _paths = [self._state_path]
            _global = Path(__file__).parent / "_failover_state.json"
            if _global != self._state_path:
                _paths.append(_global)

            for _path in _paths:
                if _path.exists():
                    data = json.loads(_path.read_text(encoding="utf-8"))
                    now = time.time()
                    _all_cooldowns = data.get("cooldowns", {})
                    self._cooldown_expiry = {
                        k: v for k, v in _all_cooldowns.items()
                        if v > now  # Only load active cooldowns
                    }
                    _expired = [k for k, v in _all_cooldowns.items() if v <= now]
                    # V30.5: Cap stale failure counts to prevent zombie cooldowns
                    self._failure_count = {
                        k: min(v, 3) for k, v in data.get("failures", {}).items()
                    }
                    self._success_count = data.get("successes", {})
                    # V41 FIX (Bug 3): Restore sticky model preference
                    _saved_sticky = data.get("sticky_model")
                    if _saved_sticky and _saved_sticky in self._models:
                        self._sticky_model = _saved_sticky
                    logger.info(
                        "🔄  Loaded failover state: %d active cooldowns, %d expired, sticky=%s",
                        len(self._cooldown_expiry), len(_expired),
                        self._sticky_model or "(none)",
                    )
                    if self._cooldown_expiry:
                        for m, exp in self._cooldown_expiry.items():
                            logger.info("🔄    %s: cooldown expires in %.0fs", m, exp - now)
                    if _expired:
                        logger.info("🔄    Expired: %s", ", ".join(_expired))
                    return  # Successfully loaded
        except Exception as exc:
            logger.debug("Could not load failover state: %s", exc)

    def _save_state(self) -> None:
        """Save failover state to disk."""
        try:
            data = {
                "cooldowns": self._cooldown_expiry,
                "failures": self._failure_count,
                "successes": self._success_count,
                "sticky_model": self._sticky_model,  # V41 FIX (Bug 3)
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            _json = json.dumps(data, indent=2)
            self._state_path.write_text(_json, encoding="utf-8")
            # V52: Also save to global location for launcher page access
            _global = Path(__file__).parent / "_failover_state.json"
            if _global != self._state_path:
                try:
                    _global.write_text(_json, encoding="utf-8")
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Could not save failover state: %s", exc)

    def get_active_model(self, pro_only: bool = False) -> str:
        """
        Return the best available model, respecting cooldowns.

        Priority: always return the highest-priority (lowest index) model
        that is not currently in cooldown. Sticky is only used as a
        tiebreaker across equal-priority calls — it never blocks a
        higher-priority model from being selected once it recovers.

        This ensures immediate promotion back to gemini-3.1-pro-preview
        as soon as its cooldown expires, instead of staying on a fallback.

        V74: When pro_only=True, ONLY return models from the 'pro' bucket.
        If no Pro model is available, return None (pause signal) instead of
        falling over to Flash/Lite. This ensures coding tasks always use
        the best model and pause when quota is exhausted.
        """
        now = time.time()

        # Find the best (lowest index) uncooled model
        best_available = None
        for model in self._models:
            if self._is_available(model, now):
                # V74: If pro_only, skip non-pro models
                if pro_only and config.classify_model(model) != "pro":
                    continue
                best_available = model
                break

        if best_available is None:
            if pro_only:
                logger.warning(
                    "⏸  All Pro models on cooldown. PRO_ONLY_CODING active — "
                    "pausing until Pro quota recovers.",
                )
            else:
                logger.warning(
                    "🔄  ALL models on cooldown. Signaling caller to skip this tick.",
                )
            return None

        # V54: Promote immediately if a better model is now available.
        # The models list is ordered best→worst. If the best available
        # model has a lower index than the current sticky, log a promotion
        # and switch over right away.
        if (
            self._sticky_model
            and self._sticky_model != best_available
            and self._is_available(self._sticky_model, now)
        ):
            # Both are available — keep sticky only if it IS the best
            sticky_idx = self._models.index(self._sticky_model) if self._sticky_model in self._models else 9999
            best_idx   = self._models.index(best_available)
            if best_idx < sticky_idx:
                # Better model has recovered — promote
                G = config.ANSI_GREEN
                R = config.ANSI_RESET
                logger.info(
                    "🔄  [Model Restore] %s recovered — promoting from %s → %s",
                    best_available, self._sticky_model, best_available,
                )
                print(f"  {G}🔄 Model restored: {self._sticky_model} → {best_available}{R}")
                self._sticky_model = best_available
                self._save_state()
            # If sticky is already the best, fall through and return it
            return self._sticky_model

        # Promote/set sticky to best available
        if self._sticky_model != best_available:
            self._sticky_model = best_available
        return best_available


    def _is_available(self, model: str, now: float) -> bool:
        """Check if a model is not in cooldown."""
        return self._cooldown_expiry.get(model, 0) <= now

    def all_models_on_cooldown(self) -> bool:
        """
        V30.1: Return True if EVERY model in the chain is actively cooled down.

        Used by main.py to gracefully skip LLM calls during rate limit
        storms instead of crashing or firing into a 429 wall.
        """
        now = time.time()
        return all(
            self._cooldown_expiry.get(m, 0) > now
            for m in self._models
        )

    def get_soonest_cooldown_remaining(self) -> float:
        """Return seconds until the soonest model exits cooldown."""
        now = time.time()
        if not self._cooldown_expiry:
            return 0.0
        soonest = min(
            self._cooldown_expiry.get(m, 0) for m in self._models
        )
        return max(0.0, soonest - now)

    def report_failure(self, model: str) -> None:
        """
        Report a model failure. Applies escalating cooldown.

        Cooldown escalation: 1m → 5m → 25m → 1h (capped).
        """
        failures = self._failure_count.get(model, 0) + 1
        self._failure_count[model] = failures

        delay_idx = min(failures - 1, len(self.COOLDOWN_DELAYS) - 1)
        delay = self.COOLDOWN_DELAYS[delay_idx]

        self._cooldown_expiry[model] = time.time() + delay

        # Clear sticky if it's the model that just failed
        if self._sticky_model == model:
            self._sticky_model = None

        M = config.ANSI_MAGENTA
        R = config.ANSI_RESET
        logger.warning(
            "🔄  Model %s failed (attempt #%d). Cooldown: %ds",
            model, failures, delay,
        )
        print(f"  {M}🔄 Model {model} failed (#{failures}). Cooldown: {delay}s{R}")

        self._save_state()

        # V74: Refresh quota data on every failure — /stats is free
        # Only probe if PTY is already warm (avoid 60s cold-start in tests/early boot)
        try:
            _qp = get_quota_probe()
            if _qp._pty_ready:
                _qp.run_stats_probe()
        except Exception:
            pass

    def report_quota_exhausted(self, model: str, cooldown_seconds: float) -> None:
        """
        V40: Report a model quota exhaustion with an EXACT cooldown.

        Unlike report_failure() which uses escalating delays, this sets the
        cooldown to the exact duration from the API's TerminalQuotaError
        (e.g. "Your quota will reset after 12h39m13s").

        The model is cooled for the exact reset time and the failover chain
        immediately switches to the next available model.
        """
        self._cooldown_expiry[model] = time.time() + cooldown_seconds
        self._failure_count[model] = self._failure_count.get(model, 0) + 1

        # Clear sticky so next get_active_model() picks a different model
        if self._sticky_model == model:
            self._sticky_model = None

        M = config.ANSI_MAGENTA
        R = config.ANSI_RESET
        hours = int(cooldown_seconds // 3600)
        mins = int((cooldown_seconds % 3600) // 60)
        logger.warning(
            "⚡  Model %s QUOTA EXHAUSTED. Cooldown: %dh%dm",
            model, hours, mins,
        )
        print(f"  {M}⚡ Model {model} quota exhausted. Cooldown: {hours}h{mins}m{R}")

        # Try to find next available model
        next_model = self.get_active_model()
        if next_model:
            logger.info("🔄  Failover → model %s", next_model)
            print(f"  {M}🔄 Failing over to model: {next_model}{R}")
        else:
            logger.warning("🔄  ALL models exhausted — no failover available.")

        self._save_state()

        # V74: Refresh quota data on quota exhaustion — /stats is free
        try:
            _qp = get_quota_probe()
            if _qp._pty_ready:
                _qp.run_stats_probe()
        except Exception:
            pass

    def report_timeout(self, model: str) -> None:
        """
        V30.6: Report a model timeout (NOT a rate limit or error).

        Applies a short 30s cooldown WITHOUT incrementing failure count.
        Timeouts are transient network issues and should not trigger
        escalating 1h cooldowns like actual API errors do.
        """
        timeout_cooldown = 30  # Short cooldown for timeouts
        self._cooldown_expiry[model] = time.time() + timeout_cooldown

        # Clear sticky if it's the model that just timed out
        if self._sticky_model == model:
            self._sticky_model = None

        M = config.ANSI_MAGENTA
        R = config.ANSI_RESET
        logger.warning(
            "⏱️  Model %s timed out. Short cooldown: %ds (no failure escalation)",
            model, timeout_cooldown,
        )
        print(f"  {M}⏱️ Model {model} timed out. Cooldown: {timeout_cooldown}s{R}")

        self._save_state()

    def report_success(self, model: str) -> None:
        """Report a successful model call. Resets failure count."""
        self._failure_count[model] = 0
        self._success_count[model] = self._success_count.get(model, 0) + 1
        self._sticky_model = model
        # Don't save on every success (too much I/O), just clear cooldown
        if model in self._cooldown_expiry:
            del self._cooldown_expiry[model]

    def seconds_until_any_available(self) -> float:
        """
        V32: Return seconds until the soonest model comes off cooldown.

        Used by the WAITING handler when ALL_MODELS_EXHAUSTED to sleep
        for the exact right duration instead of a hardcoded 60s.

        Returns 0.0 if any model is already available.
        Returns 600.0 (10min cap) if no models have cooldown entries.
        """
        now = time.time()

        # Check if any model is already available
        for m in self._models:
            if self._is_available(m, now):
                return 0.0

        # Find the soonest expiry
        if not self._cooldown_expiry:
            return 600.0  # No cooldown data → fallback cap

        soonest = min(self._cooldown_expiry.values())
        remaining = soonest - now
        return max(0.0, remaining)

    def get_status(self) -> dict:
        """Return current failover chain status."""
        now = time.time()
        return {
            "active_model": self._sticky_model or self._models[0],
            "models": {
                m: {
                    "available": self._is_available(m, now),
                    "cooldown_remaining": max(0, self._cooldown_expiry.get(m, 0) - now),
                    "failures": self._failure_count.get(m, 0),
                    "successes": self._success_count.get(m, 0),
                }
                for m in self._models
            },
        }

    def __repr__(self) -> str:
        active = self.get_active_model()
        return f"ModelFailoverChain(active={active}, chain={self._models})"


# ─────────────────────────────────────────────────────────────
# 3. ContextBudget — Track Gemini context consumption
# ─────────────────────────────────────────────────────────────

class ContextBudget:
    """
    Track characters and estimated tokens sent to Gemini.

    Inspired by OpenClaw's /context command which breaks down
    context window usage per-component.

    Tracks:
      - Total chars sent (prompts)
      - Total chars received (responses)
      - Per-call history for the last N calls
      - Warns when approaching budget limits
    """

    # Rough estimate: ~4 chars per token (OpenAI-style)
    CHARS_PER_TOKEN = 4

    def __init__(
        self,
        warn_chars: int = 500_000,
        max_chars: int = 2_000_000,
        history_size: int = 50,
    ):
        self.warn_chars = warn_chars
        self.max_chars = max_chars
        self.history_size = history_size

        self._total_sent = 0
        self._total_received = 0
        self._call_count = 0
        self._history: list[dict] = []
        self._warned = False
        self._session_start = time.time()

    def record(self, prompt_chars: int, response_chars: int, model: str = "") -> None:
        """Record a Gemini call's context usage."""
        self._total_sent += prompt_chars
        self._total_received += response_chars
        self._call_count += 1

        entry = {
            "prompt_chars": prompt_chars,
            "response_chars": response_chars,
            "model": model,
            "timestamp": time.time(),
        }
        self._history.append(entry)
        if len(self._history) > self.history_size:
            self._history = self._history[-self.history_size:]

        # Warn if approaching budget
        if not self._warned and self._total_sent > self.warn_chars:
            self._warned = True
            logger.warning(
                "📊  Context budget warning: %d chars sent (%.0f%% of %d limit)",
                self._total_sent,
                (self._total_sent / self.max_chars) * 100,
                self.max_chars,
            )

    @property
    def total_sent(self) -> int:
        return self._total_sent

    @property
    def total_received(self) -> int:
        return self._total_received

    @property
    def estimated_tokens_sent(self) -> int:
        return self._total_sent // self.CHARS_PER_TOKEN

    @property
    def estimated_tokens_received(self) -> int:
        return self._total_received // self.CHARS_PER_TOKEN

    @property
    def budget_pct(self) -> float:
        """Return percentage of budget consumed."""
        return min(100.0, (self._total_sent / self.max_chars) * 100)

    def get_report(self) -> str:
        """Generate a human-readable context budget report."""
        duration = (time.time() - self._session_start) / 60
        avg_prompt = self._total_sent // max(1, self._call_count)
        avg_response = self._total_received // max(1, self._call_count)

        return (
            f"📊 Context Budget Report ({duration:.0f}m session)\n"
            f"   Calls: {self._call_count}\n"
            f"   Sent: {self._total_sent:,} chars (~{self.estimated_tokens_sent:,} tokens)\n"
            f"   Received: {self._total_received:,} chars (~{self.estimated_tokens_received:,} tokens)\n"
            f"   Avg prompt: {avg_prompt:,} chars | Avg response: {avg_response:,} chars\n"
            f"   Budget: {self.budget_pct:.1f}% of {self.max_chars:,} char limit"
        )

    def should_prune(self) -> bool:
        """Return True if context consumption suggests pruning is needed."""
        return self._total_sent > self.warn_chars

    # V74: Proactive context budget gate (§3.2) ───────────────────
    def would_fit(self, prompt_chars: int) -> bool:
        """
        V74: Check if a prompt of this size would fit within the remaining budget.
        Use BEFORE sending to Gemini to avoid wasted API calls.
        """
        return (self._total_sent + prompt_chars) <= self.max_chars

    def suggest_trim(self, prompt_chars: int) -> int:
        """
        V74: Return how many characters must be trimmed from a prompt to fit
        within the remaining budget. Returns 0 if prompt already fits.
        """
        overshoot = (self._total_sent + prompt_chars) - self.max_chars
        return max(0, overshoot)

    @property
    def remaining_chars(self) -> int:
        """V74: Characters remaining in the budget before hitting the limit."""
        return max(0, self.max_chars - self._total_sent)

    def __repr__(self) -> str:
        return (
            f"ContextBudget(sent={self._total_sent:,}, "
            f"calls={self._call_count}, "
            f"budget={self.budget_pct:.1f}%)"
        )


# ─────────────────────────────────────────────────────────────
# 4. TaskComplexityRouter — Smart model selection by task type
# ─────────────────────────────────────────────────────────────

class TaskComplexityRouter:
    """
    Route prompts to the right model tier based on task complexity.

    V12 UPGRADE — Adaptive Learning:
      - Persists routing stats to _router_stats.json across restarts
      - Tracks per-tier success/failure rates
      - Auto-escalates Flash → Pro when Flash failure > 30%
      - Gets smarter with every call

    - Complex tasks (errors, architecture, multi-file) → Pro models
    - Simple tasks (yes/no, status, formatting) → Flash models
    - Default → Auto (let Gemini decide)

    This saves Pro quota for when it matters most.
    """

    # Patterns indicating complex tasks → Pro
    COMPLEX_PATTERNS = [
        re.compile(r"(?:Error|Exception|Traceback|FATAL|FAILED)[:\s]", re.IGNORECASE),
        re.compile(r"(?:debug|diagnos|troubleshoot|investigate)", re.IGNORECASE),
        re.compile(r"(?:architect|design|refactor|restructur)", re.IGNORECASE),
        re.compile(r"(?:security|vulnerabilit|auth|encrypt)", re.IGNORECASE),
        re.compile(r"(?:multi.?file|across.*files|entire codebase)", re.IGNORECASE),
        re.compile(r"(?:fix.*bug|resolve.*issue|root cause)", re.IGNORECASE),
        re.compile(r"```[\s\S]{500,}```", re.MULTILINE),  # Large code blocks
        re.compile(r"(?:implement|build|create).*(?:system|engine|module)", re.IGNORECASE),
    ]

    # Patterns indicating simple tasks → Flash
    SIMPLE_PATTERNS = [
        re.compile(r"^(?:Reply with|Say|Respond with)\s", re.IGNORECASE),
        re.compile(r"^(?:yes|no|ok|true|false)\s*\??$", re.IGNORECASE),
        re.compile(r"(?:format|convert|summarize in one)", re.IGNORECASE),
        re.compile(r"(?:what is the status|is it running|check if)", re.IGNORECASE),
        re.compile(r"(?:list the|count the|how many)", re.IGNORECASE),
        re.compile(r"(?:extract.*from|parse.*json|parse.*output)", re.IGNORECASE),
    ]

    _FLASH_FAIL_THRESHOLD = 0.30  # Escalate Flash → Pro when > 30% failure

    def __init__(self):
        self._STATS_PATH = config.get_state_dir() / "_router_stats.json"
        self._route_history: list[dict] = []
        self._pro_count = 0
        self._flash_count = 0
        self._auto_count = 0
        # Per-tier success/failure for adaptive learning
        self._tier_outcomes: dict[str, dict] = {
            "pro": {"success": 0, "failure": 0},
            "flash": {"success": 0, "failure": 0},
            "auto": {"success": 0, "failure": 0},
        }
        self._flash_escalated = False  # True when Flash is auto-disabled
        self._load_stats()

    def classify(self, prompt: str) -> str:
        """
        Classify a prompt's complexity.

        Returns: 'pro', 'flash', or 'auto'
        """
        # Short prompts are almost always simple
        if len(prompt) < 50:
            for pattern in self.SIMPLE_PATTERNS:
                if pattern.search(prompt):
                    return "flash"

        # Check for complex patterns first (they take priority)
        complex_score = 0
        for pattern in self.COMPLEX_PATTERNS:
            if pattern.search(prompt):
                complex_score += 1

        # Check for simple patterns
        simple_score = 0
        for pattern in self.SIMPLE_PATTERNS:
            if pattern.search(prompt):
                simple_score += 1

        # Decide based on scores
        if complex_score >= 2:
            return "pro"
        elif complex_score == 1 and simple_score == 0:
            return "pro"
        elif simple_score >= 1 and complex_score == 0:
            return "flash"
        else:
            return "auto"

    def get_model_for(self, prompt: str) -> str:
        """
        Return the actual model name for a prompt.

        Uses classify() to determine tier, then picks from config lists.
        Applies adaptive escalation if Flash is failing too often.

        V74: When PRO_ONLY_CODING is enabled, ALL coding prompts go to Pro.
        Flash classification is only honoured for explicit preferred_tier='flash'
        callers (lint, health checks) — not for the coding pipeline.
        """
        tier = self.classify(prompt)

        # V74: PRO_ONLY_CODING — force all coding to Pro, never Flash
        _pro_only = getattr(config, "PRO_ONLY_CODING", False)
        if _pro_only and tier == "flash":
            logger.info(
                "🎯  [V74] PRO_ONLY_CODING active — overriding Flash → Pro for coding task"
            )
            tier = "pro"

        # Adaptive: if Flash is failing > 30%, escalate to Auto
        if tier == "flash" and self._should_escalate_flash():
            logger.info("🎯  Flash failure rate > 30%% — escalating to Auto")
            tier = "auto"
            self._flash_escalated = True

        if tier == "flash":
            model = config.GEMINI_DEFAULT_FLASH
            self._flash_count += 1
        elif tier == "pro":
            # Use failover chain for pro (it handles cooldowns)
            chain = get_failover_chain()
            model = chain.get_active_model(pro_only=_pro_only)
            self._pro_count += 1
        else:
            # Auto — let the failover chain decide
            chain = get_failover_chain()
            model = chain.get_active_model(pro_only=_pro_only)
            self._auto_count += 1

        logger.debug(
            "🎯  Model routing: tier=%s → model=%s (pro_only=%s, prompt: %.60s…)",
            tier, model, _pro_only, prompt,
        )
        return model

    def record_outcome(self, tier: str, success: bool) -> None:
        """
        Record whether a call on a given tier succeeded or failed.

        This feeds the adaptive learning system. After enough data,
        the router auto-escalates tiers with high failure rates.
        """
        if tier not in self._tier_outcomes:
            self._tier_outcomes[tier] = {"success": 0, "failure": 0}
        key = "success" if success else "failure"
        self._tier_outcomes[tier][key] += 1

        # Reset escalation if Flash starts working again
        if tier == "flash" and success and self._flash_escalated:
            flash = self._tier_outcomes["flash"]
            total = flash["success"] + flash["failure"]
            if total >= 5:
                rate = flash["failure"] / max(1, total)
                if rate < self._FLASH_FAIL_THRESHOLD:
                    self._flash_escalated = False
                    logger.info("🎯  Flash recovery detected — re-enabling Flash routing")

        # Persist periodically (every 10 outcomes)
        total_all = sum(
            v["success"] + v["failure"] for v in self._tier_outcomes.values()
        )
        if total_all % 10 == 0:
            self._save_stats()

    def _should_escalate_flash(self) -> bool:
        """Return True if Flash failure rate exceeds threshold."""
        flash = self._tier_outcomes.get("flash", {})
        total = flash.get("success", 0) + flash.get("failure", 0)
        if total < 5:
            return False  # Not enough data yet
        rate = flash.get("failure", 0) / max(1, total)
        return rate > self._FLASH_FAIL_THRESHOLD

    def get_stats(self) -> dict:
        """Return routing statistics."""
        total = self._pro_count + self._flash_count + self._auto_count
        return {
            "pro": self._pro_count,
            "flash": self._flash_count,
            "auto": self._auto_count,
            "total": total,
            "flash_pct": (self._flash_count / max(1, total)) * 100,
            "tier_outcomes": self._tier_outcomes,
            "flash_escalated": self._flash_escalated,
        }

    def _load_stats(self) -> None:
        """Load routing stats from disk."""
        try:
            if self._STATS_PATH.exists():
                data = json.loads(self._STATS_PATH.read_text(encoding="utf-8"))
                self._pro_count = data.get("pro_count", 0)
                self._flash_count = data.get("flash_count", 0)
                self._auto_count = data.get("auto_count", 0)
                self._tier_outcomes = data.get("tier_outcomes", self._tier_outcomes)
                self._flash_escalated = data.get("flash_escalated", False)
                logger.debug("🎯  Router stats loaded from disk.")
        except Exception:
            pass

    def _save_stats(self) -> None:
        """Persist routing stats to disk."""
        try:
            data = {
                "pro_count": self._pro_count,
                "flash_count": self._flash_count,
                "auto_count": self._auto_count,
                "tier_outcomes": self._tier_outcomes,
                "flash_escalated": self._flash_escalated,
                "saved_at": time.time(),
            }
            self._STATS_PATH.write_text(
                json.dumps(data, indent=2), encoding="utf-8",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 5. RateLimitTracker — Learn from rate limit errors
# ─────────────────────────────────────────────────────────────

_RATE_LIMIT_PATTERNS = [
    re.compile(r"429", re.IGNORECASE),
    re.compile(r"quota.?exceeded", re.IGNORECASE),
    re.compile(r"RESOURCE_EXHAUSTED", re.IGNORECASE),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"retry.?after:\s*(\d+)", re.IGNORECASE),
    re.compile(r"TerminalQuotaError", re.IGNORECASE),
    re.compile(r"exhausted your capacity", re.IGNORECASE),
]

# V30.5: Parse exact cooldown time from quota error messages
_QUOTA_TIME_PATTERN = re.compile(
    r"(?:reset|available|retry).*?(?:after|in)\s+((?:\d+h)?\s*(?:\d+m)?\s*(?:\d+s)?)",
    re.IGNORECASE,
)


def _parse_quota_reset_seconds(error_text: str) -> int:
    """
    Extract exact cooldown duration from quota error output.
    E.g. '17m50s' -> 1070, '1h' -> 3600, '45s' -> 45.
    Returns 0 if no time found.
    """
    match = _QUOTA_TIME_PATTERN.search(error_text)
    if not match:
        return 0
    time_str = match.group(1).strip()
    total = 0
    h = re.search(r"(\d+)h", time_str)
    m = re.search(r"(\d+)m", time_str)
    s = re.search(r"(\d+)s", time_str)
    if h:
        total += int(h.group(1)) * 3600
    if m:
        total += int(m.group(1)) * 60
    if s:
        total += int(s.group(1))
    return total


class RateLimitTracker:
    """
    Learn from rate limit errors and adapt behavior.

    Tracks per-model rate limit events, parses retry-after hints,
    and computes adaptive delays to prevent future limits.
    """

    def __init__(self, state_path=None):
        self._state_path = state_path or (config.get_state_dir() / "_rate_limits.json")
        self._events: list[dict] = []
        self._per_model_cooldown: dict[str, float] = {}  # model → epoch when OK
        self._total_rate_limits = 0
        self._load_state()

    @staticmethod
    def is_rate_limit_error(error_text: str) -> bool:
        """Check if an error message indicates a rate limit."""
        for pattern in _RATE_LIMIT_PATTERNS:
            if pattern.search(error_text):
                return True
        return False

    def record_rate_limit(self, model: str, error_msg: str) -> float:
        """
        Record a rate limit event. Returns seconds to wait.

        Parses retry-after from the error if available,
        otherwise uses adaptive estimation.
        """
        self._total_rate_limits += 1
        now = time.time()

        # Try to parse retry-after
        wait_seconds = config.RATE_LIMIT_DEFAULT_WAIT_S
        retry_match = re.search(r"retry.?after:\s*(\d+)", error_msg, re.IGNORECASE)
        if retry_match:
            wait_seconds = int(retry_match.group(1))
        else:
            # V30.5: Parse exact quota reset time (e.g. "17m50s" → 1070)
            exact_reset = _parse_quota_reset_seconds(error_msg)
            if exact_reset > 0:
                wait_seconds = exact_reset + 10  # 10s buffer
                logger.info(
                    "⚡  Parsed exact quota reset: %ds + 10s buffer = %ds",
                    exact_reset, wait_seconds,
                )
            else:
                # Adaptive: more recent limits → longer wait
                recent = [e for e in self._events if now - e["timestamp"] < 300]
                if len(recent) >= 3:
                    wait_seconds = min(config.RATE_LIMIT_MAX_WAIT_S, wait_seconds * 2)
                elif len(recent) >= 5:
                    wait_seconds = config.RATE_LIMIT_MAX_WAIT_S

        # Set cooldown
        self._per_model_cooldown[model] = now + wait_seconds

        # Record event
        event = {
            "model": model,
            "timestamp": now,
            "wait_seconds": wait_seconds,
            "error_snippet": error_msg[:200],
        }
        self._events.append(event)
        if len(self._events) > config.RATE_LIMIT_HISTORY_SIZE:
            self._events = self._events[-config.RATE_LIMIT_HISTORY_SIZE:]

        M = config.ANSI_MAGENTA
        R = config.ANSI_RESET
        logger.warning(
            "⚡  Rate limit hit on %s — waiting %ds (total: %d)",
            model, wait_seconds, self._total_rate_limits,
        )
        print(f"  {M}⚡ Rate limit on {model} — cooldown {wait_seconds}s{R}")

        self._save_state()
        return wait_seconds

    def should_wait(self, model: str) -> float:
        """Return seconds to wait for this model (0 if OK to proceed)."""
        expiry = self._per_model_cooldown.get(model, 0)
        remaining = expiry - time.time()
        return max(0.0, remaining)

    def suggest_alternative_model(self, current_model: str) -> str:
        """
        Return the next model in the progressive downgrade sequence.
        """
        chain = get_failover_chain()
        models = chain._models
        try:
            current_idx = models.index(current_model)
            for i in range(current_idx + 1, len(models)):
                if chain._is_available(models[i], time.time()):
                    logger.info("⚡  Downgrading from %s → %s due to rate limit", current_model, models[i])
                    return models[i]
        except ValueError:
            pass
        return current_model

    def get_stats(self) -> dict:
        """Return rate limit statistics."""
        now = time.time()
        recent = [e for e in self._events if now - e["timestamp"] < 3600]
        return {
            "total_rate_limits": self._total_rate_limits,
            "last_hour": len(recent),
            "active_cooldowns": {
                m: max(0, exp - now)
                for m, exp in self._per_model_cooldown.items()
                if exp > now
            },
        }

    def _load_state(self):
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._events = data.get("events", [])
                self._total_rate_limits = data.get("total", 0)
                # Restore active cooldowns
                for m, exp in data.get("cooldowns", {}).items():
                    if exp > time.time():
                        self._per_model_cooldown[m] = exp
        except Exception:
            pass

    def _save_state(self):
        try:
            data = {
                "events": self._events[-config.RATE_LIMIT_HISTORY_SIZE:],
                "total": self._total_rate_limits,
                "cooldowns": self._per_model_cooldown,
                "saved_at": time.time(),
            }
            self._state_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 7. DailyBudgetTracker — AI Ultra quota management + boost mode
# ─────────────────────────────────────────────────────────────

class DailyBudgetTracker:
    """
    Track daily Gemini CLI request usage against AI Ultra quotas.

    Features:
      - Counts CLI invocations per day (resets at midnight UTC)
      - Warns at 80% and 90% of daily limit
      - Provides effective worker count (user-settable, 1-6)
      - Persists worker setting to disk
      - Persists to disk for crash recovery

    AI Ultra quotas: 120 RPM (burst), 2000/day (budget)
    """

    def __init__(
        self,
        daily_limit: int = config.AI_ULTRA_DAILY,
        normal_workers: int = config.MAX_CONCURRENT_WORKERS,
        state_path: Path | None = None,
    ):
        self.daily_limit = daily_limit
        self.normal_workers = normal_workers
        self._state_path = state_path or (
            config._SUPERVISOR_DIR / "_daily_budget.json"
        )

        # Daily tracking
        self._today: str = ""
        self._request_count: int = 0
        self._warned_80: bool = False
        self._warned_90: bool = False

        # User-settable worker count (1-6), None = use normal_workers
        self._user_workers: int | None = None

        # Quota pause tracking
        self._quota_paused: bool = False
        self._quota_resume_at: float = 0.0  # epoch when quota resumes

        self._load_state()
        self._check_day_rollover()

    def _check_day_rollover(self) -> None:
        """Reset counters if the day changed (midnight Pacific Time)."""
        _PT = _get_pacific_tz()
        today = datetime.now(_PT).strftime("%Y-%m-%d")
        if today != self._today:
            if self._today:
                logger.info(
                    "📊  Daily budget reset (midnight PT): %d/%d requests used yesterday",
                    self._request_count, self.daily_limit,
                )
            self._today = today
            self._request_count = 0
            self._warned_80 = False
            self._warned_90 = False
            # Day rollover does NOT reset user_workers — it's a persistent setting
            # Auto-resume from quota pause on day rollover
            if self._quota_paused:
                self._quota_paused = False
                self._quota_resume_at = 0.0
                logger.info("📊  Quota pause lifted — new day, new quota.")

    def record_request(self) -> None:
        """Record a Gemini CLI invocation."""
        self._check_day_rollover()
        self._request_count += 1

        pct = (self._request_count / self.daily_limit) * 100

        if not self._warned_80 and pct >= 80:
            self._warned_80 = True
            logger.warning(
                "📊  Daily budget 80%%: %d/%d requests (%d remaining)",
                self._request_count, self.daily_limit,
                self.daily_limit - self._request_count,
            )
        elif not self._warned_90 and pct >= 90:
            self._warned_90 = True
            logger.warning(
                "📊  Daily budget 90%%: %d/%d requests — consider reducing activity",
                self._request_count, self.daily_limit,
            )

        # V44: Save on every request for persistence across restarts.
        # IO cost is negligible (~200 bytes) vs the Gemini CLI call it follows.
        self._save_state()

    def set_workers(self, count: int) -> int:
        """
        Set the number of concurrent workers (1-6).

        Returns the clamped worker count.
        """
        self._check_day_rollover()
        count = max(1, min(6, count))
        self._user_workers = count

        logger.info(
            "🔧  Worker count set to %d. Daily usage: %d/%d (%d%%).",
            count,
            self._request_count, self.daily_limit,
            int((self._request_count / self.daily_limit) * 100),
        )
        self._save_state()
        return count

    def get_effective_workers(self) -> int:
        """Return the number of workers to use right now."""
        self._check_day_rollover()

        # User-set worker count always takes priority
        if self._user_workers is not None:
            return self._user_workers

        return self.normal_workers

    def get_status(self) -> dict:
        """Return current budget status for UI display."""
        self._check_day_rollover()

        # V62: Find the best known exact reset time from the quota probe snapshots
        _exact_resets_at = 0.0
        try:
            from .retry_policy import get_quota_probe
            _qp = get_quota_probe()
            _now = time.time()
            for _snap in _qp._snapshots.values():
                if isinstance(_snap, dict):
                    _rat = _snap.get("resets_at", 0)
                    if _rat > _now and (_exact_resets_at == 0 or _rat < _exact_resets_at):
                        _exact_resets_at = _rat  # soonest reset across all models
        except Exception:
            pass

        return {
            "daily_used": self._request_count,
            "daily_limit": self.daily_limit,
            "daily_pct": round((self._request_count / self.daily_limit) * 100, 1),
            "remaining": self.daily_limit - self._request_count,
            "workers_current": self.get_effective_workers(),
            "workers_max": 6,
            "effective_workers": self.get_effective_workers(),
            "quota_paused": self._quota_paused,
            "quota_resume_at": self._quota_resume_at,
            "quota_resets_at_exact": _exact_resets_at,
            "seconds_until_reset": self.seconds_until_reset(),
        }

    def _load_state(self) -> None:
        """Load persisted budget state."""
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._today = data.get("today", "")
                self._request_count = data.get("request_count", 0)
                self._user_workers = data.get("user_workers", None)
                self._warned_80 = data.get("warned_80", False)
                self._warned_90 = data.get("warned_90", False)
                self._quota_paused = data.get("quota_paused", False)
                self._quota_resume_at = data.get("quota_resume_at", 0.0)
                logger.debug("📊  Budget state loaded: %d/%d today", self._request_count, self.daily_limit)
        except Exception:
            pass

    def _save_state(self) -> None:
        """Persist budget state to disk."""
        try:
            data = {
                "today": self._today,
                "request_count": self._request_count,
                "user_workers": self._user_workers,
                "warned_80": self._warned_80,
                "warned_90": self._warned_90,
                "quota_paused": self._quota_paused,
                "quota_resume_at": self._quota_resume_at,
                "saved_at": time.time(),
            }
            self._state_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8",
            )
        except Exception:
            pass

    # ── Quota pause/resume ────────────────────────────────────

    @property
    def is_quota_exhausted(self) -> bool:
        """Return True if daily request count has reached the limit."""
        self._check_day_rollover()
        return self._request_count >= self.daily_limit

    @property
    def quota_paused(self) -> bool:
        """Return True if all tasks should be paused due to quota exhaustion."""
        # Auto-clear if resume time has passed
        if self._quota_paused and self._quota_resume_at > 0:
            if time.time() >= self._quota_resume_at:
                self._quota_paused = False
                self._quota_resume_at = 0.0
                logger.info("📊  Quota pause auto-cleared — resume time reached.")
        return self._quota_paused

    @property
    def quota_resume_at(self) -> float:
        """Return epoch timestamp when quota is expected to resume."""
        return self._quota_resume_at

    def seconds_until_reset(self) -> float:
        """
        Return seconds until the next midnight Pacific Time.

        Google's daily quotas reset at midnight PT.
        """
        _PT = _get_pacific_tz()
        now_pt = datetime.now(_PT)
        # Next midnight PT
        tomorrow_pt = (now_pt + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return (tomorrow_pt - now_pt).total_seconds()

    def pause_for_quota(self, cooldown_seconds: float | None = None) -> None:
        """
        Pause all tasks until quota resets.

        Args:
            cooldown_seconds: If provided, pause for this exact duration.
                              If None, pause until midnight PT + 60s buffer.
        """
        if cooldown_seconds is None:
            cooldown_seconds = self.seconds_until_reset() + 60  # 60s buffer

        self._quota_paused = True
        self._quota_resume_at = time.time() + cooldown_seconds

        hours = int(cooldown_seconds // 3600)
        mins = int((cooldown_seconds % 3600) // 60)

        M = config.ANSI_MAGENTA
        R = config.ANSI_RESET
        logger.warning(
            "⏸️  QUOTA PAUSE: All tasks paused for %dh%dm",
            hours, mins,
        )
        print(f"  {M}⏸️ QUOTA PAUSE: All tasks paused for {hours}h{mins}m{R}")
        self._save_state()

    def resume_from_quota(self) -> None:
        """Resume tasks after quota pause."""
        self._quota_paused = False
        self._quota_resume_at = 0.0
        logger.info("▶️  QUOTA RESUME: Tasks resumed — quota available.")
        print(f"  {config.ANSI_GREEN}▶️ QUOTA RESUME: Tasks resumed{config.ANSI_RESET}")
        self._save_state()

    def verified_resume_from_quota(self) -> bool:
        """
        V73: Probe-verified resume — runs /stats to confirm quota is available.

        Returns True if quota is confirmed available and pause is lifted.
        Returns False if quota is still exhausted (caller should re-sleep).
        """
        try:
            from .retry_policy import get_quota_probe
            _qp = get_quota_probe()

            # Run the /stats probe (~2s with warm PTY)
            _count = _qp.run_stats_probe()

            if _count > 0:
                # Probe succeeded — check if ANY model has remaining quota
                _any_available = False
                _soonest_reset = 0.0
                for _m_name, _snap in _qp._snapshots.items():
                    if not isinstance(_snap, dict):
                        continue
                    _pct = _snap.get("remaining_pct", 0)
                    if _pct > 0:
                        _any_available = True
                        break
                    # Track the soonest reset for re-scheduling
                    _rat = _snap.get("resets_at", 0)
                    if _rat > time.time() and (_soonest_reset == 0 or _rat < _soonest_reset):
                        _soonest_reset = _rat

                if _any_available:
                    self.resume_from_quota()
                    logger.info("✅  [QuotaVerify] Probe confirms quota available — resuming.")
                    return True
                else:
                    # Still exhausted — recalculate resume timer from fresh probe
                    if _soonest_reset > time.time():
                        _new_wait = _soonest_reset - time.time() + 30  # 30s buffer
                    else:
                        _new_wait = 300  # 5min fallback if no reset time known
                    self._quota_resume_at = time.time() + _new_wait
                    _h, _m = int(_new_wait) // 3600, (int(_new_wait) % 3600) // 60
                    logger.warning(
                        "⏸  [QuotaVerify] Probe confirms still exhausted — "
                        "re-sleeping %dh%02dm until next reset.",
                        _h, _m,
                    )
                    self._save_state()
                    return False
            else:
                # Probe failed (PTY unavailable or no data returned)
                # Fall back to auto-reset checks
                for _m_name in list(_qp._snapshots.keys()):
                    _qp._auto_reset_if_due(_m_name)

                # Check if any model auto-reset to available
                _any_reset = any(
                    isinstance(s, dict) and s.get("remaining_pct", 0) > 0
                    for s in _qp._snapshots.values()
                )
                if _any_reset:
                    self.resume_from_quota()
                    logger.info(
                        "✅  [QuotaVerify] Probe unavailable but auto-reset "
                        "detected — optimistic resume."
                    )
                    return True
                else:
                    # No data at all — optimistic resume (don't block forever)
                    self.resume_from_quota()
                    logger.warning(
                        "⚠️  [QuotaVerify] Probe unavailable, no auto-reset — "
                        "optimistic resume (will retry on next 429)."
                    )
                    return True
        except Exception as _exc:
            logger.debug("⚠️  [QuotaVerify] Error during verification: %s", _exc)
            # On error, optimistic resume
            self.resume_from_quota()
            return True


# ─────────────────────────────────────────────────────────────
# 8. GeminiQuotaProbe — live quota tracking from CLI output (V62)
# ─────────────────────────────────────────────────────────────

class GeminiQuotaProbe:
    """
    V62: Parse and track per-model quota data from Gemini CLI output.

    The Gemini CLI embeds usage information in its exit output matching
    the same format as the interactive /stats command:

        gemini-2.5-flash         –   100.0% resets in 23h 30m
        gemini-2.5-flash-lite    –    77.6% resets in 9h 33m
        gemini-2.5-pro           –    99.8% resets in 21h 5m

    This class:
      1. Parses that output after each CLI invocation
      2. Stores per-model remaining% and reset timers
      3. Provides should_avoid(model) → True if model below threshold
      4. Provides get_best_available(models) → model with most headroom
      5. Persists snapshots to _quota_probe.json for crash recovery

    Usage:
        probe = get_quota_probe()
        probe.update_from_cli_output(stdout, stderr)
        if probe.should_avoid("gemini-2.5-pro"):
            model = probe.get_best_available(model_list)
    """

    # Regex: captures lines like "gemini-2.5-flash    –   77.6% resets in 9h 33m"
    # Also handles: "100.0% resets in 23h 30m", "99.8% resets in 21h 5m"
    _QUOTA_LINE_RE = re.compile(
        r"(gemini[\w.\-]+)"       # model name
        r"\s+[–—-]\s+"            # separator (en-dash, em-dash, or hyphen)
        r"(\d+\.?\d*)%"           # remaining percentage
        r"\s+resets?\s+in\s+"     # "resets in" / "reset in"
        r"((?:\d+h\s*)?"          # optional hours
        r"(?:\d+m\s*)?)",         # optional minutes
        re.IGNORECASE,
    )

    # Parse "23h 30m" → seconds
    _TIME_PART_RE = re.compile(r"(\d+)([hms])", re.IGNORECASE)

    def __init__(self, state_path: Path | None = None):
        self._state_path = state_path or (
            config.get_state_dir() / "_quota_probe.json"
        )
        # Per-model quota snapshots
        # { "model-name": {"remaining_pct": 77.6, "resets_in_s": 34380, "resets_at": epoch, "probed_at": epoch} }
        self._snapshots: dict[str, dict] = {}
        self._last_probe_at: float = 0.0

        # V62: Smart fallback — track CLI calls since last successful probe.
        # Each call decrements the estimated remaining% from the probe baseline.
        # Reset when a fresh probe succeeds or on day rollover.
        self._requests_since_probe: int = 0
        self._usage_tracking_day: str = ""  # YYYY-MM-DD; reset counter on day change

        # V62+: Persistent PTY session — kept alive between probe calls.
        # First call spawns the CLI (~25s), follow-up calls reuse it (~2s).
        self._pty = None               # winpty.PTY instance
        self._pty_buffer: list = []    # chunks from the threaded reader
        self._pty_reader_thread = None
        self._pty_running = False
        self._pty_ready = False        # True once CLI prompt is visible

        self._load_state()

    def _load_state(self) -> None:
        """Load persisted quota probe state."""
        try:
            _paths = [self._state_path]
            _global = Path(__file__).parent / "_quota_probe.json"
            if _global != self._state_path:
                _paths.append(_global)

            for _path in _paths:
                if _path.exists():
                    data = json.loads(_path.read_text(encoding="utf-8"))
                    self._snapshots = data.get("snapshots", {})
                    self._last_probe_at = data.get("last_probe_at", 0.0)
                    self._requests_since_probe = data.get("requests_since_probe", 0)
                    self._usage_tracking_day = data.get("usage_tracking_day", "")
                    logger.debug(
                        "📊  [QuotaProbe] Loaded %d model snapshots from disk (usage since probe: %d).",
                        len(self._snapshots), self._requests_since_probe,
                    )
                    return
        except Exception as exc:
            logger.debug("📊  [QuotaProbe] Could not load state: %s", exc)

    def _save_state(self) -> None:
        """Persist quota probe state to disk."""
        try:
            data = {
                "snapshots": self._snapshots,
                "last_probe_at": self._last_probe_at,
                "requests_since_probe": self._requests_since_probe,
                "usage_tracking_day": self._usage_tracking_day,
                "saved_at": time.time(),
            }
            _json = json.dumps(data, indent=2)
            self._state_path.write_text(_json, encoding="utf-8")
            # Also save globally for launcher access
            _global = Path(__file__).parent / "_quota_probe.json"
            if _global != self._state_path:
                try:
                    _global.write_text(_json, encoding="utf-8")
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("📊  [QuotaProbe] Could not save state: %s", exc)

    def record_usage(self, model: str = "") -> None:
        """
        V62: Record a Gemini CLI invocation for smart fallback estimation.

        Called after every CLI call (alongside DailyBudgetTracker.record_request).
        Increments _requests_since_probe, which is used to adjust the last-known
        probe baseline and estimate current remaining quota.

        On day rollover, resets the counter (quota resets at midnight PT).
        """
        _PT = _get_pacific_tz()
        today = datetime.now(_PT).strftime("%Y-%m-%d")
        if today != self._usage_tracking_day:
            # New day — quota has reset, clear the local counter
            self._requests_since_probe = 0
            self._usage_tracking_day = today

        self._requests_since_probe += 1
        self._save_state()

        # V74: Refresh quota data on every CLI call — /stats is free
        # Only probe if PTY is already warm (avoid cold-start in tests/early boot)
        try:
            if self._pty_ready:
                self.run_stats_probe()
        except Exception:
            pass

    def _parse_reset_duration(self, time_str: str) -> int:
        """Parse '23h 30m' → seconds."""
        total = 0
        for match in self._TIME_PART_RE.finditer(time_str):
            value = int(match.group(1))
            unit = match.group(2).lower()
            if unit == "h":
                total += value * 3600
            elif unit == "m":
                total += value * 60
            elif unit == "s":
                total += value
        return total

    # ── Persistent PTY helpers ──

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Remove ANSI escape codes for clean text matching."""
        import re as _re_ansi
        text = _re_ansi.sub(r'\x1b\[[^a-zA-Z]*[a-zA-Z]', '', text)
        text = _re_ansi.sub(r'\x1b\][^\x07]*\x07', '', text)
        return text

    def _pty_reader_loop(self):
        """Background thread: blocking reads from the PTY."""
        while self._pty_running:
            try:
                data = self._pty.read()  # blocking
                if data:
                    self._pty_buffer.append(data)
            except Exception:
                break

    def _pty_get_buffer(self) -> str:
        return "".join(self._pty_buffer)

    def _pty_wait_for(self, phrases: list[str], timeout: float, from_pos: int = 0) -> bool:
        """Wait until any phrase appears in NEW PTY output (after from_pos)."""
        start = time.time()
        while time.time() - start < timeout:
            # Only search buffer chunks AFTER from_pos
            new_text = "".join(self._pty_buffer[from_pos:])
            clean = self._strip_ansi(new_text)
            for p in phrases:
                if p in clean:
                    return True
            time.sleep(0.5)
        return False

    def _pty_type_slowly(self, text: str, delay: float = 0.05):
        """Type each character with a delay, simulating human input."""
        for ch in text:
            self._pty.write(ch)
            time.sleep(delay)

    def _ensure_pty(self) -> bool:
        """
        Ensure the persistent PTY session is running.
        First call: spawns CLI (~25s). Follow-up calls: returns immediately.
        Returns True if the PTY is ready for commands.
        """
        if self._pty_ready and self._pty is not None:
            return True

        try:
            import winpty
        except ImportError:
            logger.debug("📊  [QuotaProbe] pywinpty not installed — PTY probe unavailable")
            return False

        import os as _os
        npm_prefix = _os.path.join(_os.environ.get("APPDATA", ""), "npm")
        node_entry = _os.path.join(
            npm_prefix, "node_modules", "@google", "gemini-cli", "dist", "index.js"
        )
        if not _os.path.exists(node_entry):
            logger.debug("📊  [QuotaProbe] gemini CLI not found at %s", node_entry)
            return False

        try:
            cmd = f"node {node_entry}"
            logger.info("📊  [QuotaProbe] Spawning persistent PTY session...")

            self._pty = winpty.PTY(120, 30)
            self._pty.spawn(cmd)
            self._pty_buffer = []
            self._pty_running = True

            import threading
            self._pty_reader_thread = threading.Thread(
                target=self._pty_reader_loop, daemon=True,
                name="pty-quota-reader",
            )
            self._pty_reader_thread.start()

            # Wait for full initialization (status bar shows '/model')
            ready = self._pty_wait_for(["/model", "context left"], 60)
            if not ready:
                logger.warning("📊  [QuotaProbe] PTY: CLI didn't reach prompt in 60s")
                self._pty_close()
                return False

            time.sleep(2)  # stabilization
            self._pty_ready = True
            logger.info("📊  [QuotaProbe] PTY session ready (persistent).")
            return True

        except Exception as exc:
            logger.debug("📊  [QuotaProbe] PTY spawn failed: %s", exc)
            self._pty_close()
            return False

    def _pty_close(self):
        """Shutdown the persistent PTY session gracefully."""
        if self._pty is not None and self._pty_ready:
            try:
                # Send /quit to the CLI so it exits cleanly
                self._pty_type_slowly("/quit")
                time.sleep(0.2)
                self._pty.write("\r\n")
                time.sleep(1)
                logger.info("📊  [QuotaProbe] PTY session closed (sent /quit).")
            except Exception:
                pass
        self._pty_running = False
        self._pty_ready = False
        self._pty = None

    def run_stats_probe(self) -> int:
        """
        V62+: Run `/stats` probe via a PERSISTENT PTY session.

        Uses pywinpty to create a real pseudo-terminal that the Gemini CLI
        accepts as interactive. The PTY stays alive between calls:
          - First call: spawns CLI (~25s init), sends /stats, parses output
          - Follow-up calls: reuses the open session (~2s)

        Format (from real CLI output):
            gemini-2.5-flash          -      67.0% resets in 17h 47m
            gemini-3.1-pro-preview    -      40.5% resets in 15h 22m

        ZERO COST — /stats is a built-in info command, no API calls.

        Returns:
            Number of models updated.
        """
        try:
            if not self._ensure_pty():
                logger.debug("📊  [QuotaProbe] PTY not available — using estimation")
                return 0

            now = time.time()

            # Mark buffer position so we only parse new output
            pre_len = len(self._pty_buffer)

            # Type /stats slowly (CLI's input handler needs this)
            self._pty_type_slowly("/stats")
            time.sleep(0.3)
            self._pty.write("\r\n")

            # Wait for stats output — ONLY check NEW buffer data
            self._pty_wait_for(["resets in", "Usage remaining"], 15, from_pos=pre_len)
            time.sleep(5)  # let all model lines render

            # Get only the NEW output since /stats was sent
            new_output = "".join(self._pty_buffer[pre_len:])
            clean = self._strip_ansi(new_output)

            # Parse quota lines
            updated = 0
            for match in self._QUOTA_LINE_RE.finditer(clean):
                model_name  = match.group(1)
                remaining   = float(match.group(2))
                time_str    = match.group(3).strip()
                resets_in_s = self._parse_reset_duration(time_str) or 3600

                self._snapshots[model_name] = {
                    "remaining_pct": remaining,
                    "resets_in_s": resets_in_s,
                    "resets_at": now + resets_in_s,
                    "probed_at": now,
                    "source": "pty_probe",
                }
                updated += 1

            if updated:
                self._last_probe_at = now
                self._requests_since_probe = 0
                _PT = _get_pacific_tz()
                self._usage_tracking_day = datetime.now(_PT).strftime("%Y-%m-%d")
                self._save_state()
                logger.info(
                    "📊  [QuotaProbe] PTY stats: %d models captured. Usage counter reset.",
                    updated,
                )
                # Auto-discover: classify new models, remove deprecated ones
                try:
                    config.update_models_from_probe(self._snapshots)
                except Exception as _ad_exc:
                    logger.debug("📊  [AutoDiscovery] Error: %s", _ad_exc)
            else:
                logger.debug("📊  [QuotaProbe] PTY stats returned no quota lines.")

            return updated

        except Exception as exc:
            logger.debug("📊  [QuotaProbe] PTY stats probe error: %s", exc)
            # If the PTY died, mark it for re-init on next call
            self._pty_close()
            return 0

    def update_from_cli_output(self, stdout: str, stderr: str) -> int:
        """
        Parse quota data from Gemini CLI session output.

        Scans both stdout and stderr for lines matching the /stats format:
            gemini-2.5-flash  –  77.6% resets in 9h 33m

        Returns the number of model quotas parsed.
        """
        if not config.QUOTA_PROBE_ENABLED:
            return 0

        combined = (stdout or "") + "\n" + (stderr or "")
        parsed_count = 0
        now = time.time()

        for match in self._QUOTA_LINE_RE.finditer(combined):
            model = match.group(1).strip()
            remaining_pct = float(match.group(2))
            reset_str = match.group(3).strip()
            resets_in_s = self._parse_reset_duration(reset_str)

            self._snapshots[model] = {
                "remaining_pct": remaining_pct,
                "resets_in_s": resets_in_s,
                "resets_at": now + resets_in_s,  # V62: absolute epoch for cross-session accuracy
                "probed_at": now,
            }
            parsed_count += 1
            logger.debug(
                "📊  [QuotaProbe] %s: %.1f%% remaining, resets in %ds",
                model, remaining_pct, resets_in_s,
            )

        if parsed_count > 0:
            self._last_probe_at = now
            self._save_state()
            logger.info(
                "📊  [QuotaProbe] Parsed %d model quotas from CLI output.",
                parsed_count,
            )

            # Log warnings for low-quota models
            for model, snap in self._snapshots.items():
                if snap["remaining_pct"] < config.QUOTA_PROBE_AVOID_THRESHOLD:
                    hours = snap["resets_in_s"] // 3600
                    mins = (snap["resets_in_s"] % 3600) // 60
                    logger.warning(
                        "📊  [QuotaProbe] ⚠️  %s at %.1f%% — reset in %dh%dm",
                        model, snap["remaining_pct"], hours, mins,
                    )

        return parsed_count

    def _auto_reset_if_due(self, model: str) -> dict | None:
        """
        V62: Check if a model's quota has reset and auto-update it.

        Google uses ROLLING 24h windows, so quota recovers gradually as older
        requests age out — NOT all at once at the `resets_at` timestamp.
        The 429's `retryDelayMs` is the time until the FULL reset, not until
        any quota becomes available again.

        Logic:
        - If past `resets_at` → full reset to 100%.
        - If >25% of the reset window has elapsed and model was at 0% →
          partial recovery (linearly interpolated), since rolling windows
          mean quota is already recovering.

        Returns the (possibly updated) snapshot, or None if no data exists.
        """
        snap = self._snapshots.get(model)
        if not snap:
            return None

        resets_at = snap.get("resets_at", 0)
        now = time.time()

        if resets_at and now >= resets_at:
            # Quota window has fully rolled over — reset to 100%
            snap["remaining_pct"] = 100.0
            snap["resets_at"] = 0  # Unknown next reset until re-probed
            snap["probed_at"] = now  # New window starts now
            snap["auto_reset"] = True
            snap["source"] = "auto_reset_full"
            # V62: Reset the call counter — the 250-call budget starts fresh
            self._requests_since_probe = 0
            logger.info(
                "📊  [QuotaProbe] %s auto-reset to 100%% (reset window passed) — call counter reset",
                model,
            )
            self._save_state()
        elif resets_at and snap.get("remaining_pct", 100) <= 0:
            # V73: Model is genuinely exhausted. Trust the /stats probe data.
            # Rolling recovery estimation has been removed — only the live
            # /stats probe or a full window rollover can change the value.
            pass
        return snap

    def should_avoid(self, model: str, threshold_pct: float | None = None) -> bool:
        """
        Return True if the given model's quota is below the avoidance threshold.

        Uses the most recent probe data. If no data exists for the model,
        returns False (optimistic — don't block without evidence).
        """
        if not config.QUOTA_PROBE_ENABLED:
            return False

        if threshold_pct is None:
            threshold_pct = config.QUOTA_PROBE_AVOID_THRESHOLD

        snap = self._auto_reset_if_due(model)
        if not snap:
            return False

        return snap["remaining_pct"] < threshold_pct

    # V62: Maximum age (seconds) to trust an "exhausted" snapshot.
    # Google uses rolling 24h windows — quota recovers gradually.
    # A 0% reading from 30+ minutes ago is likely stale.
    _MAX_EXHAUSTION_TRUST_S = 900  # 15 minutes

    def is_exhausted(self, model: str) -> bool:
        """
        Return True if the given model has zero remaining quota AND
        the data is fresh enough to trust.

        Only triggers on confirmed exhaustion (0%), not low quota.
        If no data exists, returns False (optimistic — keep using it).
        If data is older than _MAX_EXHAUSTION_TRUST_S, returns False
        (stale exhaustion — the model likely has quota again).
        Automatically resets to 100% if the reset window has passed.
        """
        if not config.QUOTA_PROBE_ENABLED:
            return False
        snap = self._auto_reset_if_due(model)
        if not snap:
            return False
        if snap["remaining_pct"] > 0:
            return False
        # V62: Don't trust stale exhaustion data — rolling windows recover
        probed_at = snap.get("probed_at", 0)
        if probed_at and (time.time() - probed_at) > self._MAX_EXHAUSTION_TRUST_S:
            logger.info(
                "📊  [QuotaProbe] %s snapshot says 0%% but is %.0fm old — "
                "not trusting (rolling window likely recovered).",
                model, (time.time() - probed_at) / 60,
            )
            return False
        return True

    def get_best_available(self, models: list[str]) -> str | None:
        """
        Return the model from the list with the highest remaining quota.

        Only considers models with recent probe data (< 2h old).
        Returns None if no probe data is available.
        """
        if not config.QUOTA_PROBE_ENABLED:
            return None

        now = time.time()
        best_model = None
        best_pct = -1.0

        for model in models:
            snap = self._auto_reset_if_due(model)
            if not snap:
                continue
            if snap["remaining_pct"] > best_pct:
                best_pct = snap["remaining_pct"]
                best_model = model

        return best_model

    def get_best_model_with_quota(self, models: list[str]) -> str | None:
        """
        Return the first model from the priority-ordered list that has quota.

        The input list is expected to be in priority order (best model first).
        Returns the first model that has remaining_pct > 0% with fresh data.
        Returns None if no probe data is available or all models have quota
        (i.e., no override needed).
        """
        if not config.QUOTA_PROBE_ENABLED:
            return None

        now = time.time()
        has_any_data = False

        for model in models:
            snap = self._auto_reset_if_due(model)
            if not snap:
                continue
            has_any_data = True
            if snap["remaining_pct"] > 0:
                return model  # First priority model with quota

        if not has_any_data:
            return None  # No probe data — don't override

        return None  # All exhausted

    def get_quota_snapshot(self) -> dict:
        """
        Return the full quota probe snapshot for API/UI display.

        Returns a dict with per-model remaining% and reset timers,
        plus metadata about the last probe.

        V62: Always includes ALL models from GEMINI_MODEL_PROBE_LIST,
        even those not yet probed (shown at 100%).
        """
        now = time.time()
        result = {
            "enabled": config.QUOTA_PROBE_ENABLED,
            "last_probe_at": self._last_probe_at,
            "last_probe_age_s": round(now - self._last_probe_at, 1) if self._last_probe_at else None,
            "models": {},
        }

        # V73: Seed ALL models from the probe list at 100% defaults.
        # These are placeholders until a real /stats probe overwrites them.
        # No estimation — quota comes ONLY from the Gemini /stats endpoint.
        _model_bucket_map = getattr(config, 'QUOTA_MODEL_TO_BUCKET', {})
        for model in config.GEMINI_MODEL_PROBE_LIST:
            bucket_name = _model_bucket_map.get(model, "unknown")
            result["models"][model] = {
                "remaining_pct": 100.0,
                "resets_in_s": 0,
                "resets_in_human": "—",
                "resets_at": 0,
                "stale": True,   # Not yet probed
                "age_s": None,
                "exhausted": False,
                "auto_reset": False,
                "source": "no_probe_data",
                "bucket": bucket_name,
                "alert_level": "ok",
            }

        # V73: Overlay with actual /stats probe data — no estimation.
        # Quota values come ONLY from the Gemini /stats endpoint.
        _model_bucket = getattr(config, 'QUOTA_MODEL_TO_BUCKET', {})

        for model, snap in self._snapshots.items():
            # Auto-reset models whose quota window has fully rolled over
            self._auto_reset_if_due(model)
            snap = self._snapshots[model]  # Re-read after potential reset

            age = now - snap.get("probed_at", 0)
            resets_at = snap.get("resets_at", 0)
            auto_reset = snap.get("auto_reset", False)
            source = snap.get("source", "unknown")
            probe_pct = snap["remaining_pct"]
            bucket_name = _model_bucket.get(model, "unknown")

            # Calculate remaining time until reset
            if resets_at and resets_at > now:
                remaining_reset_s = resets_at - now
            else:
                remaining_reset_s = 0

            # Alert level for UI color coding
            if probe_pct <= 0:
                alert_level = "exhausted"
            elif probe_pct <= 10:
                alert_level = "critical"
            elif probe_pct <= 25:
                alert_level = "low"
            else:
                alert_level = "ok"

            result["models"][model] = {
                "remaining_pct": round(probe_pct, 1),
                "resets_in_s": round(remaining_reset_s),
                "resets_in_human": f"{int(remaining_reset_s // 3600)}h{int((remaining_reset_s % 3600) // 60)}m",
                "resets_at": resets_at,
                "stale": False,
                "age_s": round(age, 1),
                "exhausted": probe_pct <= 0.0,
                "auto_reset": auto_reset,
                "source": source,
                "bucket": bucket_name,
                "alert_level": alert_level,
            }

        # V62: BUCKET-MATE SYNCHRONIZATION.
        # Models in the same bucket share a quota pool. If we have real probe
        # data for one model but not its bucket-mate, copy the data across so
        # the UI shows consistent values (e.g., both Pro models at 94.1%).
        _buckets = getattr(config, 'QUOTA_BUCKETS', {})
        for bname, bdata in _buckets.items():
            bucket_models = bdata.get("models", [])
            if len(bucket_models) < 2:
                continue
            # Find the model with the best (most recent/real) data
            best_source = None
            best_model = None
            for bm in bucket_models:
                info = result["models"].get(bm)
                if not info:
                    continue
                src = info.get("source", "default")
                if src != "default":
                    # Prefer more recently probed data
                    if best_source is None or (info.get("age_s") or 999999) < (result["models"].get(best_model, {}).get("age_s") or 999999):
                        best_source = src
                        best_model = bm
            # Sync: copy best data to bucket-mates still at default
            if best_model:
                donor = result["models"][best_model]
                for bm in bucket_models:
                    if bm == best_model:
                        continue
                    mate = result["models"].get(bm, {})
                    if mate.get("source", "default") == "default":
                        # Copy the donor's values to the default mate
                        result["models"][bm] = {
                            **donor,
                            "source": f"bucket_sync ({best_model})",
                        }

        # V62: Include usage tracking metadata for UI display
        result["requests_since_probe"] = self._requests_since_probe
        result["buckets"] = {
            bname: {
                "label": bdata.get("label", bname),
                "estimated_rpd": bdata.get("estimated_rpd", 0),
                "models": bdata.get("models", []),
            }
            for bname, bdata in _buckets.items()
        }

        return result

    def __repr__(self) -> str:
        if not self._snapshots:
            return "GeminiQuotaProbe(no data)"
        summaries = []
        for m, s in self._snapshots.items():
            summaries.append(f"{m}={s['remaining_pct']:.0f}%")
        return f"GeminiQuotaProbe({', '.join(summaries)})"


# ─────────────────────────────────────────────────────────────
# Module-level singletons (initialized by main.py)
# ─────────────────────────────────────────────────────────────

_default_retry_policy: RetryPolicy | None = None
_default_failover_chain: ModelFailoverChain | None = None
_default_context_budget: ContextBudget | None = None
_default_router: TaskComplexityRouter | None = None
_default_rate_tracker: RateLimitTracker | None = None
_default_daily_budget: DailyBudgetTracker | None = None
_default_quota_probe: GeminiQuotaProbe | None = None


def init(
    retry: RetryPolicy | None = None,
    failover: ModelFailoverChain | None = None,
    budget: ContextBudget | None = None,
) -> None:
    """Initialize module-level singletons."""
    global _default_retry_policy, _default_failover_chain, _default_context_budget
    global _default_router, _default_rate_tracker, _default_daily_budget
    global _default_quota_probe
    _default_retry_policy = retry or RetryPolicy()
    _default_failover_chain = failover or ModelFailoverChain()
    _default_context_budget = budget or ContextBudget()
    _default_router = TaskComplexityRouter()
    _default_rate_tracker = RateLimitTracker()
    _default_daily_budget = DailyBudgetTracker()
    _default_quota_probe = GeminiQuotaProbe()
    logger.info("🔄  Retry/failover/budget/router/rate-limit/daily-budget/quota-probe systems initialized: %s", _default_retry_policy)


def get_retry_policy() -> RetryPolicy:
    """Get the default retry policy."""
    global _default_retry_policy
    if _default_retry_policy is None:
        _default_retry_policy = RetryPolicy()
    return _default_retry_policy


def get_failover_chain() -> ModelFailoverChain:
    """Get the default model failover chain."""
    global _default_failover_chain
    if _default_failover_chain is None:
        _default_failover_chain = ModelFailoverChain()
    return _default_failover_chain


def get_context_budget() -> ContextBudget:
    """Get the default context budget tracker."""
    global _default_context_budget
    if _default_context_budget is None:
        _default_context_budget = ContextBudget()
    return _default_context_budget


def get_router() -> TaskComplexityRouter:
    """Get the default task complexity router."""
    global _default_router
    if _default_router is None:
        _default_router = TaskComplexityRouter()
    return _default_router

# Alias — imported by headless_executor.py as get_complexity_router
get_complexity_router = get_router


def get_rate_tracker() -> RateLimitTracker:
    """Get the default rate limit tracker."""
    global _default_rate_tracker
    if _default_rate_tracker is None:
        _default_rate_tracker = RateLimitTracker()
    return _default_rate_tracker


def get_daily_budget() -> DailyBudgetTracker:
    """Get the default daily budget tracker."""
    global _default_daily_budget
    if _default_daily_budget is None:
        _default_daily_budget = DailyBudgetTracker()
    return _default_daily_budget


def get_quota_probe() -> GeminiQuotaProbe:
    """Get the default quota probe tracker."""
    global _default_quota_probe
    if _default_quota_probe is None:
        _default_quota_probe = GeminiQuotaProbe()
        # Register atexit handler to close PTY when supervisor exits
        import atexit
        def _cleanup_pty():
            if _default_quota_probe is not None:
                _default_quota_probe._pty_close()
        atexit.register(_cleanup_pty)
    return _default_quota_probe

