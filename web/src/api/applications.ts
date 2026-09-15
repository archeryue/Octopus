/** Typed fetch wrappers for the /api/applications routes (applications.md §5).
 *
 * Applications are agent-built static web apps: the row lives here, the files
 * are served under `/apps/{id}/` and rendered in an iframe. */

import type { ApplicationCreate, ApplicationRead, ApplicationUpdate } from ".";

const API = window.location.origin;

/** The cookie the server accepts on `/apps/*` — an iframe can't send an
 * Authorization header, and neither can the app's own `<script src>` /
 * `fetch` sub-requests (applications.md §3). */
export const APP_TOKEN_COOKIE = "octopus_app_token";

function authHeaders(token: string): HeadersInit {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail =
      body && typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export async function fetchApplications(
  token: string,
  opts: { archived?: boolean } = {}
): Promise<ApplicationRead[]> {
  const query = opts.archived ? "?archived=true" : "";
  return json(
    await fetch(`${API}/api/applications${query}`, { headers: authHeaders(token) })
  );
}

/** Archive an application: it leaves the sidebar but keeps its row and its
 * files, so restoring from the create page's Archived tab puts it back
 * exactly as it was. */
export async function archiveApplication(
  token: string,
  id: string
): Promise<ApplicationRead> {
  return json(
    await fetch(`${API}/api/applications/${id}/archive`, {
      method: "POST",
      headers: authHeaders(token),
    })
  );
}

export async function unarchiveApplication(
  token: string,
  id: string
): Promise<ApplicationRead> {
  return json(
    await fetch(`${API}/api/applications/${id}/unarchive`, {
      method: "POST",
      headers: authHeaders(token),
    })
  );
}

/** The create body as callers actually write it. `instructions` and
 * `entrypoint` carry server-side defaults, but openapi-typescript keeps
 * defaulted fields required — so relax exactly those two here rather than
 * making every call site restate them. */
export type NewApplication = Omit<
  ApplicationCreate,
  "instructions" | "entrypoint"
> &
  Partial<Pick<ApplicationCreate, "instructions" | "entrypoint">>;

export async function createApplication(
  token: string,
  body: NewApplication
): Promise<ApplicationRead> {
  return json(
    await fetch(`${API}/api/applications`, {
      method: "POST",
      headers: authHeaders(token),
      body: JSON.stringify(body),
    })
  );
}

export async function updateApplication(
  token: string,
  id: string,
  body: ApplicationUpdate
): Promise<ApplicationRead> {
  return json(
    await fetch(`${API}/api/applications/${id}`, {
      method: "PATCH",
      headers: authHeaders(token),
      body: JSON.stringify(body),
    })
  );
}

export async function buildApplication(
  token: string,
  id: string,
  prompt: string
): Promise<ApplicationRead> {
  return json(
    await fetch(`${API}/api/applications/${id}/build`, {
      method: "POST",
      headers: authHeaders(token),
      body: JSON.stringify({ prompt }),
    })
  );
}

export async function deleteApplication(token: string, id: string): Promise<void> {
  const res = await fetch(`${API}/api/applications/${id}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
}

/** Publish the token to the `/apps` path so the iframe (and everything the
 * app itself loads) authenticates. Same-origin, so a plain document.cookie
 * write is all it takes; `Secure` only when the page is already https, or the
 * browser drops it on plain-http localhost. */
export function primeAppCookie(token: string): void {
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${APP_TOKEN_COOKIE}=${encodeURIComponent(
    token
  )}; path=/apps; SameSite=Lax${secure}`;
}

/** URL of an application's entry document. `v` busts the iframe cache on an
 * explicit reload (the server sends no-store, but a same-src assignment
 * wouldn't re-navigate at all). */
export function applicationUrl(id: string, version?: number | string): string {
  const q = version === undefined ? "" : `?v=${encodeURIComponent(String(version))}`;
  return `${API}/apps/${id}/${q}`;
}
