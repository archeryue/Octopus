"""AgentManager — stateless CRUD + business rules over the `agents` table.

Agents are the durable definition of an assistant (agent-refactor.md §5.1):
pure DB rows, no in-memory subprocess. This layer enforces name uniqueness,
`is_system` protection, and the delete/archive guards for the routes.
SessionManager reads agent rows directly through the Database at spawn time
(so editing an agent affects its open sessions on their next turn); it does
not go through this manager.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from . import agent_memory
from .database import Database


class AgentError(Exception):
    """Agent business-rule violation. Routes map this to a 400/409."""


class AgentManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def list_agents(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        return await self.db.load_agents(include_archived=include_archived)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return await self.db.get_agent(agent_id)

    async def get_default_agent(self) -> dict[str, Any] | None:
        """The protected Default Agent (is_system=1), created by migration."""
        return await self.db.get_system_agent()

    async def create_agent(
        self,
        *,
        name: str,
        description: str = "",
        avatar: str | None = None,
        system_prompt: str = "",
        model: str | None = None,
        credential_id: str | None = None,
        backend: str = "claude-code",
        mcp_servers: list[str] | None = None,
        tool_allow: str = "",
        tool_deny: str = "",
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise AgentError("Agent name is required")
        if await self.db.get_agent_by_name(name) is not None:
            raise AgentError(f"An agent named {name!r} already exists")
        agent_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await self.db.save_agent(
            agent_id=agent_id,
            name=name,
            created_at=now,
            updated_at=now,
            description=description,
            avatar=avatar,
            system_prompt=system_prompt,
            model=model,
            credential_id=credential_id,
            backend=backend,
            mcp_servers=mcp_servers,
            tool_allow=tool_allow,
            tool_deny=tool_deny,
            is_system=False,
        )
        agent = await self.db.get_agent(agent_id)
        assert agent is not None
        # Provision the agent's durable state dirs (canonical memory/ +
        # claude-home/) up front; also ensured lazily per turn.
        agent_memory.ensure_agent_dirs(agent_id)
        return agent

    async def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            raise AgentError("Agent not found")
        if "name" in fields and fields["name"] is not None:
            new_name = fields["name"].strip()
            if not new_name:
                raise AgentError("Agent name cannot be empty")
            clash = await self.db.get_agent_by_name(new_name)
            if clash is not None and clash["id"] != agent_id:
                raise AgentError(f"An agent named {new_name!r} already exists")
            fields["name"] = new_name
        await self.db.update_agent(agent_id, **fields)
        updated = await self.db.get_agent(agent_id)
        assert updated is not None
        return updated

    async def archive_agent(self, agent_id: str) -> None:
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            raise AgentError("Agent not found")
        if agent["is_system"]:
            raise AgentError("The Default Agent cannot be archived")
        await self.db.archive_agent(agent_id)

    async def delete_agent(self, agent_id: str) -> None:
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            raise AgentError("Agent not found")
        if agent["is_system"]:
            raise AgentError("The Default Agent cannot be deleted")
        if await self.db.count_sessions_for_agent(agent_id) > 0:
            raise AgentError(
                "Agent still has sessions; archive it instead of deleting"
            )
        await self.db.delete_agent(agent_id)
        # Hard delete also removes the agent's durable state (memory +
        # claude-home). Archiving keeps it, mirroring archived-session history.
        agent_memory.remove_agent_dir(agent_id)
