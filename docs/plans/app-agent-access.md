# Applications that can talk to the agents

> **Status:** shipped — The conversations a *running* application holds with an agent, over a per-app scoped token.

> **Implementation status: SHIPPED.** `server/app_agent.py` (the manager), the
> `/apps/{id}/agent/*` routes in `server/routers/applications.py`, the scoped
> token in `server/applications.py` + `script_env`, the `sessions.app_id`
> column, and the header chrome in `web/src/components/ApplicationView.tsx`.
> Tests: `tests/test_app_agent.py` (27), `tests/test_app_agent_real.py` (a
> live agent answering a question about context an app supplied, across two
> turns of one conversation), the `ApplicationView` / `BackendPanel` /
> `SidebarAgents` renderer tests, and the applications e2e.
>
> Two changes ship together: the application view's chrome moves out of the
> page's way, and an application gains a first-class way to hold a
> conversation with an Octopus agent.

## 1. What's missing

An Application today is a page (optionally with a backend) that Octopus hosts
and an agent builds. The agent is *upstream* of the app: it writes the code,
then leaves. Nothing in the running app can reach an agent.

That ceiling is the interesting one. Take "SmartReader" — an app that opens a
web page or a PDF and lets you discuss it. Everything about it is easy in
Octopus terms *except* the part that matters: the discussion. The app can
fetch the document (backend), store it (`$APP_DATA_DIR`), render it (page) —
and then has no way to ask Octopus's own agents a single question about it.
Its only options are to ship its own API key to some third-party model, or to
pretend the feature doesn't exist.

So: an application needs to be able to hold a conversation with an agent, from
its page **and** from its backend.

## 2. The shape: one API, mounted where the app already lives

```
/apps/{app_id}/agent/agents                      GET    who can I talk to
/apps/{app_id}/agent/conversations               GET    my threads
/apps/{app_id}/agent/conversations/{cid}         GET    one thread's messages
/apps/{app_id}/agent/conversations/{cid}         DELETE forget a thread
/apps/{app_id}/agent/ask                         POST   one question, one answer (JSON)
/apps/{app_id}/agent/chat                        POST   the same turn, streamed (SSE)
```

It sits under `/apps/{id}/` for the same reason the backend proxy does: the
page reaches it at `agent/…` relative to itself, with no base URL to configure
and no CORS. `ask` and `chat` are the same operation with two deliveries —
a backend script wants one JSON blob, a chat UI wants tokens as they land.

## 3. A conversation is a session

An app conversation is a real Octopus session (`origin='app'`, new nullable
`sessions.app_id` naming its owner). Not a parallel mechanism, because every
property we'd have to reinvent is already there: the transcript persists,
the harness resumes it so turn 5 remembers turn 1, tools and MCP servers work,
the broadcast bus already carries deltas and results, and you can open the
thread in the normal chat view and read exactly what your app has been saying.

`app_id` rather than an id encoded in `origin`: it's how a route proves a
conversation belongs to the app that asked for it, how the sidebar filters
them out, and how deleting an application takes its conversations with it.

## 4. Two credentials, because two callers

The page inside the iframe already authenticates with the `octopus_app_token`
cookie, so its `fetch("agent/ask", …)` needs nothing new.

A backend script gets a **scoped token** instead:
`HMAC-SHA256(auth_token, "app:<app_id>")`, handed to it as `OCTOPUS_APP_TOKEN`
with `OCTOPUS_AGENT_API` as the base URL. It opens exactly one application's
`/apps/<id>/…` surface and is useless anywhere else in Octopus. This keeps the
promise `script_env` already makes — the server's environment, and the master
token in it, never reach app code (application-backends.md §4) — while still
letting a backend hold a conversation. It needs no storage: derived from the
master token, so rotating that rotates every app's token with it.

## 5. What the agent is told

The first turn of a conversation carries a preamble naming the app, then every
turn is `message` plus optional `context` (what the app wants the agent to look
at — the article, the selection, the row). Three things the preamble fixes,
because each is a way the conversation would otherwise go wrong:

* **Reply as prose.** The person is in an app, not a terminal.
* **Don't touch the app's code.** Changing the app happens in its build
  session, where it's reviewable, not through a message the app composed.
* **Work in the app's data directory.** `<slug>.data/` is the session's
  working dir, so files the agent writes land in the app's own state and
  survive a rebuild.

## 6. Limits

An app is a program; a program can loop. Each cap returns a specific error
rather than letting Octopus absorb the cost:

| cap | value | why |
|---|---|---|
| concurrent turns per app | 4 | each one is a CLI process |
| conversations per app | 50 | a thread-per-request bug is otherwise invisible |
| message | 32k chars | |
| context | 200k chars | roughly 50k tokens; beyond this, summarize first |
| `ask` wait | 300s | a real turn can be slow; a hung one shouldn't pin a socket |

## 7. UI: chrome that gets out of the way

The application view carried a permanent composer bar under the page. It cost
~60px of every app forever to serve something you do rarely, and the ask was
explicit: put it away. The bar and the "Build session" button merge into one
top-right **Iterate** control whose popover holds the change request, the
build-session link and the last error. The page gets its space back.

Conversations started by the app are *hidden* from the sidebar rails — an app
that talks to an agent ten times an hour must not bury your own sessions — and
reachable from the same header, so "what is my app saying to my agent?" is one
click, not a mystery.

## 8. Testing

Backend: token derivation and scope (a token for app A opens nothing of app
B's), the full route surface incl. every cap and every 4xx, a conversation
resuming across turns, the SSE frame order (`conversation` → `delta`* →
`message` → `done`), deletion cascading, and a real-CLI test that asks a live
agent a question through the app API and reads its answer.
Frontend: the Iterate popover (opens, sends, closes, reports errors, links to
the build session) and the conversations list.
E2E: the page has no bottom bar, the popover sends a change request, and the
app's own conversations never appear in the sidebar.

## 9. What this defers

* **A UI inside Octopus for answering an agent's question asked during an app
  conversation.** The stream carries the question so the app can render it,
  and the existing auto-answer timeout still resolves it; a dedicated
  answer-from-the-app endpoint needs a real app that asks for it.
* **Per-app agent allow-lists.** Any app can address any non-archived agent by
  name. Scoping that needs a policy UI, and the single-user posture
  (application-backends.md §10) doesn't force one yet.
* **Attachments in an app turn.** Text `context` covers the SmartReader case;
  files would need the app to upload through the attachments API first.
