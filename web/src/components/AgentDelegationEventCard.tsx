/**
 * Renders one of the three turn-injection artifacts a delegation can
 * produce, parsed from the user-message text the backend's
 * DelegationManager injected into the parent session:
 *
 *   `[agent-reply:<name> delegation=<id>]\n<body>`
 *   `[agent-question:<name> delegation=<id> question_id=<qid>]\n<body>`
 *   `[agent-error:<name> delegation=<id> reason=<r>]\n<body>`
 *
 * Each becomes a card with the target agent's name in the header,
 * the body in an expandable region, and a deep-link into the
 * delegation's child session. The question variant deliberately
 * renders options as PLAIN TEXT — the principal-chain rule
 * (agent-collaboration.md §6) says the human is NOT supposed to
 * answer a delegated agent's question; the parent agent's model is.
 */

import { useEffect, useState } from "react";
import {
  IconArrowBackUp,
  IconChevronDown,
  IconChevronRight,
  IconExclamationCircle,
  IconExternalLink,
  IconMessageQuestion,
  IconSubtask,
} from "@tabler/icons-react";

import { useSessionStore } from "../stores/sessionStore";

export type DelegationEventKind = "reply" | "question" | "error";

export interface ParsedDelegationEvent {
  kind: DelegationEventKind;
  agentName: string;
  delegationId: string;
  questionId?: string;
  reason?: string;
  body: string;
}

// Agent names can have spaces ("Code Reviewer", "E2E DelegTarget").
// Match the name non-greedily up to the literal " delegation=" or
// " question_id=" / " reason=" separator. The id is a simple
// identifier with no whitespace or `]`. The error reason may contain
// spaces, so we let it match anything up to the closing `]`.
const REPLY_RE = /^\[agent-reply:(.+?)\s+delegation=([^\]\s]+)\]\n?/;
const QUESTION_RE =
  /^\[agent-question:(.+?)\s+delegation=(\S+)\s+question_id=([^\]\s]+)\]\n?/;
const ERROR_RE =
  /^\[agent-error:(.+?)\s+delegation=(\S+)\s+reason=([^\]]+)\]\n?/;

/**
 * Try to parse a user-message content string as a delegation event.
 * Returns null when the string doesn't start with any of the three
 * known prefixes — callers fall back to the plain user-message
 * rendering. Exported separately so MessageBubble can detect first,
 * then decide to render this card.
 */
export function parseDelegationEvent(
  content: string | undefined
): ParsedDelegationEvent | null {
  if (!content) return null;
  let m = content.match(REPLY_RE);
  if (m) {
    return {
      kind: "reply",
      agentName: m[1],
      delegationId: m[2],
      body: content.slice(m[0].length),
    };
  }
  m = content.match(QUESTION_RE);
  if (m) {
    return {
      kind: "question",
      agentName: m[1],
      delegationId: m[2],
      questionId: m[3],
      body: content.slice(m[0].length),
    };
  }
  m = content.match(ERROR_RE);
  if (m) {
    return {
      kind: "error",
      agentName: m[1],
      delegationId: m[2],
      reason: m[3].trim(),
      body: content.slice(m[0].length),
    };
  }
  return null;
}

function KindIcon({ kind }: { kind: DelegationEventKind }) {
  if (kind === "question") {
    return <IconMessageQuestion size={14} className="text-yellow-700" />;
  }
  if (kind === "error") {
    return <IconExclamationCircle size={14} className="text-destructive" />;
  }
  return <IconSubtask size={14} className="text-primary" />;
}

const KIND_LABEL: Record<DelegationEventKind, string> = {
  reply: "replied",
  question: "is asking",
  error: "ended with an error",
};

const KIND_BORDER: Record<DelegationEventKind, string> = {
  reply: "border-primary/40 bg-primary/5",
  question: "border-yellow-600/40 bg-yellow-50",
  error: "border-destructive/40 bg-destructive/5",
};

