"""
V74: Agent-to-Agent (A2A) Communication Protocol (Audit §4.8)

Implements a structured inter-agent communication protocol for the
Antigravity Auto Supervisor. Enables agents to exchange messages,
delegate subtasks, report results, and coordinate work in parallel.

Architecture:
  - AgentCard: Declares agent capabilities, model preference, context limit
  - A2AMessage: Structured message envelope with typing, routing, priority
  - A2ARouter: Routes messages between agents, tracks conversations
  - A2ADispatcher: Parallel execution of agent tasks via asyncio

Integration points:
  - agent_council.py: Use A2ADispatcher for parallel Swarm Debate
  - gemini_advisor.py: Route through A2A for per-agent model selection
  - main.py: Register agents at startup via AgentRegistry
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger("supervisor.a2a_protocol")


# ─────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────

class MessageType(str, Enum):
    """Types of A2A messages."""
    REQUEST = "request"         # Ask agent to perform work
    RESPONSE = "response"       # Agent returns result
    DELEGATE = "delegate"       # Agent delegates to another agent
    STATUS = "status"           # Progress update
    ERROR = "error"             # Error notification
    CAPABILITY = "capability"   # Capability discovery


class AgentRole(str, Enum):
    """Standard agent roles in the supervisor system."""
    DIAGNOSTICIAN = "diagnostician"
    DEBUGGER = "debugger"
    ARCHITECT = "architect"
    SYNTHESIZER = "synthesizer"
    TESTER = "tester"
    AUDITOR = "auditor"
    FIXER = "fixer"
    REVIEWER = "reviewer"
    PLANNER = "planner"
    RESEARCHER = "researcher"


class Priority(str, Enum):
    """Message priority levels."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class AgentCard:
    """
    Declares an agent's identity, capabilities, and preferences.

    Each registered agent has a card that other agents can discover
    to determine capability fit for task delegation.
    """
    agent_id: str = ""
    role: str = ""                    # AgentRole value
    display_name: str = ""
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    model_preference: str = ""        # e.g., "gemini-2.5-pro-preview"
    context_limit: int = 200000       # Max chars for this agent's context
    max_concurrent: int = 1           # Max parallel tasks this agent handles
    system_prompt: str = ""           # Agent-specific system prompt
    endpoint: str = ""                # HTTP endpoint (for remote A2A agents)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "display_name": self.display_name,
            "description": self.description,
            "capabilities": self.capabilities,
            "model_preference": self.model_preference,
            "context_limit": self.context_limit,
            "max_concurrent": self.max_concurrent,
            "endpoint": self.endpoint,
        }

    def supports(self, capability: str) -> bool:
        """Check if agent supports a given capability."""
        return capability.lower() in [c.lower() for c in self.capabilities]


@dataclass
class A2AMessage:
    """
    Structured message envelope for agent-to-agent communication.

    Every interaction between agents is wrapped in this envelope,
    providing routing, typing, correlation, and audit trail.
    """
    message_id: str = ""
    conversation_id: str = ""        # Groups related messages
    msg_type: str = "request"        # MessageType value
    sender: str = ""                 # Agent ID of sender
    recipient: str = ""              # Agent ID of recipient
    priority: str = "normal"         # Priority value
    payload: dict = field(default_factory=dict)
    context: str = ""                # Additional context (e.g., code, logs)
    timestamp: float = 0.0
    reply_to: str = ""               # message_id this replies to
    ttl_s: int = 300                 # Time-to-live in seconds

    def __post_init__(self):
        if not self.message_id:
            self.message_id = self._generate_id()
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.conversation_id:
            self.conversation_id = self.message_id

    @staticmethod
    def _generate_id() -> str:
        """Generate a unique message ID."""
        raw = f"{time.time()}-{id(object())}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def expired(self) -> bool:
        return time.time() - self.timestamp > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "type": self.msg_type,
            "sender": self.sender,
            "recipient": self.recipient,
            "priority": self.priority,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "reply_to": self.reply_to,
        }

    def reply(self, payload: dict, msg_type: str = "response") -> A2AMessage:
        """Create a reply message to this message."""
        return A2AMessage(
            conversation_id=self.conversation_id,
            msg_type=msg_type,
            sender=self.recipient,
            recipient=self.sender,
            payload=payload,
            reply_to=self.message_id,
        )


