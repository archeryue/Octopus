"""The database schema, as one DDL script.

Split out of the 2,600-line `database.py` (docs/plans/polish-2026-09.md §3 A4)
so the shape of the data is readable on its own, without scrolling past the
hundred methods that operate on it. `Database.initialize` executes this, then
runs the migrations in `migrations.py` to bring an older file up to it.
"""

from __future__ import annotations

import json

_DEFAULT_MCP_SERVERS = ["ask", "bg", "ask_agent", "research", "schedule"]
_DEFAULT_MCP_SERVERS_JSON = json.dumps(_DEFAULT_MCP_SERVERS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Agents are the durable definition of an assistant (agent-refactor.md §4.1):
-- identity + system prompt + model + credential + built-in MCP set + tool
-- policy. They OWN sessions and schedules. Memory (the
-- north star) hangs off the agent_id later; not in this refactor.
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,                    -- 12-char hex, same scheme as sessions
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    avatar TEXT,                            -- emoji or URL, optional
    system_prompt TEXT NOT NULL DEFAULT '',
    model TEXT,                             -- e.g. "claude-opus-4-7"; null = backend default
    credential_id TEXT REFERENCES backend_credentials(id) ON DELETE SET NULL,
    backend TEXT NOT NULL DEFAULT 'claude-code',  -- default harness for new sessions
    mcp_servers TEXT NOT NULL DEFAULT '["ask","bg","ask_agent","research","schedule"]',
                                            -- JSON array of built-in Octopus MCP server ids.
    tool_allow TEXT NOT NULL DEFAULT '',    -- newline-separated tool/MCP names; empty = allow all
    tool_deny  TEXT NOT NULL DEFAULT '',    -- newline-separated; deny takes precedence over allow
    subagents TEXT NOT NULL DEFAULT '[]',   -- JSON list of sub-agent definitions this agent
                                            -- brings with it: {name, description, prompt,
                                            -- model?, tools?}. Rendered as Claude Code's
                                            -- `--agents` JSON (native-subagents.md §6);
                                            -- empty list = the CLI's built-ins only.
    is_system INTEGER NOT NULL DEFAULT 0,   -- 1 = the protected Default Agent (cannot be deleted)
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agents_name_unique ON agents(name) WHERE archived = 0;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT,                -- backend resume id: a Claude session id
                                           -- OR a Codex thread_id (backend-agnostic;
                                           -- name kept for back-compat — codex-backend.md §4.3)
    archived INTEGER NOT NULL DEFAULT 0,   -- hidden from default list; row kept for history
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,  -- owner; nullable in SQLite, required by API
    origin TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'schedule' | 'delegation' | 'fork' | 'application' | 'app'
    backend TEXT NOT NULL DEFAULT 'claude-code',  -- 'claude-code' | 'codex' (codex-backend.md §4.1)
    -- Agent-to-agent: a delegation child session points at the parent
    -- session it was spawned from. SET NULL on parent delete (orphan beats
    -- mass-delete; sessions are precious). NULL on every non-delegation
    -- session. (agent-collaboration.md §4.1)
    parent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    -- The original prompt for a delegation, stored verbatim so the UI can
    -- render "Octo asked: «…»" without rummaging through the first message.
    -- NULL on every non-delegation session.
    delegation_request TEXT,
    -- The application this session belongs to, for the two kinds a running
    -- app owns: its build session and the conversations the app itself holds
    -- with an agent (origin='app'). It's what lets a route prove a
    -- conversation belongs to the app asking for it, what the sidebar filters
    -- on, and what deleting an application follows to take its threads with
    -- it. NULL on every ordinary session. (app-agent-access.md §3)
    app_id TEXT,
    -- Session tree-rewind / fork (session-rewind.md §4). A fork is a
    -- clone of a parent session up to (but not including) a chosen user
    -- message. All NULL/0 on non-fork sessions. forked_from_session_id is a
    -- PLAIN reference (no FK action): parent delete leaves it dangling so the
    -- UI can still render "forked from (deleted session)" and the orphan
    -- bucket in buildForkTree anchors correctly (§5.5).
    forked_from_session_id TEXT,           -- parent session id (dangling-ok)
    fork_after_seq INTEGER,                -- last copied seq; rewound user msg
                                           -- lives at fork_after_seq+1 on parent
    fork_needs_replay INTEGER NOT NULL DEFAULT 0,  -- HISTORY_REPLAY backends
                                           -- (Codex) until first result lands
    fork_metadata TEXT,                    -- EPHEMERAL JSON (prefilled prompt,
                                           -- side-effect summary, first-turn
                                           -- note, label); cleared after first
                                           -- result
    fork_revert_record TEXT,               -- DURABLE JSON safe-revert outcome;
                                           -- NEVER cleared
    fork_status TEXT                       -- 'initializing'|'reverting'|'ready'
                                           -- crash-recovery marker; NULL on
                                           -- non-fork rows
);
-- NOTE: the index on forked_from_session_id is created in _apply_migrations
-- (after the additive ALTER), NOT here — a legacy DB reaching this script
-- already has a `sessions` table, so CREATE TABLE IF NOT EXISTS is a no-op and
-- the column wouldn't exist yet at _SCHEMA time.

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_use_id TEXT,
    is_error INTEGER,
    session_id_ref TEXT,
    cost REAL,
    attachments TEXT,                       -- JSON list[AttachmentMetadata], null when none
    -- Per-turn git anchor captured when a user message row is written
    -- (session-rewind.md §4 + §5.6.3). Powers the safe-revert preflight.
    git_head TEXT,                          -- `git rev-parse HEAD`; NULL when not a git repo
    git_status_clean INTEGER,               -- 1 iff `git status --porcelain` was empty
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

