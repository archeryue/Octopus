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
            "subagents": _load_json_list(row[15]),
            "pinned": bool(row[16]),
            "pin_order": row[17],
            "user_id": row[18],
        }
        # The active-session count that `load_agents` / `get_agent` append
        # after the columns. Derived from the column list rather than written
        # as a literal, because a literal is what just broke: adding `user_id`
        # to `_AGENT_COLS` shifted this by one and eighty tests went red with
        # "active_session_count: input should be a valid integer".
        trailing = len(DatabaseBase._AGENT_COLS.split(", "))
        if len(row) > trailing:
            agent["active_session_count"] = row[trailing]
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
        user_id: str | None = None,
    ) -> None:
        await self._ensure_connected()
        servers_json = json.dumps(
            mcp_servers if mcp_servers is not None else _DEFAULT_MCP_SERVERS
        )
        await self.conn.execute(
            "INSERT INTO agents "
            "(id, name, description, avatar, system_prompt, model, "
            " credential_id, backend, mcp_servers, tool_allow, tool_deny, "
            " is_system, archived, created_at, updated_at, subagents, user_id, "
            " pinned, pin_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 1, "
            f" {self._NEXT_PIN_ORDER.format(table='agents')})",
            (
                agent_id, name, description, avatar, system_prompt, model,
                credential_id, backend or "claude-code", servers_json,
                tool_allow, tool_deny, int(bool(is_system)),
                created_at, updated_at, json.dumps(subagents or []), user_id,
            ),
        )
        await self.conn.commit()

    async def load_agents(
        self, *, include_archived: bool = False, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Agents, optionally only one account's (multi-tenancy.md §5).

        `user_id=None` means "no scoping asked for" — the boot-time backfills
        want every row. A *request* passes one, and so does the delegation
        manager, which resolves a target agent by name within the delegating
        session's own account (§7): a name is not a way to reach into another
        account and run a turn there.
        """
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        query = (
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a"
        )
        where: list[str] = []
        params: list[Any] = []
        if not include_archived:
            where.append("a.archived = 0")
        if user_id is not None:
            where.append("a.user_id = ?")
            params.append(user_id)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY a.is_system DESC, a.created_at"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_agent(row) for row in rows]

    async def get_agent(
        self, agent_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One agent, or None — including when it exists and belongs to someone
        else, which a caller must not be able to tell from "no such agent"."""
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        sql = f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a WHERE a.id = ?"
        params: list[Any] = [agent_id]
        if user_id is not None:
            sql += " AND a.user_id = ?"
            params.append(user_id)
        cursor = await self.conn.execute(sql, params)
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

    async def get_system_agent(
        self, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """This account's protected Default Agent (is_system=1).

        There is one per account, not one per box (multi-tenancy.md §5): it is
        where a fresh conversation goes when nobody named an agent, so an
        account without one has nowhere to start — and an *unscoped* answer
        would put that conversation in the first account that ever existed.
        The pre-accounts install has exactly one, created by the migration, and
        `user_id=None` still finds it.
        """
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        sql = (
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.is_system = 1"
        )
        params: list[Any] = []
        if user_id is not None:
            sql += " AND a.user_id = ?"
            params.append(user_id)
        cursor = await self.conn.execute(sql + " LIMIT 1", params)
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

    async def set_agent_pinned(self, agent_id: str, pinned: bool) -> None:
        """Put the agent in the sidebar (at the bottom) or take it out."""
        await self._set_pinned("agents", agent_id, pinned)

    async def reorder_agent_pins(self, ordered_ids: list[str]) -> None:
        """The sidebar order of the pinned agents — see `_reorder_pins`."""
        await self._reorder_pins("agents", ordered_ids)

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
