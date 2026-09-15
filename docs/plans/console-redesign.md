# Console redesign — the blue workspace UI

> **Implementation status: SHIPPED.** The whole interface now follows the
> "Octopus Console" design: `web/src/styles/tokens.css` carries the palette
> and type, the sidebar is `SidebarAgents` / `SidebarApplications` /
> `SidebarManage` / `SidebarAccount`, every manage surface is a page
> (`SchedulesPage`, `ConnectorsPage`, `HarnessPage`) and both create flows are
> pages too (`AgentFormPage`, `ApplicationFormPage`).

## 1. What changed, in one sentence

Dialogs became pages, the sidebar became a two-level workspace over a system
group, and the whole thing moved from a neutral grey scheme to the design's
blue one — same features, addressed the way the design addresses them.

## 2. The token layer is the design

Everything visual comes from `tokens.css`; no component hard-codes a hex.

| Token | Value | Where it shows |
|---|---|---|
| `--primary-600` | `#2563b8` | filled buttons, the user bubble, selected rows, links |
| `--primary-50/100` | `#eaf1fb` / `#d6e3f7` | selected fills and accent borders |
| `--gray-50` | `#f7f9fc` | the sidebar ground |
| `--gray-300/400` | `#e4eaf4` / `#dde4ee` | hairlines and card borders |
| `--gray-700/900` | `#7f8ba0` / `#2b3138` | meta text and body text |
| `--success` / `--warn` | `#3f9d6b` / `#c8703f` | healthy vs needs-attention |
| `--running` | `#8a6db5` | a delegation in flight — neither healthy nor broken |

Type is Hanken Grotesk for the interface and **IBM Plex Mono for every
technical string** — ids, counts, cron, costs, status words, command names.
That split is what makes the console read as a console; keep it.

Shared primitives live in `index.css`: `.tile` (identity squares), `.pill`
(status chips in four tints), `.badge-running`, `.card`, `.page-body`.

## 3. Sidebar: workspace over system

```
Octopus                     ← brand (the logo itself is unchanged)
AGENTS                   +
  ▾ 🐙 Octo              +     ← fold · tile · name · new-session
      ● root      idle         ← sessions on a hairline rail
  ▸ 🔬 Vera    ● 1 running     ← violet badge when a session is mid-turn
APPLICATIONS             +
  🪟 Habit Tracker
───────────────────────
MANAGE
  ◷ Schedules   2 · next 09:00
  ⚡ Connectors  5 ●
  ⚙ Harness     1 error ●
───────────────────────
A  Octopus / token             ← account menu
```

The selected state lives on **sessions**, not agents: an agent row is a fold,
and the thing you're actually looking at is a session. The MANAGE summaries
are the app's ambient health readout — a lapsed credential or a broken
connector is visible from anywhere without opening anything.

## 4. Pages, not dialogs

`mainView` in the store is the router (`chat` | `application` |
`application-create` | `agent-form` | `schedules` | `connectors` | `harness`).
`setActiveSessionId` resets it to `chat`, so every existing "open this
session" call site keeps working. ChatView stays mounted but hidden behind
other views — it owns the composer draft, scroll position and pending
attachments, and unmounting it on a trip to Schedules would throw all of that
away.

Every view opens with the same `PageHeader`: breadcrumb left, actions right.

## 5. Archived, not a market

The design's two "market" screens became **Archived** tabs on the create
pages: archive an agent or an application and it leaves the sidebar but keeps
everything (an application keeps its files, so restoring renders it exactly as
it was), then comes back from that tab. No catalog, no fake install counts —
your own shelf. This needed real backend work: `POST /api/agents/{id}/unarchive`,
`applications.archived` + `POST /api/applications/{id}/(un)archive`, and the
applications name index narrowed to live rows so an archived name frees up
(matching how agents already behaved).

## 6. What the design didn't say, and what we did

The design draws a clean sidebar with no hover affordances and a one-click
"+". Taken literally that would have deleted real capability, so:

- **Session create** keeps a name field (inline in the rail, not a dialog),
  with working dir / engine / credential folded behind a "+ working dir ·
  engine" toggle. Default look matches the design; the overrides survive.
- **Delete** for sessions and applications is a hover-only action on the row.
- **Archive** for an application sits in its own header, next to reload and
  open-in-tab, because that's where you are when you decide to shelve it.
- Assistant turns lost their name label; attribution moved onto the identity
  tile (`title`/`aria-label`), which is what the design shows.

## 7. Screens with no backend

`Agent Market`, `Application Market`, per-tool connector permissions, schedule
run history with cost, bridge toggles and the app "shell v3 / core" split are
in the design but have nothing behind them. They are not faked. `next_run_at`
was the one gap worth closing — it's real now, read from APScheduler's live
trigger state and surfaced in the sidebar summary and the Schedules header.

## 8. Testing

Unit: `SidebarApplications`, `SidebarManage`, `ApplicationFormPage`,
`AgentFormPage` (+ the existing chat/card suites). E2E: every spec was moved
onto the new vocabulary — `.chat-header .crumb-current` for the session name,
`.btn-manage-*` to reach the manage pages, the inline `.session-create` row
for creation, `.btn-tab-archived` for the archived tabs.
