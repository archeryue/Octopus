# Applications — agent-built web apps, rendered in-app

> **Status:** shipped — Agent-built web apps rendered in-app, with a build session and a status derived from the entrypoint on disk.

> **Implementation status: SHIPPED.** `server/applications.py`,
> `server/routers/applications.py`, the `applications` table, and the
> `ApplicationList` / `ApplicationCreate` / `ApplicationView` frontend
> components are all in the tree. Tests: `tests/test_applications.py`,
> `web/src/components/ApplicationList.test.tsx`,
> `web/src/components/ApplicationView.test.tsx`,
> `web/e2e/applications.spec.ts`.

## 1. The idea

An **Application** is a small web app that one of your agents wrote for you,
living in a directory Octopus owns, listed in the sidebar, and rendered in the
main pane like a browser tab.

The whole loop is:

1. Click **+** on the sidebar's *Applications* section.
2. Type a name + a description of what you want, pick the agent that should
   build it.
3. Octopus creates the app directory, opens a **build session** under that
   agent (working dir = the app dir), and sends it a build brief.
4. When the agent's turn ends and `index.html` exists, the app flips to
   `ready` and the main pane renders it in an iframe.
5. Ask for changes straight from the app view — that's another turn in the
   same build session, so the agent keeps its context.

No new runtime, no port allocation, no process supervision: applications are
**static files served by the Octopus process**. That constraint is what makes
the feature small enough to be perfect instead of half-built (§10).

## 2. Data model

One table, `applications` (new-table-only — `CREATE TABLE IF NOT EXISTS` is a
no-op migration on existing DBs):

| column | meaning |
|---|---|
| `id` | 12-char hex, same scheme as sessions/agents |
| `name` | display name; unique (case-insensitive) |
| `description` | what the user asked for; also fed to the agent |
| `icon` | emoji shown in the sidebar |
| `agent_id` | builder agent; `ON DELETE SET NULL` (apps outlive agents) |
| `session_id` | the build session; `ON DELETE SET NULL` |
| `app_dir` | absolute path to the app root |
| `entrypoint` | file served for `/apps/{id}/` — default `index.html` |
| `status` | `building` \| `ready` \| `failed` |
| `error` | last failure reason (null when fine) |
| `created_at` / `updated_at` / `last_built_at` | timestamps |

`app_dir` is allocated under `settings.applications_dir`
(`~/.octopus/applications`) as a slug of the name, de-duplicated with a `-2`,
`-3`, … suffix. It is stored on the row, so renaming an application never
moves files.

The build session is a plain `Session` row with `origin='application'` — the
same trick delegations use (`origin='delegation'`). No parallel session
concept; it shows up in the sidebar under its agent and behaves like any other
conversation.

## 3. Serving the app

`GET /apps/{app_id}/{path}` streams a file out of `app_dir`:

* the path is resolved and checked to be inside `app_dir` (`..`, absolute
  paths and symlinks that escape are 404s, not 403s — we don't confirm what
  exists outside);
* a directory (or the bare app root) resolves to `entrypoint`;
* `Cache-Control: no-store`, so a rebuild is visible on reload;
* auth accepts a bearer header, `?token=`, **or** the `octopus_app_token`
  cookie. The cookie exists because an `<iframe>` cannot send an
  `Authorization` header, and the app's own sub-resource requests
  (`<script src>`, `<link href>`, `fetch`) have to authenticate too. The SPA
  sets it (same-origin, `path=/apps`) right before mounting the iframe.

The iframe is sandboxed with `allow-scripts allow-same-origin allow-forms
allow-popups allow-modals allow-downloads`. `allow-same-origin` is deliberate:
without it the app gets an opaque origin and `localStorage` throws, which
breaks the most common kind of small web app there is. What we withhold is
`allow-top-navigation`, so a buggy app can't navigate the Octopus tab away
from under the user. The apps are written by the user's own agent, which
already has shell access to the host — the sandbox is a blast-radius limiter,
not a trust boundary.

## 4. Lifecycle

```
POST /api/applications           create row + dir + build session, send brief
      ↓ (status=building)
  agent's turn runs in the build session
      ↓ result / error broadcast on the session bus
ApplicationManager re-checks the entrypoint
      ↓
status = ready (entrypoint exists)  |  failed (it doesn't)
```

`ApplicationManager` subscribes to `SessionManager`'s broadcast bus (the
pattern `DelegationManager` uses). Every terminal event (`result`, `error`) on
a session that is some application's build session re-evaluates that app:
entrypoint present → `ready`, absent → `failed` with the reason. That makes
*every* turn in the build session a rebuild check, so "ask for changes" gets
status tracking for free.

Each transition broadcasts `application_created` / `application_updated` /
`application_deleted` over the same WS bus every client already listens to, so
the sidebar and the app view update live.

## 5. REST surface

