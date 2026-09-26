"""Agents — the durable assistant definitions that own everything else.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .base import DatabaseBase, _load_json_list
from .schema import _DEFAULT_MCP_SERVERS


class AgentsMixin(DatabaseBase):
    # --- agent-scoped enablement join -------------------------------------

    async def set_agent_connector(
        self, agent_id: str, installation_id: str, enabled: bool
    ) -> None:
        """Toggle one connector for one agent (presence in the join = on)."""
        await self._ensure_connected()
        if enabled:
            await self.conn.execute(
                "INSERT OR IGNORE INTO agent_connectors "
                "(agent_id, installation_id) VALUES (?, ?)",
                (agent_id, installation_id),
            )
        else:
            await self.conn.execute(
                "DELETE FROM agent_connectors "
                "WHERE agent_id = ? AND installation_id = ?",
                (agent_id, installation_id),
            )
        await self.conn.commit()

    async def get_agent_connector_ids(self, agent_id: str) -> list[str]:
        """Installation ids enabled for an agent."""
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT installation_id FROM agent_connectors WHERE agent_id = ?",
            (agent_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_enabled_connectors_for_agent(
        self, agent_id: str
    ) -> list[dict[str, Any]]:
        """Full installation rows for an agent's enabled connectors — the
        join SessionManager reads at spawn time to build the MCP set."""
        await self._ensure_connected()
        cols = ", ".join(f"ci.{c}" for c in self._CONNECTOR_COLS.split(", "))
        cursor = await self.conn.execute(
            f"SELECT {cols} FROM connector_installations ci "
            "JOIN agent_connectors ac ON ac.installation_id = ci.id "
            "WHERE ac.agent_id = ? ORDER BY ci.created_at",
            (agent_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_connector(row) for row in rows]

    @staticmethod
    def _row_to_agent(row: sqlite3.Row) -> dict[str, Any]:
        try:
            mcp_servers = json.loads(row[7]) if row[7] else []
        except (json.JSONDecodeError, TypeError):
            mcp_servers = []
        agent = {
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "avatar": row[3],
            "system_prompt": row[4] or "",
            "model": row[5],
            "credential_id": row[6],
            "mcp_servers": mcp_servers,
            "tool_allow": row[8] or "",
            "tool_deny": row[9] or "",
            "is_system": bool(row[10]),
            "archived": bool(row[11]),
            "created_at": row[12],
            "updated_at": row[13],
            "backend": row[14] or "claude-code",
            "subagents": _load_json_list(row[15] if len(row) > 15 else None),
        }
        # Optional active-session count appended by load_agents / get_agent.
        if len(row) > 16:
            agent["active_session_count"] = row[16]
        return agent

    async def save_agent(
        self,
        *,
        agent_id: str,
        name: str,
        created_at: str,
        updated_at: str,
        description: str = "",
        avatar: str | None = None,
        system_prompt: str = "",
        model: str | None = None,
        credential_id: str | None = None,
        backend: str = "claude-code",
        mcp_servers: list[str] | None = None,
        tool_allow: str = "",
        tool_deny: str = "",
        is_system: bool = False,
        subagents: list[dict[str, Any]] | None = None,
    ) -> None:
        await self._ensure_connected()
        servers_json = json.dumps(
            mcp_servers if mcp_servers is not None else _DEFAULT_MCP_SERVERS
        )
        await self.conn.execute(
            "INSERT INTO agents "
            "(id, name, description, avatar, system_prompt, model, "
            " credential_id, backend, mcp_servers, tool_allow, tool_deny, "
            " is_system, archived, created_at, updated_at, subagents) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                agent_id, name, description, avatar, system_prompt, model,
                credential_id, backend or "claude-code", servers_json,
                tool_allow, tool_deny, int(bool(is_system)),
                created_at, updated_at, json.dumps(subagents or []),
            ),
        )
        await self.conn.commit()

    async def load_agents(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        query = (
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a"
        )
        if not include_archived:
            query += " WHERE a.archived = 0"
        query += " ORDER BY a.is_system DESC, a.created_at"
        cursor = await self.conn.execute(query)
        rows = await cursor.fetchall()
        return [self._row_to_agent(row) for row in rows]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        cursor = await self.conn.execute(
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def get_agent_by_name(
        self, name: str, *, include_archived: bool = False
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        query = (
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.name = ?"
        )
        params: list[Any] = [name]
        if not include_archived:
            query += " AND a.archived = 0"
        cursor = await self.conn.execute(query, params)
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def get_system_agent(self) -> dict[str, Any] | None:
        """The protected Default Agent (is_system=1), created by migration."""
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        cursor = await self.conn.execute(
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.is_system = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def update_agent(self, agent_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {
            "name", "description", "avatar", "system_prompt", "model",
            "credential_id", "backend", "mcp_servers", "tool_allow", "tool_deny",
            "subagents",
            "archived",
        }
        # credential_id / model / avatar are nullable and may be cleared.
        nullable = {"credential_id", "model", "avatar"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if v is None and k not in nullable:
                continue
            if k in ("mcp_servers", "subagents"):
                updates[k] = json.dumps(v if v is not None else [])
            elif k == "archived":
                updates[k] = int(bool(v))
            else:
                updates[k] = v
        if not updates:
            return
        updates["updated_at"] = datetime.now(UTC).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [agent_id]
        await self.conn.execute(
            f"UPDATE agents SET {set_clause} WHERE id = ?", values
        )
        await self.conn.commit()

    async def archive_agent(self, agent_id: str) -> None:
        """Soft-delete an agent and cascade-archive its sessions."""
        await self._ensure_connected()
        await self.conn.execute(
            "UPDATE agents SET archived = 1, updated_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), agent_id),
        )
        await self.conn.execute(
            "UPDATE sessions SET archived = 1 WHERE agent_id = ?",
            (agent_id,),
        )
        await self.conn.commit()

    async def unarchive_agent(self, agent_id: str) -> None:
        """Restore an archived agent. Its sessions stay archived: they were
        archived as a cascade, and silently reviving a dozen old threads is
        not what "restore this agent" means — the archived-sessions page is
        where a session comes back from."""
        await self._ensure_connected()
        await self.conn.execute(
            "UPDATE agents SET archived = 0, updated_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), agent_id),
        )
        await self.conn.commit()

    async def delete_agent(self, agent_id: str) -> bool:
        """Hard-delete an agent. FK ON DELETE CASCADE removes its sessions,
        schedules — guarded by AgentManager so this is
        only reached when the agent has no sessions."""
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "DELETE FROM agents WHERE id = ?", (agent_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0
