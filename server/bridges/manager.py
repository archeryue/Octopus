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
    "/new": "Start a fresh session under the bound agent",
    "/agent": "Rebind this chat to a different agent (/agent <name|id>)",
    "/sessions": "List sessions for the bound agent",
    "/switch": "Point at an existing session (/switch <session_id>)",
    "/current": "Show current session info",
    "/help": "Show available commands",
}


class BridgeManager:
    """Routes messages between messaging platforms and SessionManager.

    A chat binds durably to an **Agent** (agent-refactor.md §5.5); the
    Default Agent on first contact. Inbound messages route to a *sticky
    session* pointer that rolls as threads come and go — `/new` rolls it,
    `/agent` rebinds the chat, `/switch` repoints within the bound agent.

    Responsibilities:
    - Maintains (platform, chat_id) -> (agent_id, sticky session_id|None)
    - Handles slash commands (/new, /agent, /sessions, /switch, /current, /help)
    - Forwards user messages to SessionManager.start_message()
    - Routes SessionManager events back to the correct bridge + chat
    - Manages bridge lifecycle (start/stop)
    """

    def __init__(self, session_mgr: SessionManager, db: Database) -> None:
        self.session_mgr = session_mgr
        self.db = db
        self._bridges: dict[str, Bridge] = {}
        # "platform:chat_id" -> (agent_id, sticky session_id | None)
        self._mappings: dict[str, tuple[str, str | None]] = {}

    async def initialize(self) -> None:
        """Load persisted chat→agent bindings (with sticky session) from DB."""
        rows = await self.db.load_bridge_mappings()
        for row in rows:
            key = f"{row['platform']}:{row['chat_id']}"
            self._mappings[key] = (row["agent_id"], row["session_id"])
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
        for name, bridge in self._bridges.items():
            try:
                await bridge.shutdown()
                logger.info("Bridge '%s' stopped", name)
            except Exception:
                logger.exception("Error stopping bridge '%s'", name)

    def _mapping_key(self, platform: str, chat_id: str) -> str:
        return f"{platform}:{chat_id}"

    def _binding(self, platform: str, chat_id: str) -> tuple[str, str | None] | None:
        """(agent_id, sticky session_id|None) for a chat, or None if unbound."""
        return self._mappings.get(self._mapping_key(platform, chat_id))

    def get_session_id(self, platform: str, chat_id: str) -> str | None:
        """The chat's current sticky session id (used by broadcast routing
        and tool-decision handling). None when unbound or no live thread."""
        binding = self._binding(platform, chat_id)
        return binding[1] if binding else None

    async def bind_agent(
        self, platform: str, chat_id: str, agent_id: str, session_id: str | None = None
    ) -> None:
        """Bind (or rebind) a chat to an agent, optionally with a sticky
        session. Rebinding to a different agent clears the sticky pointer."""
        key = self._mapping_key(platform, chat_id)
        self._mappings[key] = (agent_id, session_id)
        await self.db.save_bridge_mapping(platform, chat_id, agent_id, session_id)

    async def set_sticky_session(
        self, platform: str, chat_id: str, session_id: str | None
    ) -> None:
        """Repoint the chat's sticky session without touching its agent."""
        key = self._mapping_key(platform, chat_id)
        binding = self._mappings.get(key)
        if binding is None:
            return
        self._mappings[key] = (binding[0], session_id)
        await self.db.set_bridge_sticky_session(platform, chat_id, session_id)

    async def remove_mapping(self, platform: str, chat_id: str) -> None:
        key = self._mapping_key(platform, chat_id)
        self._mappings.pop(key, None)
        await self.db.delete_bridge_mapping(platform, chat_id)

    async def _ensure_bound(self, platform: str, chat_id: str) -> str | None:
        """Return the chat's agent_id, binding it to the Default Agent on
        first contact. None only if no agent exists at all (shouldn't
        happen — migration always creates the Default Agent)."""
        binding = self._binding(platform, chat_id)
        if binding is not None:
            return binding[0]
        agent = await self.db.get_system_agent()
        if agent is None:
            return None
        await self.bind_agent(platform, chat_id, agent["id"], None)
        return agent["id"]

    # --- Incoming message handling ---

    async def handle_incoming(
        self, platform: str, chat_id: str, text: str, bridge: Bridge
    ) -> None:
        """Process an incoming message from any platform."""
        text = text.strip()

        if text.startswith("/"):
            await self._handle_command(platform, chat_id, text, bridge)
            return

        # A bound chat always has an agent; a thread is created on demand.
        # This deletes the old "no session connected" dead-end (§5.5).
        agent_id = await self._ensure_bound(platform, chat_id)
        if agent_id is None:
            await bridge.send_text(chat_id, "No agent is configured yet.")
            return

        session_id = self.get_session_id(platform, chat_id)
        session = self.session_mgr.get_session(session_id) if session_id else None
        if session is None:
            # No sticky thread (first contact, or it was archived/deleted) —
            # open a fresh one under the bound agent and make it sticky.
            try:
                session = await self.session_mgr.create_session(
                    agent_id, origin="bridge"
                )
            except ValueError as e:
                await bridge.send_text(chat_id, f"Error: {e}")
                return
            await self.set_sticky_session(platform, chat_id, session.id)
            session_id = session.id

        try:
            await self.session_mgr.start_message(session_id, text)
        except ValueError as e:
            await bridge.send_text(chat_id, f"Error: {e}")

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

        for key, (_agent_id, sticky) in self._mappings.items():
            if sticky != session_id:
                continue
            platform, chat_id = key.split(":", 1)
            bridge = self._bridges.get(platform)
            if bridge:
                try:
                    await bridge.handle_event(chat_id, msg)
                except Exception:
                    logger.exception("Broadcast to %s failed", key)

    async def register_broadcast(self) -> None:
        self.session_mgr.on_broadcast("bridge_manager", self._on_broadcast)

    async def unregister_broadcast(self) -> None:
        self.session_mgr.remove_broadcast("bridge_manager")

    # --- Command handling ---

    async def _handle_command(
        self, platform: str, chat_id: str, text: str, bridge: Bridge
    ) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if command == "/new":
            agent_id = await self._ensure_bound(platform, chat_id)
            if agent_id is None:
                await bridge.send_text(chat_id, "No agent is configured yet.")
                return
            try:
                session = await self.session_mgr.create_session(
                    agent_id, name=args or None, origin="bridge"
                )
            except ValueError as e:
                await bridge.send_text(chat_id, f"Error: {e}")
                return
            await self.set_sticky_session(platform, chat_id, session.id)
            await bridge.send_text(
                chat_id,
                f"Created session '{session.name}' ({session.id}). "
                "You can start sending messages.",
            )

        elif command == "/agent":
            if not args:
                await bridge.send_text(chat_id, "Usage: /agent <name|id>")
                return
            target = await self.db.get_agent(args.strip())
            if target is None:
                target = await self.db.get_agent_by_name(args.strip())
            if target is None:
                await bridge.send_text(chat_id, f"Agent '{args.strip()}' not found.")
                return
            # Rebind clears the sticky session — the next message opens a
            # fresh thread under the new agent.
            await self.bind_agent(platform, chat_id, target["id"], None)
            await bridge.send_text(
                chat_id,
                f"Now bound to agent '{target['name']}'. "
                "A new thread starts on your next message.",
            )

        elif command == "/sessions":
            agent_id = await self._ensure_bound(platform, chat_id)
            sessions = [
                s for s in self.session_mgr.list_sessions() if s.agent_id == agent_id
            ]
            if not sessions:
                await bridge.send_text(
                    chat_id, "No sessions yet for this agent. Use /new to start one."
                )
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
            agent_id = await self._ensure_bound(platform, chat_id)
            target_id = args.strip()
            session = self.session_mgr.get_session(target_id)
            if session is None:
                await bridge.send_text(
                    chat_id, f"Session '{target_id}' not found."
                )
                return
            if session.agent_id != agent_id:
                await bridge.send_text(
                    chat_id,
                    "That session belongs to a different agent. Use /agent to "
                    "rebind this chat first.",
                )
                return
            await self.set_sticky_session(platform, chat_id, session.id)
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
                f"Messages: {session._message_count}\n"
                f"Working dir: {session.working_dir}",
            )

        elif command == "/help":
            lines = [f"  {cmd} - {desc}" for cmd, desc in BRIDGE_COMMANDS.items()]
            await bridge.send_text(chat_id, "Commands:\n" + "\n".join(lines))

        else:
            await bridge.send_text(
                chat_id, f"Unknown command: {command}. Use /help."
            )
