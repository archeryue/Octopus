import { useSessionStore, type Identity } from "../stores/sessionStore";

/** Ask who this browser is signed in as, and act on what the server says.
 *
 * Held in the store because two places need it — the account row's name and
 * the sidebar hiding the operator-only Monitor page (multi-tenancy.md §3, §8).
 *
 * It is also the app's token check. Three outcomes, and the difference matters:
 *
 *   - **200** → store the identity.
 *   - **401** → the token the browser held is no longer accepted (expired,
 *     revoked, the account disabled, or the machine came back with the session
 *     gone). Drop it, so the app shows the sign-in screen. Without this the
 *     token stays, every request 401s, and the page sits on an empty main view
 *     with no data — the "stuck, can't even sign out" state, which on a phone
 *     you cannot escape because the account menu is one tap away and this isn't.
 *   - **anything else / network error** → leave the token in place. The server
 *     may be briefly down (a restart), and a blip must not sign anyone out.
 *
 * Run on mount, on token change, and when the tab becomes visible again — the
 * last so a phone returning from the background re-checks rather than trusting
 * a session that may have lapsed while it slept.
 */
export async function loadIdentity(): Promise<void> {
  const { token, setIdentity, setToken } = useSessionStore.getState();
  if (!token) return;
  try {
    const res = await fetch(`${window.location.origin}/api/auth/identity`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 401) {
      setIdentity(null);
      setToken("");
      return;
    }
    if (res.ok) setIdentity((await res.json()) as Identity);
  } catch {
    // Unreachable server (offline / mid-restart): keep the token, stay put.
  }
}
