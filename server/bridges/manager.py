from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..database import Database
    from ..session_manager import SessionManager

from .base import QUIET_SUPPRESSED_EVENTS, Bridge

logger = logging.getLogger(__name__)

BRIDGE_COMMANDS = {
    "/new": "Start a fresh session under the bound agent",
    "/agent": "Rebind this chat to a different agent (/agent <name|id>)",
    "/sessions": "List sessions (tap one to switch)",
    "/switch": "Point at an existing session (/switch <session_id>)",
    "/current": "Show current session info",
    "/quiet": "Show only the agent's replies (hide tool activity)",
    "/verbose": "Also show tool activity (tool calls, results, cost)",
    "/showme": "Browser-only — opens a file in the in-app viewer",
    "/help": "Show available commands",
}


@dataclass
class ChatBinding:
    """A chat's durable binding: which agent it talks to, the sticky session
    pointer (the currently-open thread, nullable), and its output verbosity.

    `verbose` is quiet by default — only the agent's natural-language replies,
    errors, and approval prompts reach the chat. It's a chat-level preference
    that persists across `/agent` rebinds and `/new`/`/switch` thread rolls.
    """

    agent_id: str
    session_id: str | None = None
    verbose: bool = False


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

    # Most-recent sessions to render as tappable buttons in /sessions. Telegram
    # caps inline keyboards; older sessions stay reachable via /switch <id>.
    SESSION_LIST_LIMIT = 30

    def __init__(self, session_mgr: SessionManager, db: Database) -> None:
        self.session_mgr = session_mgr
        self.db = db
        self._bridges: dict[str, Bridge] = {}
        # "platform:chat_id" -> ChatBinding
        self._mappings: dict[str, ChatBinding] = {}

    async def initialize(self) -> None:
        """Load persisted chat→agent bindings (with sticky session) from DB."""
        rows = await self.db.load_bridge_mappings()
        for row in rows:
            key = f"{row['platform']}:{row['chat_id']}"
            self._mappings[key] = ChatBinding(
                agent_id=row["agent_id"],
                session_id=row["session_id"],
                verbose=bool(row.get("verbose")),
            )
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

    def _binding(self, platform: str, chat_id: str) -> ChatBinding | None:
        """The chat's ChatBinding, or None if unbound."""
        return self._mappings.get(self._mapping_key(platform, chat_id))

    def get_session_id(self, platform: str, chat_id: str) -> str | None:
        """The chat's current sticky session id (used by broadcast routing
        and tool-decision handling). None when unbound or no live thread."""
        binding = self._binding(platform, chat_id)
        return binding.session_id if binding else None

    async def bind_agent(
        self, platform: str, chat_id: str, agent_id: str, session_id: str | None = None
    ) -> None:
        """Bind (or rebind) a chat to an agent, optionally with a sticky
        session. Rebinding to a different agent clears the sticky pointer but
        preserves the chat's verbosity preference (a chat-level setting)."""
        key = self._mapping_key(platform, chat_id)
        existing = self._mappings.get(key)
        self._mappings[key] = ChatBinding(
            agent_id=agent_id,
            session_id=session_id,
            verbose=existing.verbose if existing else False,
        )
        # The DB upsert preserves verbose on conflict (see save_bridge_mapping).
        await self.db.save_bridge_mapping(platform, chat_id, agent_id, session_id)

    async def set_sticky_session(
        self, platform: str, chat_id: str, session_id: str | None
    ) -> None:
        """Repoint the chat's sticky session without touching its agent."""
        key = self._mapping_key(platform, chat_id)
        binding = self._mappings.get(key)
        if binding is None:
            return
        binding.session_id = session_id
        await self.db.set_bridge_sticky_session(platform, chat_id, session_id)

    async def set_verbose(
        self, platform: str, chat_id: str, verbose: bool
    ) -> bool:
        """Set the chat's output verbosity. Returns False if the chat isn't
        bound yet (callers ensure binding first)."""
        key = self._mapping_key(platform, chat_id)
        binding = self._mappings.get(key)
        if binding is None:
            return False
        binding.verbose = verbose
        await self.db.set_bridge_verbose(platform, chat_id, verbose)
        return True

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
            return binding.agent_id
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

        Routes events to every chat whose sticky session is the event's
        session. A chat in quiet mode (the default) drops tool-activity and
        bookkeeping events (`QUIET_SUPPRESSED_EVENTS`); octo replies, errors
        and approval prompts always pass.
        """
        session_id = msg.get("session_id")
        if not session_id:
            return

        event_type = msg.get("type")
        for key, binding in self._mappings.items():
            if binding.session_id != session_id:
                continue
            if not binding.verbose and event_type in QUIET_SUPPRESSED_EVENTS:
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

    # --- Session switching ---

    async def switch_session(
        self, platform: str, chat_id: str, session_id: str
    ) -> str:
        """Repoint the chat's sticky session to an existing session of the
        bound agent. Returns a user-facing status line (shared by the
        `/switch` command and the `/sessions` inline-button callback)."""
        agent_id = await self._ensure_bound(platform, chat_id)
        session = self.session_mgr.get_session(session_id)
        if session is None:
            return f"Session '{session_id}' not found."
        if session.agent_id != agent_id:
            return (
                "That session belongs to a different agent. Use /agent to "
                "rebind this chat first."
            )
        await self.set_sticky_session(platform, chat_id, session.id)
        return f"Switched to session '{session.name}' ({session.id})."

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
            sessions = sorted(
                (s for s in self.session_mgr.list_sessions() if s.agent_id == agent_id),
                key=lambda s: s.created_at,
                reverse=True,
            )
            if not sessions:
                await bridge.send_text(
                    chat_id, "No sessions yet for this agent. Use /new to start one."
                )
                return
            current_sid = self.get_session_id(platform, chat_id)
            shown = sessions[: self.SESSION_LIST_LIMIT]
            items = [
                {
                    "id": s.id,
                    "name": s.name,
                    "status": s.status.value,
                    "current": s.id == current_sid,
                }
                for s in shown
            ]
            note = None
            if len(sessions) > self.SESSION_LIST_LIMIT:
                note = (
                    f"Showing the {self.SESSION_LIST_LIMIT} most recent of "
                    f"{len(sessions)}. Use /switch <id> for older ones."
                )
            await bridge.send_session_list(chat_id, items, note)

        elif command == "/switch":
            if not args:
                await bridge.send_text(chat_id, "Usage: /switch <session_id>")
                return
            msg = await self.switch_session(platform, chat_id, args.strip())
            await bridge.send_text(chat_id, msg)

        elif command in ("/quiet", "/verbose"):
            verbose = command == "/verbose"
            if await self._ensure_bound(platform, chat_id) is None:
                await bridge.send_text(chat_id, "No agent is configured yet.")
                return
            await self.set_verbose(platform, chat_id, verbose)
            await bridge.send_text(
                chat_id,
                "Verbose mode on — I'll also show tool calls, results and cost."
                if verbose
                else "Quiet mode on — I'll send only my replies (no tool activity).",
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

        elif command == "/showme":
            # The viewer modal lives only in the browser; intercept here so a
            # Telegram user doesn't get a baffling "Unknown command" from the
            # CLI (which eats `/<word>` messages before the model sees them).
            await bridge.send_text(
                chat_id,
                "/showme only works in the browser — the in-app file viewer "
                "can't render in Telegram. Open Octopus in a web session to "
                "use it.",
            )

        elif command == "/help":
            lines = [f"  {cmd} - {desc}" for cmd, desc in BRIDGE_COMMANDS.items()]
            await bridge.send_text(chat_id, "Commands:\n" + "\n".join(lines))

        else:
            await bridge.send_text(
                chat_id, f"Unknown command: {command}. Use /help."
            )