-- A schedule belongs to the Agent ("every morning, summarize my inbox"),
-- not to a throwaway thread. Each fire materializes a fresh session under
-- the agent (scheduler.py). No persistent session_id here anymore.
--
-- Recurrence is exactly one of:
--   * interval_seconds  — fire every N seconds (APScheduler interval trigger)
--   * cron + timezone   — fire on a 5-field crontab in that tz (cron trigger)
-- recurrence_label is the human-readable description shown in the UI (the AI
-- parser supplies it for natural-language schedules; the interval fast-path
-- derives it). Nullable for legacy rows — the UI falls back to formatting
-- interval_seconds.
-- origin_session_id: when a schedule is created from the `/schedule` chat
-- command it remembers the session it was typed in, so each fire appends the
-- run into that same conversation (the result lands where the user is looking)
-- instead of a throwaway session. Nullable — agent/API-created schedules have
-- none and fall back to a fresh schedule-origin session. No FK: liveness is
-- decided at fire time by session_manager.get_session, so a stale pointer (the
-- origin session was deleted/archived) harmlessly degrades to the fallback.
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    origin_session_id TEXT,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_seconds INTEGER,
    cron TEXT,
    timezone TEXT,
    recurrence_label TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    -- The session the most recent fire ran in. Two jobs: the UI links "last
    -- run" to what actually happened, and the runner refuses to start a fire
    -- while the previous one is still going (scheduler.md — overlap guard).
    last_run_session_id TEXT,
    run_at TEXT  -- nullable ISO datetime; when set, fires once at that time then auto-deletes
);

CREATE TABLE IF NOT EXISTS backend_credentials (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,                 -- "claude-code" | "codex" | …
    label TEXT NOT NULL,
    auth_type TEXT NOT NULL,               -- "api_key" | "oauth"
    secret_encrypted TEXT NOT NULL,        -- LEGACY: kept for back-compat reads
                                           -- during the storage-split rollout.
                                           -- New writes go into credential_secrets.
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', -- "active" | "needs_reconnect"
    token_expires_at TEXT,                 -- ISO8601, null for non-expiring keys
    needs_reconnect INTEGER NOT NULL DEFAULT 0,
    last_refresh_error_code TEXT           -- see oauth_errors.RefreshErrorCode
);

