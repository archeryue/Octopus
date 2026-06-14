# Harness transient-error retry

## 1. Problem

Claude Code and Codex sometimes fail a turn for reasons that have nothing
to do with the user's request or their credit — the *provider's* backend is
briefly unreliable: HTTP 5xx, Anthropic "Overloaded" (529), a dropped/reset
stream, a gateway timeout. Today such a turn just surfaces as a failed
result and the user has to manually resend. These errors are transient and
usually clear within seconds, so the careful behavior is a bounded
automatic retry.

This is the sibling of harness-credential-reauth.md: same per-backend
pattern machinery, different disposition.

## 2. Three dispositions for a failed turn

A failed turn's error text (terminal event content/raw + the CLI's stderr)
is classified, in order, behind the harness contract:

1. **Auth-credential rejection** (401 / revoked / expired) →
   `Harness.is_auth_error` → flag the credential needs_reconnect and STOP
   (harness-credential-reauth.md). Never retried — re-auth won't fix itself.
2. **Transient backend error** (5xx / overloaded / dropped connection /
   timeout) → `Harness.is_transient_error` → bounded retry (this doc).
3. **Everything else** — including quota/credit ("rate limit", 429,
   "insufficient quota", billing) — surfaces as-is. Deliberately NOT
   retried: the user said "not my credit limit", and hammering a quota
   error wastes time and can worsen rate-limiting.

The classifiers are mutually exclusive by construction — `429` / "rate
limit" / "quota" appear in neither pattern set; auth phrases appear only in
the auth set.

## 3. Detection behind the harness contract

Mirrors the auth work (no `if backend ==` outside `server/harness/`):

- `RuntimeProfile.transient_error_patterns: tuple[str, ...]` — lowercased,
  auth-/quota-free, server-reliability phrases per backend.
- `Harness.is_transient_error(text) -> bool` — case-insensitive substring
  match; pure.

Patterns are specific server-reliability phrases ("overloaded", "internal
server error", "service unavailable", "bad gateway", "529", "connection
reset", "timed out", "stream error", …), chosen narrow after Vera's note
that broad tokens (a bare "unauthorized") cause false positives.

**Server-side throttle vs. the user's usage limit.** Anthropic emits a
server-side throttle as "Server is temporarily limiting requests (**not your
usage limit**) · Rate limited" — a transient blip that SHOULD retry, despite
containing "Rate limited". We must still NOT retry the user's *own* quota /
usage limit. So we match the throttle on its specific phrasing
("temporarily limiting requests", "not your usage limit") rather than a bare
"rate limit" / "429" (which also appears in the user's-limit message, and
stays non-retryable). This was a real miss: the bare-"rate limit" exclusion
swallowed the server throttle and stopped the turn instead of retrying.

## 4. Retry in the run loop (`session_manager._run_backend`)

The retry slots into the existing post-turn dispatch, BEFORE the
`saw_result` early-return (a transient failure arrives as an `is_error`
`result`, so `saw_result` is true) and the Claude premature-exit recovery:

- A turn is *failed* when it produced an `is_error` result/error event, or
  no result at all.
- On `is_transient_error(blob)` **AND no output yet** (`not saw_tool_use and
  not saw_text`): retry the SAME prompt after exponential backoff
  (`_TRANSIENT_RETRY_BASE_DELAY * 2**(n-1)`), bounded by
  `_MAX_TRANSIENT_RETRIES` (2). A discreet system marker is persisted +
  broadcast each retry; on exhaustion a clear error is surfaced.

**Why gate on no-output-yet.** Re-running the prompt is the simplest correct
recovery only when the failed attempt had no side effects: the dominant
transient case (overloaded/5xx at the call boundary) emits nothing before
failing, so re-running is clean. If assistant text already streamed or a
tool already ran, re-running could duplicate text or re-execute a tool — so
we don't auto-retry then; the error surfaces and the user decides. (This is
the inverse of the premature-exit recovery, which RESUMES with "continue"
specifically because it fires only after a `tool_use`.)

The backoff `sleep` is a normal `await` inside the turn, so a user interrupt
cancels it like any in-flight turn.

**Resume-id preservation (Vera review).** A failed no-output attempt can still
emit `session_started` and mutate `session.claude_session_id`. Left alone, the
retry would become `--resume <failed-id> -- <original prompt>` — not the
original invocation, risking a duplicated prompt or a resumed dead partial
conversation. So the logical turn snapshots `resume_at_turn_start =
session.claude_session_id` before the loop and, on a transient retry, restores
it (memory + DB) before re-running. The premature-exit recovery is unaffected —
it deliberately resumes the captured id with "continue".

## 5. What this defers

- No retry after partial output (see §4 rationale) — surfaced, not retried.
- No retry of quota/rate-limit/credit failures (§2).
- Retry counts/backoff are module constants, not yet user-configurable.
