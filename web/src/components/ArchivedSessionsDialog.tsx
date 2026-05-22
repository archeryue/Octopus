import { useCallback, useEffect } from "react";
import { IconEye, IconRestore } from "@tabler/icons-react";
import {
  useSessionStore,
  type Agent,
  type SessionInfo,
} from "../stores/sessionStore";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";

const API_URL = window.location.origin;

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}

/** Manage page for archived sessions, reached from the account menu. Lists
 * every archived session grouped by its owning agent (they no longer clutter
 * the sidebar). "View" opens it read-only; "Unarchive" brings it back live. */
export function ArchivedSessionsDialog({ open, onOpenChange }: Props) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const archived = useSessionStore((s) => s.archivedSessions);
  const setArchived = useSessionStore((s) => s.setArchivedSessions);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const setMessages = useSessionStore((s) => s.setMessages);
  const setPendingQueue = useSessionStore((s) => s.setPendingQueue);
  const setPendingQuestions = useSessionStore((s) => s.setPendingQuestions);

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  const fetchArchived = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/sessions?include_archived=true`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const all: SessionInfo[] = await res.json();
        setArchived(all.filter((s) => s.archived));
      }
    } catch {
      // ignore
    }
  }, [token, setArchived]);

  useEffect(() => {
    if (open) fetchArchived();
  }, [open, fetchArchived]);

  // Select an archived session for read-only viewing. ChatView detects the
  // active session is archived (it's in archivedSessions) and renders the
  // read-only banner + history. Mirrors SessionList's old selectSession.
  const view = async (s: SessionInfo) => {
    setActiveAgentId(s.agent_id ?? null);
    setActiveSessionId(s.id);
    onOpenChange(false);
    try {
      const [detailRes, bgRes] = await Promise.all([
        fetch(`${API_URL}/api/sessions/${s.id}`, { headers }),
        fetch(`${API_URL}/api/sessions/${s.id}/bg-tasks`, { headers }),
      ]);
      if (detailRes.ok) {
        const data = await detailRes.json();
        setMessages(s.id, data.messages || []);
        setPendingQueue(s.id, data.pending_queue || []);
        setPendingQuestions(s.id, data.pending_questions || []);
        if (typeof data.next_message_seq === "number") {
          useSessionStore
            .getState()
            .setLastAppliedSeq(s.id, data.next_message_seq - 1);
        }
      }
      if (bgRes.ok) {
        useSessionStore.getState().setBgTasks(s.id, await bgRes.json());
      }
    } catch {
      // ignore
    }
  };

  const unarchive = async (s: SessionInfo) => {
    try {
      const res = await fetch(`${API_URL}/api/sessions/${s.id}/unarchive`, {
        method: "POST",
        headers,
      });
      if (res.ok) {
        const revived: SessionInfo = await res.json();
        // Dedupe on insert in case a session_unarchived broadcast already
        // landed in the live list.
        setSessions([...sessions.filter((x) => x.id !== revived.id), revived]);
        setArchived(archived.filter((x) => x.id !== s.id));
        setActiveAgentId(revived.agent_id ?? null);
        setActiveSessionId(revived.id);
        onOpenChange(false);
      }
    } catch {
      // ignore
    }
  };

  const groups: { agent: Agent | null; items: SessionInfo[] }[] = agents
    .map((a) => ({
      agent: a as Agent | null,
      items: archived.filter((s) => s.agent_id === a.id),
    }))
    .filter((g) => g.items.length > 0);
  const knownAgentIds = new Set(agents.map((a) => a.id));
  const orphans = archived.filter(
    (s) => !s.agent_id || !knownAgentIds.has(s.agent_id)
  );
  if (orphans.length) groups.push({ agent: null, items: orphans });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="archived-sessions-dialog max-w-xl">
        <DialogHeader>
          <DialogTitle>Archived sessions</DialogTitle>
          <DialogDescription>
            Sessions you've archived, grouped by agent. View one read-only, or
            unarchive it to continue the conversation as a live session.
          </DialogDescription>
        </DialogHeader>

        {archived.length === 0 ? (
          <div className="archived-empty rounded-lg border border-dashed border-border bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground">
            No archived sessions. Type{" "}
            <code className="font-mono text-foreground">/archive</code> in a
            chat to archive its history.
          </div>
        ) : (
          <div className="archived-groups space-y-4 max-h-[60vh] overflow-y-auto">
            {groups.map(({ agent, items }, gi) => (
              <div
                key={agent?.id ?? `orphan-${gi}`}
                className="archived-agent-group"
              >
                <div className="archived-agent-header flex items-center gap-1.5 px-1 pb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  <span className="text-sm leading-none">
                    {agent?.avatar || "🐙"}
                  </span>
                  <span className="truncate">
                    {agent?.name ?? "Unknown agent"}
                  </span>
                </div>
                <div className="flex flex-col gap-1">
                  {items.map((s) => (
                    <div
                      key={s.id}
                      className="archived-session-row group flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2"
                    >
                      <span className="archived-session-name min-w-0 flex-1 truncate text-sm italic text-foreground">
                        {s.name}
                      </span>
                      <button
                        className="btn-archived-view inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
                        onClick={() => view(s)}
                        title="View read-only history"
                      >
                        <IconEye size={13} />
                        View
                      </button>
                      <button
                        className="btn-archived-unarchive inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-primary hover:bg-primary/10"
                        onClick={() => unarchive(s)}
                        title="Unarchive — bring this session back as a live session"
                      >
                        <IconRestore size={13} />
                        Unarchive
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
