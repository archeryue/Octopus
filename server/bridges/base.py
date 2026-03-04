from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class TextBuffer:
    """Aggregates text chunks and flushes based on size or time.

    Claude sends many small assistant_text events. Platforms like Telegram
    have message length limits (4096 chars) and rate limits, so we buffer
    and flush periodically.
    """

    max_size: int
    flush_delay: float
    on_flush: Callable[[str], Awaitable[None]]
    _buffer: str = field(default="", init=False, repr=False)
    _flush_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def append(self, text: str) -> None:
        async with self._lock:
            self._buffer += text
            if len(self._buffer) >= self.max_size:
                await self._do_flush()
            else:
                self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self.flush_delay)
        async with self._lock:
            if self._buffer:
                await self._do_flush()

    async def _do_flush(self) -> None:
        if not self._buffer:
            return
        text = self._buffer
        self._buffer = ""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            self._flush_task = None
        try:
            await self.on_flush(text)
        except Exception:
            logger.exception("TextBuffer flush error")

    async def flush(self) -> None:
        """Force flush any remaining content."""
        async with self._lock:
            await self._do_flush()


class Bridge(ABC):
    """Abstract base for messaging platform integrations.

    Bridges run in-process inside the FastAPI event loop and interact
    with SessionManager directly. Each platform (Telegram, Discord, etc.)
    implements this interface.

    The base class handles:
    - Event dispatching (handle_event routes to abstract send methods)
    - Text buffering (aggregates assistant_text, flushes before other events)
    """

    name: str  # Platform identifier, e.g. "telegram"

    def __init__(self, manager: Any) -> None:  # Any to avoid circular import
        self.manager = manager
        self._text_buffers: dict[str, TextBuffer] = {}

    @abstractmethod
    async def start(self) -> None:
        """Start the bridge (connect to platform API, begin polling)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the bridge gracefully."""

    # --- Abstract send methods (platform-specific) ---

    @abstractmethod
    async def send_text(self, chat_id: str, text: str) -> None:
        """Send a text message to the platform chat."""

    @abstractmethod
    async def send_tool_approval_request(
        self,
        chat_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        """Send a tool approval request with approve/deny buttons."""

    @abstractmethod
    async def send_tool_use(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        """Show that a tool is being invoked."""

    @abstractmethod
    async def send_tool_result(
        self, chat_id: str, output: str, is_error: bool
    ) -> None:
        """Show a tool execution result."""

    @abstractmethod
    async def send_status(self, chat_id: str, status: str) -> None:
        """Send a status update (idle, running, waiting_approval)."""

    @abstractmethod
    async def send_result(
        self, chat_id: str, cost: float | None, is_error: bool
    ) -> None:
        """Send the final result summary."""

    @abstractmethod
    async def send_error(self, chat_id: str, message: str) -> None:
        """Send an error message."""

    # --- Text buffering ---

    @property
    def max_message_length(self) -> int:
        """Override per platform. Default 4096 (Telegram)."""
        return 4096

    @property
    def flush_delay(self) -> float:
        """Seconds to wait before flushing text buffer."""
        return 0.5

    def _get_or_create_buffer(self, chat_id: str) -> TextBuffer:
        if chat_id not in self._text_buffers:
            self._text_buffers[chat_id] = TextBuffer(
                max_size=self.max_message_length,
                flush_delay=self.flush_delay,
                on_flush=lambda text, cid=chat_id: self.send_text(cid, text),
            )
        return self._text_buffers[chat_id]

    async def _flush_buffer(self, chat_id: str) -> None:
        buffer = self._text_buffers.get(chat_id)
        if buffer:
            await buffer.flush()

    # --- Event dispatcher ---

    async def handle_event(self, chat_id: str, event: dict[str, Any]) -> None:
        """Route a SessionManager event to the appropriate send method."""
        event_type = event.get("type")

        try:
            if event_type == "assistant_text":
                buffer = self._get_or_create_buffer(chat_id)
                await buffer.append(event.get("content", ""))

            elif event_type == "tool_use":
                await self._flush_buffer(chat_id)
                await self.send_tool_use(
                    chat_id, event["tool"], event.get("input", {})
                )

            elif event_type == "tool_result":
                await self.send_tool_result(
                    chat_id,
                    event.get("output", ""),
                    event.get("is_error", False),
                )

            elif event_type == "tool_approval_request":
                await self._flush_buffer(chat_id)
                await self.send_tool_approval_request(
                    chat_id,
                    event["tool_use_id"],
                    event["tool_name"],
                    event.get("tool_input", {}),
                )

            elif event_type == "status":
                await self.send_status(chat_id, event.get("status", ""))

            elif event_type == "result":
                await self._flush_buffer(chat_id)
                await self.send_result(
                    chat_id, event.get("cost"), event.get("is_error", False)
                )

            elif event_type == "error":
                await self._flush_buffer(chat_id)
                await self.send_error(
                    chat_id, event.get("message", "Unknown error")
                )

            elif event_type == "user_message":
                pass  # No need to echo user's own message

            else:
                logger.debug(
                    "Bridge %s ignoring unknown event: %s", self.name, event_type
                )

        except Exception:
            logger.exception(
                "Bridge %s error handling event %s for chat %s",
                self.name,
                event_type,
                chat_id,
            )
