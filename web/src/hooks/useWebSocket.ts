import { useCallback, useEffect, useRef } from "react";
import {
  useSessionStore,
  type BgTask,
  type Message,
  type PendingQuestion,
  type ResearchJob,
  type SessionStatus,
} from "../stores/sessionStore";

type BgTaskStatus = BgTask["status"];

const WS_PROTOCOL = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${WS_PROTOCOL}//${window.location.host}/ws`;

// Access store actions directly (stable references, no re-renders)
const getState = () => useSessionStore.getState();

/** Snapshot-baseline dedup for WS message events.
 *
 * Events that correspond to a DB message row carry `seq`
 * (server-assigned). After loading a snapshot the store sets
 * `lastAppliedSeq[sessionId]` to `next_message_seq - 1`; any
 * subsequently-broadcast event with `seq <= baseline` is already in
 * the snapshot and must be dropped to avoid double-rendering.
 *
 * Events without `seq` (status / queued / dequeued /
 * tool_approval_request) are ephemeral and always applied.
 *
 * Exported only so the unit tests can exercise the guard directly;
 * call sites (handleWsMessage) consume it internally.
 */
export function shouldApplyWsEvent(
  seq: number | null | undefined,
  baseline: number | undefined
): boolean {
  if (typeof seq !== "number") return true;
  const b = typeof baseline === "number" ? baseline : -1;
  return seq > b;
}

