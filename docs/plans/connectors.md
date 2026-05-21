# Connectors — Tech Plan

Status: ✅ **LANDED & live-verified (2026-05-21).** This plan is kept for
history; the sections below describe the original design. What actually shipped
diverged in a few deliberate ways (see the banner just below). The how-to lives
in `docs/connectors-setup.md`.

> ## ✅ SHIPPED — what's live vs. this plan (2026-05-21)
>
> Built agent-scoped, Gmail + GitHub, fully browser-only, and verified against
> live Google + GitHub. Where the implementation differs from the plan text:
>
> 1. **Enablement is agent-scoped** — `agent_connectors(agent_id, installation_id)`
>    join, not the `session_connectors` in the body. SessionManager loads an
>    agent's enabled connectors each turn and merges their MCP entries into both
>    backends (Claude `--mcp-config`, Codex `-c mcp_servers.*`).
> 2. **OAuth client config is IN-APP**, not env-only. `connector_oauth_clients`
>    stores per-kind client id + encrypted secret set from the "Set up" dialog;
>    `ConnectorManager.resolve_client_creds` is DB-first then env fallback. A
>    browser-only user never touches server env. Routes:
>    `GET/PUT/DELETE /api/connectors/{kind}/oauth-client`.
> 3. **Custom connectors** (NOT in the original plan): define a new kind from
>    the browser (`custom_connectors` table + `server/connectors/custom.py`
>    `GenericOAuthProvider`/`CustomConnector`/`resolve_connector`); one generic
>    `request` MCP server (`server/mcp_servers/connectors/custom.py`) gives the
>    agent authenticated HTTP to the kind's API base. Routes:
>    `POST /api/connectors/custom`, `DELETE /api/connectors/custom/{kind}`.
> 4. **Redirect URI is derived from the browser request** (`X-Forwarded-Proto`/
>    `Host`), not `OCTOPUS_PUBLIC_BASE_URL` — works behind a tunnel with zero
>    server config (env var remains an override).
> 5. **Provider methods take client_id/client_secret explicitly** (resolved per
>    request) rather than reading settings, so DB-stored creds work.
> 6. **Built-in connectors carry in-app setup guidance** (`setup_url` +
>    `setup_steps`) shown in the Set-up dialog.
> 7. **Notion dropped**; **GitHub** is the second built-in. **No mitmproxy** —
>    typed MCP tools, as the body's §11 already chose.

> ## ⚠️ REVISION — v1 is Gmail + GitHub; Notion dropped (2026-05-20)
>
> The connector set changed. **v1 now ships Gmail + GitHub** — the two
> services the user uses every day — **not Notion + Gmail.** **Notion is
> removed entirely:** the user migrated their documents to Obsidian (local
> Markdown the agent already reads/writes directly via the filesystem, so a
> Notion connector buys nothing). Apply these deltas alongside the
> agent-scoping revision below; the OAuth / MCP-server / truncation / refresh
> machinery is connector-agnostic and stands unchanged:
>
> 1. **GitHub replaces Notion as the "first connector end-to-end."** Phase B
>    (§9) builds **GitHub**, not Notion; Phase C stays Gmail. §6.1 (Notion) is
>    void — a GitHub descriptor + MCP server takes its place (issues, PRs,
>    repo + file reads, code search, create/comment).
> 2. **Wherever the body says "Notion" as a shipped v1 connector, read
>    "GitHub"** — §1 goal list, §5.1 file layout, the §5.8 system-prompt
>    example, the §6 title, and §8's fixtures (a fake-GitHub HTTP stub
>    replaces fake-Notion). §6.3 moves `github` *out* of "not in v1."
> 3. **GitHub specifics are not yet verified** (treat as §13 items, don't
>    fabricate): authorization-code flow against
>    `github.com/login/oauth/authorize` → `/access_token`, identity via
>    `GET /user`, scopes ~ `repo` + `read:org`. A classic OAuth app's tokens
>    don't expire (like Notion's did), so the refresh path stays dead code
>    unless we adopt GitHub-App expiring tokens. The exact tool list + names
>    (under the 60-char cap, §5.3) get pinned down at implementation.
> 4. **Env vars (§7): `OCTOPUS_NOTION_OAUTH_*` → `OCTOPUS_GITHUB_OAUTH_*`;**
>    `OCTOPUS_GMAIL_OAUTH_*` stays. `docs/connectors-setup.md` documents
>    GitHub OAuth-app registration instead of Notion.

