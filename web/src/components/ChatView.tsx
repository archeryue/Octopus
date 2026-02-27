import { useEffect, useMemo, useRef, useState } from "react";
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
  const bottomRef = useRef<HTMLDivElement>(null);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const messagesMap = useSessionStore((s) => s.messages);
  const messages = activeSessionId ? (messagesMap[activeSessionId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES;
  const sessions = useSessionStore((s) => s.sessions);
  const activeSession = useMemo(() => sessions.find((s) => s.id === activeSessionId), [sessions, activeSessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

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

  const isRunning = activeSession?.status === "running";

  return (
    <div className="chat-view">
      <div className="chat-header">
        <h3>{activeSession?.name || "Session"}</h3>
        <span className={`status-badge status-${activeSession?.status}`}>
          {activeSession?.status}
        </span>
      </div>

      <div className="chat-messages">
        {messages.map((msg, i) =>
          msg.type === "tool_approval_request" ? (
            <ToolApproval
              key={i}
              message={msg}
              onApprove={(id) => approveTool(activeSessionId, id)}
              onDeny={(id) => denyTool(activeSessionId, id)}
            />
          ) : (
            <MessageBubble key={i} message={msg} />
          )
        )}
        {isRunning && (
          <div className="msg msg-loading">
            <span className="loading-dot" />
            <span className="loading-dot" />
            <span className="loading-dot" />
          </div>
        )}
        <div ref={bottomRef} />
      </div>

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
