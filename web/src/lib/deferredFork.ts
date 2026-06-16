// Pure helpers for the deferred-/fork flow (session-fork-copy.md). A working-
// dir copy + resume transcript needs a settled session, so `/fork` can't run
// mid-turn. Instead of refusing, the UI defers the intent and fires it once the
// session is idle AND fully drained. Kept pure + separate so the eligibility
// rule is unit-testable without mounting ChatView.

/**
 * True when a session can't be forked right now: a turn is running / waiting on
 * approval (status !== "idle"), a message is queued, or an AskUserQuestion is
 * open. Mirrors the backend idle-guard in SessionManager.duplicate_session.
 */
export function isSessionBusy(
  status: string | undefined,
  queueLen: number,
  questionsLen: number
): boolean {
  return status !== "idle" || queueLen > 0 || questionsLen > 0;
}
