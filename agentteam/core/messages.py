"""Agent-to-agent communication.

Agents never call each other's methods or touch each other's state directly.
Every interaction goes through the MessageBus, which:
  * delivers asynchronous notifications into the recipient's inbox,
  * routes synchronous questions to the recipient's registered answer handler,
  * records the full conversation for audit and the dashboard.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .events import EventBus

BROADCAST = "all"

_ids = itertools.count(1)


@dataclass
class Message:
    sender: str
    recipient: str
    kind: str  # question | answer | notification | bug_report | change_request | handoff
    subject: str
    content: str
    reply_to: int | None = None
    id: int = field(default_factory=lambda: next(_ids))
    ts: float = field(default_factory=time.time)
    read: bool = False


AnswerHandler = Callable[[Message], Awaitable[str]]


class MessageBus:
    def __init__(self, events: EventBus, log_path: Path | None = None):
        self._events = events
        self._handlers: dict[str, AnswerHandler] = {}
        self._inboxes: dict[str, list[Message]] = {}
        self._log_path = log_path
        self.transcript: list[Message] = []

    # ------------------------------------------------------------------ registry
    def register(self, agent: str, answer_handler: AnswerHandler) -> None:
        self._handlers[agent] = answer_handler
        self._inboxes.setdefault(agent, [])

    def members(self) -> list[str]:
        return list(self._handlers)

    def is_member(self, agent: str) -> bool:
        return agent in self._handlers

    # ------------------------------------------------------------------ delivery
    def _record(self, msg: Message) -> None:
        self.transcript.append(msg)
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(msg), default=str) + "\n")
        self._events.emit(
            "message",
            agent=msg.sender,
            sender=msg.sender,
            recipient=msg.recipient,
            kind=msg.kind,
            subject=msg.subject,
            content=msg.content[:4000],
        )

    def send(self, sender: str, recipient: str, kind: str, subject: str, content: str) -> list[Message]:
        """Fire-and-forget delivery into one inbox (or all inboxes for 'all')."""
        targets = [a for a in self._handlers if a != sender] if recipient == BROADCAST else [recipient]
        delivered = []
        for target in targets:
            if target not in self._inboxes:
                self._events.emit("error", agent=sender, message=f"Unknown recipient '{target}' for '{subject}'")
                continue
            msg = Message(sender=sender, recipient=target, kind=kind, subject=subject, content=content)
            self._inboxes[target].append(msg)
            self._record(msg)
            delivered.append(msg)
        return delivered

    async def ask(self, sender: str, recipient: str, question: str, subject: str = "Question") -> str:
        """Synchronous question/answer between two agents."""
        handler = self._handlers.get(recipient)
        if handler is None:
            return f"[No teammate with role '{recipient}' exists on this team.]"
        if recipient == sender:
            return "[You cannot ask yourself.]"
        q = Message(sender=sender, recipient=recipient, kind="question", subject=subject, content=question)
        self._record(q)
        try:
            answer = await handler(q)
        except Exception as exc:  # the asker should still be able to continue
            answer = f"[{recipient} could not answer: {exc}]"
        a = Message(sender=recipient, recipient=sender, kind="answer", subject=f"Re: {subject}", content=answer, reply_to=q.id)
        self._record(a)
        return answer

    def read_inbox(self, agent: str, mark_read: bool = True) -> list[Message]:
        unread = [m for m in self._inboxes.get(agent, []) if not m.read]
        if mark_read:
            for m in unread:
                m.read = True
        return unread

    def conversation_log(self) -> list[dict]:
        return [asdict(m) for m in self.transcript]
