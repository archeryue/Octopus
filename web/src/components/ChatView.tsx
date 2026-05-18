import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { IconMenu2 } from "@tabler/icons-react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import {
  useSessionStore,
  type Message,
  type PendingQuestion,
} from "../stores/sessionStore";
import { MessageBubble } from "./MessageBubble";
import { QuestionPrompt, type AnswerPayload } from "./QuestionPrompt";
import { ToolApproval } from "./ToolApproval";
import { Button } from "./ui/button";

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
          <div className="msg msg-question msg-question-done rounded-lg border-[0.7px] border-dashed border-border bg-muted/30 overflow-hidden opacity-75">
            <div className="question-header flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground">
              <span aria-hidden className="text-xs">?</span>
              <strong>Claude asked</strong>
            </div>
            <div className="question-body px-3 pb-3 space-y-1">
              {questions.map((q, i) => (
                <div className="question-item" key={i}>
                  <div className="question-text text-sm text-foreground leading-relaxed">
                    {q.question}
                  </div>
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
        <div className="msg msg-loading flex items-center gap-1.5 px-3 py-2">
          <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse [animation-delay:-0.32s]" />
          <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse [animation-delay:-0.16s]" />
          <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse" />
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

  const renderStatusBadge = (status: string | undefined) => {
    const base =
      "status-badge inline-flex items-center gap-1.5 text-[11px] font-medium px-2.5 py-1 rounded-full";
    if (status === "running") {
      return (
        <span className={`${base} status-running bg-primary-50 text-primary-700`}>
          <span className="inline-block size-1.5 rounded-full bg-primary animate-pulse" />
          Running
        </span>
      );
    }
    if (status === "waiting_approval") {
      return (
        <span className={`${base} status-waiting_approval bg-yellow-50 text-yellow-700`}>
          <span className="inline-block size-1.5 rounded-full bg-yellow-500" />
          Waiting
        </span>
      );
    }
    // Idle: subtle text-only label — kept in DOM as a test hook + low-key cue.
    return (
      <span className={`${base} status-idle text-muted-foreground/70`}>
        Idle
      </span>
    );
  };

  const header = (
    <div className="chat-header flex items-center gap-3 px-4 h-12 shrink-0 border-b border-border bg-card">
      <button
        className="btn btn-menu inline-flex items-center justify-center size-9 rounded-lg text-foreground hover:bg-accent md:hidden"
        onClick={onToggleSidebar}
        aria-label="Toggle sidebar"
      >
        <IconMenu2 size={18} />
      </button>
      {activeSession && (
        <>
          <h3 className="text-[15px] font-semibold text-foreground truncate">
            {activeSession.name || "Session"}
          </h3>
          {renderStatusBadge(activeSession.status)}
        </>
      )}
      <span
        className={`conn-status ${
          connected ? "on" : "off"
        } ml-auto inline-flex items-center gap-2 text-xs text-muted-foreground`}
        title={connected ? "Connected" : "Disconnected"}
      >
        <span
          className={`inline-block size-2 rounded-full ${
            connected ? "bg-green-500" : "bg-destructive animate-pulse"
          }`}
        />
        {connected ? "Connected" : "Disconnected"}
      </span>
    </div>
  );

  if (!activeSessionId) {
    return (
      <div className="chat-view flex-1 flex flex-col min-h-0">
        {header}
        <div className="chat-empty flex-1 flex flex-col items-center justify-center text-muted-foreground gap-3">
          <h2 className="text-3xl font-bold text-primary tracking-tight">Octopus</h2>
          <p className="text-sm leading-relaxed">Create or select a session to start.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="chat-view flex-1 flex flex-col min-h-0">
      {header}

      <Virtuoso
        ref={virtuosoRef}
        className="chat-messages flex-1 min-h-0"
        data={messages}
        itemContent={renderMessage}
        initialTopMostItemIndex={messages.length ? messages.length - 1 : 0}
        followOutput="smooth"
        increaseViewportBy={{ top: 400, bottom: 400 }}
        components={{ Footer: footer }}
      />

      {isWaitingForResponse && (
        <div className="waiting-hint shrink-0 px-4 py-1.5 text-center text-xs text-muted-foreground border-t border-border bg-muted/30">
          Claude is waiting for your response
        </div>
      )}

      {pendingQueue.length > 0 && (
        <div
          className="queue-list shrink-0 border-t border-border bg-muted/30 px-4 py-2 text-xs space-y-1"
          aria-label="Queued messages"
        >
          <div className="queue-list-label text-muted-foreground mb-2">
            Queued ({pendingQueue.length}) — will fire after the current turn
          </div>
          {pendingQueue.map((q, i) => (
            <div
              className="queue-item flex items-start gap-2 text-foreground"
              key={i}
            >
              <span className="queue-dot text-muted-foreground shrink-0">›</span>
              <span className="queue-content truncate">{q}</span>
            </div>
          ))}
        </div>
      )}

      <div className="chat-input-bar flex items-end gap-2 px-4 py-3 bg-background shrink-0">
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
          className="flex-1 min-h-[40px] max-h-40 resize-y rounded-lg border-[0.7px] border-gray-400 bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none transition-colors focus:border-primary focus:ring-[3px] focus:ring-primary/10"
        />
        <Button
          className="btn btn-send shrink-0"
          onClick={handleSend}
          disabled={!input.trim()}
        >
          {isRunning ? "Queue" : "Send"}
        </Button>
      </div>
    </div>
  );
}