> ## ⚠️ REVISION — connectors are AGENT-scoped (2026-05-19)
>
> This plan was written for **per-session** connector enablement. The
> first-class Agents refactor (`agent-refactor.md`, now landed) supersedes
> that: connectors are **agent-scoped**, exactly like the agent's built-in
> MCP set (agent-refactor.md §5.5 / §5.7, decision #5). Apply these deltas
> when implementing — the rest of the plan (OAuth flow, MCP-server modules,
> truncation, Notion/Gmail specifics, testing) stands unchanged:
>
> 1. **`session_connectors` → `agent_connectors`** (PK `(agent_id,
>    installation_id)`, FK `agent_id → agents(id) ON DELETE CASCADE`).
>    Enablement is per-agent; there is **no per-session connector table**.
> 2. **Effective MCP set for a turn = `agent.mcp_servers` (built-in) ∪ the
>    agent's enabled connectors.** `SessionManager._make_backend` already
>    loads the owning agent every turn (the live-reference point) — read the
>    agent's connectors there and merge into the backend's MCP config.
> 3. **UI moves from the new-session form / per-session sidebar dots (§4.1,
>    §4.2) to the Agent settings dialog** (`AgentSettings.tsx`) — a connector
>    toggle list per agent, beside the built-in-MCP checkboxes. The sidebar
>    `CONNECTORS` section becomes the install/catalog manager (installations
>    are global; enablement is per-agent).
> 4. **Both backends are ready.** Claude MCP injection is the existing
>    `--mcp-config` path (§5.6); Codex MCP injection is **settled** by
>    `codex-backend.md` §5.3 (per-session `-c mcp_servers.<key>.*` overrides,
>    `CodexBackend._mcp_config_args`) — this answers the §10.1 open question.
>    `connector.mcp_entry()` should return a backend-neutral
>    `{command,args,env}` that each backend renders into its own config shape.
> 5. **External prerequisite for end-to-end verification:** real OAuth client
>    registration (Notion/Google) per §7. The §8 fake-Notion/Gmail HTTP
>    fixtures allow building + testing Phases A–B without live clients.

## 1. Goal

Give the agent first-class, on-demand access to third-party SaaS
read/write tools (Gmail, Notion, Calendar, Slack, GitHub, …) via a
**Connectors** sidebar section the user can install once and toggle
per session.

A Connector is presented to the user as a single installable thing
("Gmail — archeryue7@gmail.com"). Under the hood it is the
combination of:

1. an OAuth-authorized **installation** (long-lived token, stored
   encrypted, reused across sessions), and
2. an **MCP server** that exposes that installation's API surface to
   the agent as `mcp__<kind>__<tool>` tool calls during a turn.

Both the Claude Code backend and (eventually) the Codex backend
register MCP servers via their respective `--mcp-config`-style flags,
so the same connector code works for both engines without duplication.

## 2. Non-goals (v1)

- **No marketplace / dynamic remote MCP discovery.** Every shipped
  connector is a Python module in this repo. No URL-installable
  connectors, no JSON manifests fetched at runtime.
- **No per-tool granular permission inside a connector.** If Gmail is
  enabled for a session, the model can call any Gmail tool the
  connector exports. (VM0 has a per-scope firewall — out of scope here
  until we have a real second user who needs it.)
- **No shared / org-wide installations.** Each installation belongs to
  the running Octopus instance. Multi-user / multi-org is future work.
- **No connector-driven inbound events.** Connectors are
  outbound-only (session → external). Inbound (external → session) is
  what `server/bridges/` already does; the two layers stay separate.

## 3. Concepts

| Term | Definition |
|---|---|
| **Kind** | The integration type (`gmail`, `notion`, `slack`, `github`, …). One Python module per kind under `server/connectors/`. Registered at import time in `server/connectors/registry.py`. |
| **Installation** | One authorized account for a kind. E.g. "Gmail account `archeryue7@gmail.com`" is one installation. Stored as a row in `connector_installations` with an encrypted token blob. Multiple installations per kind are allowed (work + personal Gmail). |
| **Per-session enablement** | A row in `session_connectors` saying "session `X` has installation `Y` turned on." When the backend spawns the CLI for session `X`, the connector's MCP entry is merged into `--mcp-config`. |
| **Default-on** | An installation may carry `enable_by_default = true`, in which case it auto-attaches to **newly created** sessions. Pre-existing sessions are not retro-enabled. |

## 4. User-facing UX

### 4.1 Sidebar slot

Insert a `CONNECTORS` section in `web/src/App.tsx:125-129`, between
`<ScheduleList />` and `<CredentialList />`. Same section visual
language as `CredentialList` / `ScheduleList`:

```
CONNECTORS                                [+]
  ● Gmail        archeryue7@gmail.com     ⋯
  ● Notion       My Workspace             ⋯
  ○ Slack        (installed, off here)    ⋯
  ○ GitHub       (installed, off here)    ⋯
```

- **Filled dot** = enabled for the active session.
- **Empty dot** = installed account, off for this session. Clicking
  the row toggles enablement for the active session — no dialog,
  optimistic update, single PATCH.
- **No active session** ⇒ the section still shows installed accounts
  (greyed dots, click is a no-op) so the user can manage installations
  without first opening a session.
- **`⋯` overflow menu**: "Rename", "Enable by default on new
  sessions" (toggle), "Disconnect" (deletes the installation; cascades
  to remove `session_connectors` rows).
- **`+` button**: opens a catalog picker modal listing every
  registered Kind that's not already installed (or that allows
  multiple installations). Selecting one starts that kind's OAuth flow
  in a dialog modeled exactly on `CredentialList`'s device-code flow.

### 4.2 New-session form

Add a "Connectors" multi-select to the session-create dialog, showing
all installations with `enable_by_default = true` pre-checked. The
selected set becomes the session's initial `session_connectors`
rows. Unchecking a default-on installation here makes the session
silently override the default for itself only.

### 4.3 Mid-session interaction

The model surfaces connector calls in the existing tool-use UI — no
new component. A Notion search shows as `mcp__notion__search(...)`
like any other tool call. Tool results render normally.

If the model calls a connector that has expired credentials,
`needs_reconnect = true` is set on the installation, the
`needs_reconnect` badge appears in the sidebar (same as
`CredentialList` already does for backend creds), and the tool result
returned to the model is a one-line "Connector needs reconnect — ask
the user to refresh it in the sidebar."

## 5. Architecture

### 5.1 Backend layout

```
server/connectors/
  __init__.py
  base.py              # ConnectorBase ABC, ConnectorInstallation dataclass
  registry.py          # KIND_REGISTRY: dict[str, type[ConnectorBase]]
  oauth.py             # ConnectorOAuthProvider protocol + login manager
                       # (distinct from server/oauth_providers.py, which is
                       # Anthropic-flavored — see §5.2 note)
  notion.py            # one file per kind
  gmail.py
  slack.py             # (later)
  github.py            # (later)
server/mcp_servers/connectors/
  __init__.py
  _shared.py           # callback env, error envelope, token-fetch helper
  notion.py            # the actual MCP stdio server; imports notion-client
  gmail.py             # imports google-api-python-client
```

### 5.2 `ConnectorBase` shape

```python
class ConnectorBase(abc.ABC):
    kind: str                  # "gmail"
    display_name: str          # "Gmail"
    category: str              # for grouping in the catalog picker
    allows_multiple: bool      # multiple installations of same kind?
    oauth: ConnectorOAuthProvider  # see §5.2 note below

    @abstractmethod
    def mcp_entry(
        self,
        installation: ConnectorInstallation,
        callback_env: dict[str, str],
    ) -> dict[str, Any]:
        """Return the dict that goes under mcpServers[<key>] in the
        --mcp-config JSON. Key is `<kind>_<short-id>` so two Gmail
        installations don't collide: `gmail_4a2f` etc.
        """

    @abstractmethod
    def system_prompt_blurb(
        self,
        installation: ConnectorInstallation,
    ) -> str:
        """Short paragraph appended to _OCTOPUS_SYSTEM_PROMPT so the
        model knows the tool exists and when to call it."""

    async def fetch_external_identity(
        self,
        token_set: OAuthTokenSet,
    ) -> tuple[str, str]:
        """After OAuth completes, call the provider's `me`/`profile`
        endpoint to get a stable (external_id, label) pair. Notion
        returns workspace_id + workspace_name; Gmail returns the
        authenticated user's email. Default raises NotImplementedError.
        """

    async def health_check(
        self,
        installation: ConnectorInstallation,
        token: str,
    ) -> HealthStatus:
        """Optional override. Default: try a cheap API call; set
        needs_reconnect on 401."""
```