@dataclass
class TaskAssignment:
    """A task delegated from one agent to another via A2A."""
    task_id: str = ""
    assigned_by: str = ""            # Agent ID
    assigned_to: str = ""            # Agent ID
    description: str = ""
    context: str = ""
    deadline_s: int = 120            # Max time for completion
    status: str = "pending"          # pending, running, completed, failed, timeout
    result: dict = field(default_factory=dict)
    started_at: float = 0.0
    completed_at: float = 0.0

    @property
    def duration_s(self) -> float:
        if self.completed_at and self.started_at:
            return self.completed_at - self.started_at
        return 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "assigned_by": self.assigned_by,
            "assigned_to": self.assigned_to,
            "status": self.status,
            "duration_s": round(self.duration_s, 1),
        }


# ─────────────────────────────────────────────────────────────
# Agent Registry
# ─────────────────────────────────────────────────────────────

class AgentRegistry:
    """
    Central registry of all known agents and their capabilities.

    Provides agent discovery, capability matching, and model preference
    resolution for the A2A protocol.

    Usage:
        registry = AgentRegistry()
        registry.register(AgentCard(agent_id="debugger-1", ...))
        agents = registry.find_by_capability("code_fix")
    """

    def __init__(self):
        self._agents: dict[str, AgentCard] = {}

    def register(self, card: AgentCard) -> None:
        """Register an agent card."""
        if not card.agent_id:
            raise ValueError("Agent card must have an agent_id")
        self._agents[card.agent_id] = card
        logger.info("🤝  [A2A] Registered agent: %s (%s)", card.agent_id, card.role)

    def unregister(self, agent_id: str) -> bool:
        """Remove an agent from the registry."""
        if agent_id in self._agents:
            del self._agents[agent_id]
            return True
        return False

    def get(self, agent_id: str) -> AgentCard | None:
        """Get agent card by ID."""
        return self._agents.get(agent_id)

    def find_by_role(self, role: str) -> list[AgentCard]:
        """Find all agents with a given role."""
        return [c for c in self._agents.values() if c.role == role]

    def find_by_capability(self, capability: str) -> list[AgentCard]:
        """Find all agents that support a given capability."""
        return [c for c in self._agents.values() if c.supports(capability)]

    def all_agents(self) -> list[AgentCard]:
        """Get all registered agent cards."""
        return list(self._agents.values())

    @property
    def count(self) -> int:
        return len(self._agents)

    def to_dict(self) -> dict:
        return {
            "agents": {k: v.to_dict() for k, v in self._agents.items()},
            "count": self.count,
        }


# ─────────────────────────────────────────────────────────────
# Message Router
# ─────────────────────────────────────────────────────────────

