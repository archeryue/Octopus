import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import {
  useSessionStore,
  type Message,
  type PendingQuestion,
} from "../stores/sessionStore";
import { MessageBubble } from "./MessageBubble";
import { QuestionPrompt, type AnswerPayload } from "./QuestionPrompt";
import { ToolApproval } from "./ToolApproval";

const EMPTY_MESSAGES: Message[] = [];

interface Props {
  sendMessage: (sessionId: string, content: string) => void;
  interrupt: (sessionId: string) => void;
  approveTool: (sessionId: string, toolUseId: string) => void;
  denyTool: (sessionId: string, toolUseId: string) => void;
  answerQuestion: (
    sessionId: string,
    questionId: string,
    answers: AnswerPayload[]
  ) => void;
  connected: boolean;
  onToggleSidebar: () => void;
}

const EMPTY_QUEUE: string[] = [];
const EMPTY_QUESTIONS: PendingQuestion[] = [];

export function ChatView({
  sendMessage,
  interrupt,
  approveTool,
  denyTool,
  answerQuestion,
  connected,
  onToggleSidebar,
}: Props) {
  const [input, setInput] = useState("");
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const messagesMap = useSessionStore((s) => s.messages);
  const messages = activeSessionId ? (messagesMap[activeSessionId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES;
  const sessions = useSessionStore((s) => s.sessions);
  const activeSession = useMemo(() => sessions.find((s) => s.id === activeSessionId), [sessions, activeSessionId]);
  const pendingQueueMap = useSessionStore((s) => s.pendingQueue);
  const pendingQueue = activeSessionId
    ? (pendingQueueMap[activeSessionId] ?? EMPTY_QUEUE)
    : EMPTY_QUEUE;
  const pendingQuestionsMap = useSessionStore((s) => s.pendingQuestions);
  const pendingQuestions = activeSessionId
    ? (pendingQuestionsMap[activeSessionId] ?? EMPTY_QUESTIONS)
    : EMPTY_QUESTIONS;
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
    (_index: number, msg: Message) => {
      if (msg.type === "tool_approval_request") {
        return (
          <ToolApproval
            message={msg}
            onApprove={(id) =>
              activeSessionId && approveTool(activeSessionId, id)
            }
            onDeny={(id) => activeSessionId && denyTool(activeSessionId, id)}
          />
        );
      }
      if (msg.type === "question_request") {
        const pending = pendingQuestions.find(
          (q) => q.question_id === msg.tool_use_id
        );
        if (pending && activeSessionId) {
          return (
            <QuestionPrompt
              question={pending}
              onSubmit={(id, answers) =>
                answerQuestion(activeSessionId, id, answers)
              }
            />
          );
        }
        // Already answered or no live state — render a compact summary so the
        // chat history shows what was asked.
        const questions =
          (msg.tool_input?.questions as PendingQuestion["questions"]) || [];
        return (
          <div className="msg msg-question msg-question-done">
            <div className="question-header">
              <span className="question-icon">?</span>
              <strong>Claude asked</strong>
            </div>
            <div className="question-body">
              {questions.map((q, i) => (
                <div className="question-item" key={i}>
                  <div className="question-text">{q.question}</div>
                </div>
              ))}
            </div>
          </div>
        );
      }
      return <MessageBubble message={msg} />;
    },
    [activeSessionId, approveTool, denyTool, answerQuestion, pendingQuestions]
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

  // Global Esc handler — interrupt the current turn whether or not the
  // textarea is focused. Skip when typing in another input (e.g. Schedules
  // form) so we don't hijack their native Esc behavior.
  useEffect(() => {
    if (!activeSessionId || !isRunning) return;
    const onEsc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      const inChatInput =
        tag === "TEXTAREA" &&
        (e.target as HTMLElement).closest(".chat-input-bar") !== null;
      const inOtherField =
        (tag === "INPUT" || tag === "TEXTAREA") && !inChatInput;
      if (inOtherField) return;
      e.preventDefault();
      interrupt(activeSessionId);
    };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [activeSessionId, isRunning, interrupt]);

  const header = (
    <div className="chat-header">
      <button
        className="btn btn-menu"
        onClick={onToggleSidebar}
        aria-label="Toggle sidebar"
      >
        ☰
      </button>
      {activeSession && (
        <>
          <h3>{activeSession.name || "Session"}</h3>
          <span className={`status-badge status-${activeSession.status}`}>
            {activeSession.status}
          </span>
        </>
      )}
      <span className={`conn-status ${connected ? "on" : "off"}`}>
        {connected ? "Connected" : "Disconnected"}
      </span>
    </div>
  );

  if (!activeSessionId) {
    return (
      <div className="chat-view">
        {header}
        <div className="chat-empty">
          <h2>Octopus</h2>
          <p>Create or select a session to start.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="chat-view">
      {header}

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

      {pendingQueue.length > 0 && (
        <div className="queue-list" aria-label="Queued messages">
          <div className="queue-list-label">
            Queued ({pendingQueue.length}) — will fire after the current turn
          </div>
          {pendingQueue.map((q, i) => (
            <div className="queue-item" key={i}>
              <span className="queue-dot">›</span>
              <span className="queue-content">{q}</span>
            </div>
          ))}
        </div>
      )}

      <div className="chat-input-bar">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            isRunning
              ? "Send to queue, or press Esc to interrupt…"
              : "Send a message..."
          }
          rows={1}
        />
        <button
          className="btn btn-send"
          onClick={handleSend}
          disabled={!input.trim()}
        >
          {isRunning ? "Queue" : "Send"}
        </button>
      </div>
    </div>
  );
}
