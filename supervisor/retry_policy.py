"""
retry_policy.py — OpenClaw-Inspired Retry, Failover & Budget Systems.

Three production-grade systems inspired by OpenClaw's architecture:

  1. RetryPolicy       — Exponential backoff with jitter (replaces fixed 2s sleep)
  2. ModelFailoverChain — Model fallback with per-model cooldowns (1m→5m→25m→1h)
  3. ContextBudget      — Track chars/tokens sent to Gemini per session

These classes are used by gemini_advisor.py and main.py to provide
resilient, efficient communication with the Gemini CLI.
"""

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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

    # Cooldown delays in seconds: 1m, 5m, 25m, 1h (cap)
    COOLDOWN_DELAYS = [60, 300, 1500, 3600]

    def __init__(
        self,
        models: Optional[list[str]] = None,
        state_path: Optional[Path] = None,
    ):
        self._models = models or list(config.GEMINI_MODEL_PROBE_LIST)
        self._state_path = state_path or (
            config.get_state_dir() / "_failover_state.json"
        )

        # Per-model state
        self._cooldown_expiry: dict[str, float] = {}   # model → epoch when cooldown ends
        self._failure_count: dict[str, int] = {}        # model → consecutive failures
        self._success_count: dict[str, int] = {}        # model → total successes
        self._sticky_model: Optional[str] = None        # session-sticky model

        self._load_state()

    def _load_state(self) -> None:
        """Load failover state from disk."""
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._cooldown_expiry = {
                    k: v for k, v in data.get("cooldowns", {}).items()
                    if v > time.time()  # Only load active cooldowns
                }
                # V30.5: Cap stale failure counts to prevent zombie cooldowns
                self._failure_count = {
                    k: min(v, 3) for k, v in data.get("failures", {}).items()
                }
                self._success_count = data.get("successes", {})
                logger.info(
                    "🔄  Loaded failover state: %d models with cooldowns",
                    len(self._cooldown_expiry),
                )
        except Exception as exc:
            logger.debug("Could not load failover state: %s", exc)

    def _save_state(self) -> None:
        """Save failover state to disk."""
        try:
            data = {
                "cooldowns": self._cooldown_expiry,
                "failures": self._failure_count,
                "successes": self._success_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._state_path.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Could not save failover state: %s", exc)

    def get_active_model(self) -> str:
        """
        Return the best available model, respecting cooldowns.

        Priority:
          1. Sticky model (if set and not cooled)
          2. First model not in cooldown
          3. Model with earliest cooldown expiry (least wait)
        """
        now = time.time()

        # Sticky model preference
        if self._sticky_model and self._is_available(self._sticky_model, now):
            return self._sticky_model

        # Find first available model
        for model in self._models:
            if self._is_available(model, now):
                self._sticky_model = model
                return model

        # All models cooled down — return None to signal caller to skip
        logger.warning(
            "🔄  ALL models on cooldown. Signaling caller to skip this tick.",
        )
        return None

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
        """
        tier = self.classify(prompt)

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
            model = chain.get_active_model()
            self._pro_count += 1
        else:
            # Auto — let the failover chain decide
            chain = get_failover_chain()
            model = chain.get_active_model()
            self._auto_count += 1

        logger.debug(
            "🎯  Model routing: tier=%s → model=%s (prompt: %.60s…)",
            tier, model, prompt,
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
# Module-level singletons (initialized by main.py)
# ─────────────────────────────────────────────────────────────

_default_retry_policy: Optional[RetryPolicy] = None
_default_failover_chain: Optional[ModelFailoverChain] = None
_default_context_budget: Optional[ContextBudget] = None
_default_router: Optional[TaskComplexityRouter] = None
_default_rate_tracker: Optional[RateLimitTracker] = None


def init(
    retry: Optional[RetryPolicy] = None,
    failover: Optional[ModelFailoverChain] = None,
    budget: Optional[ContextBudget] = None,
) -> None:
    """Initialize module-level singletons."""
    global _default_retry_policy, _default_failover_chain, _default_context_budget
    global _default_router, _default_rate_tracker
    _default_retry_policy = retry or RetryPolicy()
    _default_failover_chain = failover or ModelFailoverChain()
    _default_context_budget = budget or ContextBudget()
    _default_router = TaskComplexityRouter()
    _default_rate_tracker = RateLimitTracker()
    logger.info("🔄  Retry/failover/budget/router/rate-limit systems initialized: %s", _default_retry_policy)


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


def get_rate_tracker() -> RateLimitTracker:
    """Get the default rate limit tracker."""
    global _default_rate_tracker
    if _default_rate_tracker is None:
        _default_rate_tracker = RateLimitTracker()
    return _default_rate_tracker

