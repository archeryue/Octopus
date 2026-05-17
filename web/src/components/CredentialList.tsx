import { useCallback, useEffect, useState } from "react";
import {
  useSessionStore,
  type CredentialInfo,
} from "../stores/sessionStore";

const API = `${window.location.origin}/api/credentials`;

/** Parse a non-OK fetch Response into a short message safe to show the user.
 *
 * If the body is JSON (FastAPI's standard `{detail: ...}` shape), pull
 * `detail`. If it's HTML (Cloudflare 502, nginx error page, etc.), don't
 * dump it into the UI — show the status code with a short hint instead. */
async function friendlyErrorMessage(
  res: Response,
  action: string
): Promise<string> {
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      const body = await res.json();
      const detail = body?.detail;
      if (typeof detail === "string" && detail.trim()) return detail;
      if (Array.isArray(detail) && detail.length) {
        return detail.map((d: unknown) => String((d as { msg?: string })?.msg ?? d)).join("; ");
      }
    } catch {
      // fall through to status-only message
    }
  }
  // 502 / 503 from a tunnel or reverse proxy usually means the Octopus
  // backend timed out or wasn't reachable — most likely fix is to check
  // the server logs.
  if (res.status === 502 || res.status === 504) {
    return (
      `Could not ${action} — the Octopus backend didn't respond in time ` +
      `(${res.status} from gateway). Check the server logs for details.`
    );
  }
  if (res.status === 503) {
    return `Could not ${action} — server unavailable (503). Check the server logs.`;
  }
  return `Could not ${action} — HTTP ${res.status}`;
}

type AddState =
  | { kind: "idle" }
  | { kind: "starting" }                                   // POST /oauth/start in flight
  | { kind: "awaiting_code"; loginId: string; deviceUrl: string }
  | { kind: "submitting" }                                 // POST /oauth/complete in flight
  | { kind: "error"; message: string };

export function CredentialList() {
  const token = useSessionStore((s) => s.token);
  const credentials = useSessionStore((s) => s.credentials);
  const setCredentials = useSessionStore((s) => s.setCredentials);

  const [addState, setAddState] = useState<AddState>({ kind: "idle" });
  const [label, setLabel] = useState("");
  const [code, setCode] = useState("");

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  const fetchCredentials = useCallback(async () => {
    try {
      const res = await fetch(API, { headers });
      if (res.ok) {
        const items: CredentialInfo[] = await res.json();
        setCredentials(items);
      }
    } catch {
      // ignore
    }
  }, [token, setCredentials]);

  useEffect(() => {
    fetchCredentials();
  }, [fetchCredentials]);

  // --------------------------------------------------------------- OAuth flow

  const startLogin = async () => {
    setAddState({ kind: "starting" });
    try {
      const res = await fetch(`${API}/oauth/start`, {
        method: "POST",
        headers,
        body: JSON.stringify({ backend: "claude-code" }),
      });
      if (!res.ok) {
        setAddState({
          kind: "error",
          message: await friendlyErrorMessage(res, "start login"),
        });
        return;
      }
      const body: { login_id: string; device_url: string } = await res.json();
      setAddState({
        kind: "awaiting_code",
        loginId: body.login_id,
        deviceUrl: body.device_url,
      });
    } catch (e) {
      setAddState({ kind: "error", message: String(e) });
    }
  };

  const submitCode = async () => {
    if (addState.kind !== "awaiting_code") return;
    if (!label.trim() || !code.trim()) {
      setAddState({
        kind: "error",
        message: "Label and code are both required",
      });
      return;
    }
    const loginId = addState.loginId;
    setAddState({ kind: "submitting" });
    try {
      const res = await fetch(`${API}/oauth/complete`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          login_id: loginId,
          code: code.trim(),
          label: label.trim(),
        }),
      });
      if (!res.ok) {
        setAddState({
          kind: "error",
          message: await friendlyErrorMessage(res, "complete login"),
        });
        return;
      }
      const created: CredentialInfo = await res.json();
      setCredentials([...credentials, created]);
      setLabel("");
      setCode("");
      setAddState({ kind: "idle" });
    } catch (e) {
      setAddState({ kind: "error", message: String(e) });
    }
  };

  const cancelLogin = async () => {
    if (addState.kind === "awaiting_code") {
      const loginId = addState.loginId;
      try {
        await fetch(`${API}/oauth/cancel`, {
          method: "POST",
          headers,
          body: JSON.stringify({ login_id: loginId }),
        });
      } catch {
        // best-effort
      }
    }
    setLabel("");
    setCode("");
    setAddState({ kind: "idle" });
  };

  // --------------------------------------------------------------- Delete

  const remove = async (id: string) => {
    try {
      const res = await fetch(`${API}/${id}`, { method: "DELETE", headers });
      if (res.ok) {
        setCredentials(credentials.filter((c) => c.id !== id));
      }
    } catch {
      // ignore
    }
  };

  // --------------------------------------------------------------- Render

  const adding = addState.kind !== "idle";

  return (
    <div className="credential-section">
      <div className="credential-header">
        <span className="credential-title">Harness</span>
        <button
          className="btn-credential-add"
          onClick={adding ? cancelLogin : startLogin}
          title={adding ? "Cancel" : "Sign in"}
        >
          {adding ? "×" : "+"}
        </button>
      </div>

      {addState.kind === "starting" && (
        <div className="credential-form">
          <div className="credential-status">
            Starting login… (spawning <code>claude setup-token</code>)
          </div>
        </div>
      )}

      {addState.kind === "awaiting_code" && (
        <div className="credential-form">
          <div className="credential-status">
            <strong>1.</strong> Open this URL in your browser, sign in, then
            copy the code shown on the page:
          </div>
          <a
            className="credential-device-url"
            href={addState.deviceUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            {addState.deviceUrl}
          </a>
          <div className="credential-status">
            <strong>2.</strong> Paste the code below and name this account:
          </div>
          <input
            type="text"
            placeholder="Label (e.g. Personal)"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          <input
            type="text"
            placeholder="Code from browser"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <div className="credential-form-buttons">
            <button
              className="btn btn-cred-submit"
              onClick={submitCode}
              disabled={!label.trim() || !code.trim()}
            >
              Finish sign-in
            </button>
          </div>
        </div>
      )}

      {addState.kind === "submitting" && (
        <div className="credential-form">
          <div className="credential-status">
            Exchanging code for token…
          </div>
        </div>
      )}

      {addState.kind === "error" && (
        <div className="credential-form">
          <div className="credential-error">{addState.message}</div>
          <button
            className="btn btn-cred-submit"
            onClick={() => setAddState({ kind: "idle" })}
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="credential-list">
        {credentials.map((c) => (
          <div className="credential-item" key={c.id}>
            <div className="credential-info">
              <span className={`credential-badge backend-${c.backend}`}>
                {c.backend === "claude-code" ? "Claude" : "Codex"}
              </span>
              <span
                className={`credential-badge auth-${c.auth_type}`}
                title={c.auth_type === "oauth" ? "Signed in" : "API key"}
              >
                {c.auth_type === "oauth" ? "OAuth" : "Key"}
              </span>
              <span className="credential-label">{c.label}</span>
            </div>
            <button
              className="btn-delete"
              onClick={() => remove(c.id)}
              title="Delete credential"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
