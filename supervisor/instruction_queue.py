"""
instruction_queue.py — User Instruction Queue.

Thin wrapper around asyncio.Queue for the V35 Command Centre.
The UI pushes instructions via POST /api/instruct, and the
monitoring loop in main.py drains them at the top of each cycle.

Thread-safe because asyncio.Queue is coroutine-safe within
the same event loop.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("supervisor.instruction_queue")


@dataclass
class Instruction:
    """A single user instruction from the Command Centre UI."""
    text: str
    timestamp: float = field(default_factory=time.time)
    source: str = "ui"  # "ui", "api", "cli"
    priority: int = 0   # 0 = normal, 1 = high (future use)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "timestamp": self.timestamp,
            "source": self.source,
            "priority": self.priority,
        }


class InstructionQueue:
    """
    Async instruction queue for the V35 Command Centre.

    Usage:
        queue = InstructionQueue()

        # UI side (via API):
        await queue.push("Change the button color to red")

        # Engine side (monitoring loop):
        instruction = queue.pop_nowait()
        if instruction:
            await executor.execute_task(instruction.text)
    """

    def __init__(self, maxsize: int = 100):
        self._queue: asyncio.Queue[Instruction] = asyncio.Queue(maxsize=maxsize)
        self._history: list[Instruction] = []
        self._on_push_callbacks: list = []

    async def push(self, text: str, source: str = "ui") -> Instruction:
        """Push a new instruction onto the queue."""
        instruction = Instruction(text=text, source=source)
        await self._queue.put(instruction)
        self._history.append(instruction)
        if len(self._history) > 200:
            self._history = self._history[-200:]

        logger.info("📬 Instruction queued: '%s' (source=%s)", text[:80], source)

        # Notify subscribers
        for cb in self._on_push_callbacks:
            try:
                cb(instruction)
            except Exception:
                pass

        return instruction

    def pop_nowait(self) -> Optional[Instruction]:
        """Non-blocking pop. Returns None if queue is empty."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def on_push(self, callback) -> None:
        """Register a callback for when instructions are pushed."""
        self._on_push_callbacks.append(callback)

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def empty(self) -> bool:
        return self._queue.empty()

    @property
    def history(self) -> list[dict]:
        return [i.to_dict() for i in self._history[-50:]]