export function AgentDelegationEventCard({
  event,
}: {
  event: ParsedDelegationEvent;
}) {
  const [expanded, setExpanded] = useState(event.kind !== "reply");
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const token = useSessionStore((s) => s.token);
  const childSession = sessions.find(
    (sess) => sess.id === event.delegationId
  );

  // Delegation child sessions are created server-side without going
  // through the frontend's POST /api/sessions path, so the store
  // doesn't learn about them automatically. If our delegation_id
  // isn't in the sessions list, fetch the child by id once and
  // splice it in — that makes the "Open child" button + the
  // "Delegated from" banner work without a full refresh.
  useEffect(() => {
    if (!event.delegationId || childSession) return;
    let cancelled = false;
    fetch(
      `${window.location.origin}/api/sessions/${encodeURIComponent(
        event.delegationId
      )}`,
      { headers: { Authorization: `Bearer ${token}` } }
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (cancelled || !data) return;
        const current = useSessionStore.getState().sessions;
        if (current.some((s) => s.id === data.id)) return;
        setSessions([...current, data]);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [event.delegationId, childSession, token, setSessions]);

  const openChild = () => {
    if (!childSession) return;
    if (childSession.agent_id) setActiveAgentId(childSession.agent_id);
    setActiveSessionId(childSession.id);
  };

  const headerText = (
    <>
      <span className="font-medium">{event.agentName}</span>
      <span className="text-muted-foreground"> {KIND_LABEL[event.kind]}</span>
      {event.kind === "error" && event.reason && (
        <>
          <span className="text-muted-foreground"> — </span>
          <span className="text-destructive">{event.reason}</span>
        </>
      )}
    </>
  );

  const firstLine = event.body.split("\n", 1)[0];

  return (
    <div className="msg msg-agent-delegation-event flex justify-end">
      <div className="max-w-[85%] space-y-1">
        <div
          className="msg-label text-xs font-semibold text-muted-foreground text-right flex items-center justify-end gap-1.5"
          data-delegation-kind={event.kind}
        >
          <IconArrowBackUp size={12} className="text-muted-foreground" />
          <span>From delegation · {event.delegationId.slice(0, 8)}</span>
        </div>
        <div
          className={`agent-delegation-card rounded-lg border px-3 py-2.5 text-sm ${KIND_BORDER[event.kind]}`}
        >
          <button
            type="button"
            onClick={() => setExpanded((e) => !e)}
            className="w-full flex items-start gap-2 text-left"
            aria-expanded={expanded}
          >
            <span className="text-muted-foreground shrink-0 mt-0.5">
              {expanded ? (
                <IconChevronDown size={14} />
              ) : (
                <IconChevronRight size={14} />
              )}
            </span>
            <KindIcon kind={event.kind} />
            <span className="flex-1 leading-snug">
              {headerText}
              {!expanded && (
                <span className="block text-xs text-muted-foreground truncate mt-0.5">
                  {firstLine}
                </span>
              )}
            </span>
          </button>
          {expanded && (
            <pre className="agent-delegation-body mt-2 ml-6 whitespace-pre-wrap break-words text-xs text-foreground font-sans leading-relaxed">
              {event.body}
            </pre>
          )}
          {event.kind === "question" && expanded && (
            <div className="mt-2 ml-6 text-[11px] italic text-muted-foreground">
              The other agent is waiting. Decide whether to answer
              directly via <code>answer_agent_question</code>, escalate
              to the user with your own <code>ask</code>, or cancel
              the delegation.
            </div>
          )}
          <div className="mt-2 ml-6 flex items-center gap-3 text-[11px]">
            {childSession && (
              <button
                type="button"
                onClick={openChild}
                className="btn-open-delegation inline-flex items-center gap-1 text-primary hover:underline"
              >
                <IconExternalLink size={11} />
                Open {event.agentName}&apos;s session
              </button>
            )}
            <span className="text-muted-foreground/70">
              delegation_id={event.delegationId}
              {event.questionId ? ` · question_id=${event.questionId}` : ""}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
