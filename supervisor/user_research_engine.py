"""
user_research_engine.py — V29 Qualitative Synthesis Engine.

Listens to the voice of the user. Ingests raw customer feedback from
support tickets, churn surveys, and in-app feedback widgets. Strips PII,
clusters complaints semantically, enforces product vision, runs a
pre-epic compliance gate, and generates FEATURE_EPIC.md for features
that cross the demand threshold.

Pipeline:
  1. Ingest raw text (webhook or batch file)
  2. PII Redaction — regex-based removal of names, emails, phones, IPs
  3. Semantic Clustering — LLM groups tickets by underlying feature request
  4. Threshold Detection — 50 unique mentions in 30-day rolling window
  5. Product Vision Gate — reject features contradicting PRODUCT_VISION.md
  6. Pre-Epic Compliance Gate — block Shariah/financial violations BEFORE code gen
  7. FEATURE_EPIC.md Generation — structured epic for the Temporal Planner
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("supervisor.user_research_engine")

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Threshold: how many unique users must request a feature before it
# triggers an autonomous FEATURE_EPIC.
FEATURE_THRESHOLD = 50

# Rolling window in days for counting mentions.
ROLLING_WINDOW_DAYS = 30

# Maximum tickets processed per batch to prevent memory pressure.
MAX_BATCH_SIZE = 500

# ─────────────────────────────────────────────────────────────
# PII Redaction Patterns
# ─────────────────────────────────────────────────────────────

_PII_PATTERNS = [
    # Email addresses
    (re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "[EMAIL_REDACTED]"),
    # IP addresses (must come before phone to prevent phone regex eating IPs)
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "[IP_REDACTED]"),
    # Credit card numbers (13-19 digits, possibly spaced — before phone)
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{1,7}\b"), "[CARD_REDACTED]"),
    # SSN / national ID patterns (before phone)
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN_REDACTED]"),
    # Phone numbers (international and domestic)
    (re.compile(r"(\+?\d{1,4}[\s.-]?)?(\(?\d{1,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,5}"), "[PHONE_REDACTED]"),
    # Proper names — capitalized word pairs likely to be names (heuristic)
    (re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+[A-Z][a-z]+\b"), "[NAME_REDACTED]"),
    # "My name is X" pattern
    (re.compile(r"(?:my name is|i(?:'| a)m)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", re.IGNORECASE), "[NAME_REDACTED]"),
    # Standalone email-like usernames in prose
    (re.compile(r"(?:from|by|user|customer|client)[:\s]+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?", re.IGNORECASE), "[USER_REDACTED]"),
]

# ─────────────────────────────────────────────────────────────
# Compliance Blocklist — features that must NEVER be built
# ─────────────────────────────────────────────────────────────

_COMPLIANCE_BLOCKLIST_PATTERNS = [
    # Interest-based financial products
    r"interest\s*(?:rate|bearing|calculat|charg|earn|accrual)",
    r"(?:compound|simple)\s+interest",
    r"apr\b",
    r"usury",
    # Non-compliant payment gateways
    r"(?:loan\s*shark|payday\s*loan|predatory\s*lend)",
    # Gambling / speculation
    r"(?:gambling|casino|betting|lottery|slot\s*machine)",
    r"(?:binary\s*option|margin\s*trad|leveraged\s*trad)",
    # Prohibited integrations
    r"(?:stripe\s*(?:interest|lending)|afterpay\s*interest)",
]

_COMPLIANCE_RX = re.compile(
    "|".join(_COMPLIANCE_BLOCKLIST_PATTERNS), re.IGNORECASE
)


class UserResearchEngine:
    """
    Qualitative Synthesis Engine.

    Attributes:
        workspace_path: Root of the project workspace.
        _feedback_store: In-memory store of redacted, clustered tickets.
        _clusters: Semantic clusters of feature requests.
        _vision: Loaded PRODUCT_VISION.md text (or None).
    """

    def __init__(self, workspace_path: str = "."):
        self._workspace = Path(workspace_path)
        self._memory_dir = self._workspace / ".ag-memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        self._store_path = self._memory_dir / "feedback_store.json"
        self._clusters_path = self._memory_dir / "feature_clusters.json"

        self._feedback_store: list[dict] = []
        self._clusters: dict[str, dict] = {}
        self._vision: Optional[str] = None

        self._load_state()
        self._load_vision()

    # ─────────────────────────────────────────────────────────
    # State Management
    # ─────────────────────────────────────────────────────────

    def _load_state(self):
        """Load persisted feedback store and clusters from disk."""
        if self._store_path.exists():
            try:
                self._feedback_store = json.loads(
                    self._store_path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                self._feedback_store = []

        if self._clusters_path.exists():
            try:
                self._clusters = json.loads(
                    self._clusters_path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                self._clusters = {}

    def _save_state(self):
        """Persist feedback store and clusters to disk."""
        try:
            self._store_path.write_text(
                json.dumps(self._feedback_store, indent=2), encoding="utf-8"
            )
            self._clusters_path.write_text(
                json.dumps(self._clusters, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.error("Failed to save research state: %s", exc)

    def _load_vision(self):
        """Load PRODUCT_VISION.md if it exists."""
        vision_path = self._workspace / "PRODUCT_VISION.md"
        if vision_path.exists():
            self._vision = vision_path.read_text(encoding="utf-8")
            logger.info("Loaded PRODUCT_VISION.md (%d chars)", len(self._vision))
        else:
            self._vision = None
            logger.info("No PRODUCT_VISION.md found — vision gate disabled.")

    # ─────────────────────────────────────────────────────────
    # 1. PII Redaction
    # ─────────────────────────────────────────────────────────

    def redact_pii(self, text: str) -> str:
        """
        Strip all personally identifiable information from text.

        Applies regex-based patterns to remove:
          - Email addresses → [EMAIL_REDACTED]
          - Phone numbers → [PHONE_REDACTED]
          - IP addresses → [IP_REDACTED]
          - Credit card numbers → [CARD_REDACTED]
          - SSN/national IDs → [SSN_REDACTED]
          - Proper names (heuristic) → [NAME_REDACTED]

        Returns the redacted text. Original text is NEVER persisted.
        """
        result = text
        for pattern, replacement in _PII_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    # ─────────────────────────────────────────────────────────
    # 2. Webhook Ingestion
    # ─────────────────────────────────────────────────────────

    def ingest_webhook(self, payload: dict) -> dict:
        """
        Ingest a single support ticket or feedback payload.

        Expected payload format:
        {
            "user_id": "anon-hash or empty",
            "text": "I really wish we had a CSV export...",
            "source": "zendesk|intercom|survey|app_feedback",
            "timestamp": 1708456800  (optional, defaults to now)
        }

        Returns:
        {
            "accepted": True/False,
            "ticket_id": "sha256-hash",
            "redacted_text": "...",
            "reason": "reason if rejected"
        }
        """
        text = payload.get("text", "").strip()
        if not text or len(text) < 10:
            return {"accepted": False, "reason": "Text too short (< 10 chars)"}

        if len(text) > 5000:
            text = text[:5000]

        # Step 1: PII redaction FIRST — before anything touches memory
        redacted = self.redact_pii(text)

        # Generate stable ticket ID
        ticket_id = hashlib.sha256(
            f"{payload.get('user_id', '')}{redacted[:200]}".encode()
        ).hexdigest()[:16]

        # Dedup check
        existing_ids = {t.get("ticket_id") for t in self._feedback_store}
        if ticket_id in existing_ids:
            return {"accepted": False, "ticket_id": ticket_id, "reason": "Duplicate"}

        ticket = {
            "ticket_id": ticket_id,
            "text": redacted,
            "source": payload.get("source", "unknown"),
            "timestamp": payload.get("timestamp", time.time()),
            "user_hash": hashlib.sha256(
                payload.get("user_id", "anon").encode()
            ).hexdigest()[:12],
            "cluster": None,
        }

        self._feedback_store.append(ticket)

        # Prune old tickets beyond rolling window
        cutoff = time.time() - (ROLLING_WINDOW_DAYS * 86400)
        self._feedback_store = [
            t for t in self._feedback_store if t.get("timestamp", 0) > cutoff
        ]

        self._save_state()

        return {
            "accepted": True,
            "ticket_id": ticket_id,
            "redacted_text": redacted,
        }

    def ingest_batch(self, tickets: list[dict]) -> dict:
        """
        Ingest a batch of support tickets.

        Returns summary: {accepted: N, rejected: N, total: N}
        """
        batch = tickets[:MAX_BATCH_SIZE]
        accepted = 0
        rejected = 0

        for ticket in batch:
            result = self.ingest_webhook(ticket)
            if result.get("accepted"):
                accepted += 1
            else:
                rejected += 1

        return {"accepted": accepted, "rejected": rejected, "total": len(batch)}

    # ─────────────────────────────────────────────────────────
    # 3. Semantic Clustering
    # ─────────────────────────────────────────────────────────

    async def cluster_feedback(self) -> dict[str, dict]:
        """
        Cluster unclustered tickets by semantic similarity.

        Uses LLM to group tickets into feature-request categories.
        Each cluster tracks:
          - cluster_name: human-readable label
          - description: what users are asking for
          - ticket_ids: list of ticket IDs
          - unique_users: set of user hashes
          - count: total mentions
          - first_seen: earliest timestamp
          - last_seen: latest timestamp

        Returns the updated clusters dict.
        """
        unclustered = [
            t for t in self._feedback_store if t.get("cluster") is None
        ]

        if not unclustered:
            return self._clusters

        # Build a sample of unclustered text for the LLM
        samples = []
        for t in unclustered[:100]:
            samples.append(f"[{t['ticket_id']}] {t['text'][:300]}")

        existing_cluster_names = list(self._clusters.keys())

        prompt = (
            "You are a product research analyst. Categorize these customer "
            "feedback tickets into feature request clusters.\n\n"
            "EXISTING CLUSTERS (assign to these if applicable):\n"
            f"{json.dumps(existing_cluster_names)}\n\n"
            "TICKETS:\n" + "\n".join(samples) + "\n\n"
            "Return a JSON object where keys are cluster names (snake_case, "
            "descriptive, e.g. 'csv_export_tool') and values are objects with:\n"
            '  "description": "what users want",\n'
            '  "ticket_ids": ["id1", "id2", ...]\n\n'
            "Rules:\n"
            "- Create new clusters only for genuinely distinct requests\n"
            "- Merge near-duplicates into existing clusters\n"
            "- Use snake_case names, max 4 words\n"
            "- Respond with ONLY the JSON object, no markdown fences\n"
        )

        try:
            from .gemini_advisor import ask_gemini_json
            result = await ask_gemini_json(prompt, timeout=120)
        except Exception as exc:
            logger.error("Clustering LLM call failed: %s", exc)
            return self._clusters

        if not result or not isinstance(result, dict):
            logger.warning("Clustering returned invalid result")
            return self._clusters

        # Merge LLM results into existing clusters
        for cluster_name, cluster_data in result.items():
            if not isinstance(cluster_data, dict):
                continue

            ticket_ids = cluster_data.get("ticket_ids", [])
            if not ticket_ids:
                continue

            if cluster_name not in self._clusters:
                self._clusters[cluster_name] = {
                    "description": cluster_data.get("description", ""),
                    "ticket_ids": [],
                    "unique_users": [],
                    "count": 0,
                    "first_seen": time.time(),
                    "last_seen": time.time(),
                }

            cluster = self._clusters[cluster_name]

            for tid in ticket_ids:
                ticket = next(
                    (t for t in self._feedback_store if t["ticket_id"] == tid),
                    None,
                )
                if ticket and tid not in cluster["ticket_ids"]:
                    ticket["cluster"] = cluster_name
                    cluster["ticket_ids"].append(tid)
                    cluster["count"] += 1
                    cluster["last_seen"] = max(
                        cluster["last_seen"], ticket.get("timestamp", 0)
                    )

                    user_hash = ticket.get("user_hash", "")
                    if user_hash and user_hash not in cluster["unique_users"]:
                        cluster["unique_users"].append(user_hash)

        self._save_state()
        return self._clusters

    # ─────────────────────────────────────────────────────────
    # 4. Threshold Detection
    # ─────────────────────────────────────────────────────────

    def check_thresholds(self) -> list[dict]:
        """
        Check which clusters have crossed the FEATURE_THRESHOLD.

        Only counts unique users within the ROLLING_WINDOW_DAYS.

        Returns list of clusters that crossed the threshold:
        [
            {"cluster": "csv_export_tool", "unique_users": 67, "description": "..."}
        ]
        """
        cutoff = time.time() - (ROLLING_WINDOW_DAYS * 86400)
        triggered = []

        for name, cluster in self._clusters.items():
            # Count unique users within the rolling window
            recent_tickets = [
                t for t in self._feedback_store
                if t.get("cluster") == name
                and t.get("timestamp", 0) > cutoff
            ]

            recent_users = set()
            for t in recent_tickets:
                uh = t.get("user_hash", "")
                if uh:
                    recent_users.add(uh)

            if len(recent_users) >= FEATURE_THRESHOLD:
                # Check if we already generated an epic for this cluster
                epic_marker = self._memory_dir / f"epic_generated_{name}.marker"
                if not epic_marker.exists():
                    triggered.append({
                        "cluster": name,
                        "unique_users": len(recent_users),
                        "description": cluster.get("description", ""),
                        "total_tickets": len(recent_tickets),
                    })

        return triggered

    # ─────────────────────────────────────────────────────────
    # 5. Product Vision Gate
    # ─────────────────────────────────────────────────────────

    async def check_vision_alignment(self, feature_name: str, description: str) -> dict:
        """
        Check if a requested feature aligns with PRODUCT_VISION.md.

        If no vision file exists, the gate is open (passes by default).

        Returns:
        {
            "aligned": True/False,
            "reason": "explanation",
            "confidence": 0.0-1.0
        }
        """
        if not self._vision:
            return {"aligned": True, "reason": "No PRODUCT_VISION.md — gate open", "confidence": 0.5}

        prompt = (
            "You are a product strategist. Evaluate whether this feature request "
            "aligns with the product vision.\n\n"
            f"PRODUCT VISION:\n{self._vision[:3000]}\n\n"
            f"REQUESTED FEATURE: {feature_name}\n"
            f"DESCRIPTION: {description}\n\n"
            "Return a JSON object:\n"
            '{\n'
            '  "aligned": true/false,\n'
            '  "reason": "clear explanation",\n'
            '  "confidence": 0.0-1.0\n'
            '}\n\n'
            "Rules:\n"
            "- REJECT features that contradict the core product identity\n"
            "- REJECT features that bloat the product beyond its core value\n"
            "- ACCEPT features that enhance the core user journey\n"
            "- Be strict. 50 users wanting a feature doesn't mean it should exist.\n"
        )

        try:
            from .gemini_advisor import ask_gemini_json
            result = await ask_gemini_json(prompt, timeout=90)
            if result and isinstance(result, dict):
                return {
                    "aligned": result.get("aligned", False),
                    "reason": result.get("reason", "Unknown"),
                    "confidence": result.get("confidence", 0.5),
                }
        except Exception as exc:
            logger.error("Vision alignment check failed: %s", exc)

        return {"aligned": True, "reason": "Vision check failed — defaulting to open", "confidence": 0.3}

    # ─────────────────────────────────────────────────────────
    # 6. Pre-Epic Compliance Gate
    # ─────────────────────────────────────────────────────────

    async def check_compliance(self, feature_name: str, description: str) -> dict:
        """
        Pre-epic compliance gate. Runs BEFORE any code is generated.

        Checks for:
          - Interest-based financial products (Shariah non-compliance)
          - Non-compliant payment integrations
          - Gambling / speculation features
          - Prohibited third-party integrations

        This is a semantic check (not AST-based like V24).
        Catches violations at the language level before wasting compute.

        Returns:
        {
            "compliant": True/False,
            "violations": ["list of violations found"],
            "severity": "none|warning|critical"
        }
        """
        text = f"{feature_name} {description}".lower()

        violations = []

        # Regex-based blocklist check
        matches = _COMPLIANCE_RX.findall(text)
        if matches:
            violations.extend([f"Blocklist match: {m}" for m in matches])

        # LLM-based semantic check for subtler violations
        prompt = (
            "You are a Shariah compliance officer and financial regulatory expert.\n\n"
            f"PROPOSED FEATURE: {feature_name}\n"
            f"DESCRIPTION: {description}\n\n"
            "Check for:\n"
            "1. Interest-based lending (riba) — any form of interest charges\n"
            "2. Excessive uncertainty (gharar) — speculative features\n"
            "3. Gambling elements (maysir)\n"
            "4. Non-halal integrations (alcohol, pork-related, adult content)\n"
            "5. Predatory financial practices\n\n"
            "Return a JSON object:\n"
            '{\n'
            '  "compliant": true/false,\n'
            '  "violations": ["list of specific violations"],\n'
            '  "severity": "none|warning|critical"\n'
            '}\n'
        )

        try:
            from .gemini_advisor import ask_gemini_json
            result = await ask_gemini_json(prompt, timeout=90)
            if result and isinstance(result, dict):
                if not result.get("compliant", True):
                    violations.extend(result.get("violations", []))
        except Exception as exc:
            logger.error("Compliance check LLM failed: %s", exc)

        if violations:
            severity = "critical" if any("interest" in v.lower() or "riba" in v.lower() for v in violations) else "warning"
            return {
                "compliant": False,
                "violations": violations,
                "severity": severity,
            }

        return {"compliant": True, "violations": [], "severity": "none"}

    # ─────────────────────────────────────────────────────────
    # 7. FEATURE_EPIC.md Generation
    # ─────────────────────────────────────────────────────────

    async def generate_feature_epic(self, cluster_data: dict) -> Optional[str]:
        """
        Generate FEATURE_EPIC.md for a cluster that crossed the threshold.

        Full pipeline:
          1. Check product vision alignment
          2. Check compliance
          3. Generate epic via LLM
          4. Write to workspace root

        Returns the epic content if generated, or None if blocked.
        """
        name = cluster_data["cluster"]
        description = cluster_data["description"]
        unique_users = cluster_data["unique_users"]

        logger.info(
            "🔬 Feature request '%s' crossed threshold (%d unique users). "
            "Running gates...", name, unique_users
        )

        # Gate 1: Product Vision
        vision_result = await self.check_vision_alignment(name, description)
        if not vision_result.get("aligned"):
            logger.info(
                "❌ Feature '%s' rejected by vision gate: %s",
                name, vision_result.get("reason"),
            )
            # Mark as processed so we don't re-check
            marker = self._memory_dir / f"epic_generated_{name}.marker"
            marker.write_text(
                json.dumps({"status": "rejected_vision", "reason": vision_result.get("reason")}),
                encoding="utf-8",
            )
            return None

        # Gate 2: Compliance
        compliance_result = await self.check_compliance(name, description)
        if not compliance_result.get("compliant"):
            logger.info(
                "🚫 Feature '%s' BLOCKED by compliance gate: %s",
                name, compliance_result.get("violations"),
            )
            marker = self._memory_dir / f"epic_generated_{name}.marker"
            marker.write_text(
                json.dumps({
                    "status": "rejected_compliance",
                    "violations": compliance_result.get("violations"),
                }),
                encoding="utf-8",
            )
            return None

        # Gate 3: Generate Epic
        # Collect sample tickets for context
        sample_tickets = [
            t["text"][:200] for t in self._feedback_store
            if t.get("cluster") == name
        ][:10]

        prompt = (
            "You are a senior product manager writing a feature EPIC.\n\n"
            f"FEATURE: {name}\n"
            f"DESCRIPTION: {description}\n"
            f"DEMAND: {unique_users} unique users requested this in 30 days\n\n"
            "SAMPLE USER FEEDBACK:\n"
            + "\n".join(f"- {t}" for t in sample_tickets)
            + "\n\n"
            "Write a complete FEATURE_EPIC.md with:\n"
            "1. ## Summary — what to build and why\n"
            "2. ## User Stories — 3-5 user stories\n"
            "3. ## Technical Requirements — specific implementation details\n"
            "4. ## Acceptance Criteria — testable criteria\n"
            "5. ## Scope Boundaries — what is NOT included\n"
            "6. ## Feature Flag — the flag name for staged rollout\n\n"
            "CRITICAL RULES:\n"
            "- Do NOT include any customer names, emails, or PII\n"
            "- Scope to frontend/UI components only where possible\n"
            "- Include a feature flag name for A/B rollout\n"
            "- Be specific and actionable, not vague\n"
        )

        try:
            from .gemini_advisor import ask_gemini
            epic_content = await ask_gemini(prompt, timeout=120)
        except Exception as exc:
            logger.error("Epic generation failed: %s", exc)
            return None

        if not epic_content or len(epic_content) < 100:
            logger.warning("Epic generation returned insufficient content")
            return None

        # Final PII scan on the generated epic — defense in depth
        epic_content = self.redact_pii(epic_content)

        # Write FEATURE_EPIC.md
        epic_path = self._workspace / "FEATURE_EPIC.md"
        epic_path.write_text(epic_content, encoding="utf-8")

        # Mark as generated
        marker = self._memory_dir / f"epic_generated_{name}.marker"
        marker.write_text(
            json.dumps({"status": "generated", "timestamp": time.time()}),
            encoding="utf-8",
        )

        logger.info(
            "✅ FEATURE_EPIC.md generated for '%s' (%d users, %d tickets)",
            name, unique_users, cluster_data.get("total_tickets", 0),
        )

        return epic_content

    # ─────────────────────────────────────────────────────────
    # Full Pipeline
    # ─────────────────────────────────────────────────────────

    async def run_pipeline(self) -> dict:
        """
        Execute the full qualitative synthesis pipeline:
          1. Cluster unclustered feedback
          2. Check thresholds
          3. Gate and generate epics for qualifying clusters

        Returns:
        {
            "clusters_updated": N,
            "thresholds_crossed": N,
            "epics_generated": N,
            "epics_blocked": N,
            "details": [...]
        }
        """
        # Step 1: Cluster
        clusters = await self.cluster_feedback()

        # Step 2: Check thresholds
        triggered = self.check_thresholds()

        epics_generated = 0
        epics_blocked = 0
        details = []

        # Step 3: Generate epics
        for cluster_data in triggered:
            result = await self.generate_feature_epic(cluster_data)
            if result:
                epics_generated += 1
                details.append({
                    "cluster": cluster_data["cluster"],
                    "status": "generated",
                    "users": cluster_data["unique_users"],
                })
            else:
                epics_blocked += 1
                details.append({
                    "cluster": cluster_data["cluster"],
                    "status": "blocked",
                    "users": cluster_data["unique_users"],
                })

        return {
            "clusters_updated": len(clusters),
            "thresholds_crossed": len(triggered),
            "epics_generated": epics_generated,
            "epics_blocked": epics_blocked,
            "details": details,
        }

    # ─────────────────────────────────────────────────────────
    # Metrics
    # ─────────────────────────────────────────────────────────

    def get_cluster_summary(self) -> list[dict]:
        """Return a summary of all clusters for monitoring."""
        summary = []
        cutoff = time.time() - (ROLLING_WINDOW_DAYS * 86400)

        for name, cluster in self._clusters.items():
            recent_tickets = [
                t for t in self._feedback_store
                if t.get("cluster") == name
                and t.get("timestamp", 0) > cutoff
            ]
            recent_users = set(t.get("user_hash", "") for t in recent_tickets)

            summary.append({
                "cluster": name,
                "description": cluster.get("description", ""),
                "total_tickets": cluster.get("count", 0),
                "recent_tickets_30d": len(recent_tickets),
                "unique_users_30d": len(recent_users),
                "threshold_pct": (len(recent_users) / FEATURE_THRESHOLD) * 100,
                "status": "TRIGGERED" if len(recent_users) >= FEATURE_THRESHOLD else "ACCUMULATING",
            })

        return sorted(summary, key=lambda x: x["unique_users_30d"], reverse=True)
