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

## 8. Folding the sidebar to an icon rail

The sidebar collapses to 56px of icons and back. The handle is a small round
button on the sidebar's edge, revealed on hover, with the chevron pointing the
way it will move the edge.

Three decisions worth writing down:

- **The trigger is the edge, not the header.** When you want the sidebar
  narrower, the edge is where your pointer already is; a header button is a
  fixed target you have to go and aim for. The hover strip is 14px and lives
  *inside* the sidebar's own width so it can never sit over the main pane and
  swallow a click meant for the chat; only the button overhangs the border,
  and while it's hidden it is `pointer-events: none` for exactly that reason.
- **The fold is CSS, driven by one class.** `--sidebar-w` on `.app-layout` is
  the single source of truth for the width, and `.sidebar.collapsed` hides the
  words — names, section labels, summaries, the session rail. Nothing threads
  a `collapsed` prop through four components, and no row has two renderings to
  keep in sync.
- **It persists; per-agent folds don't.** Collapsing the sidebar is a
  statement about how you want to work, so it survives a reload
  (`octopus_sidebar_collapsed`). Which agents are unfolded is navigation, and
  still resets to all-folded on every load (§3).

Status survives the fold, because a running agent is most of the reason to
glance at the rail at all: the `N running` badge becomes a dot pinned to the
agent's tile, and an application's build dot does the same. Rows keep working
as rows — a manage icon opens its page, an app icon opens the app. The one
row that can't is an agent, whose click normally toggles a session rail that
the rail has no room for; from the icon rail it means "show me this agent", so
it unfolds the sidebar and the agent together.

Mobile is exempt. Below 769px the sidebar is already a slide-over drawer, so a
collapse persisted from a laptop must not turn the phone's drawer into a
sliver — the whole collapsed block, and the handle, are desktop-only.

## 9. Testing

Unit: `SidebarApplications`, `SidebarManage`, `ApplicationFormPage`,
`AgentFormPage`, `SidebarEdgeToggle` (+ the existing chat/card suites). E2E:
every spec was moved onto the new vocabulary — `.chat-header .crumb-current`
for the session name, `.btn-manage-*` to reach the manage pages, the inline
`.session-create` row for creation, `.btn-tab-archived` for the archived tabs.
The fold has its own pair in `app.spec.ts`, driving the real mouse: the handle
is invisible and unclickable until the edge is hovered, which is the guarantee
worth pinning down.
