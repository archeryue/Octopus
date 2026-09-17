import type { SessionInfo } from "../stores/sessionStore";
import { useSessionStore } from "../stores/sessionStore";

const API = window.location.origin;

/** Fields `GET /api/sessions/{id}` adds on top of SessionInfo. */
const DETAIL_ONLY_FIELDS = [
  "messages",
  "pending_queue",
  "pending_questions",
  "subagents",
  "next_message_seq",
] as const;

/** `GET /api/sessions/{id}` returns a SessionInfo superset — drop the
 * detail-only fields so the sidebar list holds a clean SessionInfo. */
export function sessionInfoFromDetail(detail: Record<string, unknown>): SessionInfo {
  const info = { ...detail };
  for (const field of DETAIL_ONLY_FIELDS) delete info[field];
  return info as unknown as SessionInfo;
}

/** Make sure `sessionId` is in the sidebar list.
 *
 * Sessions created server-side — an application's build session, a delegation
 * child, a fork — exist before any client hears about them, and the list is
 * only fetched on mount and on reconnect. Without this, opening one shows a
 * transcript the sidebar and chat header can't name. No-op when it's already
 * there.
 */
export async function hydrateSession(sessionId: string): Promise<void> {
  const store = useSessionStore.getState();
  if (!sessionId || !store.token) return;
  if (store.sessions.some((s) => s.id === sessionId)) return;
  try {
    const res = await fetch(`${API}/api/sessions/${sessionId}`, {
      headers: { Authorization: `Bearer ${store.token}` },
    });
    if (!res.ok) return;
    addSessionInfo(sessionInfoFromDetail(await res.json()));
  } catch {
    // The mount fetch / reconnect refetch will pick it up.
  }
}

/** Append a session to the list unless it's already there (a concurrent
 * hydrate or the reconnect refetch may have won the race). */
export function addSessionInfo(info: SessionInfo): void {
  const store = useSessionStore.getState();
  if (store.sessions.some((s) => s.id === info.id)) return;
  store.setSessions([...store.sessions, info]);
}
