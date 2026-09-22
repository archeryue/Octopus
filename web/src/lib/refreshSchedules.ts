import { useSessionStore, type Schedule } from "../stores/sessionStore";

/** Reload the schedule list into the store.
 *
 * The pages that own the list call it after their own edits; the WebSocket
 * `schedules_changed` handler calls it for the edits they didn't make —
 * another tab, and the reason it exists: an agent setting a schedule for
 * itself mid-conversation (schedule-tool.md §6). The sidebar summary and the
 * Schedules page both read the store, so one refetch updates both.
 */
export async function refreshSchedules(): Promise<void> {
  const { token, setSchedules } = useSessionStore.getState();
  if (!token) return;
  try {
    const resp = await fetch(`${window.location.origin}/api/schedules`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (resp.ok) setSchedules((await resp.json()) as Schedule[]);
  } catch {
    // A failed refresh leaves the list as it was; the next mount refetches.
  }
}
