# Steal Plan — vm0 → Octopus (status snapshot)

vm0 (`/home/start-up/vm0`) is a polished, multi-tenant Claude sandbox
platform we own. This document tracks what we've lifted from it. All
the phases below are landed; the residual work is a single deferred
item and a list of things we deliberately did not copy.

## Done

### Foundation (Phases 1 – 4)
- Tailwind v4 with `@tailwindcss/vite`, `class-variance-authority`,
  `clsx`, `tailwind-merge`, Radix Dialog/Slot/Label/DropdownMenu/
  Tooltip, `@tabler/icons-react`. HSL semantic-token system in
  `web/src/styles/tokens.css`; `cn()` helper in `web/src/lib/utils.ts`.
- shadcn primitives: Button, Input, Label, Dialog, DropdownMenu
  (copied verbatim from vm0).
- Login screen + OAuth sign-in flow run on the new primitives
  (Radix Dialog with `step 1: open URL` / `step 2: paste code`).

### B-1 Sidebar migration
SessionList, ScheduleList, CredentialList all use vm0's section
pattern (small uppercase header + chevron + hover, rounded-pill
item rows with hover-revealed actions). All the per-section CSS in
`index.css` is gone.

### B-2 ChatView + MessageBubble
Header, input bar, empty state, queue, message bubbles, tool blocks,
ToolApproval, QuestionPrompt all on Tailwind + shadcn. User bubble
uses a white card with a primary-tinted border (not a dark fill).
Tool name renders in primary blue; "Result" label renders in green,
mirroring the destructive "Error" red.

### B-3 OAuth provider abstraction
`server/oauth_providers.py` — `OAuthProvider` Protocol + concrete
`ClaudeCodeProvider` + `PROVIDERS` registry + `get_provider(name)`.
`OAuthLoginManager` is now provider-agnostic. Adding GitHub / Lark /
Codex is one new class + one registry entry.

### B-4 Credential storage split
`credential_secrets(credential_id PK, secret_encrypted)` table holds
the encrypted blob; `backend_credentials` holds metadata + new
refresh-state columns (`status`, `token_expires_at`, `needs_reconnect`,
`last_refresh_error_code`). `ON DELETE CASCADE` keeps secrets in sync.
Legacy `secret_encrypted` column kept populated for back-compat reads.

### B-5 Typed refresh-error codes
`server/oauth_errors.py` defines `RefreshErrorCode`
(`refresh_token_expired | refresh_token_reused |
refresh_token_invalidated | refresh_token_other | network_error |
unknown`). `CredentialInfo` surfaces the relevant fields so the UI
can render "needs reconnect" once a refresh-token provider lands.

### B-6 Engineering hygiene
- `lefthook.yml` at repo root runs `tsc --noEmit` on staged
  `web/src/**/*.{ts,tsx}` and fast pytest on staged
  `{server,tests}/**/*.py`. Bootstrap via `scripts/setup-hooks.sh`.
- TS contracts generated from FastAPI's `/openapi.json` into
  `web/src/api/contracts.ts` via `bun run generate:contracts`.
  Re-exported under stable names from `web/src/api/index.ts`.

### Visual identity (vm0 wholesale steal)
- Light theme with vm0's cool-gray scale + dark-blue primary
  (`--primary-700 = #133E8B`).
- Noto Sans (body) + JetBrains Mono (code), loaded from Google Fonts.
- Sidebar 260px, hairline 0.7px borders, header bg matches sidebar
  for a single chrome layer.
- Octopus mark rendered inline as an SVG component (same path as
  the favicon), brand blue on the sidebar.

### Cascade-layer fix (the bug that hid the spacing for hours)
The unlayered `* { padding: 0 }` in `index.css` won the cascade
against every Tailwind `@layer`-scoped utility — every `px-*` /
`py-*` / `m-*` / `gap-*` was silently dropped to 0. Wrapping the
reset in `@layer base` lets utilities apply. Documented in
`verify-first` memory.

---

## Deferred

### B-7 Settings dialog with tab nav
Originally: move the per-section sidebar UI (Sessions / Schedules /
Harness) into a single Settings dialog with internal tab nav.

**Why deferred:** the current three-section sidebar was an explicit
design choice; B-7 as originally written reverses it. If we want a
Settings dialog later it should be *additive* — hold things that
don't fit the sidebar (theme toggle, server URL, etc.) — not
relocate what's already there.

---

## What we explicitly will NOT steal

- Rust microVMs / Firecracker / vsock — multi-tenant VM isolation,
  irrelevant to single-user local app.
- Clerk auth + multi-org capability strings — over-engineered for us.
- ccstate (Jotai-flavored signal store) — works at 100+ routes, not
  worth migrating our 4-route FastAPI + small zustand store.
- `(orgId, userId, type)` keying — single-tenant, key on
  `(provider_type)` alone.
- Multi-package turbo monorepo / release-please / Cloudflare worker —
  scale overhead.
