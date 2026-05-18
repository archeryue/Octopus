import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  IconArrowUp,
  IconFile,
  IconMenu2,
  IconPaperclip,
  IconPlayerStop,
  IconX,
} from "@tabler/icons-react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import {
  useSessionStore,
  type AttachmentMetadata,
  type Message,
  type PendingQuestion,
} from "../stores/sessionStore";
import { MessageBubble } from "./MessageBubble";
import { QuestionPrompt, type AnswerPayload } from "./QuestionPrompt";
import { ToolApproval } from "./ToolApproval";
import { Button } from "./ui/button";

const EMPTY_MESSAGES: Message[] = [];

// Mirror of server/attachments.py MAX_ATTACHMENTS_PER_MESSAGE. Kept as a
// constant rather than a contract type because it's a UX cap (so we can
// disable the file picker once full), not a wire shape.
const MAX_ATTACHMENTS_PER_MESSAGE = 10;
const MAX_FILE_BYTES = 25 * 1024 * 1024;

// A file the user picked but hasn't sent yet. Lives in the composer
// only — once the WS send_message fires, the server's user_message
// broadcast carries the AttachmentMetadata into the chat history.
interface PendingAttachment {
  // Stable id used as React key + remove-target. Independent from the
  // server-assigned attachment id (which only exists after upload).
  uid: string;
  file: File;
  status: "uploading" | "ready" | "error";
  meta?: AttachmentMetadata;
  error?: string;
}

interface Props {
  sendMessage: (
    sessionId: string,
    content: string,
    attachmentIds?: string[]
  ) => void;
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
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  // Counter for nested dragenter/dragleave events — without this the
  // overlay flickers as the cursor crosses child elements (textarea,
  // chips, etc.) because each crossing fires a leave on the parent.
  const dragCounter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const messagesMap = useSessionStore((s) => s.messages);
  const messages = activeSessionId ? (messagesMap[activeSessionId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES;
  const sessions = useSessionStore((s) => s.sessions);
  const archivedSessions = useSessionStore((s) => s.archivedSessions);
  const activeSession = useMemo(
    () =>
      sessions.find((s) => s.id === activeSessionId) ??
      archivedSessions.find((s) => s.id === activeSessionId),
    [sessions, archivedSessions, activeSessionId]
  );
  const isArchived = !!activeSession?.archived;
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
      return <MessageBubble message={msg} sessionId={activeSessionId ?? ""} />;
    },
    [activeSessionId, approveTool, denyTool, answerQuestion, pendingQuestions]
  );

  // ---- attachments: upload, paste, drop, picker, chips ------------------

