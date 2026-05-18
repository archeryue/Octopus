import { useCallback, useEffect, useState } from "react";
import {
  IconCheck,
  IconChevronRight,
  IconCopy,
  IconPlus,
  IconRestore,
  IconX,
} from "@tabler/icons-react";
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
  const [archivedExpanded, setArchivedExpanded] = useState(false);
  const token = useSessionStore((s) => s.token);
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const archived = useSessionStore((s) => s.archivedSessions);
  const setArchived = useSessionStore((s) => s.setArchivedSessions);
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
        if (typeof data.next_message_seq === "number") {
          useSessionStore
            .getState()
            .setLastAppliedSeq(id, data.next_message_seq - 1);
        }
      }
    } catch {
      // ignore
    }
  };

  const fetchArchived = useCallback(async () => {
    try {
      const res = await fetch(
        `${API_URL}/api/sessions?include_archived=true`,
        { headers }
      );
      if (res.ok) {
        const all: SessionInfo[] = await res.json();
        setArchived(all.filter((s) => s.archived));
      }
    } catch {
      // ignore
    }
  }, [token]);

  const unarchive = async (id: string) => {
    try {
      const res = await fetch(`${API_URL}/api/sessions/${id}/unarchive`, {
        method: "POST",
        headers,
      });
      if (res.ok) {
        const revived: SessionInfo = await res.json();
        setSessions([...sessions, revived]);
        setArchived(archived.filter((s) => s.id !== id));
        setActiveSessionId(revived.id);
      }
    } catch {
      // ignore
    }
  };

  // Fetch sessions on mount
  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  // Refresh archived list whenever the user opens it (so it reflects
  // recent /archive calls from any tab).
  useEffect(() => {
    if (archivedExpanded) fetchArchived();
  }, [archivedExpanded, fetchArchived]);

  return (
    <div className="session-list shrink-0 pb-3">
      <div className="session-list-header group flex h-8 items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors">
        <h2 className="text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Sessions
        </h2>
        <button
          className="btn-session-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={() => setShowForm((v) => !v)}
          title={showForm ? "Cancel" : "New session"}
          aria-label={showForm ? "Cancel" : "New session"}
        >
          {showForm ? <IconX size={14} /> : <IconPlus size={14} />}
        </button>
      </div>

      <div className="session-list-items flex flex-col gap-0.5 mt-1">
        {sessions.map((s) => (
          <div
            key={s.id}
            className={`session-item group flex items-center gap-2 rounded-lg px-2 py-1.5 cursor-pointer transition-colors ${
              s.id === activeSessionId
                ? "active bg-[hsl(var(--gray-200))] text-foreground"
                : "text-sidebar-foreground hover:bg-sidebar-accent"
            }`}
            onClick={() => selectSession(s.id)}
          >
            <span
              className={`status-dot status-${s.status} inline-block size-2 rounded-full shrink-0 ${
                s.status === "running"
                  ? "bg-primary animate-pulse"
                  : s.status === "waiting_approval"
                  ? "bg-yellow-500"
                  : "bg-muted-foreground/40"
              }`}
            />
            <span
              className={`session-name truncate text-sm flex-1 ${
                s.id === activeSessionId ? "font-medium" : ""
              }`}
            >
              {s.name}
            </span>
            <div className="session-item-actions flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
              <button
                className="btn-copy-id inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/60 hover:bg-card hover:text-sidebar-foreground"
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
                className="btn-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/60 hover:bg-destructive/10 hover:text-destructive"
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

      {/* Archived expander — shown below the live items, collapsed by
       *  default. Clicking opens a list with per-item Unarchive +
       *  read-only "view history" affordances. */}
      <div className="archived-section mt-2">
        <button
          type="button"
          className="btn-archived-toggle group flex w-full h-8 items-center gap-2 rounded-lg px-2 text-[12px] text-sidebar-foreground/55 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors"
          onClick={() => setArchivedExpanded((v) => !v)}
          aria-expanded={archivedExpanded}
        >
          <IconChevronRight
            size={12}
            className={`shrink-0 transition-transform ${archivedExpanded ? "rotate-90" : ""}`}
          />
          <span className="uppercase tracking-wide">
            Archived{archived.length > 0 ? ` (${archived.length})` : ""}
          </span>
        </button>
        {archivedExpanded && (
          <div className="archived-list flex flex-col gap-0.5 mt-1">
            {archived.length === 0 && (
              <div className="text-xs italic text-sidebar-foreground/50 px-2 py-1.5">
                No archived sessions.
              </div>
            )}
            {archived.map((s) => (
              <div
                key={s.id}
                className={`archived-item group flex items-center gap-2 rounded-lg px-2 py-1.5 cursor-pointer transition-colors ${
                  s.id === activeSessionId
                    ? "active bg-[hsl(var(--gray-200))] text-foreground"
                    : "text-sidebar-foreground/70 hover:bg-sidebar-accent"
                }`}
                onClick={() => selectSession(s.id)}
                title="View archived session (read-only)"
              >
                <span className="archived-name truncate text-sm italic flex-1">
                  {s.name}
                </span>
                <div className="archived-item-actions flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    className="btn-unarchive inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/60 hover:bg-card hover:text-sidebar-foreground"
                    onClick={(e) => {
                      e.stopPropagation();
                      unarchive(s.id);
                    }}
                    title="Unarchive — bring this session back as a live session"
                  >
                    <IconRestore size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {showForm && (
        <div className="session-create mt-2 rounded-lg border-[0.7px] border-border bg-card p-3 space-y-2">
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
