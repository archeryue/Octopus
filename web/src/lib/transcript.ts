import { useSessionStore, type Message } from "../stores/sessionStore";

const API = window.location.origin;

/** Apply the transcript half of a `GET /api/sessions/{id}` snapshot.
 *
 * Three places load a snapshot — selecting a session, the WebSocket reconnect
 * refetch, and the archived-session viewer — and each owes the store the same
 * three things: the messages, the WS dedup baseline, and where the window
 * starts so scroll-back knows what to ask for (polish-2026-09.md §4 B2). It
 * lives here so a fourth caller cannot get two of the three right.
 */
export function applyTranscript(
  sessionId: string,
  data: Record<string, unknown>
): void {
  const store = useSessionStore.getState();
  const messages = (data.messages as Message[]) || [];
  store.setMessages(sessionId, messages);
  store.setMessageWindow(
    sessionId,
    typeof data.oldest_loaded_seq === "number" ? data.oldest_loaded_seq : null,
    Boolean(data.has_more_messages)
  );
  // Any WS event at or below the snapshot's high-water mark is already in the
  // messages above, so the handler must drop it rather than render it twice.
  if (typeof data.next_message_seq === "number") {
    store.setLastAppliedSeq(sessionId, (data.next_message_seq as number) - 1);
  }
}

/** Fetch the page of transcript before what is held and prepend it.
 *
 * Returns how many messages were added — the chat list needs that number to
 * keep its scroll position across the prepend. Returns 0 when there is nothing
 * older, when a page is already in flight, or on any failure: scroll-back that
 * fails must leave the conversation exactly as it was.
 */
const inFlight = new Set<string>();

export async function loadOlderMessages(sessionId: string): Promise<number> {
  const store = useSessionStore.getState();
  const oldest = store.oldestSeq[sessionId];
  if (
    !sessionId ||
    !store.token ||
    !store.hasMoreMessages[sessionId] ||
    typeof oldest !== "number" ||
    oldest <= 0 ||
    inFlight.has(sessionId)
  ) {
    return 0;
  }
  inFlight.add(sessionId);
  try {
    const res = await fetch(
      `${API}/api/sessions/${sessionId}/messages?before_seq=${oldest}`,
      { headers: { Authorization: `Bearer ${store.token}` } }
    );
    if (!res.ok) return 0;
    const page = await res.json();
    return useSessionStore
      .getState()
      .prependMessages(
        sessionId,
        (page.messages as Message[]) || [],
        typeof page.oldest_loaded_seq === "number" ? page.oldest_loaded_seq : null,
        Boolean(page.has_more_messages)
      );
  } catch {
    return 0;
  } finally {
    inFlight.delete(sessionId);
  }
}