  const uploadOne = useCallback(
    async (sessionId: string, file: File): Promise<AttachmentMetadata> => {
      const token = useSessionStore.getState().token;
      const form = new FormData();
      form.append("file", file, file.name);
      const res = await fetch(
        `${window.location.origin}/api/sessions/${encodeURIComponent(
          sessionId
        )}/attachments`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
          body: form,
        }
      );
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `upload failed (${res.status})`);
      }
      return (await res.json()) as AttachmentMetadata;
    },
    []
  );

  const addFiles = useCallback(
    (files: File[]) => {
      if (!activeSessionId || files.length === 0) return;
      // Cap total at server's MAX_ATTACHMENTS_PER_MESSAGE; truncate the
      // overflow rather than rejecting so the user gets *some* attached.
      const room = MAX_ATTACHMENTS_PER_MESSAGE - pendingAttachments.length;
      if (room <= 0) return;
      const accepted = files.slice(0, room).filter((f) => {
        if (f.size > MAX_FILE_BYTES) {
          // eslint-disable-next-line no-console
          console.warn(`Attachment ${f.name} exceeds 25MB cap, skipping`);
          return false;
        }
        return true;
      });
      const newPending: PendingAttachment[] = accepted.map((file) => ({
        uid: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        file,
        status: "uploading",
      }));
      setPendingAttachments((prev) => [...prev, ...newPending]);
      // Kick off uploads in parallel; each one mutates its own slot.
      for (const p of newPending) {
        uploadOne(activeSessionId, p.file)
          .then((meta) => {
            setPendingAttachments((prev) =>
              prev.map((x) =>
                x.uid === p.uid ? { ...x, status: "ready", meta } : x
              )
            );
          })
          .catch((err: Error) => {
            setPendingAttachments((prev) =>
              prev.map((x) =>
                x.uid === p.uid
                  ? { ...x, status: "error", error: err.message }
                  : x
              )
            );
          });
      }
    },
    [activeSessionId, pendingAttachments.length, uploadOne]
  );

  const removeAttachment = useCallback((uid: string) => {
    setPendingAttachments((prev) => prev.filter((x) => x.uid !== uid));
  }, []);

  const handlePaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      // Don't preventDefault on text pastes — only intercept when files
      // are present (clipboard images, copied files).
      const files = Array.from(e.clipboardData?.files ?? []);
      if (files.length === 0) return;
      e.preventDefault();
      addFiles(files);
    },
    [addFiles]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      dragCounter.current = 0;
      setIsDragOver(false);
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length > 0) addFiles(files);
    },
    [addFiles]
  );

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    // Only react to drags that actually carry files, otherwise we'd
    // flash the overlay when the user is just rearranging text.
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    dragCounter.current += 1;
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    dragCounter.current -= 1;
    if (dragCounter.current <= 0) {
      dragCounter.current = 0;
      setIsDragOver(false);
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }, []);

  const hasUploadInFlight = pendingAttachments.some(
    (a) => a.status === "uploading"
  );
  const readyAttachmentIds = pendingAttachments
    .filter((a) => a.status === "ready" && a.meta)
    .map((a) => a.meta!.id);

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

  const handleSend = async () => {
    if (!activeSessionId) return;
    const trimmed = input.trim();
    // Allow attachment-only turns — if the user attached files and
    // didn't type anything, we still want to send.
    if (!trimmed && readyAttachmentIds.length === 0) return;
    // Block while uploads are still in flight so we don't drop attachments
    // the user just added but the server hasn't finished receiving.
    if (hasUploadInFlight) return;

    // /archive command — intercept client-side. Hides the current
    // session and seamlessly swaps the active id to a fresh session
    // with the same name/working_dir/credential_id. The user sees an
    // empty chat under the same sidebar entry.
    if (trimmed.toLowerCase() === "/archive") {
      setInput("");
      try {
        const token = useSessionStore.getState().token;
        const res = await fetch(
          `${window.location.origin}/api/sessions/${activeSessionId}/archive`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${token}` },
          }
        );
        if (!res.ok) return;
        const fresh = await res.json();
        // The `session_archived` WS broadcast also updates the
        // sessions list for any other clients — and may have already
        // landed in this tab before the HTTP response. Dedupe by id
        // on both removal AND insertion so the two paths converge to
        // the same list regardless of arrival order.
        const store = useSessionStore.getState();
        const next = store.sessions.filter(
          (s) => s.id !== activeSessionId && s.id !== fresh.id
        );
        next.push(fresh);
        store.setSessions(next);
        store.setActiveSessionId(fresh.id);
        store.setMessages(fresh.id, []);
        store.setPendingQueue(fresh.id, []);
        store.setPendingQuestions(fresh.id, []);
      } catch {
        // best-effort — failure leaves the user in the original session
      }
      return;
    }

    sendMessage(
      activeSessionId,
      trimmed,
      readyAttachmentIds.length > 0 ? readyAttachmentIds : undefined
    );
    setInput("");
    // Drop both ready uploads (they're now associated with the message
    // we just sent) and any failed uploads (the user can retry by
    // re-attaching). Leave in-flight uploads alone — addFiles already
    // prevents that case by blocking send while one is pending.
    setPendingAttachments([]);
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
    <div className="chat-header flex items-center gap-3 px-4 h-12 shrink-0 border-b border-border bg-sidebar">
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
    <div
      className="chat-view flex-1 flex flex-col min-h-0 relative"
      onDragEnter={isArchived ? undefined : handleDragEnter}
      onDragLeave={isArchived ? undefined : handleDragLeave}
      onDragOver={isArchived ? undefined : handleDragOver}
      onDrop={isArchived ? undefined : handleDrop}
    >
      {header}

      {isDragOver && !isArchived && (
        <div className="chat-drop-overlay absolute inset-0 z-20 flex items-center justify-center bg-primary/10 border-2 border-dashed border-primary/60 pointer-events-none">
          <div className="rounded-lg bg-background/90 px-6 py-4 text-center shadow-lg">
            <IconPaperclip
              size={24}
              className="mx-auto mb-1 text-primary"
              aria-hidden
            />
            <div className="text-sm font-medium text-foreground">
              Drop files to attach
            </div>
            <div className="text-xs text-muted-foreground mt-0.5">
              Up to {MAX_ATTACHMENTS_PER_MESSAGE} files, 25 MB each
            </div>
          </div>
        </div>
      )}

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

      {isArchived ? (
        <div className="chat-archived-banner px-4 py-3 border-t border-border bg-muted/40 text-sm text-muted-foreground flex items-center justify-between gap-3 shrink-0">
          <span>
            This session is <strong className="text-foreground">archived</strong>{" "}
            — viewing read-only history. Unarchive from the sidebar to
            continue the conversation.
          </span>
        </div>
      ) : (
        <div className="chat-input-bar px-4 py-1.5 bg-background shrink-0">
          {/* Rounded card containing chips + textarea + bottom action row.
              Layout copied from VM0 (zero-composer) but tuned shorter
              for Octopus' chat panel: the textarea auto-grows with
              content (field-sizing-content) so the empty composer is a
              single comfortable line, not a hero-sized block. */}
          <div className="zero-composer overflow-hidden rounded-xl border-[0.7px] border-gray-400 bg-card shadow-sm focus-within:border-primary/70 focus-within:ring-[3px] focus-within:ring-primary/10 transition-colors">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              data-testid="attachment-file-input"
              onChange={(e) => {
                const files = Array.from(e.target.files ?? []);
                addFiles(files);
                // Reset so picking the same file twice in a row still fires.
                e.target.value = "";
              }}
            />

            {pendingAttachments.length > 0 && (
              <div
                className="chat-attachment-chips flex flex-wrap gap-2 px-3 pt-2"
                aria-label="Attachments"
              >
                {pendingAttachments.map((p) => (
                  <div
                    key={p.uid}
                    className={`attachment-pending inline-flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs ${
                      p.status === "error"
                        ? "border-destructive/60 bg-destructive/5 text-destructive"
                        : "border-border bg-muted/40 text-foreground"
                    }`}
                    title={
                      p.status === "error"
                        ? p.error || "upload failed"
                        : p.file.name
                    }
                  >
                    <IconFile
                      size={14}
                      className="text-muted-foreground shrink-0"
                    />
                    <span className="font-medium truncate max-w-[10rem]">
                      {p.file.name}
                    </span>
                    {p.status === "uploading" && (
                      <span className="text-muted-foreground">uploading…</span>
                    )}
                    {p.status === "error" && (
                      <span className="text-destructive">failed</span>
                    )}
                    <button
                      type="button"
                      className="attachment-remove text-muted-foreground hover:text-foreground"
                      aria-label={`Remove ${p.file.name}`}
                      onClick={() => removeAttachment(p.uid)}
                    >
                      <IconX size={12} />
                    </button>
                  </div>
                ))}
              </div>
            )}

            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              placeholder={
                isRunning
                  ? "Send to queue, or press Esc to interrupt…"
                  : "Send a message…"
              }
              rows={1}
              // field-sizing-content makes the textarea grow with its
              // content (Tailwind v4 / native CSS), so the empty state is
              // one comfortable line and we don't need JS auto-grow.
              // max-h caps growth before it eats the chat.
              className="w-full resize-none field-sizing-content bg-transparent px-3 pt-2 pb-0 text-sm text-foreground placeholder:text-muted-foreground/50 border-0 outline-none focus:outline-none focus:ring-0 max-h-48"
            />

            <div className="composer-actions flex items-center justify-between gap-2 px-1.5 pb-1.5">
              <div className="flex items-center gap-1 text-muted-foreground">
                <button
                  type="button"
                  className="btn btn-attach inline-flex items-center justify-center size-8 rounded-lg transition-colors duration-200 hover:bg-accent hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
                  aria-label="Attach file"
                  title="Attach file"
                  disabled={
                    pendingAttachments.length >= MAX_ATTACHMENTS_PER_MESSAGE
                  }
                  onClick={() => fileInputRef.current?.click()}
                >
                  <IconPaperclip size={16} stroke={1.5} />
                </button>
              </div>
              <div className="flex items-center gap-1">
                {isRunning ? (
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    className="btn btn-stop rounded-lg h-8 w-8 p-0 shrink-0"
                    onClick={() =>
                      activeSessionId && interrupt(activeSessionId)
                    }
                    aria-label="Stop"
                    title="Stop (Esc)"
                  >
                    <IconPlayerStop size={14} />
                  </Button>
                ) : null}
                <Button
                  type="button"
                  size="sm"
                  className="btn btn-send rounded-lg h-8 w-8 p-0 shrink-0"
                  onClick={handleSend}
                  disabled={
                    hasUploadInFlight ||
                    (!input.trim() && readyAttachmentIds.length === 0)
                  }
                  aria-label={isRunning ? "Queue message" : "Send message"}
                  title={isRunning ? "Queue (current turn is running)" : "Send"}
                >
                  <IconArrowUp size={14} />
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
