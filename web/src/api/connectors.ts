/** Typed fetch wrappers for the /api/connectors routes (connectors.md §5.5).
 *
 * Installations are global; enablement is agent-scoped. The OAuth install is
 * a popup + status-poll flow (the callback lands on the server, not here). */

import type {
  ConnectorCatalogEntry,
  ConnectorInstallationInfo,
  ConnectorOAuthClientInfo,
  ConnectorOAuthStartResponse,
  ConnectorOAuthStatusResponse,
  CustomConnectorCreateRequest,
  UpdateConnectorRequest,
} from ".";

const API = window.location.origin;

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

export async function fetchCatalog(token: string): Promise<ConnectorCatalogEntry[]> {
  return json(
    await fetch(`${API}/api/connectors/catalog`, { headers: authHeaders(token) })
  );
}

export async function fetchInstallations(
  token: string
): Promise<ConnectorInstallationInfo[]> {
  return json(await fetch(`${API}/api/connectors`, { headers: authHeaders(token) }));
}

export async function startConnectorOAuth(
  token: string,
  kind: string,
  label?: string
): Promise<ConnectorOAuthStartResponse> {
  return json(
    await fetch(`${API}/api/connectors/oauth/start`, {
      method: "POST",
      headers: authHeaders(token),
      body: JSON.stringify({ kind, label: label ?? null }),
    })
  );
}

export async function pollConnectorOAuth(
  token: string,
  loginId: string
): Promise<ConnectorOAuthStatusResponse> {
  return json(
    await fetch(`${API}/api/connectors/oauth/status/${loginId}`, {
      headers: authHeaders(token),
    })
  );
}

export async function cancelConnectorOAuth(
  token: string,
  loginId: string
): Promise<void> {
  await fetch(`${API}/api/connectors/oauth/cancel`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ login_id: loginId }),
  }).catch(() => {});
}

export async function updateInstallation(
  token: string,
  id: string,
  body: UpdateConnectorRequest
): Promise<ConnectorInstallationInfo> {
  return json(
    await fetch(`${API}/api/connectors/${id}`, {
      method: "PATCH",
      headers: authHeaders(token),
      body: JSON.stringify(body),
    })
  );
}

export async function deleteInstallation(token: string, id: string): Promise<void> {
  const res = await fetch(`${API}/api/connectors/${id}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
}

export async function getOAuthClient(
  token: string,
  kind: string
): Promise<ConnectorOAuthClientInfo> {
  return json(
    await fetch(`${API}/api/connectors/${kind}/oauth-client`, {
      headers: authHeaders(token),
    })
  );
}

export async function setOAuthClient(
  token: string,
  kind: string,
  clientId: string,
  clientSecret: string
): Promise<ConnectorOAuthClientInfo> {
  return json(
    await fetch(`${API}/api/connectors/${kind}/oauth-client`, {
      method: "PUT",
      headers: authHeaders(token),
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    })
  );
}

export async function createCustomConnector(
  token: string,
  body: CustomConnectorCreateRequest
): Promise<ConnectorCatalogEntry> {
  return json(
    await fetch(`${API}/api/connectors/custom`, {
      method: "POST",
      headers: authHeaders(token),
      body: JSON.stringify(body),
    })
  );
}

export async function deleteCustomConnector(
  token: string,
  kind: string
): Promise<void> {
  const res = await fetch(`${API}/api/connectors/custom/${kind}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
}

export async function fetchAgentConnectors(
  token: string,
  agentId: string
): Promise<string[]> {
  const r = await json<{ installation_ids: string[] }>(
    await fetch(`${API}/api/agents/${agentId}/connectors`, {
      headers: authHeaders(token),
    })
  );
  return r.installation_ids;
}

export async function toggleAgentConnector(
  token: string,
  agentId: string,
  installationId: string,
  enabled: boolean
): Promise<void> {
  const res = await fetch(
    `${API}/api/agents/${agentId}/connectors/${installationId}`,
    {
      method: "PATCH",
      headers: authHeaders(token),
      body: JSON.stringify({ enabled }),
    }
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}
