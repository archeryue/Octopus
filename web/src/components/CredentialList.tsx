import { useCallback, useEffect, useState } from "react";
import {
  useSessionStore,
  type BackendKind,
  type CredentialInfo,
} from "../stores/sessionStore";

const API = `${window.location.origin}/api/credentials`;

export function CredentialList() {
  const token = useSessionStore((s) => s.token);
  const credentials = useSessionStore((s) => s.credentials);
  const setCredentials = useSessionStore((s) => s.setCredentials);

  const [adding, setAdding] = useState(false);
  const [newBackend, setNewBackend] = useState<BackendKind>("claude-code");
  const [newLabel, setNewLabel] = useState("");
  const [newSecret, setNewSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const submitNew = async () => {
    if (!newLabel.trim() || !newSecret.trim()) {
      setError("Label and secret are required");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(API, {
        method: "POST",
        headers,
        body: JSON.stringify({
          backend: newBackend,
          label: newLabel.trim(),
          auth_type: "api_key",
          secret: newSecret.trim(),
        }),
      });
      if (!res.ok) {
        const body = await res.text();
        setError(body || `Failed (${res.status})`);
        return;
      }
      const created: CredentialInfo = await res.json();
      setCredentials([...credentials, created]);
      setNewLabel("");
      setNewSecret("");
      setAdding(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

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

  return (
    <div className="credential-section">
      <div className="credential-header">
        <span className="credential-title">Credentials</span>
        <button
          className="btn-credential-add"
          onClick={() => setAdding((v) => !v)}
          aria-label="Add credential"
          title={adding ? "Cancel" : "Add credential"}
        >
          {adding ? "×" : "+"}
        </button>
      </div>

      {adding && (
        <div className="credential-form">
          <select
            value={newBackend}
            onChange={(e) => setNewBackend(e.target.value as BackendKind)}
          >
            <option value="claude-code">Claude Code</option>
            <option value="codex">Codex</option>
          </select>
          <input
            type="text"
            placeholder="Label (e.g. Personal)"
            value={newLabel}
            onChange={(e) => setNewLabel(e.target.value)}
          />
          <input
            type="password"
            placeholder="API key"
            value={newSecret}
            onChange={(e) => setNewSecret(e.target.value)}
          />
          {error && <div className="credential-error">{error}</div>}
          <button
            className="btn btn-create"
            onClick={submitNew}
            disabled={busy}
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      )}

      <div className="credential-list">
        {credentials.length === 0 && !adding && (
          <div className="credential-empty">
            No credentials configured. The CLI's default auth (e.g.{" "}
            <code>claude login</code>) will be used.
          </div>
        )}
        {credentials.map((c) => (
          <div className="credential-item" key={c.id}>
            <div className="credential-info">
              <span className={`credential-badge backend-${c.backend}`}>
                {c.backend === "claude-code" ? "Claude" : "Codex"}
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
