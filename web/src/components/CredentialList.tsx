import { useCallback, useEffect, useState } from "react";
import {
  useSessionStore,
  type CredentialInfo,
} from "../stores/sessionStore";

const API = `${window.location.origin}/api/credentials`;

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
  const [showAdvanced, setShowAdvanced] = useState(false);

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
        const detail = await res.text();
        setAddState({
          kind: "error",
          message: detail || `Failed to start login (${res.status})`,
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
        const detail = await res.text();
        setAddState({
          kind: "error",
          message: detail || `Login failed (${res.status})`,
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

  // --------------------------------------------------------------- Advanced: paste API key

  const [apiKeyLabel, setApiKeyLabel] = useState("");
  const [apiKeySecret, setApiKeySecret] = useState("");
  const [apiKeyBusy, setApiKeyBusy] = useState(false);
  const [apiKeyError, setApiKeyError] = useState<string | null>(null);

  const submitApiKey = async () => {
    if (!apiKeyLabel.trim() || !apiKeySecret.trim()) {
      setApiKeyError("Label and key are both required");
      return;
    }
    setApiKeyBusy(true);
    setApiKeyError(null);
    try {
      const res = await fetch(API, {
        method: "POST",
        headers,
        body: JSON.stringify({
          backend: "claude-code",
          label: apiKeyLabel.trim(),
          auth_type: "api_key",
          secret: apiKeySecret.trim(),
        }),
      });
      if (!res.ok) {
        const detail = await res.text();
        setApiKeyError(detail || `Failed (${res.status})`);
        return;
      }
      const created: CredentialInfo = await res.json();
      setCredentials([...credentials, created]);
      setApiKeyLabel("");
      setApiKeySecret("");
      setShowAdvanced(false);
    } catch (e) {
      setApiKeyError(String(e));
    } finally {
      setApiKeyBusy(false);
    }
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
        <span className="credential-title">Accounts</span>
        {!adding && (
          <button
            className="btn-credential-add"
            onClick={startLogin}
            title="Sign in with Claude Code"
          >
            + Sign in
          </button>
        )}
        {adding && (
          <button
            className="btn-credential-add"
            onClick={cancelLogin}
            title="Cancel"
          >
            ×
          </button>
        )}
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

      {addState.kind === "idle" && (
        <details
          className="credential-advanced"
          open={showAdvanced}
          onToggle={(e) => setShowAdvanced((e.target as HTMLDetailsElement).open)}
        >
          <summary>Advanced: paste an API key</summary>
          <div className="credential-form">
            <input
              type="text"
              placeholder="Label (e.g. Work key)"
              value={apiKeyLabel}
              onChange={(e) => setApiKeyLabel(e.target.value)}
            />
            <input
              type="password"
              placeholder="sk-ant-…"
              value={apiKeySecret}
              onChange={(e) => setApiKeySecret(e.target.value)}
            />
            {apiKeyError && (
              <div className="credential-error">{apiKeyError}</div>
            )}
            <button
              className="btn btn-cred-submit"
              onClick={submitApiKey}
              disabled={apiKeyBusy}
            >
              {apiKeyBusy ? "Saving…" : "Save"}
            </button>
          </div>
        </details>
      )}

      <div className="credential-list">
        {credentials.length === 0 && !adding && (
          <div className="credential-empty">
            No accounts yet. Click <strong>+ Sign in</strong> above to log in
            with a Claude Code subscription, or expand{" "}
            <em>Advanced</em> to paste an API key. Without an account,
            sessions will use <code>claude</code>'s default auth on the host.
          </div>
        )}
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
