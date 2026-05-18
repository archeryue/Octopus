import { useCallback, useEffect, useRef } from "react";
import {
  useSessionStore,
  type PendingQuestion,
  type SessionStatus,
} from "../stores/sessionStore";

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

    case "tool_use":
      addMessage(sessionId, {
        role: "assistant",
        type: "tool_use",
        tool_name: data.tool as string,
        tool_input: data.input as Record<string, unknown>,
        tool_use_id: data.tool_use_id as string,
      });
      break;

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
      });
      break;

    case "error":
      addMessage(sessionId || "__global", {
        role: "system",
        type: "error",
        content: data.message as string,
      });
      break;

    case "session_archived": {
      // Another client (or this tab's archive POST) hid the old
      // session and replaced it with new_session_id. Update the
      // sessions list + swap active if this tab was on the old one.
      const oldId = data.old_session_id as string;
      const newId = data.new_session_id as string;
      const name = data.name as string;
      const store = getState();
      const next = store.sessions.filter((s) => s.id !== oldId);
      if (!next.some((s) => s.id === newId)) {
        next.push({
          id: newId,
          name,
          working_dir: store.sessions.find((s) => s.id === oldId)?.working_dir ?? "",
          status: "idle",
          created_at: new Date().toISOString(),
          message_count: 0,
          claude_session_id: null,
          credential_id:
            store.sessions.find((s) => s.id === oldId)?.credential_id ?? null,
        });
      }
      store.setSessions(next);
      if (store.activeSessionId === oldId) {
        store.setActiveSessionId(newId);
        store.setMessages(newId, []);
        store.setPendingQueue(newId, []);
        store.setPendingQuestions(newId, []);
      }
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
    (sessionId: string, content: string) => {
      // Don't optimistically add to chat — the backend broadcasts a
      // user_message event whether the prompt fires immediately or after
      // dequeue, so a single broadcast handler keeps state consistent.
      send({ type: "send_message", session_id: sessionId, content });
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