CREATE INDEX IF NOT EXISTS idx_credentials_backend
  ON backend_credentials(backend);

-- Storage split (Steal Plan B-4): secrets live in their own table so a
-- future `serverOnly` flag can keep refresh tokens out of subprocess env,
-- and so we can join-or-not on the encrypted blob depending on the caller.
CREATE TABLE IF NOT EXISTS credential_secrets (
    credential_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,
    FOREIGN KEY (credential_id) REFERENCES backend_credentials(id)
        ON DELETE CASCADE
);

-- Connectors (connectors.md) — first-class third-party MCP tools the user
-- installs once (OAuth) and an agent calls during a turn. Two-layer model
-- mirroring backend_credentials: a metadata row + a split-out encrypted
-- secret. Unlike credentials there is no legacy in-table secret column — the
-- token blob lives ONLY in connector_installation_secrets.
CREATE TABLE IF NOT EXISTS connector_installations (
    id TEXT PRIMARY KEY,                   -- 12-char hex
    kind TEXT NOT NULL,                    -- 'gmail' | 'github' | …
    label TEXT NOT NULL,                   -- 'archeryue7@gmail.com'
    auth_type TEXT NOT NULL,               -- 'oauth' | 'api_key'
    external_account_id TEXT,              -- email / github "login:id" / workspace id
    scopes TEXT,                           -- JSON list of granted OAuth scopes
    enable_by_default INTEGER NOT NULL DEFAULT 0,  -- auto-enable on newly-created agents
    needs_reconnect INTEGER NOT NULL DEFAULT 0,
    token_expires_at TEXT,                 -- ISO8601, null = non-expiring
    last_refresh_error_code TEXT,          -- mirrors backend_credentials
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_connector_installations_kind
  ON connector_installations(kind);

-- Dedup: one installation per (kind, external account). The install flow
-- upserts on this — re-authorizing the same account overwrites rather than
-- duplicating. Partial index so rows mid-install (identity not yet known)
-- don't collide on a shared NULL.
CREATE UNIQUE INDEX IF NOT EXISTS connector_installations_account_unique
  ON connector_installations(kind, external_account_id)
  WHERE external_account_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS connector_installation_secrets (
    installation_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,
    FOREIGN KEY (installation_id) REFERENCES connector_installations(id)
        ON DELETE CASCADE
);

-- AGENT-scoped enablement (connectors.md revision 2026-05-20 + agent-refactor
-- §5.5): a row means "this agent has this installation turned on". The
-- effective MCP set for a turn is the agent's built-in mcp_servers ∪ its
-- enabled connectors. Cascades on both sides — deleting an agent or an
-- installation drops the link.
CREATE TABLE IF NOT EXISTS agent_connectors (
    agent_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    PRIMARY KEY (agent_id, installation_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (installation_id) REFERENCES connector_installations(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_connectors_agent
  ON agent_connectors(agent_id);

-- Per-kind OAuth *client* credentials (the app registered with the provider),
-- set in-app so a connector works without editing env + restarting. client_id
-- is not secret; the secret is encrypted like connector tokens. When there's
-- no row, resolution falls back to env (OCTOPUS_<KIND>_OAUTH_CLIENT_ID/_SECRET).
CREATE TABLE IF NOT EXISTS connector_oauth_clients (
    kind TEXT PRIMARY KEY,                 -- 'github' | 'gmail' | …
    client_id TEXT NOT NULL,
    client_secret_encrypted TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- User-defined ("custom") connectors: a brand-new connector kind added
-- entirely from the browser, no server code. The OAuth *client* creds live in
-- connector_oauth_clients (same as built-ins); this row holds the definition
-- the generic OAuth provider + generic MCP server read.
CREATE TABLE IF NOT EXISTS custom_connectors (
    kind TEXT PRIMARY KEY,                 -- user-chosen slug, e.g. 'linear'
    display_name TEXT NOT NULL,
    authorize_url TEXT NOT NULL,
    token_url TEXT NOT NULL,
    scopes TEXT,                           -- JSON list of OAuth scopes
    pkce INTEGER NOT NULL DEFAULT 0,
    api_base TEXT NOT NULL,                -- base URL the agent's request tool calls
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Async notification targets. Each row is one
-- destination Octopus can poke when a session transitions to idle
-- (and, later, when an AskUserQuestion is pending / a schedule fails).
-- `config` is a JSON blob whose shape depends on `type` (e.g. for
-- type='webhook': {"url": "https://…"}).
CREATE TABLE IF NOT EXISTS notifiers (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                    -- 'webhook' | future: 'email', 'browser_push'
    label TEXT NOT NULL,
    config TEXT NOT NULL,                  -- JSON
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Cross-turn background tasks. The model calls `bg_run(cmd)` via the
-- bg MCP server; we persist a row here, spawn the subprocess, and on
-- completion synthesize a follow-up user message in the session so the
-- model is told "your bg task finished, here's the result" in its next
-- turn. The whole point is that the bg subprocess lives in the
-- long-running FastAPI process — independent of any one claude --print
-- invocation — so it survives turn boundaries the way Bash's
-- run_in_background does not.
--
-- stdout/stderr are capped (see server.bg_tasks.MAX_STREAM_BYTES);
-- excess content is truncated from the head with a `…[truncated N bytes]`
-- prefix so the model sees the most recent output.
CREATE TABLE IF NOT EXISTS bg_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    command TEXT NOT NULL,
    description TEXT,
    working_dir TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'pending'|'running'|'completed'|'failed'|'cancelled'|'interrupted'
    exit_code INTEGER,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,  -- bool: at least one stream hit the cap
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bg_tasks_session
  ON bg_tasks(session_id, started_at);

-- Native deep-research jobs (native-deep-research.md §6). A job runs the
-- fan-out pipeline as a tracked async task; completion and delivery are
-- tracked SEPARATELY (a job can be `completed` while its report is still
-- queued behind an active turn). New-table-only — CREATE IF NOT EXISTS is a
-- no-op migration on existing DBs.
CREATE TABLE IF NOT EXISTS research_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,                  -- running|completed|failed|cancelled|interrupted
    phase TEXT,                            -- scope|search|verify|synthesize|done
    error TEXT,
    report_path TEXT,
    cost REAL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    injection_status TEXT NOT NULL DEFAULT 'pending',  -- pending|delivered|failed
    injected_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_research_jobs_session
  ON research_jobs(session_id, created_at);

-- Agent-built web applications (applications.md §2). An application is a
-- directory of static files an agent wrote, served under /apps/{id}/ and
-- rendered in the main pane like a browser tab. The row owns the directory
-- and points at the *build session* — a normal session with
-- origin='application' whose turns write the app. Both FKs SET NULL rather
-- than CASCADE: an application outlives the agent that built it and the
-- conversation that produced it. New-table-only — CREATE IF NOT EXISTS is a
-- no-op migration on existing DBs.
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,                   -- 12-char hex, as sessions/agents
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon TEXT,                             -- emoji shown in the sidebar
    icon_src TEXT,                         -- server-owned: the app's own icon
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    app_dir TEXT NOT NULL,                 -- absolute path to the app root
    entrypoint TEXT NOT NULL DEFAULT 'index.html',
    status TEXT NOT NULL DEFAULT 'building',  -- building|ready|failed
    error TEXT,
    -- Archived applications leave the sidebar but keep their row AND their
    -- files, so the create page's Archived tab can put them back.
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_built_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS applications_name_unique
  ON applications(name COLLATE NOCASE) WHERE archived = 0;
"""
