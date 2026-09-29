import { useSessionStore, type Identity } from "../stores/sessionStore";

/** Ask who this browser is signed in as, and put the answer in the store.
 *
 * Two places need it — the account row shows the username, and the sidebar's
 * Manage group hides the operator-only Monitor page from everybody else
 * (multi-tenancy.md §3, §8) — so it is fetched once, here, rather than in each
 * of them: two components asking the same route twice is two answers that can
 * disagree.
 *
 * Called on mount and whenever the bearer changes, which includes claiming the
 * install and signing in as somebody else. A failure leaves the identity null,
 * which every reader treats as "not known yet" rather than as an answer.
 */
export async function loadIdentity(): Promise<void> {
  const { token, setIdentity } = useSessionStore.getState();
  if (!token) return;
  try {
    const res = await fetch(`${window.location.origin}/api/auth/identity`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) setIdentity((await res.json()) as Identity);
  } catch {
    // An unreachable server is visible everywhere else; the sidebar stays
    // generic rather than throwing inside itself.
  }
}
