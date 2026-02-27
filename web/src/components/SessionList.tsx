import { useCallback, useEffect, useState } from "react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

const API_URL = `http://${window.location.hostname}:8000`;

export function SessionList() {
  const [newName, setNewName] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const token = useSessionStore((s) => s.token);
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const setMessages = useSessionStore((s) => s.setMessages);

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
        }),
      });
      if (res.ok) {
        const session: SessionInfo = await res.json();
        setSessions([...sessions, session]);
        setActiveSessionId(session.id);
        setNewName("");
        setWorkingDir("");
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
    <div className="session-list">
      <div className="session-list-header">
        <h2>Sessions</h2>
      </div>

      <div className="session-list-items">
        {sessions.map((s) => (
          <div
            key={s.id}
            className={`session-item ${s.id === activeSessionId ? "active" : ""}`}
            onClick={() => selectSession(s.id)}
          >
            <div className="session-item-info">
              <span className="session-name">{s.name}</span>
              <span className={`status-dot status-${s.status}`} />
            </div>
            <button
              className="btn-delete"
              onClick={(e) => {
                e.stopPropagation();
                deleteSession(s.id);
              }}
              title="Delete session"
            >
              ×
            </button>
          </div>
        ))}
      </div>

      <div className="session-create">
        <input
          type="text"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="Session name"
        />
        <input
          type="text"
          value={workingDir}
          onChange={(e) => setWorkingDir(e.target.value)}
          placeholder="Working directory (optional)"
        />
        <button className="btn btn-create" onClick={createSession}>
          + New Session
        </button>
      </div>
    </div>
  );
}
