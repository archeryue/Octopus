/**
 * Inline card rendered next to a `mcp__ask_agent__ask` tool_use. The
 * tool_use itself shows the model's call shape (target agent name,
 * request, optional files); this card shows the LIVE state of the
 * delegation — running spinner, completed, cancelled, failed — plus
 * a deep-link into the child's session and a cancel button while
 * the run is open.
 *
 * State source: `delegations[sessionId]` in the zustand store,
 * populated by either the snapshot fetch (on session load) or the
 * REST round-trip the tool_use kicked off. We match the live record
 * to this tool_use by (target_agent_name, request) — the most-recent
 * matching record wins, which gives the right answer in the common
 * case of one delegation per (target, request) tuple.
 */

import { useEffect, useState } from "react";
import {
  IconCheck,
  IconExclamationCircle,
  IconExternalLink,
  IconHandStop,
  IconLoader2,
  IconSubtask,
  IconX,
} from "@tabler/icons-react";

import { useSessionStore, type Delegation } from "../stores/sessionStore";

const STATUS_LABEL: Record<Delegation["state"], string> = {
  running: "running",
  completed: "replied",
  failed: "failed",
  cancelled: "cancelled",
};

function StatusIcon({ state }: { state: Delegation["state"] }) {
  if (state === "running") {
    return <IconLoader2 size={14} className="animate-spin text-primary" />;
  }
  if (state === "completed") {
    return <IconCheck size={14} className="text-green-700" />;
  }
  if (state === "cancelled") {
    return <IconHandStop size={14} className="text-muted-foreground" />;
  }
  return <IconExclamationCircle size={14} className="text-destructive" />;
}

export function AgentDelegationRequestCard({
  sessionId,
  agentName,
  request,
  files,
}: {
  sessionId: string;
  agentName: string;
  request: string;
  files: string[] | undefined;
}) {
  const token = useSessionStore((s) => s.token);
  const setDelegations = useSessionStore((s) => s.setDelegations);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const sessions = useSessionStore((s) => s.sessions);
  const wantName = (agentName || "").toLowerCase();
  // Pick the most recent delegation that matches the tool_use's
  // (target agent name, request) tuple. Most-recent-first because two
  // delegations with the same payload would be a deliberate retry,
  // and the user looking at the chip wants the latest state.
  const match = useSessionStore((s) => {
    const list = s.delegations[sessionId] || [];
    return [...list]
      .reverse()
      .find(
        (d) =>
          (d.target_agent_name || "").toLowerCase() === wantName &&
          d.request === request
      );
  });
  const [cancelling, setCancelling] = useState(false);

  // Lazy snapshot fetch: if the store has no delegations for this
  // session yet (e.g. user just navigated in from another session),
  // fetch the list once so the card finds its match.
  useEffect(() => {
    if (match) return;
    const url = `${window.location.origin}/api/sessions/${encodeURIComponent(
      sessionId
    )}/delegations`;
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (Array.isArray(data)) setDelegations(sessionId, data as Delegation[]);
      })
      .catch(() => {});
  }, [match, sessionId, token, setDelegations]);

  const openChild = () => {
    if (!match) return;
    const child = sessions.find((s) => s.id === match.delegation_id);
    if (child?.agent_id) setActiveAgentId(child.agent_id);
    setActiveSessionId(match.delegation_id);
  };

  const cancel = async () => {
    if (!match) return;
    setCancelling(true);
    const url = `${window.location.origin}/api/sessions/${encodeURIComponent(
      sessionId
    )}/delegations/${encodeURIComponent(match.delegation_id)}/cancel`;
    try {
      await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ reason: "cancelled from UI" }),
      });
    } finally {
      setCancelling(false);
    }
  };

  const state = match?.state ?? "running";
  const label = STATUS_LABEL[state];
  const delegationIdShort = match?.delegation_id?.slice(0, 8) ?? "…";

  return (
    <div
      className={`agent-delegation-request inline-flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-xs ${
        state === "completed"
          ? "border-primary/40 bg-primary/5"
          : state === "running"
          ? "border-border bg-card"
          : state === "cancelled"
          ? "border-border bg-muted/40 text-muted-foreground"
          : "border-destructive/40 bg-destructive/5"
      }`}
      data-delegation-state={state}
    >
      <IconSubtask size={14} className="text-primary mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0 space-y-0.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium truncate">
            Asked {agentName}
          </span>
          <span className="inline-flex items-center gap-1 text-muted-foreground">
            <StatusIcon state={state} />
            <span>{label}</span>
          </span>
          {match && (
            <span className="text-[10px] text-muted-foreground/70 font-mono">
              ({delegationIdShort})
            </span>
          )}
        </div>
        <div className="text-muted-foreground truncate" title={request}>
          “{request}”
        </div>
        {files && files.length > 0 && (
          <div className="text-[10px] text-muted-foreground/80">
            files: {files.join(", ")}
          </div>
        )}
      </div>
      <div className="flex items-center gap-1 shrink-0">
        {match && (
          <button
            type="button"
            onClick={openChild}
            className="btn-open inline-flex items-center justify-center h-6 px-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-accent"
            title={`Open ${agentName}'s session`}
          >
            <IconExternalLink size={12} />
          </button>
        )}
        {state === "running" && match && (
          <button
            type="button"
            onClick={cancel}
            disabled={cancelling}
            className="btn-cancel inline-flex items-center justify-center h-6 px-1.5 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10 disabled:opacity-50"
            title="Cancel delegation"
          >
            <IconX size={12} />
          </button>
        )}
      </div>
    </div>
  );
}