class A2ARouter:
    """
    Routes A2A messages between agents with conversation tracking.

    Features:
      - Message delivery with handler dispatch
      - Conversation history tracking
      - Message validation and TTL enforcement
      - Broadcast to role groups
    """

    def __init__(self, registry: AgentRegistry):
        self._registry = registry
        self._handlers: dict[str, Callable] = {}  # agent_id -> handler
        self._conversations: dict[str, list[A2AMessage]] = {}
        self._message_count = 0

    def register_handler(self, agent_id: str, handler: Callable) -> None:
        """Register a message handler for an agent."""
        self._handlers[agent_id] = handler

    async def send(self, message: A2AMessage) -> A2AMessage | None:
        """
        Send a message to its recipient agent.

        Returns the response message, or None if delivery failed.
        """
        self._message_count += 1

        # Validate recipient exists
        if not self._registry.get(message.recipient):
            logger.warning("🤝  [A2A] Unknown recipient: %s", message.recipient)
            return None

        # Check TTL
        if message.expired:
            logger.warning("🤝  [A2A] Message expired: %s", message.message_id)
            return None

        # Track conversation
        conv_id = message.conversation_id
        if conv_id not in self._conversations:
            self._conversations[conv_id] = []
        self._conversations[conv_id].append(message)

        # Dispatch to handler
        handler = self._handlers.get(message.recipient)
        if not handler:
            logger.debug("🤝  [A2A] No handler for agent: %s", message.recipient)
            return None

        try:
            response = await handler(message)
            if response and isinstance(response, A2AMessage):
                self._conversations[conv_id].append(response)
                return response
        except Exception as exc:
            logger.warning("🤝  [A2A] Handler error for %s: %s", message.recipient, exc)
            error_msg = message.reply(
                {"error": str(exc)[:500]},
                msg_type="error",
            )
            self._conversations[conv_id].append(error_msg)
            return error_msg

        return None

    async def broadcast(self, role: str, message: A2AMessage) -> list[A2AMessage]:
        """Send a message to all agents with a given role."""
        agents = self._registry.find_by_role(role)
        responses = []
        for agent in agents:
            msg_copy = A2AMessage(
                conversation_id=message.conversation_id,
                msg_type=message.msg_type,
                sender=message.sender,
                recipient=agent.agent_id,
                payload=message.payload,
                context=message.context,
                priority=message.priority,
            )
            resp = await self.send(msg_copy)
            if resp:
                responses.append(resp)
        return responses

    def get_conversation(self, conversation_id: str) -> list[A2AMessage]:
        """Get all messages in a conversation."""
        return self._conversations.get(conversation_id, [])

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def conversation_count(self) -> int:
        return len(self._conversations)


# ─────────────────────────────────────────────────────────────
# Parallel Dispatcher
# ─────────────────────────────────────────────────────────────

class A2ADispatcher:
    """
    Dispatches tasks to multiple agents in parallel with concurrency control.

    Features:
      - Parallel execution via asyncio.gather
      - Per-agent concurrency limiting via semaphore
      - Timeout enforcement per task
      - Result aggregation with success/failure tracking

    Usage:
        dispatcher = A2ADispatcher(router, max_concurrent=3)
        results = await dispatcher.dispatch_parallel([
            TaskAssignment(assigned_to="debugger-1", description="..."),
            TaskAssignment(assigned_to="architect-1", description="..."),
        ])
    """

    def __init__(self, router: A2ARouter, max_concurrent: int = 3):
        self._router = router
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._completed_tasks: list[TaskAssignment] = []

    async def dispatch_parallel(
        self,
        tasks: list[TaskAssignment],
        timeout_s: int = 120,
    ) -> list[TaskAssignment]:
        """
        Dispatch multiple tasks to agents in parallel.

        Each task is sent as an A2A request message to the assigned agent.
        Returns all tasks with their final status and results.
        """
        if not tasks:
            return []

        logger.info("🤝  [A2A] Dispatching %d tasks in parallel", len(tasks))

        async def _execute_task(task: TaskAssignment) -> TaskAssignment:
            async with self._semaphore:
                task.status = "running"
                task.started_at = time.time()

                message = A2AMessage(
                    msg_type="request",
                    sender=task.assigned_by or "dispatcher",
                    recipient=task.assigned_to,
                    payload={
                        "task_id": task.task_id,
                        "description": task.description,
                    },
                    context=task.context,
                    ttl_s=timeout_s,
                )

                try:
                    response = await asyncio.wait_for(
                        self._router.send(message),
                        timeout=timeout_s,
                    )

                    if response:
                        task.result = response.payload
                        if response.msg_type == "error":
                            task.status = "failed"
                        else:
                            task.status = "completed"
                    else:
                        task.status = "failed"
                        task.result = {"error": "No response from agent"}

                except asyncio.TimeoutError:
                    task.status = "timeout"
                    task.result = {"error": f"Timed out after {timeout_s}s"}
                except Exception as exc:
                    task.status = "failed"
                    task.result = {"error": str(exc)[:500]}

                task.completed_at = time.time()
                return task

        completed = await asyncio.gather(
            *[_execute_task(t) for t in tasks],
            return_exceptions=True,
        )

        results = []
        for item in completed:
            if isinstance(item, TaskAssignment):
                results.append(item)
                self._completed_tasks.append(item)
            elif isinstance(item, Exception):
                logger.warning("🤝  [A2A] Task exception: %s", item)

        success_count = sum(1 for r in results if r.status == "completed")
        logger.info(
            "🤝  [A2A] Dispatch complete: %d/%d succeeded",
            success_count, len(results),
        )

        return results

    def get_stats(self) -> dict:
        """Get dispatch statistics."""
        total = len(self._completed_tasks)
        if total == 0:
            return {"total": 0, "success_rate": 0.0}

        success = sum(1 for t in self._completed_tasks if t.status == "completed")
        failed = sum(1 for t in self._completed_tasks if t.status == "failed")
        timeout = sum(1 for t in self._completed_tasks if t.status == "timeout")
        avg_duration = (
            sum(t.duration_s for t in self._completed_tasks) / total
            if total > 0 else 0.0
        )

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "timeout": timeout,
            "success_rate": round(success / total * 100, 1) if total > 0 else 0.0,
            "avg_duration_s": round(avg_duration, 1),
        }