function handleWsMessage(data: Record<string, unknown>) {
  const {
    addMessage,
    updateSessionStatus,
    enqueuePending,
    dequeuePending,
    addPendingQuestion,
    removePendingQuestion,
    setLastAppliedSeq,
    lastAppliedSeq,
  } = getState();
  const sessionId = data.session_id as string;
  const type = data.type as string;

  const seq = typeof data.seq === "number" ? (data.seq as number) : null;
  if (seq !== null && sessionId) {
    if (!shouldApplyWsEvent(seq, lastAppliedSeq[sessionId])) {
      return; // already in snapshot; ignore to avoid duplicate
    }
    setLastAppliedSeq(sessionId, seq);
  }

  switch (type) {
    case "queued":
      enqueuePending(sessionId, data.content as string);
      break;

    case "dequeued":
      dequeuePending(sessionId);
      break;

    case "assistant_text":
      addMessage(sessionId, {
        role: "assistant",
        type: "text",
        content: data.content as string,
      });
      break;

    case "tool_use": {
      const toolName = data.tool as string;
      const toolInput = data.input as Record<string, unknown>;
      addMessage(sessionId, {
        role: "assistant",
        type: "tool_use",
        tool_name: toolName,
        tool_input: toolInput,
        tool_use_id: data.tool_use_id as string,
      });
      break;
    }

    case "tool_result":
      addMessage(sessionId, {
        role: "tool",
        type: "tool_result",
        content: data.output as string,
        tool_use_id: data.tool_use_id as string,
        is_error: data.is_error as boolean,
      });
      break;

    case "tool_approval_request":
      addMessage(sessionId, {
        role: "tool",
        type: "tool_approval_request",
        tool_name: data.tool_name as string,
        tool_input: data.tool_input as Record<string, unknown>,
        tool_use_id: data.tool_use_id as string,
      });
      updateSessionStatus(sessionId, "waiting_approval");
      break;

    case "question_request": {
      const questionId = data.question_id as string;
      const questions = (data.questions as PendingQuestion["questions"]) || [];
      addMessage(sessionId, {
        role: "assistant",
        type: "question_request",
        tool_name: "AskUserQuestion",
        tool_input: { questions },
        tool_use_id: questionId,
      });
      addPendingQuestion(sessionId, {
        question_id: questionId,
        questions,
      });
      break;
    }

    case "question_answer": {
      const questionId = data.question_id as string;
      addMessage(sessionId, {
        role: "user",
        type: "question_answer",
        content: data.content as string,
        tool_use_id: questionId,
      });
      removePendingQuestion(sessionId, questionId);
      break;
    }

    case "status":
      updateSessionStatus(sessionId, data.status as SessionStatus);
      break;

    case "result":
      addMessage(sessionId, {
        role: "system",
        type: "result",
        cost: data.cost as number,
        session_id: data.claude_session_id as string,
      });
      break;

    case "user_message":
      addMessage(sessionId, {
        role: "user",
        type: "text",
        content: data.content as string,
        attachments: (data.attachments as Message["attachments"]) ?? undefined,
        // Carry the row seq so "Fork from here" can target a just-sent
        // message without waiting for a detail reload (the event always
        // includes it for persisted rows).
        seq: seq ?? undefined,
      });
      break;

    case "error":
      addMessage(sessionId || "__global", {
        role: "system",
        type: "error",
        content: data.message as string,
      });
      // A mid-turn 401 flagged the bound credential needs_reconnect server-side
      // (harness-credential-reauth.md §6). Refetch credentials so the Harness
      // sidebar lights up its "Re-authorize" badge without a manual reload.
      if (data.code === "auth_expired") {
        const t = getState().token;
        if (t) {
          fetch(`${window.location.origin}/api/credentials`, {
            headers: { Authorization: `Bearer ${t}` },
          })
            .then((r) => (r.ok ? r.json() : null))
            .then((creds) => creds && getState().setCredentials(creds))
            .catch(() => {});
        }
      }
      break;

    case "bg_started": {
      const { upsertBgTask } = getState();
      upsertBgTask(sessionId, {
        id: data.task_id as string,
        session_id: sessionId,
        command: data.command as string,
        description: (data.description as string) ?? null,
        working_dir: "",
        status: "running",
        exit_code: null,
        stdout: "",
        stderr: "",
        truncated: false,
        started_at: data.started_at as string,
        completed_at: null,
      });
      break;
    }

    case "bg_completed": {
      // Patch the existing row in place. We don't have stdout/stderr
      // here — those land via the synthesized user_message turn — but
      // the chip needs the status + exit_code to flip from spinner
      // to badge. Full bytes are fetched on demand via the REST GET.
      const { bgTasks, upsertBgTask } = getState();
      const current = (bgTasks[sessionId] || []).find(
        (t) => t.id === data.task_id
      );
      if (current) {
        upsertBgTask(sessionId, {
          ...current,
          status: data.status as BgTaskStatus,
          exit_code: (data.exit_code as number | null) ?? null,
          truncated: !!data.truncated,
          completed_at: data.completed_at as string,
        });
      }
      break;
    }

    case "research_started": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        session_id: sessionId,
        question: data.question as string,
        status: "running",
        phase: "scope",
      });
      break;
    }

    case "research_progress": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        phase: (data.phase as string) ?? null,
        detail: data.detail as string,
        counts: (data.counts as Record<string, number>) ?? undefined,
      });
      break;
    }

    case "research_completed": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        status: "completed",
        phase: "done",
        sources: (data.sources as string[]) ?? undefined,
        verified: (data.verified as number) ?? undefined,
      });
      break;
    }

    case "research_failed": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        status: (data.status as ResearchJob["status"]) ?? "failed",
        error: (data.error as string) ?? null,
      });
      break;
    }

    case "session_archived": {
      // Another client (or this tab's archive POST) hid the old
      // session and replaced it with new_session_id. Update the
      // sessions list + swap active if this tab was on the old one.
      const oldId = data.old_session_id as string;
      // null = the session was hidden with no replacement (a scheduler-origin
      // session auto-archiving on idle, or an agent being archived).
      const newId = data.new_session_id as string | null;
      const name = data.name as string;
      const store = getState();
      const prev = store.sessions.find((s) => s.id === oldId);
      const next = store.sessions.filter((s) => s.id !== oldId);
      if (newId && !next.some((s) => s.id === newId)) {
        next.push({
          id: newId,
          name,
          working_dir: prev?.working_dir ?? "",
          status: "idle",
          created_at: new Date().toISOString(),
          message_count: 0,
          claude_session_id: null,
          credential_id: prev?.credential_id ?? null,
          agent_id: prev?.agent_id ?? null,
          origin: prev?.origin ?? "user",
          backend: prev?.backend ?? "claude-code",
          can_fork: prev?.can_fork ?? true,
          fork_is_full_copy: false,
          archived: false,
        });
      }
      store.setSessions(next);
      // Keep the archived session resolvable (so e.g. a fork's now-archived
      // parent renders its name in the fork banner instead of "(deleted
      // session)", and its "open parent" link works).
      if (prev) {
        const restArchived = store.archivedSessions.filter((s) => s.id !== oldId);
        store.setArchivedSessions([{ ...prev, archived: true }, ...restArchived]);
      }
      if (store.activeSessionId === oldId) {
        store.setActiveSessionId(newId);
        if (newId) {
          store.setMessages(newId, []);
          store.setPendingQueue(newId, []);
          store.setPendingQuestions(newId, []);
        }
      }
      break;
    }
    case "session_forked": {
      // A /fork duplicate was created (session-fork-copy.md). The parent is
      // untouched, so we only ADD the new session to the list. The initiating
      // tab already added + switched to it via handleDuplicated; dedupe by id
      // so this broadcast is a no-op there and a plain add elsewhere. We don't
      // switch the active session — only the initiator follows the fork.
      const forkId = data.fork_session_id as string;
      const store = getState();
      if (store.sessions.some((s) => s.id === forkId)) break;
      // Fetch the authoritative SessionInfo rather than synthesizing a partial
      // one (Vera review) — gives the sidebar the right working_dir, backend,
      // fork flags, etc. Re-dedupe inside the callback in case the initiating
      // tab's own add raced in first.
      const tok = store.token;
      if (!tok) break;
      void fetch(`${window.location.origin}/api/sessions/${forkId}`, {
        headers: { Authorization: `Bearer ${tok}` },
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((detail) => {
          if (!detail) return;
          const s = getState();
          if (s.sessions.some((x) => x.id === forkId)) return;
          // The detail endpoint is a SessionInfo superset (+ messages,
          // pending_queue, pending_questions, next_message_seq); strip the
          // detail-only fields so the list holds a clean SessionInfo.
          const {
            messages: _m,
            pending_queue: _q,
            pending_questions: _pq,
            next_message_seq: _n,
            ...info
          } = detail;
          s.setSessions([...s.sessions, info]);
        })
        .catch(() => {});
      break;
    }
  }
}

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const token = useSessionStore((s) => s.token);

  useEffect(() => {
    if (!token) return;

    function connect() {
      if (wsRef.current?.readyState === WebSocket.OPEN ||
          wsRef.current?.readyState === WebSocket.CONNECTING) return;

      const ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        getState().setConnected(true);
        if (reconnectTimer.current) clearTimeout(reconnectTimer.current);

        // Re-fetch state after reconnect
        const { activeSessionId, token: t } = getState();
        if (t) {
          fetch(`${window.location.origin}/api/sessions`, {
            headers: { Authorization: `Bearer ${t}` },
          })
            .then((r) => (r.ok ? r.json() : null))
            .then((sessions) => sessions && getState().setSessions(sessions))
            .catch(() => {});

          if (activeSessionId) {
            fetch(
              `${window.location.origin}/api/sessions/${activeSessionId}`,
              { headers: { Authorization: `Bearer ${t}` } }
            )
              .then((r) => (r.ok ? r.json() : null))
              .then((data) => {
                if (!data) return;
                getState().setMessages(activeSessionId, data.messages);
                getState().setPendingQueue(
                  activeSessionId,
                  data.pending_queue || []
                );
                getState().setPendingQuestions(
                  activeSessionId,
                  data.pending_questions || []
                );
                // Bump the WS-event dedup baseline so any event with
                // seq < next_message_seq is treated as already applied
                // (it's in the snapshot we just set).
                if (typeof data.next_message_seq === "number") {
                  getState().setLastAppliedSeq(
                    activeSessionId,
                    data.next_message_seq - 1
                  );
                }
              })
              .catch(() => {});
            // Bg tasks: same reload, independent endpoint. Chat history
            // contains bg_run tool_use blocks whose chips need the bg
            // task records to render correctly after a reconnect.
            fetch(
              `${window.location.origin}/api/sessions/${activeSessionId}/bg-tasks`,
              { headers: { Authorization: `Bearer ${t}` } }
            )
              .then((r) => (r.ok ? r.json() : null))
              .then((tasks) => {
                if (tasks) getState().setBgTasks(activeSessionId, tasks);
              })
              .catch(() => {});
            // Research jobs: same reconnect reload, so an in-flight job's card
            // (and recently-finished ones) reappear without a missed
            // `research_started` (native-deep-research.md §7).
            fetch(
              `${window.location.origin}/api/sessions/${activeSessionId}/research`,
              { headers: { Authorization: `Bearer ${t}` } }
            )
              .then((r) => (r.ok ? r.json() : null))
              .then((jobs) => {
                if (jobs) getState().setResearch(activeSessionId, jobs);
              })
              .catch(() => {});
          }
        }
      };

      ws.onclose = () => {
        getState().setConnected(false);
        reconnectTimer.current = setTimeout(connect, 3000);
      };

      ws.onerror = () => {
        ws.close();
      };

      ws.onmessage = (e) => {
        try {
          handleWsMessage(JSON.parse(e.data));
        } catch {
          // ignore
        }
      };
    }

    connect();

    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      reconnectTimer.current = undefined;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [token]);

  const send = useCallback((data: Record<string, unknown>) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  const sendMessage = useCallback(
    (sessionId: string, content: string, attachmentIds?: string[]) => {
      // Don't optimistically add to chat — the backend broadcasts a
      // user_message event whether the prompt fires immediately or after
      // dequeue, so a single broadcast handler keeps state consistent.
      const payload: Record<string, unknown> = {
        type: "send_message",
        session_id: sessionId,
        content,
      };
      if (attachmentIds && attachmentIds.length > 0) {
        payload.attachment_ids = attachmentIds;
      }
      send(payload);
    },
    [send]
  );

  const interrupt = useCallback(
    (sessionId: string) => {
      send({ type: "interrupt", session_id: sessionId });
    },
    [send]
  );

  const approveTool = useCallback(
    (sessionId: string, toolUseId: string) => {
      send({
        type: "approve_tool",
        session_id: sessionId,
        tool_use_id: toolUseId,
      });
    },
    [send]
  );

  const denyTool = useCallback(
    (sessionId: string, toolUseId: string, reason?: string) => {
      send({
        type: "deny_tool",
        session_id: sessionId,
        tool_use_id: toolUseId,
        reason: reason || "",
      });
    },
    [send]
  );

  const answerQuestion = useCallback(
    (
      sessionId: string,
      questionId: string,
      answers: { selected?: string[]; text?: string }[]
    ) => {
      send({
        type: "answer_question",
        session_id: sessionId,
        question_id: questionId,
        answers,
      });
    },
    [send]
  );

  return { send, sendMessage, interrupt, approveTool, denyTool, answerQuestion };
}
