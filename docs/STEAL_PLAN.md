# Steal Plan — vm0 → Octopus

Concrete, ordered phases. Each phase is independently committable and
verifiable. Stop after Phase 4 in this session; everything below it is
queued and prioritized.

## Why this exists

vm0 (`/home/start-up/vm0`) is a polished, multi-tenant Claude sandbox
platform we own. We've already pulled the OAuth-via-pure-Python pattern
from it once (commit `37e4351`). The deeper UI / componentization /
provider-abstraction patterns are still on the table, and the visual
gap between the two products is doing the most damage to Octopus's
"feels like shit" perception today.

This plan picks what's worth stealing, in priority order, with file
references back to vm0 for each one.

---

## Phase 1 — UI foundation (Tailwind v4 + tokens + helpers)

**Goal:** the styling system every later phase depends on, with zero UI
change yet — old surfaces keep their `index.css` styles untouched.

**Steal targets:**
- `vm0/turbo/packages/ui/src/styles/globals.css` — the HSL semantic
  token system (background, foreground, card, muted, border, primary,
  accent, destructive, ring, radius) under `@theme`
- `vm0/turbo/packages/ui/src/lib/utils.ts` — `cn()` helper

**Deps to install:**
- `tailwindcss@^4` + `@tailwindcss/vite`
- `class-variance-authority`, `clsx`, `tailwind-merge`
- `@radix-ui/react-dialog`, `@radix-ui/react-slot`, `@radix-ui/react-label`
- `@tabler/icons-react`

**Files touched:**
- `web/package.json` — new deps
- `web/vite.config.ts` — `@tailwindcss/vite` plugin
- `web/src/styles/tokens.css` (new) — `@import "tailwindcss";` + `@theme` token block
- `web/src/main.tsx` — import tokens.css alongside index.css
- `web/src/lib/utils.ts` (new) — `cn()`

**Verification:** tsc clean, vitest 8/8, Playwright 31/31 (no visible
change — only foundation added).

**Risk:** low. Additive only. If Tailwind's reset bites existing
styles, we scope `@import` to `:not(.legacy)` selector or similar.

---

## Phase 2 — shadcn primitives

**Goal:** Button, Input, Label, Dialog, Card available as drop-in
components.

**Steal targets:**
- `vm0/turbo/packages/ui/src/components/ui/button.tsx`
- `vm0/turbo/packages/ui/src/components/ui/input.tsx`
- `vm0/turbo/packages/ui/src/components/ui/label.tsx`
- `vm0/turbo/packages/ui/src/components/ui/dialog.tsx`
- `vm0/turbo/packages/ui/src/components/ui/card.tsx` (if present)

Trim to what we use. Skip variants we don't need (e.g. `size="xl"`).

**Files touched:** new files in `web/src/components/ui/`.

**Verification:** components compile, tests still 31/31.

**Risk:** low. Pure additions.

---

## Phase 3 (pilot) — Login screen rewrite

**Goal:** prove the new stack on the most isolated surface. If the
login looks materially better, foundation is validated; we keep going.

**File:** `web/src/App.tsx` (the `if (!token)` branch)

Replace:
- `.login-screen` (full-screen flex center, custom dark bg)
- `.login-card` (custom padding/border)
- `<input type="password" />` + `.btn-login`

With:
- Centered `<Card>` on `bg-background`
- `<Label>Token</Label>` + `<Input type="password" />`
- `<Button>Connect</Button>`

Drop old CSS in same commit.

**Verification:** Playwright login tests (3 in `app.spec.ts::Login`)
still pass. tsc clean.

**Risk:** medium — touches 1 user-facing surface. If selectors break
in tests, fix in same commit.

---

## Phase 4 — OAuth sign-in modal

**Goal:** the OAuth flow is the worst-looking surface right now (cramped
inline form in the sidebar). Move to a Radix Dialog with proper steps.

**File:** `web/src/components/CredentialList.tsx`

- "+" in Harness header opens `<Dialog>`
- Step 1: device URL + copy button + "I've signed in →" CTA
- Step 2: label + code paste field + "Finish" button
- Status surfaces ("Exchanging code…", error states) live in dialog
  footer

Sidebar Harness section just shows the list of accounts + "+" trigger.

**Verification:** new Playwright test for the dialog flow (mock
`/oauth/start` + `/oauth/complete`). Existing OAuth start mock test
adjusted to the new selectors.

**Risk:** medium — touches the most-edited UI file.

---

## Phase 5 — Verify + commit + push

Run the full suite, commit each phase as its own commit, push.

---

## Backlog (not in this session)

Ordered by impact / effort ratio. Pull one at a time.

### B-1 Sidebar polish migration
Migrate `SessionList` / `ScheduleList` / `CredentialList` headers and
items to shadcn primitives. Drop the per-section CSS in `index.css`.

### B-2 ChatView + MessageBubble
Tool blocks (collapsible JSON), result badges, error messages — all
look technical-bare. Apply the new tokens + Card primitive.

### B-3 Provider abstraction (backend) — DONE
Done in `server/oauth_providers.py`. `OAuthProvider` Protocol +
`ClaudeCodeProvider` concrete + `PROVIDERS` registry +
`get_provider(name)` lookup. `OAuthLoginManager` is now
provider-agnostic — it just calls `provider.build_authorize_url(...)`,
`provider.exchange_code(...)`, `provider.mint_api_key(...)`. Adding a
new provider (GitHub / Lark / Codex) is one new class + one registry
entry.

### B-6 Engineering hygiene — DONE
- `lefthook.yml` at repo root runs `tsc --noEmit` on staged
  `web/src/**/*.{ts,tsx}` and fast pytest on staged
  `{server,tests}/**/*.py`. Bootstrap via `scripts/setup-hooks.sh`.
- TS contracts generated from FastAPI's `/openapi.json` into
  `web/src/api/contracts.ts` via `bun run generate:contracts`.
  Re-exported under stable names from `web/src/api/index.ts`.

### B-7 Settings dialog with tab nav
Move per-section sidebar UI (Sessions / Schedules / Harness) into a
single Settings dialog with internal tab nav. Pattern:
`vm0/turbo/apps/platform/.../settings-dialog` (Radix Dialog +
internal Sidebar). Reduces sidebar clutter, mobile-friendly.

---

## Deferred until needed

### B-4 Credential storage split
Two tables: `credentials` (metadata: type, status, `token_expires_at`,
`needs_reconnect`, `last_refresh_error_code`) + `credential_secrets`
(AES-encrypted ciphertext). Add `serverOnly` flag on secret-field
configs so refresh tokens never reach the spawned subprocess env.

**Why deferred:** the split's payoff is hiding refresh tokens from the
subprocess and tracking per-credential refresh state. Claude Code's
flow mints a long-lived `sk-ant-` key that *is* the credential — no
refresh token to hide, no expiry to track. Adds schema migration risk
+ encryption-at-rest complexity for zero current user-visible benefit.
Pull this when adding a second provider (GitHub / Lark / Codex) that
issues short-lived access tokens + refresh tokens.

### B-5 Typed refresh-error codes
`"refresh_token_expired" | "refresh_token_reused" |
"refresh_token_invalidated" | "refresh_token_other"`. UI shows
"re-sign-in" vs "we'll retry" based on this.

**Why deferred:** prerequisite for refresh-token-based providers.
Claude Code's `sk-ant-` key doesn't expire short-term, so there's no
refresh path to surface errors from. Pull alongside B-4 when adding
the second provider.

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
