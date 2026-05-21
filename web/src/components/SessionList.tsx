import { useCallback, useEffect, useState } from "react";
import {
  IconCheck,
  IconChevronRight,
  IconCopy,
  IconRestore,
  IconX,
} from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

const API_URL = window.location.origin;

/** The session list for a single agent — rendered nested under its agent row
 * in AgentList (sessions belong to an agent). No "Sessions" header; the
 * new-session "+" lives on the agent row and drives `formOpen`. */
export function SessionList({
  agentId,
  formOpen,
  onCloseForm,
}: {
  agentId: string;
  formOpen: boolean;
  onCloseForm: () => void;
}) {
  const [newName, setNewName] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const [credentialId, setCredentialId] = useState<string>("");
  const [backend, setBackend] = useState<string>("claude-code");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [archivedExpanded, setArchivedExpanded] = useState(false);

  const token = useSessionStore((s) => s.token);
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const archived = useSessionStore((s) => s.archivedSessions);
  const setArchived = useSessionStore((s) => s.setArchivedSessions);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const setMessages = useSessionStore((s) => s.setMessages);
  const setPendingQueue = useSessionStore((s) => s.setPendingQueue);
  const setPendingQuestions = useSessionStore((s) => s.setPendingQuestions);
  const credentials = useSessionStore((s) => s.credentials);
  const availableBackends = useSessionStore((s) => s.availableBackends);
  // Credentials shown in the create form are scoped to the selected backend.
  const backendCreds = credentials.filter((c) => c.backend === backend);
  const codexAvailable = availableBackends.includes("codex");

  // This list shows exactly its agent's sessions (bucketed by agent_id).
  const agentSessions = sessions.filter((s) => s.agent_id === agentId);
  const agentArchived = archived.filter((s) => s.agent_id === agentId);

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  const createSession = async () => {
    const name = newName.trim() || "New Session";
    try {
      const res = await fetch(`${API_URL}/api/agents/${agentId}/sessions`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          name,
          working_dir: workingDir.trim() || null,
          credential_id: credentialId || null,
          backend,
        }),
      });
      if (res.ok) {
        const session: SessionInfo = await res.json();
        setSessions([...sessions, session]);
        setActiveAgentId(agentId);
        setActiveSessionId(session.id);
        setNewName("");
        setWorkingDir("");
        setCredentialId("");
        setBackend("claude-code");
        onCloseForm();
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
    setActiveAgentId(agentId);
    setActiveSessionId(id);
    try {
      const [detailRes, bgRes] = await Promise.all([
        fetch(`${API_URL}/api/sessions/${id}`, { headers }),
        fetch(`${API_URL}/api/sessions/${id}/bg-tasks`, { headers }),
      ]);
      if (detailRes.ok) {
        const data = await detailRes.json();
        setMessages(id, data.messages || []);
        setPendingQueue(id, data.pending_queue || []);
        setPendingQuestions(id, data.pending_questions || []);
        if (typeof data.next_message_seq === "number") {
          useSessionStore
            .getState()
            .setLastAppliedSeq(id, data.next_message_seq - 1);
        }
      }
      if (bgRes.ok) {
        useSessionStore.getState().setBgTasks(id, await bgRes.json());
      }
    } catch {
      // ignore
    }
  };

  const fetchArchived = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/sessions?include_archived=true`, {
        headers,
      });
      if (res.ok) {
        const all: SessionInfo[] = await res.json();
        setArchived(all.filter((s) => s.archived));
      }
    } catch {
      // ignore
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        setActiveAgentId(agentId);
        setActiveSessionId(revived.id);
      }
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    if (archivedExpanded) fetchArchived();
  }, [archivedExpanded, fetchArchived]);

  return (
    <div className="session-list session-list-nested ml-3 mt-0.5 mb-1 pl-2 border-l border-sidebar-border/40">
      <div className="session-list-items flex flex-col gap-0">
        {agentSessions.length === 0 && !formOpen && (
          <div className="text-[11px] italic text-sidebar-foreground/40 px-2 py-1">
            No sessions yet.
          </div>
        )}
        {agentSessions.map((s) => (
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

      {/* Archived expander — per agent, collapsed by default. */}
      <div className="archived-section mt-1">
        <button
          type="button"
          className="btn-archived-toggle group flex w-full h-7 items-center gap-2 rounded-lg px-2 text-[11px] text-sidebar-foreground/50 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors"
          onClick={() => setArchivedExpanded((v) => !v)}
          aria-expanded={archivedExpanded}
        >
          <IconChevronRight
            size={11}
            className={`shrink-0 transition-transform ${archivedExpanded ? "rotate-90" : ""}`}
          />
          <span className="uppercase tracking-wide">
            Archived{agentArchived.length > 0 ? ` (${agentArchived.length})` : ""}
          </span>
        </button>
        {archivedExpanded && (
          <div className="archived-list flex flex-col gap-0.5 mt-1">
            {agentArchived.length === 0 && (
              <div className="text-xs italic text-sidebar-foreground/50 px-2 py-1.5">
                No archived sessions.
              </div>
            )}
            {agentArchived.map((s) => (
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

      {formOpen && (
        <div className="session-create mt-2 rounded-lg border-[0.7px] border-border bg-card p-3 space-y-2">
          <Input
            type="text"
            className="h-9 text-sm"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Session name"
            autoFocus
          />
          <Input
            type="text"
            className="h-9 text-sm"
            value={workingDir}
            onChange={(e) => setWorkingDir(e.target.value)}
            placeholder="Working directory (optional)"
          />
          {codexAvailable && (
            <div
              className="session-backend-select flex gap-2"
              role="radiogroup"
              aria-label="Backend"
            >
              {[
                { id: "claude-code", label: "Claude" },
                { id: "codex", label: "Codex" },
              ].map((b) => (
                <button
                  key={b.id}
                  type="button"
                  role="radio"
                  aria-checked={backend === b.id}
                  className={`btn-backend btn-backend-${b.id} flex-1 h-8 rounded-md border text-xs transition-colors ${
                    backend === b.id
                      ? "border-primary bg-primary/10 text-foreground font-medium"
                      : "border-border text-muted-foreground hover:bg-sidebar-accent"
                  }`}
                  onClick={() => {
                    setBackend(b.id);
                    setCredentialId("");
                  }}
                >
                  {b.label}
                </button>
              ))}
            </div>
          )}
          {backendCreds.length > 0 && (
            <select
              className="session-credential-select flex h-9 w-full rounded-md border border-border bg-input px-3 py-1 text-sm text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/30"
              value={credentialId}
              onChange={(e) => setCredentialId(e.target.value)}
            >
              <option value="">Default auth (CLI login)</option>
              {backendCreds.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
          )}
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setNewName("");
                setWorkingDir("");
                onCloseForm();
              }}
            >
              Cancel
            </Button>
            <Button className="btn btn-create" size="sm" onClick={createSession}>
              Create
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
