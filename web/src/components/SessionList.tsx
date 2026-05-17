import { useCallback, useEffect, useState } from "react";
import { IconCheck, IconCopy, IconX } from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

const API_URL = window.location.origin;

export function SessionList() {
  const [newName, setNewName] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const [credentialId, setCredentialId] = useState<string>("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const token = useSessionStore((s) => s.token);
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const setMessages = useSessionStore((s) => s.setMessages);
  const setPendingQueue = useSessionStore((s) => s.setPendingQueue);
  const credentials = useSessionStore((s) => s.credentials);
  const claudeCreds = credentials.filter((c) => c.backend === "claude-code");

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/sessions`, { headers });
      if (res.ok) {
        setSessions(await res.json());
      }
    } catch {
      // ignore
    }
  }, [token]);

  const createSession = async () => {
    const name = newName.trim() || "New Session";
    try {
      const res = await fetch(`${API_URL}/api/sessions`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          name,
          working_dir: workingDir.trim() || null,
          credential_id: credentialId || null,
        }),
      });
      if (res.ok) {
        const session: SessionInfo = await res.json();
        setSessions([...sessions, session]);
        setActiveSessionId(session.id);
        setNewName("");
        setWorkingDir("");
        setCredentialId("");
        setShowForm(false);
      }
    } catch {
      // ignore
    }
  };

  const deleteSession = async (id: string) => {
    try {
      await fetch(`${API_URL}/api/sessions/${id}`, {
        method: "DELETE",
        headers,
      });
      setSessions(sessions.filter((s) => s.id !== id));
      if (activeSessionId === id) {
        setActiveSessionId(null);
      }
    } catch {
      // ignore
    }
  };

  const selectSession = async (id: string) => {
    setActiveSessionId(id);
    // Fetch message history
    try {
      const res = await fetch(`${API_URL}/api/sessions/${id}`, { headers });
      if (res.ok) {
        const data = await res.json();
        setMessages(id, data.messages || []);
        setPendingQueue(id, data.pending_queue || []);
      }
    } catch {
      // ignore
    }
  };

  // Fetch sessions on mount
  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  return (
    <div className="session-list flex flex-col flex-1 overflow-hidden">
      <div className="session-list-header flex items-center justify-between px-4 h-12 shrink-0 border-b border-border">
        <h2 className="text-sm font-semibold text-muted-foreground">Sessions</h2>
        <button
          className="btn-session-add inline-flex h-6 w-6 items-center justify-center rounded-md text-base text-primary hover:bg-accent"
          onClick={() => setShowForm((v) => !v)}
          title={showForm ? "Cancel" : "New session"}
          aria-label={showForm ? "Cancel" : "New session"}
        >
          {showForm ? "×" : "+"}
        </button>
      </div>

      <div className="session-list-items flex-1 overflow-y-auto">
        {sessions.map((s) => (
          <div
            key={s.id}
            className={`session-item group flex items-center justify-between px-4 py-2 border-b border-border cursor-pointer transition-colors ${
              s.id === activeSessionId ? "active bg-accent" : "hover:bg-accent/50"
            }`}
            onClick={() => selectSession(s.id)}
          >
            <div className="session-item-info flex items-center gap-2 min-w-0">
              <span className="session-name truncate text-sm text-foreground">{s.name}</span>
              <span
                className={`status-dot status-${s.status} inline-block size-2 rounded-full shrink-0 ${
                  s.status === "running"
                    ? "bg-primary animate-pulse"
                    : s.status === "waiting_approval"
                    ? "bg-yellow-400"
                    : "bg-muted-foreground/40"
                }`}
              />
            </div>
            <div className="session-item-actions flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
              <button
                className="btn-copy-id inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-background hover:text-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  navigator.clipboard.writeText(s.id);
                  setCopiedId(s.id);
                  setTimeout(() => setCopiedId(null), 1500);
                }}
                title="Copy session ID"
              >
                {copiedId === s.id ? <IconCheck size={14} /> : <IconCopy size={14} />}
              </button>
              <button
                className="btn-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                onClick={(e) => {
                  e.stopPropagation();
                  deleteSession(s.id);
                }}
                title="Delete session"
              >
                <IconX size={14} />
              </button>
            </div>
          </div>
        ))}
      </div>

      {showForm && (
        <div className="session-create px-3 py-3 border-b border-border space-y-2">
          <Input
            type="text"
            className="h-9 text-sm"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Session name"
          />
          <Input
            type="text"
            className="h-9 text-sm"
            value={workingDir}
            onChange={(e) => setWorkingDir(e.target.value)}
            placeholder="Working directory (optional)"
          />
          {claudeCreds.length > 0 && (
            <select
              className="session-credential-select flex h-9 w-full rounded-md border border-border bg-input px-3 py-1 text-sm text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/30"
              value={credentialId}
              onChange={(e) => setCredentialId(e.target.value)}
            >
              <option value="">Default auth (CLI login)</option>
              {claudeCreds.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
          )}
          <div className="flex justify-end">
            <Button className="btn btn-create" size="sm" onClick={createSession}>
              Create
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