# ─────────────────────────────────────────────────────────────
# Default agent cards
# ─────────────────────────────────────────────────────────────

DEFAULT_AGENTS = [
    AgentCard(
        agent_id="diagnostician-1",
        role=AgentRole.DIAGNOSTICIAN.value,
        display_name="Diagnostician",
        description="Analyzes issues and determines root cause",
        capabilities=["diagnosis", "triage", "classification"],
        model_preference="gemini-2.5-pro-preview",
    ),
    AgentCard(
        agent_id="debugger-1",
        role=AgentRole.DEBUGGER.value,
        display_name="Debugger",
        description="Deep technical debugging and error analysis",
        capabilities=["debugging", "error_analysis", "stack_trace"],
        model_preference="gemini-2.5-pro-preview",
    ),
    AgentCard(
        agent_id="architect-1",
        role=AgentRole.ARCHITECT.value,
        display_name="Architect",
        description="System-level analysis and structural recommendations",
        capabilities=["architecture", "design", "refactoring"],
        model_preference="gemini-2.5-pro-preview",
    ),
    AgentCard(
        agent_id="fixer-1",
        role=AgentRole.FIXER.value,
        display_name="Fixer",
        description="Generates code patches and fixes",
        capabilities=["code_fix", "patch_generation", "refactoring"],
        model_preference="gemini-2.5-pro-preview",
    ),
    AgentCard(
        agent_id="reviewer-1",
        role=AgentRole.REVIEWER.value,
        display_name="Reviewer",
        description="Validates code changes and patches",
        capabilities=["code_review", "validation", "security_audit"],
        model_preference="gemini-2.5-pro-preview",
    ),
    AgentCard(
        agent_id="tester-1",
        role=AgentRole.TESTER.value,
        display_name="Tester",
        description="Validates fixes and tests outcomes",
        capabilities=["testing", "verification", "regression_check"],
        model_preference="gemini-2.5-pro-preview",
    ),
]


def create_default_registry() -> AgentRegistry:
    """Create an AgentRegistry with all default supervisor agents."""
    registry = AgentRegistry()
    for card in DEFAULT_AGENTS:
        registry.register(card)
    return registry