| route | purpose |
|---|---|
| `GET /api/applications` | list (sidebar) |
| `POST /api/applications` | create + kick off the build |
| `GET /api/applications/{id}` | one row |
| `PATCH /api/applications/{id}` | rename / re-icon / change entrypoint |
| `POST /api/applications/{id}/build` | another build turn (`{prompt}`) |
| `DELETE /api/applications/{id}` | drop the row; `keep_files=true` keeps the dir |
| `GET /apps/{id}/{path}` | the app itself (cookie/bearer/query auth) |

`DELETE` removes the directory by default, but **only** after confirming it
sits under `settings.applications_dir` — a hand-edited `app_dir` pointing at
`/` can't make us `rmtree` it. The build session is never deleted: sessions
are history, and history is precious.

## 6. The build brief

The prompt the agent receives is assembled in
`ApplicationManager.compose_build_prompt`. It states the app name, the user's
description, the absolute directory, and the contract that makes rendering
work at all:

* entry point is `index.html` at the app root;
* static files only — no build step, no dev server, no `npm install`;
* libraries come from a CDN or get vendored into the directory;
* state lives in the browser (`localStorage`) — there is no backend;
* it renders inside an iframe at whatever size the pane is, so it must work
  at both desktop and phone widths;
* finish by verifying `index.html` exists.

Follow-up builds reuse the same session and get a shorter brief (the agent
already has the context) plus the same closing verification requirement.

## 7. Frontend

* `ApplicationList` — the sidebar section. Fetches `/api/applications` once,
  lives off the store afterwards (WS keeps it fresh). Rows show the icon,
  name, a status dot, and a hover delete button. **+** opens the create pane.
* `ApplicationCreate` — the main-pane form: name, description, agent picker,
  optional extra instructions. Submitting POSTs and selects the new app.
* `ApplicationView` — the main-pane app host: a header (icon, name, status,
  reload / open-in-tab / archive, the backend readout, the app's own agent
  conversations, and **Iterate**) over the iframe, and nothing else. Asking
  for a change lives in the Iterate popover along with the build-session
  link; it used to be a composer bar pinned under the page, which cost ~60px
  of every application forever to serve something you do rarely
  (app-agent-access.md §7). While `building` it shows a spinner panel instead
  of the frame; while `failed`, the error and where to ask for a fix.

Routing is store-level, not URL-level (Octopus has no router): `mainView` is
`"chat" | "application" | "application-create"`, and picking a session flips
it back to `"chat"` from inside `setActiveSessionId` so every existing call
site gets the behavior without edits. ChatView stays mounted (just hidden)
behind an application, so switching panes doesn't throw away a composer draft
or scroll position.

A build session is created server-side, so no client has it in the sidebar
list — which is only fetched on mount and on reconnect. `lib/hydrateSession.ts`
closes that gap: the `application_*` WS handler pulls the new session in, and
`lib/selectSession.ts` (shared with the sidebar) adopts the detail it already
fetched. Forks use the same helper, replacing their own copy of the logic.

## 8. Testing

* `tests/test_applications.py` — manager + routes + static serving:
  slug allocation and de-duplication, create wires a build session and fires
  the brief, the broadcast subscriber flips `building → ready` when the
  entrypoint appears and `→ failed` when it doesn't, follow-up build reuses
  the session, path traversal is refused, cookie/query/bearer auth, delete
  removes the dir only inside the managed root, and the delete guard against
  a tampered `app_dir`.
* `web/src/components/ApplicationList.test.tsx` /
  `ApplicationView.test.tsx` — sidebar rendering + status dots + selection,
  and the view's building/ready/failed branches.
* `web/e2e/applications.spec.ts` — the real loop with the real `claude` CLI:
  create an app from the UI, wait for `ready`, assert the iframe actually
  renders the agent's markup, ask for a change and see it land, delete it.

## 9. Config

```
OCTOPUS_APPLICATIONS_DIR   default ~/.octopus/applications
```

The e2e suite points it at a temp dir (cleaned in global teardown) so runs
never litter the developer's real applications root.

## 10. What this defers

* **Server-side apps.** Applications are static by contract. Anything that
  needs a process (a Flask API, a Next dev server) needs port allocation,
  supervision, health checks and a reverse proxy — a different feature with a
  different plan, and one that shouldn't be bolted onto this one halfway.
* **Sharing / publishing.** Apps are served behind the same token as the rest
  of Octopus. Public publishing is a hosting decision, not a UI one.
* **Interrupting an in-flight build on delete.** Deleting an application
  removes the row and the directory, but does not stop a build turn that is
  already running in its session — and that turn will happily recreate the
  directory it was writing to. The result is an orphan: files on disk with no
  row, so nothing lists it, nothing reaps it, and it stays there. Observed in
  practice (a delete at T, the directory back at T+2min). Left as-is on the
  owner's call — it needs a delete to reach into the session manager and
  interrupt a turn, which is a bigger seam than the symptom warrants. The
  cleanup is `rm -rf` on the stale directory.
* **Versioning / rollback.** The build directory is the app. Git-anchoring an
  application the way `/rewind` anchors a turn needs the app dir to be a repo,
  which is a real design question rather than an increment of this one.
