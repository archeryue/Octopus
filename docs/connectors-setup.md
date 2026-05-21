# Connectors — setup

Connectors let an agent call third-party accounts (GitHub, Gmail, or any
OAuth2 API you define) as tools. Everything here is doable **from the browser**
— no server shell, env, or filesystem access required — so it works when you
reach Octopus remotely (e.g. via a tunnel).

The one unavoidable step is registering an OAuth app with the provider to get a
**client id + secret** — but that's done in your browser too. (A hosted SaaS
hides this by pre-registering one shared app centrally; self-hosted, you're the
operator, so you register your own.)

## The flow, in the UI

1. Sidebar → **Connectors → +** opens the catalog.
2. A connector with no OAuth client shows **Set up** → paste its client id and
   secret (stored **encrypted** in the DB; no env edit, no restart). It then
   shows **Connect**.
3. **Connect** opens the provider's consent screen in a new tab; approve it and
   the installation appears in the sidebar.
4. Enable it per agent in **Agent settings → Connectors**. That agent gets the
   connector's tools on its next turn.

The **Set up** dialog shows the exact **Redirect URI** to register with the
provider — derived from the URL you're hitting (so behind a tunnel it's your
public host, not localhost). Copy it from there.

## GitHub

In the Set up dialog, follow the steps (link goes to GitHub Developer settings):

1. GitHub → Settings → Developer settings → **OAuth Apps → New OAuth App**.
2. **Authorization callback URL** = the redirect URI shown in the dialog.
3. Register, then **Generate a new client secret**.
4. Paste the Client ID + secret into Set up → **Save** → **Connect**.

Scopes (`repo`, `read:org`) are requested at sign-in. Classic OAuth-App tokens
don't expire, so there's nothing to refresh.

## Gmail (Google Cloud)

Gmail is fussier — three things trip people up, in order:

1. **Enable the Gmail API** for your project:
   `console.cloud.google.com/apis/library/gmail.googleapis.com` → **Enable**.
   *Without this, sign-in succeeds but the profile lookup fails with `403`.*
2. **Add yourself as a Test user**: Google Auth Platform → **Audience**
   (`console.cloud.google.com/auth/audience`) → **Test users → Add users** →
   your Gmail address. *While the app is in "Testing", anyone not listed is
   blocked with `Error 403: access_denied`.*
3. **Create the OAuth client**: APIs & Services → Credentials → Create
   credentials → **OAuth client ID → Web application** → add the redirect URI
   under **Authorized redirect URIs**. Paste the Client ID + secret into Set up.

Scope is `gmail.modify` (read, label, draft, send). **Caveat:** while the app
stays in "Testing", Google expires the refresh token after ~7 days, so Gmail
will periodically show **needs reconnect** — click reconnect to re-authorize.
Removing that requires publishing the app and passing Google's verification
review (heavy; only worth it beyond personal use).

## Custom connectors (any OAuth2 API)

Catalog → **Add custom connector** to define a new kind without server code:

- **Slug** (e.g. `linear`) and **display name**.
- **Authorize URL**, **Token URL**, **API base URL**, **scopes**, **PKCE** on/off.
- **Client ID + secret** from the OAuth app you register with that provider
  (callback = the redirect URI shown in the form).

The agent gets one tool, `mcp__<kind>_<id>__request(method, path, query?,
body?)`, that calls `{api_base}{path}` with the stored token attached. Remove a
custom connector anytime with the **×** in the catalog (this also deletes its
installations + stored creds).

## Env-var fallback (operators with server access)

If you run the server yourself, you can pre-seed client creds via env instead
of the Set up dialog (the in-app config takes precedence when both exist):

```bash
OCTOPUS_GITHUB_OAUTH_CLIENT_ID=...     OCTOPUS_GITHUB_OAUTH_CLIENT_SECRET=...
OCTOPUS_GMAIL_OAUTH_CLIENT_ID=...      OCTOPUS_GMAIL_OAUTH_CLIENT_SECRET=...
# Only needed if the request-derived redirect URI is ever wrong for your setup:
OCTOPUS_PUBLIC_BASE_URL=https://your-stable-host
```

Secrets (client secrets and connector tokens) are encrypted at rest with
Fernet, keyed by `OCTOPUS_AUTH_TOKEN`; tokens are only ever read by the
connector's MCP subprocess at tool-call time.
