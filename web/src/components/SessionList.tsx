import { useEffect, useState } from "react";
import {
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconCopy,
  IconEye,
  IconEyeOff,
  IconGitFork,
  IconSubtask,
  IconX,
} from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { buildForkTree, type ForkTreeNode } from "../lib/forkTree";
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
  // Fork subtrees default to expanded; clicking the triangle collapses one.
  const [collapsedForks, setCollapsedForks] = useState<Set<string>>(new Set());

  const token = useSessionStore((s) => s.token);
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const setMessages = useSessionStore((s) => s.setMessages);
  const setPendingQueue = useSessionStore((s) => s.setPendingQueue);
  const setPendingQuestions = useSessionStore((s) => s.setPendingQuestions);
  const credentials = useSessionStore((s) => s.credentials);
  const availableBackends = useSessionStore((s) => s.availableBackends);
  const agents = useSessionStore((s) => s.agents);
  // Credentials shown in the create form are scoped to the selected backend.
  const backendCreds = credentials.filter((c) => c.backend === backend);
  const codexAvailable = availableBackends.includes("codex");
  // New sessions inherit the agent's default harness; the form still lets you
  // override per session.
  const agentBackend =
    agents.find((a) => a.id === agentId)?.backend ?? "claude-code";

  // Seed the form's backend (and clear any stale credential pick) from the
  // agent each time the create form opens.
  useEffect(() => {
    if (formOpen) {
      setBackend(agentBackend);
      setCredentialId("");
    }
  }, [formOpen, agentBackend]);

  const showDelegations = useSessionStore((s) => s.showDelegations);
  const setShowDelegations = useSessionStore((s) => s.setShowDelegations);

  // This list shows exactly its agent's sessions (bucketed by agent_id).
  // Archived sessions live in the account-menu manage page, not here.
  // Delegation sessions (origin === "delegation") are hidden by default
  // to keep the sidebar clean under heavy fan-out — the user can flip
  // the global showDelegations toggle to see them. The hidden-count
  // pill at the bottom of this list surfaces what's been filtered.
  // (agent-collaboration.md §6)
  const allAgentSessions = sessions.filter((s) => s.agent_id === agentId);
  const hiddenDelegationCount = showDelegations
    ? 0
    : allAgentSessions.filter((s) => s.origin === "delegation").length;
  const agentSessions = showDelegations
    ? allAgentSessions
    : allAgentSessions.filter((s) => s.origin !== "delegation");

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

  const toggleCollapse = (id: string) =>
    setCollapsedForks((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // One session row + its fork subtree (session-tree-rewind.md §6.3). A fork is
  // a rewind: its parent is archived on creation, so the fork normally renders
  // as a plain top-level root. Nesting (a disclosure triangle + an "@msg N"
  // branch badge) only re-appears when the parent is present — e.g. after it's
  // been unarchived.
  const renderForkNode = (node: ForkTreeNode, depth: number) => {
    const s = node.session;
    const hasChildren = node.children.length > 0;
    const collapsed = collapsedForks.has(s.id);
    // Fork chrome (the git-fork icon + "@msg N" badge) is only meaningful when
    // the fork sits beneath its parent in the tree — i.e. it's nested (depth >
    // 0). A rewind fork whose parent is archived renders as a top-level root
    // and is shown as a plain session, indistinguishable from the original.
    const showForkChrome = depth > 0;
    return (
      <div key={s.id} className="fork-node">
        <div
          className={`session-item group flex items-center gap-2 rounded-lg px-2 py-1.5 cursor-pointer transition-colors ${
            s.id === activeSessionId
              ? "active bg-[hsl(var(--gray-200))] text-foreground"
              : "text-sidebar-foreground hover:bg-sidebar-accent"
          }`}
          style={{ paddingLeft: `${0.5 + depth * 0.85}rem` }}
          onClick={() => selectSession(s.id)}
        >
          {hasChildren ? (
            <button
              type="button"
              className="fork-disclosure inline-flex items-center text-muted-foreground/70 hover:text-foreground"
              onClick={(e) => {
                e.stopPropagation();
                toggleCollapse(s.id);
              }}
              title={collapsed ? "Expand forks" : "Collapse forks"}
              aria-label={collapsed ? "Expand forks" : "Collapse forks"}
            >
              {collapsed ? (
                <IconChevronRight size={12} />
              ) : (
                <IconChevronDown size={12} />
              )}
            </button>
          ) : (
            <span className="inline-block w-3 shrink-0" />
          )}
          <span
            className={`status-dot status-${s.status} inline-block size-2 rounded-full shrink-0 ${
              s.status === "running"
                ? "bg-primary animate-pulse"
                : s.status === "waiting_approval"
                ? "bg-yellow-500"
                : "bg-muted-foreground/40"
            }`}
          />
          {showForkChrome && (
            <IconGitFork
              size={11}
              className="fork-marker shrink-0 text-muted-foreground/70"
              aria-hidden
            />
          )}
          <span
            className={`session-name truncate text-sm flex-1 ${
              s.id === activeSessionId ? "font-medium" : ""
            }`}
          >
            {s.name}
          </span>
          {showForkChrome && s.fork_after_seq != null && !s.fork_is_full_copy && (
            <span
              className="fork-badge shrink-0 rounded bg-muted px-1 text-[10px] font-mono text-muted-foreground"
              title={`Forked before message ${s.fork_after_seq + 1}`}
            >
              @msg {s.fork_after_seq + 1}
            </span>
          )}
          {s.origin === "delegation" && (
            <span
              className="delegation-marker inline-flex items-center text-muted-foreground/80"
              title="Delegation session"
              aria-label="Delegation session"
            >
              <IconSubtask size={12} />
            </span>
          )}
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
        {hasChildren &&
          !collapsed &&
          node.children.map((c) => renderForkNode(c, depth + 1))}
      </div>
    );
  };

  const renderForkForest = (list: SessionInfo[]) => {
    const roots = buildForkTree(list);
    return <>{roots.map((n) => renderForkNode(n, 0))}</>;
  };

  return (
    <div className="session-list session-list-nested ml-3 mt-0.5 mb-1 pl-2 border-l border-sidebar-border/40">
      <div className="session-list-items flex flex-col gap-0">
        {agentSessions.length === 0 && !formOpen && (
          <div className="text-[11px] italic text-sidebar-foreground/40 px-2 py-1">
            No sessions yet.
          </div>
        )}
        {hiddenDelegationCount > 0 && !formOpen && (
          <button
            type="button"
            className="delegation-toggle group flex items-center gap-1.5 px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground rounded transition-colors"
            onClick={() => setShowDelegations(true)}
            title="Show delegation sessions"
          >
            <IconEye size={11} />
            <span>
              +{hiddenDelegationCount} delegation
              {hiddenDelegationCount === 1 ? "" : "s"} hidden
            </span>
          </button>
        )}
        {showDelegations &&
          allAgentSessions.some((s) => s.origin === "delegation") &&
          !formOpen && (
            <button
              type="button"
              className="delegation-toggle flex items-center gap-1.5 px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground rounded transition-colors"
              onClick={() => setShowDelegations(false)}
              title="Hide delegation sessions"
            >
              <IconEyeOff size={11} />
              <span>Hide delegations</span>
            </button>
          )}
        {renderForkForest(agentSessions)}
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