**Why a separate OAuth abstraction, not the existing
`server/oauth_providers.py`:** the existing `OAuthProvider` protocol
(at `server/oauth_providers.py:53-84`) requires `mint_api_key`
(Anthropic-specific: trade an OAuth token for a `sk-ant-` API key)
and assumes the `<code>#<state>` paste callback format unique to
Anthropic's `oauth/code/callback` page. Connector providers (Notion,
Google, Slack…) don't mint API keys and use the standard
redirect-URI OAuth2 flow. We introduce a sibling
`ConnectorOAuthProvider` protocol in `server/connectors/oauth.py`:

```python
class ConnectorOAuthProvider(Protocol):
    kind: str
    authorize_url: str
    token_url: str
    default_scopes: list[str]
    pkce: bool                 # most modern providers want PKCE

    def build_authorize_url(self, *, redirect_uri: str,
                            code_challenge: str | None,
                            state: str) -> str: ...

    async def exchange_code(self, *, code: str, redirect_uri: str,
                            code_verifier: str | None,
                            state: str) -> OAuthTokenSet: ...

    async def refresh(self, refresh_token: str) -> OAuthTokenSet: ...
```

We reuse `OAuthTokenSet` (and the PKCE / state helpers) from
`server/oauth_login.py:210-237`. The two systems share data shapes
but not flows.

### 5.3 MCP servers per connector

Each connector ships its own stdio MCP server under
`server/mcp_servers/connectors/<kind>.py`. The server follows the
shape of `server/mcp_servers/ask.py` (the closest existing template
— it long-polls Octopus over HTTP from inside an MCP-subprocess).
Concretely:

- Reads `OCTOPUS_API_BASE`, `OCTOPUS_AUTH_TOKEN`,
  `OCTOPUS_SESSION_ID`, and `OCTOPUS_INSTALLATION_ID` from env
  (compare `server/mcp_servers/ask.py:107-114`).
- On each tool call, fetches a fresh access token from
  `GET /api/connectors/{installation_id}/token` (with retry on a
  ConnectionError — the host may be momentarily busy). A small
  in-process cache holds the token until its `expires_at`, so we
  don't HTTP every call.
- Calls the third-party API using the kind's official SDK
  (`google-api-python-client` for Gmail, `notion-client` for Notion).
- **Caps result size.** Returns are truncated to 32 KB of text by
  default (configurable per tool) — large unbounded results can
  trip the CLI premature-exit bug (CLI drops `tool_result` on
  stdout for results > ~50 KB; see
  `docs/2026-05-18-bg-pipeline-hardening.md` §2). When truncated,
  append a `…[truncated <N> bytes; use mcp__<kind>__fetch_page to
  paginate]` marker.
- On 401, POSTs `/api/connectors/{id}/mark-needs-reconnect` and
  returns a one-line "Token expired — ask the user to reconnect
  <Kind> via Octopus's sidebar."
- On rate-limit (429) or transient 5xx: respect `Retry-After`, retry
  up to 3× with exponential backoff inside the same tool call (the
  agent should not have to know about transient failures).

Each connector's tools are named `mcp__<kind>__<verb>` (e.g.
`mcp__gmail__search`, `mcp__gmail__send_draft`,
`mcp__notion__fetch`). The CLI advertises all tools from every
registered MCP server, so the model sees them automatically — no
extra plumbing.

**Tool-name convention** (avoid hitting the model's 64-char
tool-name limit, which `mcp__<kind>__` already eats 7-15 chars of):
verbs are short imperative snake_case — `search`, `get`, `create`,
`update`, `append`, not `create_a_new_page` or `searchAndFilter`.
Documented in `server/connectors/base.py`'s class docstring and
enforced by a unit test that asserts every registered tool fits in
under 60 chars.

### 5.4 Database

Three new tables (additive migration in `server/database.py`,
matching the existing ALTER-in-`_apply_migrations` pattern at
`server/database.py:149-196`):

```sql
CREATE TABLE IF NOT EXISTS connector_installations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                  -- 'gmail' | 'notion' | …
    label TEXT NOT NULL,                 -- 'archeryue7@gmail.com'
    auth_type TEXT NOT NULL,             -- 'oauth' | 'api_key'
    external_account_id TEXT,            -- workspace_id / email / org id
    scopes TEXT,                         -- JSON list: granted OAuth scopes
    enable_by_default INTEGER NOT NULL DEFAULT 0,
    needs_reconnect INTEGER NOT NULL DEFAULT 0,
    token_expires_at TEXT,               -- ISO8601, null for non-expiring
    last_refresh_error_code TEXT,        -- mirrors backend_credentials
    created_at TEXT NOT NULL,
    UNIQUE (kind, external_account_id)   -- prevent dup install for same account
);

CREATE TABLE IF NOT EXISTS connector_installation_secrets (
    installation_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,      -- see schema below
    FOREIGN KEY (installation_id)
        REFERENCES connector_installations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_connectors (
    session_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, installation_id),
    FOREIGN KEY (session_id)
        REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (installation_id)
        REFERENCES connector_installations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_session_connectors_session
    ON session_connectors(session_id);
```

`secret_encrypted` is a Fernet-encrypted (via `server/crypto.py`,
key derived from `OCTOPUS_AUTH_TOKEN`) JSON blob:

```json
{
  "access_token": "...",
  "refresh_token": "...",       // null if provider doesn't refresh
  "expires_at_epoch": 1715958400.0,
  "scopes": ["gmail.modify"],
  "token_type": "Bearer",
  "raw_provider_response": {…}  // kept for forensic debugging
}
```

The split-secret table mirrors the
`backend_credentials` / `credential_secrets` split that already
exists (lines 60-86 of `server/database.py`) so a future
`serverOnly` flag can keep refresh tokens out of the subprocess env
if we add scoped MCP servers later.

`UNIQUE (kind, external_account_id)` prevents two installations of
the same account; the install flow upserts on conflict (re-OAuth on
the same workspace overwrites the row rather than creating a
duplicate).

### 5.5 API routes

Add a new router `server/routers/connectors.py`. All routes are
bearer-authenticated with `OCTOPUS_AUTH_TOKEN` *except* the OAuth
callback (it's bounced into from the third party's browser redirect
— it can't carry our bearer; it carries the OAuth `state` + `code`
instead, and the `state` is the trust anchor).

