import { useCallback, useEffect, useState } from "react";
import { IconCheck, IconPlus, IconSubtask, IconX } from "@tabler/icons-react";
import { fetchInstallations } from "../api/connectors";
import { selectSession } from "../lib/selectSession";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";
import { SidebarSectionHeader } from "./SidebarSectionHeader";

const API = window.location.origin;

/** The AGENTS section of the sidebar — the workspace's top half.
 *
 * Two levels, per the console design: an agent row (fold caret, avatar tile,
 * name, a new-session "+", and a running badge when any of its sessions is
 * mid-turn) with its sessions nested underneath on a hairline rail. Sessions
 * are what you actually click all day, so they get the selected treatment: a
 * tinted pill with an accent border and the session's status set in mono on
 * the right.
 *
 * This is also the sidebar's single data orchestrator (as it was before the
 * redesign): agents, sessions, available backends and connector installations
 * are fetched once here and everything else reads the store.
 */
export function SidebarAgents() {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const setAgents = useSessionStore((s) => s.setAgents);
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const activeAgentId = useSessionStore((s) => s.activeAgentId);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const mainView = useSessionStore((s) => s.mainView);
  const openAgentForm = useSessionStore((s) => s.openAgentForm);
  const setAvailableBackends = useSessionStore((s) => s.setAvailableBackends);
  const setConnectorInstallations = useSessionStore(
    (s) => s.setConnectorInstallations
  );
  const showDelegations = useSessionStore((s) => s.showDelegations);

  // Which agents are unfolded. Multiple may be open; folding keeps the
  // sidebar from filling with sessions when there are many agents.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Which agent has the inline "new session" row open, and what's typed into
  // it. The row lives in the session rail rather than in a dialog, so the
  // sidebar stays as quiet as the design draws it — but the overrides the old
  // create form carried (working dir, engine, credential) are still here,
  // folded behind "more" so they cost nothing until you want them.
  const [formAgentId, setFormAgentId] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const [formBackend, setFormBackend] = useState("");
  const [formCredentialId, setFormCredentialId] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const credentials = useSessionStore((s) => s.credentials);
  const availableBackends = useSessionStore((s) => s.availableBackends);

  const headers = { Authorization: `Bearer ${token}` };

  const fetchAgents = useCallback(async () => {
    const res = await fetch(`${API}/api/agents`, { headers });
    if (res.ok) setAgents((await res.json()) as Agent[]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, setAgents]);

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/sessions`, { headers });
      if (res.ok) setSessions((await res.json()) as SessionInfo[]);
    } catch {
      // ignore
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, setSessions]);

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
    // Connector installations are global; the agent form reads them to render
    // each agent's per-connector toggles.
    fetchInstallations(token).then(setConnectorInstallations).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // Keep a valid agent selected, and unfold it so its sessions are visible.
  useEffect(() => {
    if (!agents.length) return;
    if (activeAgentId && agents.some((a) => a.id === activeAgentId)) return;
    const def = agents.find((a) => a.is_system) ?? agents[0];
    setActiveAgentId(def.id);
    setExpanded((prev) => new Set(prev).add(def.id));
  }, [agents, activeAgentId, setActiveAgentId]);

  const toggleExpand = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const openCreateRow = (agentId: string) => {
    setActiveAgentId(agentId);
    setExpanded((prev) => new Set(prev).add(agentId));
    setFormAgentId(agentId);
    setNewName("");
    setWorkingDir("");
    setFormBackend(agents.find((a) => a.id === agentId)?.backend ?? "claude-code");
    setFormCredentialId("");
    setShowAdvanced(false);
  };

  const closeCreateRow = () => {
    setFormAgentId(null);
    setShowAdvanced(false);
  };

  const createSession = async (agentId: string) => {
    try {
      const res = await fetch(`${API}/api/agents/${agentId}/sessions`, {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify({
          name: newName.trim() || null,
          working_dir: workingDir.trim() || null,
          credential_id: formCredentialId || null,
          backend: formBackend || null,
        }),
      });
      if (!res.ok) return;
      const session: SessionInfo = await res.json();
      setSessions([...useSessionStore.getState().sessions, session]);
      closeCreateRow();
      selectSession(session.id, agentId);
    } catch {
      // ignore
    }
  };

  const deleteSession = async (id: string) => {
    try {
      await fetch(`${API}/api/sessions/${id}`, { method: "DELETE", headers });
    } catch {
      // ignore — the list refresh below is what the user sees
    }
    const remaining = useSessionStore
      .getState()
      .sessions.filter((s) => s.id !== id);
    setSessions(remaining);
    if (useSessionStore.getState().activeSessionId === id) {
      useSessionStore.getState().setActiveSessionId(null);
    }
  };

  return (
    <div className="agent-list shrink-0">
      <SidebarSectionHeader
        label="Agents"
        className="agent-list-header"
        action={{
          icon: <IconPlus size={14} />,
          onClick: () => openAgentForm(null),
          title: "New agent",
          label: "New agent",
          className: "btn-agent-add",
        }}
      />

      <div className="agent-list-items flex flex-col">
        {agents.map((a) => {
          const isExpanded = expanded.has(a.id);
          const agentSessions = sessions.filter((s) => s.agent_id === a.id);
          const visible = showDelegations
            ? agentSessions
            : agentSessions.filter((s) => s.origin !== "delegation");
          const hiddenDelegations = agentSessions.length - visible.length;
          const runningCount = agentSessions.filter(
            (s) => s.status === "running"
          ).length;

          return (
            <div key={a.id} className="agent-group">
              <div
                className="agent-item group flex items-center gap-2 rounded-lg px-2 py-1.5 cursor-pointer hover:bg-gray-100 transition-colors"
                onClick={() => {
                  setActiveAgentId(a.id);
                  toggleExpand(a.id);
                }}
                onDoubleClick={() => openAgentForm(a.id)}
                title={a.description || a.name}
              >
                <span
                  className={`agent-fold shrink-0 text-[10px] leading-none text-gray-600 transition-transform ${
                    isExpanded ? "rotate-90" : ""
                  }`}
                  aria-hidden
                >
                  ▸
                </span>
                <span className="agent-avatar tile tile-plain shrink-0">
                  {a.avatar || "🐙"}
                </span>
                <span className="agent-name truncate text-sm font-semibold text-gray-900">
                  {a.name}
                </span>
                {runningCount > 0 ? (
                  <span className="agent-running badge-running ml-auto shrink-0">
                    <span className="dot" />
                    {runningCount} running
                  </span>
                ) : (
                  <button
                    className="btn-session-add ml-auto inline-flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-md text-gray-600 opacity-0 group-hover:opacity-100 hover:bg-gray-200 hover:text-gray-900 transition"
                    onClick={(e) => {
                      e.stopPropagation();
                      openCreateRow(a.id);
                    }}
                    title="New session"
                    aria-label={`New session for ${a.name}`}
                  >
                    <IconPlus size={14} />
                  </button>
                )}
              </div>

              {isExpanded && (
                <div className="session-rail ml-[15px] mt-0.5 mb-1 flex flex-col gap-0.5 border-l-[1.5px] border-gray-400/70 pl-3">
                  {formAgentId === a.id && (
                    <div className="session-create rounded-lg border border-primary-100 bg-primary-50/60 p-2">
                      <div className="flex items-center gap-1.5">
                        <input
                          className="min-w-0 flex-1 rounded-md border border-gray-400 bg-card px-2 py-1 text-[13px] text-gray-900 outline-none placeholder:text-gray-600 focus:border-primary"
                          placeholder="Session name"
                          value={newName}
                          autoFocus
                          onChange={(e) => setNewName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") createSession(a.id);
                            if (e.key === "Escape") closeCreateRow();
                          }}
                        />
                        <button
                          type="button"
                          className="btn-create inline-flex size-6 shrink-0 items-center justify-center rounded-md bg-primary text-white transition-colors hover:bg-primary-700"
                          onClick={() => createSession(a.id)}
                          title="Create session"
                          aria-label="Create session"
                        >
                          <IconCheck size={13} />
                        </button>
                        <button
                          type="button"
                          className="btn-create-cancel inline-flex size-6 shrink-0 items-center justify-center rounded-md text-gray-700 transition-colors hover:bg-gray-200"
                          onClick={closeCreateRow}
                          title="Cancel"
                          aria-label="Cancel new session"
                        >
                          <IconX size={13} />
                        </button>
                      </div>

                      {/* Overrides stay one click away: a session normally
                        * inherits the agent's engine, credential and the
                        * server's default working dir, and the design's
                        * sidebar shows none of that — but the capability
                        * can't just vanish with the old form. */}
                      <button
                        type="button"
                        className="btn-session-advanced mt-1.5 font-mono text-[10.5px] text-gray-700 hover:text-primary"
                        onClick={() => setShowAdvanced((v) => !v)}
                      >
                        {showAdvanced ? "− less" : "+ working dir · engine"}
                      </button>

                      {showAdvanced && (
                        <div className="mt-1.5 flex flex-col gap-1.5">
                          <input
                            className="session-working-dir rounded-md border border-gray-400 bg-card px-2 py-1 font-mono text-[11.5px] text-gray-900 outline-none placeholder:text-gray-600 focus:border-primary"
                            placeholder="working dir (default: server's)"
                            value={workingDir}
                            onChange={(e) => setWorkingDir(e.target.value)}
                          />
                          <select
                              className="session-backend-select rounded-md border border-gray-400 bg-card px-2 py-1 text-[11.5px] text-gray-900 outline-none focus:border-primary"
                              value={formBackend}
                              onChange={(e) => {
                                setFormBackend(e.target.value);
                                setFormCredentialId("");
                              }}
                              aria-label="Engine"
                            >
                              {availableBackends.map((b) => (
                                <option key={b} value={b}>
                                  {b === "claude-code" ? "Claude Code" : "Codex"}
                                </option>
                              ))}
                            </select>
                          {credentials.some((c) => c.backend === formBackend) && (
                            <select
                              className="session-credential-select rounded-md border border-gray-400 bg-card px-2 py-1 text-[11.5px] text-gray-900 outline-none focus:border-primary"
                              value={formCredentialId}
                              onChange={(e) => setFormCredentialId(e.target.value)}
                              aria-label="Credential"
                            >
                              <option value="">Agent's credential</option>
                              {credentials
                                .filter((c) => c.backend === formBackend)
                                .map((c) => (
                                  <option key={c.id} value={c.id}>
                                    {c.label}
                                  </option>
                                ))}
                            </select>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                  {visible.map((s) => {
                    const isActive =
                      mainView === "chat" && s.id === activeSessionId;
                    return (
                      <div
                        key={s.id}
                        className={`session-item group/session flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 cursor-pointer transition-colors ${
                          isActive
                            ? "active bg-primary-50 border border-primary-100 shadow-[0_1px_2px_rgba(37,99,184,0.06)]"
                            : "border border-transparent hover:bg-gray-100"
                        }`}
                        onClick={() => selectSession(s.id, a.id)}
                      >
                        <span
                          className={`status-dot status-${s.status} inline-block size-[7px] shrink-0 rounded-full ${
                            s.status === "running"
                              ? "bg-success animate-pulse"
                              : s.status === "waiting_approval"
                              ? "bg-warn"
                              : "bg-gray-500"
                          }`}
                        />
                        <span
                          className={`session-name truncate text-[13.5px] ${
                            isActive
                              ? "font-semibold text-gray-950"
                              : "text-gray-800"
                          }`}
                        >
                          {s.name}
                        </span>
                        {s.origin === "delegation" && (
                          <span
                            className="delegation-marker inline-flex shrink-0 text-gray-600"
                            title="Delegation session"
                            aria-label="Delegation session"
                          >
                            <IconSubtask size={12} />
                          </span>
                        )}
                        {isActive && (
                          <span className="session-status ml-auto shrink-0 font-mono text-[10px] text-primary-300">
                            {s.status}
                          </span>
                        )}
                        <button
                          className={`btn-delete inline-flex size-5 shrink-0 items-center justify-center rounded-md text-gray-600 opacity-0 transition-opacity hover:bg-danger-bg hover:text-destructive group-hover/session:opacity-100 ${
                            isActive ? "" : "ml-auto"
                          }`}
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteSession(s.id);
                          }}
                          title="Delete session"
                          aria-label={`Delete ${s.name}`}
                        >
                          <IconX size={12} />
                        </button>
                      </div>
                    );
                  })}
                  {visible.length === 0 && (
                    <button
                      type="button"
                      className="session-empty px-2.5 py-1.5 text-left text-[12.5px] text-gray-700 hover:text-gray-900"
                      onClick={() => openCreateRow(a.id)}
                    >
                      No sessions — start one
                    </button>
                  )}
                  {hiddenDelegations > 0 && (
                    <DelegationToggle count={hiddenDelegations} />
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** The hidden-delegation pill (agent-collaboration.md §6) — delegation
 * children are noise in the sidebar until you want them. */
function DelegationToggle({ count }: { count: number }) {
  const setShowDelegations = useSessionStore((s) => s.setShowDelegations);
  return (
    <button
      type="button"
      className="delegation-toggle btn-show-delegations px-2.5 py-1 text-left font-mono text-[10.5px] text-gray-700 hover:text-primary"
      onClick={() => setShowDelegations(true)}
      title="Show delegation sessions"
    >
      + {count} delegation{count === 1 ? "" : "s"}
    </button>
  );
}
