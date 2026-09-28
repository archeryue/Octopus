/** Typed fetch wrappers for the agent sidebar-pin routes (sidebar-pins.md).
 *
 * The rest of the agent CRUD still talks to `/api/agents` from the
 * components that own it; these are the calls the sidebar and the Agents
 * page share. */

import type { AgentRead } from ".";

const API = `${window.location.origin}/api/agents`;

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

/** Pin an agent to the sidebar (at the bottom), or take it out. Unpinned
 * agents are still live: listed on the Agents page, callable, scheduled. */
export async function setAgentPinned(
  token: string,
  id: string,
  pinned: boolean
): Promise<AgentRead> {
  return json(
    await fetch(`${API}/${id}/${pinned ? "pin" : "unpin"}`, {
      method: "POST",
      headers: authHeaders(token),
    })
  );
}

/** The sidebar order of the pinned agents, top first. Returns every live
 * agent in its new state. */
export async function reorderAgentPins(
  token: string,
  ids: string[]
): Promise<AgentRead[]> {
  return json(
    await fetch(`${API}/pin-order`, {
      method: "PUT",
      headers: authHeaders(token),
      body: JSON.stringify({ ids }),
    })
  );
}