```
GET    /api/connectors/catalog                       # registered kinds + per-kind config availability
GET    /api/connectors                               # installations (no secrets)
POST   /api/connectors/oauth/start                   # body: {kind}; returns {login_id, authorize_url}
GET    /api/connectors/oauth/callback                # 3rd-party browser redirect lands here
GET    /api/connectors/oauth/status/{login_id}       # frontend polls this while the popup is open
POST   /api/connectors/oauth/cancel                  # body: {login_id}; drop an in-flight attempt
PATCH  /api/connectors/{id}                          # body: {label?, enable_by_default?}
DELETE /api/connectors/{id}                          # uninstall (cascades session_connectors)
POST   /api/connectors/{id}/test                     # forced health check
GET    /api/connectors/{id}/token                    # INTERNAL — only the MCP subprocess
POST   /api/connectors/{id}/mark-needs-reconnect     # INTERNAL — only the MCP subprocess

GET    /api/sessions/{sid}/connectors                # ids enabled for this session
PUT    /api/sessions/{sid}/connectors                # body: {installation_ids: [...]}; replace set
PATCH  /api/sessions/{sid}/connectors/{iid}          # body: {enabled: bool}; toggle one
```

Two routes deserve special notes:

**`GET /api/connectors/oauth/callback`** is the redirect-URI the
third party POSTs the user's browser to after they consent. It
accepts `code` + `state` query params, validates `state` against the
pending login (CSRF), exchanges the code for tokens, calls
`fetch_external_identity()` to backfill the `label` /
`external_account_id`, persists the installation, and returns a
small HTML page that says "Connected — you can close this tab" and
`window.close()`s. **The frontend never sees the code.** Polling
`/api/connectors/oauth/status/{login_id}` is how it knows the install
finished.

**`GET /api/connectors/{id}/token`** is the only route the
MCP-subprocess calls. It returns `{access_token, expires_at_epoch}`
— never the refresh token (refresh is server-side; the MCP server
re-fetches when its cached `expires_at` is near). Concurrency-safe:
guarded by an `asyncio.Lock` per installation_id so two MCP servers
hitting an expired token at the same time refresh once.

### 5.5.5 OAuth flow (install a connector)

This is structurally different from Octopus's existing
`OAuthLoginManager` paste flow (`server/oauth_login.py`), so worth
spelling out. Octopus runs locally on `127.0.0.1:<port>` (or behind
a Cloudflare quick-tunnel; see `server/tunnel.py`); whichever it is,
the public base URL needs to match what the user registered as the
redirect URI with the third party.

```
[1] Frontend (catalog modal):
    user clicks "Connect Notion"
    → POST /api/connectors/oauth/start  {kind: "notion"}

[2] Backend:
    - resolves `notion` from KIND_REGISTRY
    - mints login_id (uuid4 hex[:16]), state (32 bytes b64url),
      pkce verifier+challenge
    - reads OCTOPUS_PUBLIC_BASE_URL from settings (defaults to
      f"http://127.0.0.1:{settings.port}" when unset; surfaced as
      a clear config error if the user is behind a tunnel and
      didn't set it)
    - redirect_uri = f"{public_base}/api/connectors/oauth/callback"
    - authorize_url = provider.build_authorize_url(redirect_uri=…,
                       code_challenge=…, state=login_id+":"+state)
    - stores PendingLogin(login_id, kind, state, verifier,
                          redirect_uri, requested_label)
      in an in-memory dict on a singleton (15 min TTL, GC like
      OAuthLoginManager._gc)
    - returns {login_id, authorize_url}

[3] Frontend:
    - window.open(authorize_url) — new tab
    - starts polling GET /api/connectors/oauth/status/{login_id}
      every 1.5s
    - shows "waiting for browser auth…" in the dialog

[4] User authorizes in the new tab; provider redirects browser to
    {public_base}/api/connectors/oauth/callback?code=…&state=login_id:state

[5] Backend callback:
    - split state on ":" → login_id, raw_state
    - look up PendingLogin, verify raw_state matches (CSRF)
    - provider.exchange_code(code=…, redirect_uri=…,
                              code_verifier=verifier, state=raw_state)
      → OAuthTokenSet
    - external_id, external_label = await connector.fetch_external_identity(token_set)
    - upsert connector_installations on (kind, external_account_id)
    - encrypt + write connector_installation_secrets
    - mark PendingLogin.state = "success", attach installation_id
    - return small HTML: "Connected — you can close this tab."
      + window.close() script

[6] Frontend status poll:
    - sees state=success with installation_id
    - hides the dialog
    - prepends the new installation to the sidebar list
    - if the catalog was opened from a session, immediately calls
      PATCH /api/sessions/{sid}/connectors/{iid} {enabled: true}
      so the new connector is on for that session by default
```

Refresh is server-side. `/api/connectors/{id}/token` (called by the
MCP subprocess) decrypts the bundle, and if
`expires_at_epoch - now < 300` seconds, calls
`provider.refresh(refresh_token)`, encrypts the new bundle back, and
returns the fresh access token. On refresh failure (revoked token,
provider 401), the installation row is marked
`needs_reconnect = 1` and `last_refresh_error_code` records why; the
sidebar shows the badge and the MCP server's tool result tells the
agent to ask the user to reconnect.

### 5.6 Backend wire-up

`ClaudeCodeBackend.build_args` already builds an `mcpServers` dict
inline (`server/backends/claude_code.py:251-274`). Three changes:

1. **Constructor gains a `connectors` param.** Today the constructor
   is `__init__(permission_callback, model, session_id)`
   (`claude_code.py:180-199`). We add
   `connectors: list[tuple[ConnectorBase, ConnectorInstallation]] = []`
   defaulting to empty so existing call sites keep working.
2. **`_make_backend` queries enabled connectors.** Current shape
   (`session_manager.py:866-871`) just returns
   `ClaudeCodeBackend(session_id=session.id)`. We expand to:
   ```python
   def _make_backend(self, session: Session) -> BackendBase:
       conns = await self.db.get_session_connectors_with_installations(session.id)
       # → list[(ConnectorBase, ConnectorInstallation)] for enabled rows only
       return ClaudeCodeBackend(session_id=session.id, connectors=conns)
   ```
   The DB helper joins `session_connectors` ⨝ `connector_installations`
   filtered by `enabled = 1` and decorates each installation row with
   the registry-looked-up ConnectorBase instance.
3. **`build_args` merges entries.** Inside the existing `mcp_config`
   construction (`claude_code.py:251-274`), iterate `self._connectors`
   and for each call `conn.mcp_entry(install, callback_env)` then
   merge into the `mcpServers` dict with key
   `f"{kind}_{install.id[:6]}"` so two installations of the same
   kind don't collide. Each entry includes
   `OCTOPUS_INSTALLATION_ID = install.id` in its env (on top of the
   shared callback env).

