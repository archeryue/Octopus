"""Notifier base class + the small event shape passed to each one."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class NotifierEvent:
    """Normalized payload sent to every notifier.

    `type` distinguishes triggers (session_idle, schedule_failed, …).
    The other fields are best-effort context for the destination to
    use — webhook posts the whole dict as JSON, future email/push
    notifiers will format `title` + `message` for human display.
    """

    type: str  # 'session_idle' | future: 'question_pending', 'schedule_failed'
    title: str
    message: str
    session_id: str | None = None
    session_name: str | None = None
    extra: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "message": self.message,
        }
        if self.session_id:
            payload["session_id"] = self.session_id
        if self.session_name:
            payload["session_name"] = self.session_name
        if self.extra:
            payload["extra"] = self.extra
        return payload


class NotifierBase(ABC):
    """One destination. Concrete subclasses implement `send`."""

    type: str = ""  # subclass overrides — 'webhook', 'email', …

    def __init__(self, *, id: str, label: str, config: dict[str, Any]) -> None:
        self.id = id
        self.label = label
        self.config = config

    @abstractmethod
    async def send(self, event: NotifierEvent) -> None:
        """Deliver `event` to this destination.

        Exceptions are caught by the manager and logged. Don't raise
        unless the failure should be surfaced (currently none do).
        """
