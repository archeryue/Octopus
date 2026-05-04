import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { useSessionStore, type Message } from "../stores/sessionStore";
import { MessageBubble } from "./MessageBubble";
import { ToolApproval } from "./ToolApproval";

const EMPTY_MESSAGES: Message[] = [];

interface Props {
  sendMessage: (sessionId: string, content: string) => void;
  approveTool: (sessionId: string, toolUseId: string) => void;
  denyTool: (sessionId: string, toolUseId: string) => void;
}

export function ChatView({ sendMessage, approveTool, denyTool }: Props) {
  const [input, setInput] = useState("");
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const messagesMap = useSessionStore((s) => s.messages);
  const messages = activeSessionId ? (messagesMap[activeSessionId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES;
  const sessions = useSessionStore((s) => s.sessions);
  const activeSession = useMemo(() => sessions.find((s) => s.id === activeSessionId), [sessions, activeSessionId]);
  const virtuosoRef = useRef<VirtuosoHandle>(null);

  // Scroll to the bottom when switching into a session whose history has
  // already loaded. `initialTopMostItemIndex` is captured at mount time, so
  // it doesn't help when messages arrive asynchronously after the click.
  const hasMessages = messages.length > 0;
  useEffect(() => {
    if (!activeSessionId || !hasMessages) return;
    virtuosoRef.current?.scrollToIndex({ index: "LAST", behavior: "auto" });
  }, [activeSessionId, hasMessages]);

  const isRunning = activeSession?.status === "running";

  const isWaitingForResponse = useMemo(() => {
    if (isRunning || activeSession?.status !== "idle") return false;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "assistant" && m.type === "text") {
        return /\?\s*$/.test((m.content ?? "").trim());
      }
    }
    return false;
  }, [messages, isRunning, activeSession?.status]);

  const renderMessage = useCallback(
    (_index: number, msg: Message) =>
      msg.type === "tool_approval_request" ? (
        <ToolApproval
          message={msg}
          onApprove={(id) => activeSessionId && approveTool(activeSessionId, id)}
          onDeny={(id) => activeSessionId && denyTool(activeSessionId, id)}
        />
      ) : (
        <MessageBubble message={msg} />
      ),
    [activeSessionId, approveTool, denyTool]
  );

  const footer = useCallback(
    () =>
      isRunning ? (
        <div className="msg msg-loading">
          <span className="loading-dot" />
          <span className="loading-dot" />
          <span className="loading-dot" />
        </div>
      ) : null,
    [isRunning]
  );

  const handleSend = () => {
    if (!input.trim() || !activeSessionId) return;
    sendMessage(activeSessionId, input.trim());
    setInput("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  if (!activeSessionId) {
    return (
      <div className="chat-empty">
        <h2>Octopus</h2>
        <p>Create or select a session to start.</p>
      </div>
    );
  }

  return (
    <div className="chat-view">
      <div className="chat-header">
        <h3>{activeSession?.name || "Session"}</h3>
        <span className={`status-badge status-${activeSession?.status}`}>
          {activeSession?.status}
        </span>
      </div>

      <Virtuoso
        ref={virtuosoRef}
        className="chat-messages"
        data={messages}
        itemContent={renderMessage}
        initialTopMostItemIndex={messages.length ? messages.length - 1 : 0}
        followOutput="smooth"
        increaseViewportBy={{ top: 400, bottom: 400 }}
        components={{ Footer: footer }}
      />

      {isWaitingForResponse && (
        <div className="waiting-hint">Claude is waiting for your response</div>
      )}

      <div className="chat-input-bar">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Send a message..."
          rows={1}
          disabled={isRunning}
        />
        <button
          className="btn btn-send"
          onClick={handleSend}
          disabled={!input.trim() || isRunning}
        >
          Send
        </button>
      </div>
    </div>
  );
}