Then a fourth change: extend `_OCTOPUS_SYSTEM_PROMPT`. The constant
in `claude_code.py:37-110` is built at import time; we instead build
it dynamically in `build_args` by appending a
`== Connectors ==` section composed of each connector's
`system_prompt_blurb(install)` output. Per-turn re-send is fine
(it's already re-sent each `claude --print` invocation; the CLI
doesn't persist system prompts across `--resume`).

This is the only backend-side change needed for Claude Code. The
existing `--mcp-config` shape and tool-routing flow handle the rest.

For **Codex** (when `CodexBackend` lands per
`docs/future-features.md` item #1): the same connector list is
passed in, and `CodexBackend.build_args` writes an analogous Codex
MCP config. Codex's CLI honors `~/.codex/config.toml`, with MCP
servers declared as:

```toml
[mcp_servers.notion_4a2f]
command = "/.../python"
args = ["-m", "server.mcp_servers.connectors.notion"]
[mcp_servers.notion_4a2f.env]
OCTOPUS_API_BASE = "http://127.0.0.1:8765"
OCTOPUS_AUTH_TOKEN = "..."
OCTOPUS_SESSION_ID = "..."
OCTOPUS_INSTALLATION_ID = "..."
PYTHONPATH = "..."
```

Per-session isolation: `CodexBackend.build_args` writes a fresh
TOML into a per-session tempdir and exports `CODEX_HOME=<tempdir>`
on the subprocess env. VM0 already uses this `CODEX_HOME` redirect
pattern in `crates/guest-agent/src/cli/codex_setup.rs:42`, so the
mechanism is proven against the real binary.

The connector layer itself (DB tables, OAuth, MCP-server modules,
router, frontend) is engine-agnostic — only the
`build_args`/`mcp_entry` glue differs per backend.

### 5.7 Frontend layout

New files:

```
web/src/components/ConnectorList.tsx       # sidebar section
web/src/components/ConnectorCatalog.tsx    # picker modal
web/src/api/connectors.ts                  # typed fetch wrappers
```

Store additions (`web/src/stores/sessionStore.ts`):

```ts
type ConnectorInstallation = { id; kind; label; needs_reconnect;
                               enable_by_default; created_at };
type ConnectorCatalogEntry  = { kind; display_name; allows_multiple;
                                requires; icon_slug };

connectorCatalog: ConnectorCatalogEntry[];
connectorInstallations: ConnectorInstallation[];
sessionConnectorIds: Record<string, Set<string>>;  // sessionId → installationIds
setConnectorInstallations / setSessionConnectors / …
```

`App.tsx` insertion: one new line under the existing `<ScheduleList
/>`:

```tsx
<SessionList />
<ScheduleList />
<ConnectorList />          {/* NEW */}
<CredentialList />
```

`ConnectorList` follows the section-header pattern of
`CredentialList` / `ScheduleList` (compare `CredentialList.tsx:206-218`
for the header markup). The toggle-on-row interaction is new but the
visual is borrowed from the existing `credential-item` row.

`ConnectorCatalog` is a `Dialog` with a grid of cards (one per
catalog entry not already installed unless `allows_multiple`).
Clicking a card starts the OAuth dialog, which is the
`CredentialList`'s existing `awaiting_code` + `submitting` flow
generalized to take a `kind` rather than a hardcoded backend.

### 5.8 System prompt

The agent learns about installed connectors via the existing
`--append-system-prompt` mechanism (`claude_code.py:304`). Each
connector's `system_prompt_blurb(installation)` returns a short
section that gets concatenated under a `== Connectors ==` heading
appended after the existing in-app-tools section. Re-sent every
turn (the `claude` CLI doesn't persist system prompts across
`--resume`).

Concrete shape with both v1 connectors installed and enabled:

```
== Connectors ==

You also have access to the following third-party connectors. Treat
them as first-class tools — call them whenever the request involves
the linked account.

[gmail / archeryue7@gmail.com]
  mcp__gmail_4a2f__search(query)                 — Gmail search syntax
  mcp__gmail_4a2f__get(message_id)               — full message body
  mcp__gmail_4a2f__list_labels()                 — label catalog
  mcp__gmail_4a2f__create_draft(to, subject, body, in_reply_to?)
  mcp__gmail_4a2f__send_draft(draft_id)          — REQUIRES user confirm
  mcp__gmail_4a2f__label(message_id, label_ids)
  mcp__gmail_4a2f__unlabel(message_id, label_ids)

Before calling `send_draft`, ALWAYS show the drafted message in your
reply and call `mcp__ask__user` with a yes/no question. Never send
without an explicit user OK in the same conversation turn.

[notion / Octopus Workspace]
  mcp__notion_8c1e__search(query, filter?)
  mcp__notion_8c1e__fetch(id)                    — page or database
  mcp__notion_8c1e__create_page(parent_id, title, content_blocks)
  mcp__notion_8c1e__update_page(id, properties?, content_blocks?)
  mcp__notion_8c1e__append_blocks(parent_id, blocks)
  mcp__notion_8c1e__create_comment(page_id, text)
  mcp__notion_8c1e__list_users()

Notion writes are reversible (revision history), so no confirm step
is required — but explain what you're about to do before doing it.
```

The `_8c1e` suffixes are `install.id[:6]`, which keeps two Notion
workspaces or two Gmail accounts distinguishable in tool names.

## 6. v1 shipped connectors

The plan ships two end-to-end at v1 to prove the whole stack works
with real third parties. Both deliver real value on day one; neither
ships in a half-done state.

### 6.1 Notion

- **OAuth**: `https://api.notion.com/v1/oauth/authorize` →
  `https://api.notion.com/v1/oauth/token`. No PKCE
  (Notion uses HTTP Basic auth on the token endpoint with the
  registered client_id + client_secret). Scopes are *workspace-wide*
  (you authorize the integration once per workspace and it gets
  whatever the workspace admin allowed).
- **External identity**: `GET https://api.notion.com/v1/users/me`
  returns the bot user + the bot's `workspace_name`. We use
  `workspace_id` as `external_account_id` and `workspace_name` as
  `label`.
- **SDK**: `notion-client` (Python). Already on PyPI, well-maintained.
- **Tools** (final tool name = `mcp__notion_<short>__<verb>`):
  - `search(query, filter?)` — page/db title search
  - `fetch(id)` — full content (page + all child blocks, paginated
    server-side, capped at 32 KB serialized; large pages get a
    truncation marker)
  - `create_page(parent_id, title, content_blocks)`
  - `update_page(id, properties?, content_blocks?)`
  - `append_blocks(parent_id, blocks)` — add to bottom of a page
  - `create_comment(page_id, text)`
  - `list_users()` — for @mentions
- **Refresh**: Notion's tokens **don't expire** by default
  (workspace-OAuth grants are long-lived). We still record
  `expires_at_epoch` as `inf` and the refresh path is dead code for
  this connector; if Notion changes the policy, the existing path
  handles it without per-connector code.

### 6.2 Gmail

- **OAuth**: `https://accounts.google.com/o/oauth2/v2/auth` →
  `https://oauth2.googleapis.com/token`. PKCE strongly recommended
  (we enable it). Scopes: `https://www.googleapis.com/auth/gmail.modify`
  (covers read, label, draft, send; explicit user consent required
  in the browser).
- **External identity**: `users.getProfile(userId='me')` →
  `emailAddress`. That becomes both `external_account_id` and `label`.
- **SDK**: `google-api-python-client` + `google-auth`. Already-thin
  Python wrappers around Gmail's REST API.
- **Tools**:
  - `search(query, max_results=20)` — Gmail search syntax (`from:`,
    `is:unread`, etc.)
  - `get(message_id)` — full message with body decoded
  - `list_labels()`
  - `create_draft(to, subject, body, in_reply_to_message_id?)`
  - `send_draft(draft_id)` — **requires `mcp__ask__user` confirm**
    in the same turn; the connector module checks the host for a
    fresh ask-answer (see Open Question #2 in §10 for fallback if
    the model ignores the system-prompt instruction)
  - `label(message_id, label_ids)` / `unlabel(message_id, label_ids)`
- **Refresh**: Google's access tokens expire in 3600 s, refresh
  tokens are long-lived but **revocable**. Refresh failures (the
  token was revoked because the user clicked "Remove access" in
  Google Account settings) surface as `needs_reconnect` with
  `last_refresh_error_code = "invalid_grant"`.

### 6.3 Not in v1 scope

`slack`, `github`, `calendar`, `linear` are not in v1 but the
abstraction must be open enough to drop them in as a single PR
each: one `server/connectors/<kind>.py` (the descriptor) + one
`server/mcp_servers/connectors/<kind>.py` (the MCP server) + an
OAuth provider entry in `KIND_REGISTRY` + a catalog icon + the
test bundle from §8. No core surface should change.

## 7. OAuth client configuration

Per kind, the OAuth client_id and (when needed) client_secret come
from env vars read at `server.config.settings` load time:

```
OCTOPUS_PUBLIC_BASE_URL                # required if behind a tunnel; defaults to http://127.0.0.1:<port>
OCTOPUS_NOTION_OAUTH_CLIENT_ID
OCTOPUS_NOTION_OAUTH_CLIENT_SECRET
OCTOPUS_GMAIL_OAUTH_CLIENT_ID
OCTOPUS_GMAIL_OAUTH_CLIENT_SECRET
```

The catalog endpoint surfaces a per-kind `available: bool` flag
computed from whether the env vars are set, and the catalog picker
shows unavailable kinds disabled with a tooltip ("set
`OCTOPUS_GMAIL_OAUTH_CLIENT_ID` and `_CLIENT_SECRET` in env to
enable; see docs/connectors-setup.md").

A new doc `docs/connectors-setup.md` (created in Phase A) walks the
user through registering an OAuth client at Google Cloud Console
and Notion Integration settings, what redirect URI to enter
(matching `OCTOPUS_PUBLIC_BASE_URL`), and which scopes to grant.

Multi-tenant hosted deployments (different Octopus instances sharing
one OAuth client) are out of scope for v1. Each developer / installer
registers their own OAuth client with the third party.

## 8. Testing

Per CLAUDE.md, all suites must pass with zero failures before merge.

| Suite | What we add |
|---|---|
| Backend unit (`pytest tests/`) | `test_connectors_registry.py` (KIND_REGISTRY shape, tool-name-length invariant); `test_connectors_oauth.py` (PKCE state validation, state-CSRF rejection, refresh-on-near-expiry, refresh-failure marks needs_reconnect); `test_connectors_db.py` (CRUD, UNIQUE(kind, external_account_id) upsert, cascade delete to session_connectors); `test_router_connectors.py` (auth, 404s, PATCH/PUT/DELETE shapes, callback HTML response, status-poll lifecycle); `test_session_manager_connectors.py` (per-session `build_args` merges MCP entries with right keys + env); `test_connector_notion_mcp.py` + `test_connector_gmail_mcp.py` (the MCP server modules with `respx`-mocked HTTP, including 401→reconnect path and 429→retry path); `test_connector_truncation.py` (32 KB cap fires, marker is correct). |
| Backend integration | `test_oauth_flow_end_to_end.py`: spin up a fake-Notion HTTP server (mirrors `tests/_fixtures/fake_claude_cli.py` pattern), drive the install flow programmatically (POST start → simulate browser callback → assert installation persisted + secret round-trips through decrypt). Plus `test_real_cli_connector.py` (skipped when `claude` not on PATH) that registers a fake-Notion MCP server and verifies an actual `claude --print` invocation calls the tool and receives a result. |
| Frontend unit (`vitest`) | `ConnectorList.test.tsx` (dot states, toggle calls right endpoint, overflow menu actions); `ConnectorCatalog.test.tsx` (filters by `available`, opens new tab on connect, polls status); `connectorStore.test.ts` (catalog/installations/per-session id sets, optimistic toggle). |
| TypeScript check | `cd web && npx tsc --noEmit`. |
| E2E (`playwright`) | `connectors-install.spec.ts`: open catalog → fake-OAuth provider in a second Playwright context (so we can drive the callback) → installation appears + auto-enabled for active session. `connectors-per-session.spec.ts`: install once, open session A enable, open session B verify off, toggle in B, verify both states reflected in spawned `--mcp-config` (assert via a fake-CLI capture in test fixtures). `connectors-needs-reconnect.spec.ts`: fake-401 from the third-party API, badge appears, model gets the reconnect-message tool result, reconnect flow restores the row. `connectors-truncation.spec.ts`: fake a 100 KB Notion `fetch` result, verify the model receives a 32 KB-capped result with the truncation marker (and not the full payload that would risk the §17 CLI bug). |

The fake-Notion and fake-Gmail HTTP servers are the integration test
surface that lets us ship without a real OAuth client during CI.
Both are httpx-async-based stubs in `tests/_fixtures/` returning
canned response shapes for the small endpoint set we actually use.

## 9. Implementation phases

Each phase ends at a state where every test in §8 that exists at that
phase passes, and the feature is usable end-to-end at that scope —
**not** an MVP that ships and then gets polished later, per CLAUDE.md
("Do It Right The First Time").

**Phase A — Backend scaffolding (no UI yet).**
1. DB tables + migrations (`connector_installations`,
   `connector_installation_secrets`, `session_connectors`).
2. `server/connectors/base.py`, `registry.py`, `oauth.py` (incl.
   `ConnectorOAuthProvider` protocol).
3. `server/routers/connectors.py` with all routes from §5.5 incl.
   the `/oauth/callback` HTML response and the
   `/{id}/token` + `/mark-needs-reconnect` internal endpoints.
4. Token refresh + `needs_reconnect` lifecycle in the token route.
5. Unit tests + the fake-Notion fixture (used in Phase B). The
   integration test that drives the real `claude` binary is gated
   on `claude` being on PATH — present in CI, skipped locally.

**Phase B — First connector end-to-end (Notion).**
1. `server/connectors/notion.py` (descriptor + provider config +
   `fetch_external_identity`).
2. `server/mcp_servers/connectors/notion.py` (the MCP server,
   ~300 lines with truncation + reconnect path).
3. `ClaudeCodeBackend` constructor + `build_args` change
   (`server/backends/claude_code.py:180`, `:251`).
4. `session_manager._make_backend` queries enabled connectors
   (`server/session_manager.py:866`).
5. `ConnectorList.tsx` + `ConnectorCatalog.tsx` + store fields +
   `App.tsx` insertion.
6. OAuth dialog (new tab + status poll, not paste-flow — diverges
   from `CredentialList`'s code paste).
7. `docs/connectors-setup.md` covering Notion OAuth registration.
8. Backend integration test + Playwright `connectors-install`,
   `connectors-per-session`, `connectors-truncation` specs.

**Phase C — Gmail.**
1. `server/connectors/gmail.py` + `server/mcp_servers/connectors/gmail.py`.
2. Catalog entry, OAuth provider config (PKCE-enabled).
3. `send_draft` confirmation system-prompt rule + the server-side
   fallback check if Open Question §10.3 lands on "enforce server-side."
4. `docs/connectors-setup.md` update for Google Cloud Console.
5. Tests in parallel with Notion's.

**Phase D — Polish promised surfaces.**
1. New-session form's "Connectors" multi-select (§4.2).
2. `needs_reconnect` badge wiring + notifier hook (§12) +
   Playwright `connectors-needs-reconnect` spec.
3. Per-installation rename + default-on toggle in the `⋯` menu.
4. CLI `pull` help-text update calling out connector non-export.

Phases A → D each produce a shippable cut. No phase leaves a `# TODO`
behind. If we don't have time for a phase, it doesn't start.

## 10. Open questions / decisions to confirm before Phase A

1. **Verify `codex` TOML MCP config against the real binary.**
   Codex isn't installed on this machine (`which codex` returns
   "not found"). The plan as-written assumes `~/.codex/config.toml`
   with `[mcp_servers.name]` entries, honored via `CODEX_HOME`
   redirect. Need a 30-min spike when CodexBackend starts: install
   the `codex` CLI, write a minimal config that registers our `ask`
   MCP server, run `codex exec` against it, confirm the model can
   call `mcp__ask__user`. If the actual flag/config shape diverges,
   the change is isolated to `CodexBackend.build_args` — connector
   modules are unaffected.

2. **`OCTOPUS_PUBLIC_BASE_URL` resolution when behind a tunnel.**
   `server/tunnel.py` lets Octopus expose itself via Cloudflare
   quick-tunnel. The OAuth redirect URI must match what the user
   registered with the third party. Two options:
   - **Static config**: require the user to set
     `OCTOPUS_PUBLIC_BASE_URL` when behind a tunnel, error clearly
     at install time if it's missing. Simplest, most explicit.
   - **Dynamic**: read `X-Forwarded-Host` from the OAuth-start
     request and use that as the redirect base. Works without
     config but requires the tunnel to set the header (Cloudflare
     does) and means redirect_uri varies per-install — Google/Notion
     pre-register exact URIs, so this only works if the user
     pre-registered the tunnel host.

   **Recommend** the static option (simpler, predictable). Document
   that the tunnel URL must be stable for connector OAuth to work,
   which means users wanting connectors over a tunnel need a stable
   custom Cloudflare tunnel, not a quick-tunnel ephemeral URL.

3. **Should `send_draft` confirmation be system-prompt-only or
   enforced server-side?** Plan as-written uses the system prompt
   (cheap, follows existing pattern). If the model ignores it, the
   Gmail MCP server gains a hard check: before `send_draft`, call
   the host's `/api/sessions/{sid}/questions?recent_answer=...` and
   refuse if there's no answer with `approved=true` for this draft
   in the current turn. Decide after first real-account test.

4. **Concurrency safety on `/api/connectors/{id}/token`.** Two MCP
   subprocesses can hit it at the same time on an expired token; both
   try to refresh. Use an `asyncio.Lock` per installation in the
   router, keyed on `installation_id`. Cap dict size or evict on
   uninstall to avoid leaking locks. Trivial — flagged so we don't
   forget.

5. **Tool-name length.** `mcp__<kind>_<short>__<verb>` is fine for
   v1 (`mcp__gmail_4a2f__create_draft` is 33 chars). Some future
   kinds (HubSpot, Atlassian) have verbose verbs — keep verbs short
   and snake_case from day one and add a unit test that asserts
   every registered tool fits in under 60 chars.

6. **Result-size truncation policy.** §5.3 says cap at 32 KB to
   avoid the §17 premature-exit bug. But Notion `fetch` on a long
   page hits this quickly. Two competing concerns: stay safe
   from the CLI bug, but don't lose user data. Recommend:
   - Default cap 32 KB per tool result.
   - Add a `mcp__<kind>__fetch_page(id, cursor)` for paginating
     large results — explicit opt-in by the model.
   - Document the cap in each tool's docstring so the model knows
     it can paginate.

7. **What happens to `session_connectors` on session archive?**
   Archiving a session sets `sessions.archived = 1` but keeps the
   row. The `session_connectors` rows stay too — fine, since they
   only cost on the spawn path which won't fire for an archived
   session. If the session is later unarchived, the connector set
   is preserved.

8. **CLI handoff/pull export.** `server/cli.py handoff` and `pull`
   round-trip a session as JSONL. Connector installations are
   machine-local secrets; we **do not** export them via
   handoff/pull. After a `pull`, the session arrives with an empty
   `session_connectors` set on the destination machine. Document
   this in `cli.py`'s `pull` help text and the matching docs.

## 11. VM0 reference — what we borrowed, what we didn't

VM0 (`/home/start-up/vm0`) was the prompt for this feature. Where
the designs diverge, here's the reasoning.

**Borrowed**

- Per-(user, agent) sparse enablement join table (their
  `user_connectors` at `turbo/packages/db/src/schema/user-connector.ts`
  → our `session_connectors`). Identical pattern, different
  granularity (we scope per-session, not per-agent-type).
- One installation row per OAuth-authorized account
  (`turbo/packages/db/src/schema/connector.ts`). We add a
  `UNIQUE(kind, external_account_id)` constraint to enforce dedup;
  VM0 enforces it at the org level.
- OAuth provider definitions as a registry of per-kind configs with
  `{authorizationUrl, tokenUrl, scopes, secrets}`
  (`turbo/packages/connectors/src/connectors/notion.ts` and
  `gmail.ts` are 35 lines each — pure metadata).

**Deliberately did not borrow**

- **Network-layer mitmproxy as the connector mechanism.** VM0 routes
  the agent's HTTPS through a mitmproxy addon
  (`crates/runner/mitm-addon/src/mitm_addon.py`) that pattern-matches
  URLs and injects auth headers. The agent calls plain `curl` /
  `WebFetch` and never sees a "Gmail tool." This is the right call
  for VM0's actual goal — multi-tenant sandbox isolation, hundreds
  of services as URL rewrite rules — but it's the wrong fit for
  Octopus: we're local and single-user; the agent benefits more
  from typed, named tools than from transparent network rewrites;
  and we'd inherit CA-cert-trust complexity. We use MCP servers
  instead — visible `mcp__notion__search` tool calls in the chat
  UI, real schemas, SDK use.
- **Settings-*page* layout vs sidebar section.** VM0's
  `zero-connectors-page.tsx` is a full settings page with category
  sidebar, search box, and ~150 cards. We put connectors in a
  **sidebar section** instead. The justification: VM0 has so many
  connectors that browsing them is itself a task; for us, with 2-6
  connectors typical, list-in-sidebar is faster (no navigation
  away). The catalog dialog (§4.1) is the small VM0-page-equivalent
  surface, opened on demand.
- **`--tools` allow-list flag for Claude.** VM0 passes
  `--tools <comma-list>` to scope what the model can call
  (`crates/guest-agent/src/cli/command.rs:67-75`). We don't need
  scoping (`--dangerously-skip-permissions` already trusts the
  subprocess) and we'd lose the implicit tool advertisement that
  comes from `--mcp-config` listing every server.
- **Org / firewall / scope-review modals.** VM0's
  `ConnectorPermissionDialog` lets the user audit OAuth scopes before
  accepting. We accept default scopes for v1; scope-review lands
  when (and if) we have multi-tenancy.

**Per-connector workload — the real cost difference**

VM0's connector module is ~35 lines of metadata
(`gmail.ts` defines endpoints + secret-key names; the *behavior*
lives in the mitmproxy firewall config). Ours has to ship a real
MCP server module per kind (`server/mcp_servers/connectors/gmail.py`,
~200-400 lines wrapping the official SDK). So adding the 50th
connector in our scheme costs roughly 10× the per-connector code of
VM0's scheme. That's the price of giving the agent typed semantic
tools instead of opaque HTTP. For v1 with 2 connectors, the price
is right; if we ever ship 50+, we should revisit and consider a
codegen layer that turns OpenAPI specs into MCP modules.

## 12. Interactions with existing Octopus surfaces

Found while researching. Calling these out so we don't break them.

- **`server/tunnel.py` (Cloudflare quick-tunnel).** If the user
  enables a tunnel, the OAuth callback runs against the tunnel URL,
  not localhost. The user must register their tunnel URL as the
  redirect URI with each OAuth provider and set
  `OCTOPUS_PUBLIC_BASE_URL` accordingly. See Open Question §10.2 —
  this means quick-tunnel ephemeral URLs don't compose well with
  connectors; document that users wanting connectors-over-tunnel
  need a stable named tunnel.
- **`server/notifiers/` (webhook notifications).** Existing pattern
  for notifying when a session goes idle. Connector failures
  (`needs_reconnect`) should *also* fire a notifier event so the
  user finds out before next using the connector. Hook into
  `notifier_manager.notify_session_event(session_id, "connector_needs_reconnect", {kind, label})`.
- **CLI premature-exit bug** (post-mortem in
  `docs/2026-05-18-bg-pipeline-hardening.md` §2; auto-respawn
  recovery lives in `server/session_manager._run_backend`).
  Large tool results (text > ~50 KB or any image > ~900K tokens
  context) trigger the CLI to drop the `tool_result` event on
  stdout, silently. Connector results are exactly the high-risk
  shape (Notion `fetch` of a long page, Gmail `search` returning
  many messages). Mitigation: hard cap 32 KB per result in the
  connector MCP server (§5.3) and surface pagination tools for
  larger reads. Re-evaluate if the upstream CLI ever lands a fix.
- **`server/scheduler.py` recurring sessions.** A scheduled task
  that runs in an existing session will get whatever
  `session_connectors` are enabled for that session — same
  connectors the user would see if they ran the prompt manually.
  No special handling needed.
- **`server/bridges/telegram.py` inbound.** A Telegram-routed
  user-message goes through the same session, so the same connector
  set applies. The model can call `mcp__gmail__search` in response
  to a Telegram message; the result is delivered to Telegram as
  text. Nothing to change.
- **`server/cli.py handoff` / `pull`.** Round-trip a session as
  JSONL. Connector installations are machine-local secrets — we do
  **not** include them in the export. After a `pull`, the session
  arrives on the destination machine with an empty
  `session_connectors` set. Add a `pull` help-text line: "Note:
  connectors do not transfer between machines; re-enable on the
  destination."

## 13. What's still unverified

These are claims in this plan that I haven't proven on real
infrastructure. Each one needs a verification step before or during
implementation.

- **Codex MCP TOML config shape.** §5.6 — `codex` not installed here.
- **Notion's API doesn't paginate `users/me`-style identity.** Plan
  uses `GET /v1/users/me` (the bot endpoint); confirm the actual
  workspace-id field name when implementing.
- **Google OAuth's PKCE support for installed apps.** Plan enables
  PKCE; confirm Google honors `code_challenge` for the
  `client_secret`-bearing flow Octopus uses (vs the PKCE-only
  "installed application" flow which has different consent UX).
- **`notion-client` SDK supports MCP-compatible streaming**. Plan
  assumes synchronous calls; if any operation requires SSE the MCP
  server has to bridge it. Likely fine for our tool set.
- **`mcp__viewer__show_file` env injection doesn't collide.** Our
  MCP servers all share a callback env (`OCTOPUS_API_BASE`,
  `OCTOPUS_AUTH_TOKEN`, `OCTOPUS_SESSION_ID`). Adding
  `OCTOPUS_INSTALLATION_ID` for connectors should be fine but
  verify that the viewer server doesn't read it and fail closed if
  it sees an unexpected env var.
