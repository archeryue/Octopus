import { useCallback, useEffect, useState } from "react";
import { IconChevronRight, IconPlus, IconSettings } from "@tabler/icons-react";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";
import { AgentSettings } from "./AgentSettings";
import { SessionList } from "./SessionList";

const API = window.location.origin;

export function AgentList() {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const setAgents = useSessionStore((s) => s.setAgents);
  const activeAgentId = useSessionStore((s) => s.activeAgentId);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const setSessions = useSessionStore((s) => s.setSessions);
  const setAvailableBackends = useSessionStore((s) => s.setAvailableBackends);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<Agent | null>(null);
  // Which agents are unfolded (showing their sessions). Multiple may be open;
  // folding keeps the sidebar from filling with sessions when there are many
  // agents.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Which agent's new-session form is open (driven by the row's + button).
  const [formAgentId, setFormAgentId] = useState<string | null>(null);

  const headers = { Authorization: `Bearer ${token}` };

  const fetchAgents = useCallback(async () => {
    const res = await fetch(`${API}/api/agents`, { headers });
    if (!res.ok) return;
    const data: Agent[] = await res.json();
    setAgents(data);
    // Auto-select + auto-unfold an agent when none is active (default to the
    // system agent) so the user sees its sessions on first load.
    const cur = useSessionStore.getState().activeAgentId;
    if ((!cur || !data.some((a) => a.id === cur)) && data.length) {
      const def = data.find((a) => a.is_system) ?? data[0];
      setActiveAgentId(def.id);
      setExpanded((prev) => new Set(prev).add(def.id));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, setAgents, setActiveAgentId]);

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/sessions`, { headers });
      if (res.ok) setSessions((await res.json()) as SessionInfo[]);
    } catch {
      // ignore
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, setSessions]);

  // Agents + sessions + available backends are fetched once here (AgentList is
  // the single sidebar orchestrator); the nested SessionLists read from the
  // store and don't re-fetch.
  useEffect(() => {
    if (!token) return;
    fetchAgents();
    fetchSessions();
    fetch(`${API}/api/backends`, { headers })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.available) setAvailableBackends(d.available);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const toggleExpand = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const openCreate = () => {
    setEditing(null);
    setDialogOpen(true);
  };
  const openEdit = (a: Agent) => {
    setEditing(a);
    setDialogOpen(true);
  };
  const openNewSession = (id: string) => {
    setActiveAgentId(id);
    setExpanded((prev) => new Set(prev).add(id));
    setFormAgentId(id);
  };

  return (
    <div className="agent-list shrink-0 pb-3">
      <div className="agent-list-header group flex h-8 items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors">
        <h2 className="text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Agents
        </h2>
        <button
          className="btn-agent-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={openCreate}
          title="New agent"
          aria-label="New agent"
        >
          <IconPlus size={14} />
        </button>
      </div>

      <div className="agent-list-items flex flex-col gap-0.5 mt-1">
        {agents.map((a) => {
          const isActive = a.id === activeAgentId;
          const isExpanded = expanded.has(a.id);
          return (
            <div key={a.id} className="agent-group">
              <div
                className={`agent-item group flex items-center gap-1.5 rounded-lg pl-1 pr-2 py-1.5 cursor-pointer transition-colors ${
                  isActive
                    ? "active bg-[hsl(var(--gray-200))] text-foreground"
                    : "text-sidebar-foreground hover:bg-sidebar-accent"
                }`}
                onClick={() => {
                  setActiveAgentId(a.id);
                  toggleExpand(a.id);
                }}
              >
                <IconChevronRight
                  size={13}
                  className={`agent-fold shrink-0 text-sidebar-foreground/40 transition-transform ${
                    isExpanded ? "rotate-90" : ""
                  }`}
                />
                <span className="agent-avatar shrink-0 text-base leading-none w-5 text-center">
                  {a.avatar || "🐙"}
                </span>
                <span
                  className={`agent-name truncate text-sm flex-1 ${
                    isActive ? "font-medium" : ""
                  }`}
                >
                  {a.name}
                </span>
                <div
                  className={`agent-item-actions flex items-center gap-0.5 transition-opacity ${
                    isActive ? "opacity-100" : "opacity-0 group-hover:opacity-100"
                  }`}
                >
                  <button
                    className="btn-session-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-card hover:text-sidebar-foreground"
                    onClick={(e) => {
                      e.stopPropagation();
                      openNewSession(a.id);
                    }}
                    title="New session"
                    aria-label={`New session for ${a.name}`}
                  >
                    <IconPlus size={14} />
                  </button>
                  <button
                    className="btn-agent-settings inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/60 hover:bg-card hover:text-sidebar-foreground"
                    onClick={(e) => {
                      e.stopPropagation();
                      openEdit(a);
                    }}
                    title="Agent settings"
                    aria-label={`Settings for ${a.name}`}
                  >
                    <IconSettings size={14} />
                  </button>
                </div>
              </div>
              {/* Sessions live inside their agent — foldable per agent. */}
              {isExpanded && (
                <SessionList
                  agentId={a.id}
                  formOpen={formAgentId === a.id}
                  onCloseForm={() =>
                    setFormAgentId((cur) => (cur === a.id ? null : cur))
                  }
                />
              )}
            </div>
          );
        })}
      </div>

      <AgentSettings
        open={dialogOpen}
        onOpenChange={(v) => {
          setDialogOpen(v);
          if (!v) {
            fetchAgents();
            fetchSessions();
          }
        }}
        agent={editing}
      />
    </div>
  );
}
