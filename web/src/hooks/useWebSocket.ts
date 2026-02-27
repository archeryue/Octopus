import { useCallback, useEffect, useRef } from "react";
import { useSessionStore, type SessionStatus } from "../stores/sessionStore";

const WS_HOST = window.location.hostname || "localhost";
const WS_URL = `ws://${WS_HOST}:8000/ws`;

// Access store actions directly (stable references, no re-renders)
const getState = () => useSessionStore.getState();

function handleWsMessage(data: Record<string, unknown>) {
  const { addMessage, updateSessionStatus } = getState();
  const sessionId = data.session_id as string;
  const type = data.type as string;

  switch (type) {
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
      break;

    case "error":
      addMessage(sessionId || "__global", {
        role: "system",
        type: "error",
        content: data.message as string,
      });
      break;
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
      getState().addMessage(sessionId, { role: "user", type: "text", content });
      send({ type: "send_message", session_id: sessionId, content });
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

  return { send, sendMessage, approveTool, denyTool };
}
