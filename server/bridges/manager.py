from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..database import Database
    from ..session_manager import SessionManager

from .base import Bridge

logger = logging.getLogger(__name__)

BRIDGE_COMMANDS = {
    "/new": "Create a new session",
    "/sessions": "List active sessions",
    "/switch": "Switch to a different session",
    "/current": "Show current session info",
    "/help": "Show available commands",
}


class BridgeManager:
    """Routes messages between messaging platforms and SessionManager.

    Responsibilities:
    - Maintains chat_id -> session_id mappings (persisted in DB)
    - Handles slash commands (/new, /sessions, /switch, /current, /help)
    - Forwards user messages to SessionManager.send_message()
    - Routes SessionManager events back to the correct bridge + chat
    - Manages bridge lifecycle (start/stop)
    """

    def __init__(self, session_mgr: SessionManager, db: Database) -> None:
        self.session_mgr = session_mgr
        self.db = db
        self._bridges: dict[str, Bridge] = {}
        self._mappings: dict[str, str] = {}  # "platform:chat_id" -> session_id
        self._active_streams: dict[str, asyncio.Task] = {}

    async def initialize(self) -> None:
        """Load persisted chat-session mappings from database."""
        rows = await self.db.load_bridge_mappings()
        for row in rows:
            key = f"{row['platform']}:{row['chat_id']}"
            self._mappings[key] = row["session_id"]
        logger.info("Loaded %d bridge mappings from database", len(rows))

    def register_bridge(self, bridge: Bridge) -> None:
        self._bridges[bridge.name] = bridge

    async def start_all(self) -> None:
        for name, bridge in self._bridges.items():
            try:
                await bridge.start()
                logger.info("Bridge '%s' started", name)
            except Exception:
                logger.exception("Failed to start bridge '%s'", name)

    async def stop_all(self) -> None:
        for task in self._active_streams.values():
            task.cancel()
        self._active_streams.clear()

        for name, bridge in self._bridges.items():
            try:
                await bridge.stop()
                logger.info("Bridge '%s' stopped", name)
            except Exception:
                logger.exception("Error stopping bridge '%s'", name)

    def _mapping_key(self, platform: str, chat_id: str) -> str:
        return f"{platform}:{chat_id}"

    def get_session_id(self, platform: str, chat_id: str) -> str | None:
        return self._mappings.get(self._mapping_key(platform, chat_id))

    async def set_mapping(
        self, platform: str, chat_id: str, session_id: str
    ) -> None:
        key = self._mapping_key(platform, chat_id)
        self._mappings[key] = session_id
        await self.db.save_bridge_mapping(platform, chat_id, session_id)

    async def remove_mapping(self, platform: str, chat_id: str) -> None:
        key = self._mapping_key(platform, chat_id)
        self._mappings.pop(key, None)
        await self.db.delete_bridge_mapping(platform, chat_id)

    # --- Incoming message handling ---

    async def handle_incoming(
        self, platform: str, chat_id: str, text: str, bridge: Bridge
    ) -> None:
        """Process an incoming message from any platform."""
        text = text.strip()

        if text.startswith("/"):
            await self._handle_command(platform, chat_id, text, bridge)
            return

        session_id = self.get_session_id(platform, chat_id)
        if session_id is None:
            await bridge.send_text(
                chat_id,
                "No session connected. Use /new to create one or /sessions to list existing ones.",
            )
            return

        session = self.session_mgr.get_session(session_id)
        if session is None:
            await self.remove_mapping(platform, chat_id)
            await bridge.send_text(
                chat_id, "Session no longer exists. Use /new to create one."
            )
            return

        key = self._mapping_key(platform, chat_id)
        old_task = self._active_streams.pop(key, None)
        if old_task and not old_task.done():
            old_task.cancel()

        task = asyncio.create_task(
            self._stream_to_bridge(platform, chat_id, session_id, text, bridge)
        )
        self._active_streams[key] = task
        task.add_done_callback(lambda t: self._active_streams.pop(key, None))

    async def _stream_to_bridge(
        self,
        platform: str,
        chat_id: str,
        session_id: str,
        text: str,
        bridge: Bridge,
    ) -> None:
        try:
            async for event in self.session_mgr.send_message(session_id, text):
                await bridge.handle_event(chat_id, event)
        except Exception as e:
            logger.exception("Stream error for %s:%s", platform, chat_id)
            try:
                await bridge.send_error(chat_id, str(e))
            except Exception:
                pass

    # --- Tool approval ---

    async def handle_tool_decision(
        self,
        platform: str,
        chat_id: str,
        tool_use_id: str,
        approved: bool,
        reason: str = "",
    ) -> None:
        session_id = self.get_session_id(platform, chat_id)
        if not session_id:
            return

        if approved:
            self.session_mgr.approve_tool(session_id, tool_use_id)
        else:
            self.session_mgr.deny_tool(session_id, tool_use_id, reason)

    # --- Broadcast handler ---

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        """Handle broadcast events from SessionManager.

        Routes tool_approval_request and status events to all chats
        mapped to the relevant session.
        """
        session_id = msg.get("session_id")
        if not session_id:
            return

        for key, sid in self._mappings.items():
            if sid != session_id:
                continue
            platform, chat_id = key.split(":", 1)
            bridge = self._bridges.get(platform)
            if bridge:
                try:
                    await bridge.handle_event(chat_id, msg)
                except Exception:
                    logger.exception("Broadcast to %s failed", key)

    async def register_broadcast(self) -> None:
        self.session_mgr.on_broadcast(self._on_broadcast)

    async def unregister_broadcast(self) -> None:
        self.session_mgr.remove_broadcast(self._on_broadcast)

    # --- Command handling ---

    async def _handle_command(
        self, platform: str, chat_id: str, text: str, bridge: Bridge
    ) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if command == "/new":
            name = args or "Bridge Session"
            session = await self.session_mgr.create_session(name)
            await self.set_mapping(platform, chat_id, session.id)
            await bridge.send_text(
                chat_id,
                f"Created session '{session.name}' ({session.id}). "
                "You can start sending messages.",
            )

        elif command == "/sessions":
            sessions = self.session_mgr.list_sessions()
            if not sessions:
                await bridge.send_text(chat_id, "No sessions available.")
                return
            current_sid = self.get_session_id(platform, chat_id)
            lines = []
            for s in sessions:
                marker = " (current)" if s.id == current_sid else ""
                lines.append(
                    f"  {s.id} - {s.name} [{s.status.value}]{marker}"
                )
            await bridge.send_text(
                chat_id,
                "Sessions:\n"
                + "\n".join(lines)
                + "\n\nUse /switch <id> to switch.",
            )

        elif command == "/switch":
            if not args:
                await bridge.send_text(chat_id, "Usage: /switch <session_id>")
                return
            target_id = args.strip()
            session = self.session_mgr.get_session(target_id)
            if session is None:
                await bridge.send_text(
                    chat_id, f"Session '{target_id}' not found."
                )
                return
            await self.set_mapping(platform, chat_id, session.id)
            await bridge.send_text(
                chat_id,
                f"Switched to session '{session.name}' ({session.id}).",
            )

        elif command == "/current":
            session_id = self.get_session_id(platform, chat_id)
            if not session_id:
                await bridge.send_text(chat_id, "No session connected.")
                return
            session = self.session_mgr.get_session(session_id)
            if not session:
                await bridge.send_text(chat_id, "Session no longer exists.")
                return
            await bridge.send_text(
                chat_id,
                f"Current session: {session.name} ({session.id})\n"
                f"Status: {session.status.value}\n"
                f"Messages: {len(session.messages)}\n"
                f"Working dir: {session.working_dir}",
            )

        elif command == "/help":
            lines = [f"  {cmd} - {desc}" for cmd, desc in BRIDGE_COMMANDS.items()]
            await bridge.send_text(chat_id, "Commands:\n" + "\n".join(lines))

        else:
            await bridge.send_text(
                chat_id, f"Unknown command: {command}. Use /help."
            )
