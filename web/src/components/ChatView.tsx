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
  type SessionInfo,
} from "../stores/sessionStore";
import { ForkDialog } from "./ForkDialog";
import { ResearchCard } from "./ResearchCard";
import { MessageBubble } from "./MessageBubble";
import { QuestionPrompt, type AnswerPayload } from "./QuestionPrompt";
import { ToolApproval } from "./ToolApproval";
import {
  SlashCommandMenu,
  buildRememberPrompt,
  filterSlashCommands,
  type SlashCommand,
} from "./SlashCommandMenu";
import { Button } from "./ui/button";
import { isSessionBusy } from "../lib/deferredFork";

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
  // Open the all-agents Schedules overview (App owns the dialog). Used by the
  // bare `/schedule` command.
  onOpenSchedules: () => void;
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
  onOpenSchedules,
}: Props) {
  const [input, setInput] = useState("");
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  // Fork dialog (session-tree-rewind.md §6.1-6.2). `null` = closed; `{}` = the
  // /fork picker; `{ seq }` = the per-message "Fork from here" confirm step.
  const [forkDialog, setForkDialog] = useState<{ seq?: number } | null>(null);
  // Slash-command autocomplete: which item is highlighted, and whether the
  // user dismissed the menu with Esc (re-opens as soon as they type again).
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
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
  const agents = useSessionStore((s) => s.agents);
  const activeSession = useMemo(
    () =>
      sessions.find((s) => s.id === activeSessionId) ??
      archivedSessions.find((s) => s.id === activeSessionId),
    [sessions, archivedSessions, activeSessionId]
  );
  const activeAgent = useMemo(
    () => agents.find((a) => a.id === activeSession?.agent_id),
    [agents, activeSession?.agent_id]
  );
  // Display name for assistant attribution throughout the chat. Harness-
  // neutral fallback when the session has no owning agent.
  const agentLabel = activeAgent?.name || "Assistant";
  const isArchived = !!activeSession?.archived;
  const pendingQueueMap = useSessionStore((s) => s.pendingQueue);
  const pendingQueue = activeSessionId
    ? (pendingQueueMap[activeSessionId] ?? EMPTY_QUEUE)
    : EMPTY_QUEUE;
  const pendingQuestionsMap = useSessionStore((s) => s.pendingQuestions);
  const pendingQuestions = activeSessionId
    ? (pendingQuestionsMap[activeSessionId] ?? EMPTY_QUESTIONS)
    : EMPTY_QUESTIONS;
  const pendingForksMap = useSessionStore((s) => s.pendingForks);
  // Forks whose POST is currently in flight — guards the watcher against
  // double-firing the same deferred fork across re-renders.
  const forksInFlight = useRef<Set<string>>(new Set());
  // Bounded backoff for the 409-race retry path: per-session attempt count +
  // outstanding retry timers, and a tick that re-runs the watcher when a
  // backoff elapses (the frontend may see the session as idle while the
  // backend is still briefly busy for a reason isSessionBusy can't see — an
  // active delegation, another tab's fork — so we can't rely on a store change
  // to re-trigger). See the deferred-fork watcher below.
  const forkAttempts = useRef<Map<string, number>>(new Map());
  const forkRetryTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map()
  );
  const [forkRetryTick, setForkRetryTick] = useState(0);
  // Drop a session's deferred-fork backoff state (pending timer + attempt
  // count) — called whenever its intent is resolved or abandoned so a stale
  // timer can't re-fire it. Closes over stable refs; safe to recreate.
  const clearForkBackoff = (sid: string) => {
    const t = forkRetryTimers.current.get(sid);
    if (t !== undefined) {
      clearTimeout(t);
      forkRetryTimers.current.delete(sid);
    }
    forkAttempts.current.delete(sid);
  };
  const virtuosoRef = useRef<VirtuosoHandle>(null);

  // Scroll to the bottom when switching into a session whose history has
  // already loaded. `initialTopMostItemIndex` is captured at mount time, so
  // it doesn't help when messages arrive asynchronously after the click.
  const hasMessages = messages.length > 0;
  useEffect(() => {
    if (!activeSessionId || !hasMessages) return;
    virtuosoRef.current?.scrollToIndex({ index: "LAST", behavior: "auto" });
  }, [activeSessionId, hasMessages]);

  // Prefilled chat input on fork open (session-tree-rewind.md §6.1). When the
  // active session carries a non-null fork_prefilled_prompt (the rewound user
  // message text, set in fork_metadata until the first turn), populate the
  // input once. The ref keeps us from clobbering the user's edits, and once
  // they send, the backend clears fork_metadata so it won't re-prefill.
  const prefilledForRef = useRef<string | null>(null);
  useEffect(() => {
    const prompt = activeSession?.fork_prefilled_prompt;
    if (
      activeSessionId &&
      prompt &&
      prefilledForRef.current !== activeSessionId
    ) {
      prefilledForRef.current = activeSessionId;
      setInput(prompt);
    }
  }, [activeSessionId, activeSession?.fork_prefilled_prompt]);

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
              <strong>{agentLabel} asked</strong>
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
      return (
        <MessageBubble
          message={msg}
          sessionId={activeSessionId ?? ""}
          agentName={activeAgent?.name}
          agentAvatar={activeAgent?.avatar}
          onFork={(seq) => setForkDialog({ seq })}
        />
      );
    },
    [
      activeSessionId,
      approveTool,
      denyTool,
      answerQuestion,
      pendingQuestions,
      activeAgent?.name,
      activeAgent?.avatar,
      agentLabel,
    ]
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

  // Slash-command autocomplete state, derived from the current input.
  const slashCommands = useMemo(() => filterSlashCommands(input), [input]);
  const showSlashMenu = slashCommands.length > 0 && !slashDismissed;

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    // Any edit re-arms the menu and resets the highlight to the top match.
    setSlashIndex(0);
    setSlashDismissed(false);
  };

  // Complete a slash command into the composer: name + trailing space so the
  // caret lands in the args position and the menu hides (whitespace ends the
  // command-typing state). For no-arg commands the trailing space is trimmed
  // away by handleSend's matching.
  const completeSlash = (cmd: SlashCommand) => {
    setInput(`${cmd.name} `);
    setSlashIndex(0);
    setSlashDismissed(false);
    // Keep editing in the textarea with the caret at the end.
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.focus();
      const end = ta.value.length;
      ta.setSelectionRange(end, end);
    });
  };

  const handleSend = async () => {
    if (!activeSessionId) return;
    const trimmed = input.trim();
    // Allow attachment-only turns — if the user attached files and
    // didn't type anything, we still want to send.
    if (!trimmed && readyAttachmentIds.length === 0) return;
    // Block while uploads are still in flight so we don't drop attachments
    // the user just added but the server hasn't finished receiving.
    if (hasUploadInFlight) return;

    // /reset command — escape hatch for wedged sessions. Calls the
    // server's force-reset endpoint, which cancels the active task,
    // releases the lock, kills the backend subprocess, and clears any
    // pending questions/approvals. Use when interrupt failed or the
    // session is stuck in "Running" forever.
    if (trimmed.toLowerCase() === "/reset") {
      setInput("");
      try {
        const token = useSessionStore.getState().token;
        await fetch(
          `${window.location.origin}/api/sessions/${activeSessionId}/reset`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${token}` },
          }
        );
        // The server broadcasts a status=idle event; the WS handler
        // applies it. We also clear local pending state so the input
        // bar drops out of any "waiting on answer" UI immediately.
        const store = useSessionStore.getState();
        store.setPendingQuestions(activeSessionId, []);
        store.setPendingQueue(activeSessionId, []);
        // A reset is a recovery action — drop any deferred /fork for this
        // session so it doesn't fire once the reset lands it back at idle.
        store.clearPendingFork(activeSessionId);
        clearForkBackoff(activeSessionId);
      } catch {
        // best-effort — if the request itself fails the session stays as-is
      }
      return;
    }

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
        // The archived session is going away — drop any deferred /fork for it.
        store.clearPendingFork(activeSessionId);
        clearForkBackoff(activeSessionId);
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

    // /rewind — open the rewind picker (session-tree-rewind.md §6.2). Message
    // selection + confirmation happen in the dialog. (The underlying mechanism
    // is still a "fork"/branch internally; only the command name is /rewind.)
    if (trimmed.toLowerCase() === "/rewind") {
      setInput("");
      setForkDialog({});
      return;
    }

    // /fork [name] — duplicate the whole session onto an INDEPENDENT full copy
    // of its working dir (session-fork-copy.md). A consistent copy + resume
    // transcript needs a SETTLED session, so we can't fork mid-turn. Rather
    // than refuse, we ALWAYS record the intent and let the single watcher fire
    // it: immediately if idle, or once the session goes idle + its queue drains
    // if busy. Recording the intent (never POSTing directly here) is what makes
    // a 409 race recoverable — the watcher owns retry. Optional trailing name
    // overrides the default "<parent> (fork)".
    const lowerFork = trimmed.toLowerCase();
    if (lowerFork === "/fork" || lowerFork.startsWith("/fork ")) {
      setInput("");
      const store = useSessionStore.getState();
      const sid = activeSessionId;
      const label = trimmed.slice("/fork".length).trim() || undefined;
      const busy = isSessionBusy(
        activeSession?.status,
        pendingQueueMap[sid]?.length ?? 0,
        pendingQuestionsMap[sid]?.length ?? 0
      );
      store.setPendingFork(sid, label ?? null);
      store.addMessage(sid, {
        role: "system",
        type: "notice",
        content: busy
          ? "⑂ Fork queued — it'll run when this session goes idle."
          : "⑂ Forking — copying the working directory…",
      });
      return;
    }

    // /schedule command — natural-language scheduling. Bare `/schedule` opens
    // the overview; otherwise the whole line goes to the backend, which parses
    // it (explicit "30m …" instantly, else a one-shot AI parse using this
    // agent's Claude) into a recurrence + prompt and creates the schedule.
    // The browser timezone is sent so "9am" means the user's local 9am.
    // Confirmation / errors render as a local notice bubble.
    const lower = trimmed.toLowerCase();
    if (lower === "/schedule" || lower.startsWith("/schedule ")) {
      setInput("");
      const store = useSessionStore.getState();
      const sid = activeSessionId;
      const addNotice = (content: string, isError = false) =>
        store.addMessage(sid, { role: "system", type: "notice", content, is_error: isError });

      const args = trimmed.slice("/schedule".length).trim();
      if (!args) {
        onOpenSchedules();
        return;
      }
      // Echo the command and post a loading notice — the natural-language
      // parse is a one-shot model call and can take a second or two.
      store.addMessage(sid, { role: "user", type: "text", content: trimmed });
      addNotice(`📅 Scheduling “${args}”…`);

      const agentId = activeSession?.agent_id;
      if (!agentId) {
        addNotice("Couldn't schedule: this session has no owning agent.", true);
        return;
      }
      try {
        const res = await fetch(
          `${window.location.origin}/api/agents/${agentId}/schedules/from_text`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${store.token}`,
            },
            body: JSON.stringify({
              text: args,
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
              // Remember the originating session so each fire appends its run
              // into this conversation instead of a throwaway one.
              session_id: sid,
            }),
          }
        );
        if (!res.ok) {
          let detail = `Couldn't create schedule (HTTP ${res.status}).`;
          try {
            const body = await res.json();
            if (typeof body?.detail === "string") detail = body.detail;
          } catch {
            // keep the generic message
          }
          addNotice(detail, true);
          return;
        }
        const created = await res.json();
        store.setSchedules([...store.schedules, created]);
        addNotice(
          `📅 Scheduled "${created.name}" — ${created.recurrence_label}. Manage in Schedules.`
        );
      } catch {
        addNotice("Couldn't create schedule — network error.", true);
      }
      return;
    }

    // /research command — kick off an Octopus-native deep-research job
    // (native-deep-research.md §7). Returns immediately with a job id; the
    // progress shows in a ResearchCard and the cited report arrives as a turn.
    if (lower === "/research" || lower.startsWith("/research ")) {
      setInput("");
      const store = useSessionStore.getState();
      const sid = activeSessionId;
      const addNotice = (content: string, isError = false) =>
        store.addMessage(sid, { role: "system", type: "notice", content, is_error: isError });
      const question = trimmed.slice("/research".length).trim();
      if (!question) {
        addNotice("Usage: /research <question>", true);
        return;
      }
      store.addMessage(sid, { role: "user", type: "text", content: trimmed });
      addNotice(`🔎 Starting deep research: “${question}”…`);
      try {
        const res = await fetch(
          `${window.location.origin}/api/sessions/${sid}/research`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${store.token}`,
            },
            body: JSON.stringify({ question }),
          }
        );
        if (!res.ok) {
          let detail = `Couldn't start research (HTTP ${res.status}).`;
          try {
            const body = await res.json();
            if (typeof body?.detail === "string") detail = body.detail;
          } catch {
            /* keep generic */
          }
          addNotice(detail, true);
          return;
        }
        const job = await res.json();
        store.upsertResearch(sid, {
          id: job.id,
          session_id: sid,
          question,
          status: "running",
          phase: job.phase ?? "scope",
        });
      } catch {
        addNotice("Couldn't start research — network error.", true);
      }
      return;
    }

    // /remember command — persist a note to the agent's long-term memory.
    // Harness-agnostic: we send a memory-writing instruction as a normal
    // turn, and the session's agent (Claude or Codex) writes it using its
    // own file tools against the per-agent memory dir both harnesses share.
    if (lower === "/remember" || lower.startsWith("/remember ")) {
      const note = trimmed.slice("/remember".length).trim();
      if (!note) {
        useSessionStore.getState().addMessage(activeSessionId, {
          role: "system",
          type: "notice",
          content: "Usage: /remember <something to save to memory>",
          is_error: true,
        });
        setInput("");
        return;
      }
      sendMessage(activeSessionId, buildRememberPrompt(note));
      setInput("");
      return;
    }

    if (lower === "/showme" || lower.startsWith("/showme ")) {
      const text = trimmed.slice("/showme".length).trim();
      if (!text) {
        useSessionStore.getState().addMessage(activeSessionId, {
          role: "system",
          type: "notice",
          content: "Usage: /showme <file reference>",
          is_error: true,
        });
        setInput("");
        return;
      }

      // Immediate UX feedback: clear the composer, echo the command into
      // the transcript (so it doesn't look like Enter was missed), and
      // post a loading notice (the resolver call can take a second or two
      // when the model has to interpret a fuzzy reference).
      setInput("");
      const store = useSessionStore.getState();
      store.addMessage(activeSessionId, {
        role: "user",
        type: "text",
        content: trimmed,
      });
      store.addMessage(activeSessionId, {
        role: "system",
        type: "notice",
        content: `Looking for “${text}”…`,
      });

      try {
        const res = await fetch(
          `${window.location.origin}/api/sessions/${activeSessionId}/showme/resolve`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${store.token}`,
            },
            body: JSON.stringify({ text }),
          }
        );
        if (!res.ok) {
          let detail = `Couldn't resolve file reference (HTTP ${res.status}).`;
          try {
            const body = await res.json();
            if (typeof body?.detail === "string") detail = body.detail;
          } catch {
            // keep the generic message
          }
          useSessionStore.getState().addMessage(activeSessionId, {
            role: "system",
            type: "notice",
            content: detail,
            is_error: true,
          });
          return;
        }
        const data = await res.json();
        if (typeof data?.path === "string" && data.path) {
          useSessionStore.getState().openViewer(activeSessionId, data.path);
        } else {
          useSessionStore.getState().addMessage(activeSessionId, {
            role: "system",
            type: "notice",
            content:
              typeof data?.message === "string" && data.message
                ? data.message
                : "Couldn't resolve that file reference.",
            is_error: true,
          });
        }
      } catch {
        useSessionStore.getState().addMessage(activeSessionId, {
          role: "system",
          type: "notice",
          content: "Couldn't resolve file reference — network error.",
          is_error: true,
        });
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
    // When the slash-command menu is open it captures navigation keys:
    // arrows move the highlight, Enter/Tab complete, Esc dismisses (without
    // bubbling to the global Esc-to-interrupt handler).
    if (showSlashMenu) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex((i) => (i + 1) % slashCommands.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex(
          (i) => (i - 1 + slashCommands.length) % slashCommands.length
        );
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const cmd = slashCommands[Math.min(slashIndex, slashCommands.length - 1)];
        if (cmd) completeSlash(cmd);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setSlashDismissed(true);
        return;
      }
    }
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

  // Delegation child sessions get a small banner under the header that
  // names the parent agent + session and offers a "Open parent" link.
  // The parent_session_id field is set by DelegationManager when the
  // child Session row is created (agent-collaboration.md §4.1).
  const parentSession = useMemo(
    () =>
      activeSession?.parent_session_id
        ? sessions.find((s) => s.id === activeSession.parent_session_id) ??
          archivedSessions.find(
            (s) => s.id === activeSession.parent_session_id
          )
        : undefined,
    [activeSession?.parent_session_id, sessions, archivedSessions]
  );
  const parentAgent = useMemo(
    () => agents.find((a) => a.id === parentSession?.agent_id),
    [agents, parentSession?.agent_id]
  );
  const openParent = () => {
    if (!parentSession) return;
    const store = useSessionStore.getState();
    if (parentSession.agent_id)
      store.setActiveAgentId(parentSession.agent_id);
    store.setActiveSessionId(parentSession.id);
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
          {activeAgent && (
            <span
              className="chat-agent inline-flex items-center gap-1.5 text-sm text-muted-foreground shrink-0"
              title={`Agent: ${activeAgent.name}`}
            >
              <span aria-hidden className="text-base leading-none">
                {activeAgent.avatar || "🐙"}
              </span>
              <span className="hidden sm:inline truncate max-w-[10rem]">
                {activeAgent.name}
              </span>
              <span aria-hidden className="text-border">
                /
              </span>
            </span>
          )}
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

  // Fork sessions get a banner mirroring the delegation one
  // (session-tree-rewind.md §6.4): "Forked from <parent> at message N".
  // The parent may be deleted (dangling ref) → "(deleted session)".
  const forkParentSession = useMemo(
    () =>
      activeSession?.forked_from_session_id
        ? sessions.find((s) => s.id === activeSession.forked_from_session_id) ??
          archivedSessions.find(
            (s) => s.id === activeSession.forked_from_session_id
          )
        : undefined,
    [activeSession?.forked_from_session_id, sessions, archivedSessions]
  );
  const openForkParent = () => {
    if (!forkParentSession) return;
    const store = useSessionStore.getState();
    if (forkParentSession.agent_id)
      store.setActiveAgentId(forkParentSession.agent_id);
    store.setActiveSessionId(forkParentSession.id);
  };

  // A fork was just created: add it to the store, switch to it (the prefilled
  // input effect populates the composer from fork_prefilled_prompt), and load
  // its copied history. (session-tree-rewind.md §6.1)
  const handleForked = async (fork: SessionInfo) => {
    const store = useSessionStore.getState();
    // A fork is a rewind: it replaces its parent, which the backend archives.
    // Drop the parent from the live list here too so the swap is correct on
    // this tab regardless of when the `session_archived` broadcast lands.
    const parentId = fork.forked_from_session_id ?? null;
    const parent = parentId
      ? store.sessions.find((s) => s.id === parentId)
      : undefined;
    const next = store.sessions.filter(
      (s) => s.id !== fork.id && s.id !== parentId
    );
    next.push(fork);
    store.setSessions(next);
    // Keep the (now-archived) parent in archivedSessions so the fork banner can
    // still resolve its name and the "open parent" button works — otherwise it
    // renders "(deleted session)".
    if (parent) {
      const rest = store.archivedSessions.filter((s) => s.id !== parent.id);
      store.setArchivedSessions([{ ...parent, archived: true }, ...rest]);
    }
    if (fork.agent_id) store.setActiveAgentId(fork.agent_id);
    store.setActiveSessionId(fork.id);
    try {
      const res = await fetch(`${window.location.origin}/api/sessions/${fork.id}`, {
        headers: { Authorization: `Bearer ${store.token}` },
      });
      if (res.ok) {
        const data = await res.json();
        store.setMessages(fork.id, data.messages || []);
        store.setPendingQueue(fork.id, []);
        store.setPendingQuestions(fork.id, []);
        if (typeof data.next_message_seq === "number")
          store.setLastAppliedSeq(fork.id, data.next_message_seq - 1);
      }
    } catch {
      /* ignore — the session is selected regardless */
    }
  };

  // A /fork duplicate was just created (session-fork-copy.md): unlike a rewind,
  // the parent is UNTOUCHED, so we only add the new session to the list. When
  // `switchTo` (the foreground case — the user just typed /fork here), we also
  // make it active and load its carried-over history; when false (a deferred
  // fork firing while the user is reading elsewhere) we add it quietly and post
  // a notice on the parent instead of yanking the view away.
  const handleDuplicated = async (
    fork: SessionInfo,
    { switchTo }: { switchTo: boolean } = { switchTo: true }
  ) => {
    const store = useSessionStore.getState();
    const next = store.sessions.filter((s) => s.id !== fork.id);
    next.push(fork);
    store.setSessions(next);
    if (!switchTo) {
      const parentId = fork.forked_from_session_id;
      if (parentId)
        store.addMessage(parentId, {
          role: "system",
          type: "notice",
          content: `⑂ Fork ready — “${fork.name}”. Open it from the sidebar.`,
        });
      return;
    }
    if (fork.agent_id) store.setActiveAgentId(fork.agent_id);
    store.setActiveSessionId(fork.id);
    try {
      const res = await fetch(`${window.location.origin}/api/sessions/${fork.id}`, {
        headers: { Authorization: `Bearer ${store.token}` },
      });
      if (res.ok) {
        const data = await res.json();
        store.setMessages(fork.id, data.messages || []);
        store.setPendingQueue(fork.id, []);
        store.setPendingQuestions(fork.id, []);
        if (typeof data.next_message_seq === "number")
          store.setLastAppliedSeq(fork.id, data.next_message_seq - 1);
      }
    } catch {
      /* ignore — the session is selected regardless */
    }
  };

  // POST the duplicate for `sid` and open it. Returns "ok" | "retry" | "fail".
  // "retry" means the backend's idle-guard rejected us (a turn / delegation
  // slipped in between our check and the POST) — the caller keeps the pending
  // fork and schedules a retry. `switchTo` is false for the deferred path
  // (don't steal focus). Sole caller is the watcher.
  const runFork = async (
    sid: string,
    label: string | undefined,
    { switchTo }: { switchTo: boolean }
  ): Promise<"ok" | "retry" | "fail"> => {
    const store = useSessionStore.getState();
    const addNotice = (content: string, isError = false) =>
      store.addMessage(sid, { role: "system", type: "notice", content, is_error: isError });
    try {
      const res = await fetch(
        `${window.location.origin}/api/sessions/${sid}/duplicate`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${store.token}`,
          },
          body: JSON.stringify({ label }),
        }
      );
      if (!res.ok) {
        let reason = "";
        let detail = `Couldn't fork (HTTP ${res.status}).`;
        try {
          const body = await res.json();
          const d = body?.detail;
          if (typeof d === "string") detail = d;
          else if (d && typeof d === "object") {
            reason = d.reason ?? "";
            if (typeof d.message === "string") detail = d.message;
          }
        } catch {
          /* keep the generic message */
        }
        if (res.status === 409 && reason === "fork_blocked_parent_turn_active")
          return "retry";
        store.clearPendingFork(sid);
        addNotice(detail, true);
        return "fail";
      }
      const fork = await res.json();
      store.clearPendingFork(sid);
      await handleDuplicated(fork, { switchTo });
      return "ok";
    } catch {
      // Network error — don't strand a deferred fork forever; surface it.
      store.clearPendingFork(sid);
      addNotice("Couldn't fork — network error.", true);
      return "fail";
    }
  };

  // Deferred-fork watcher (session-fork-copy.md): the SOLE executor of every
  // /fork — the command handler only records the intent in `pendingForks`, and
  // this fires it the moment its session is idle AND its queue / question
  // prompts drain. Re-runs whenever a session's status / queue changes, or a
  // backoff tick elapses. `forksInFlight` prevents double-firing across renders
  // and effect re-runs while the POST is in flight. A "retry" (the backend
  // idle-guard rejected a race the frontend couldn't see) schedules a bounded
  // backoff re-attempt; after MAX_FORK_RETRIES we give up with a notice rather
  // than spin or strand the intent forever.
  useEffect(() => {
    const MAX_FORK_RETRIES = 6;
    for (const sid of Object.keys(pendingForksMap)) {
      if (forksInFlight.current.has(sid)) continue;
      // Honor an in-progress backoff: while a retry timer is pending, an
      // unrelated re-render must NOT jump the cooldown and POST early (Vera
      // review). The timer's own callback re-runs this effect when it elapses.
      if (forkRetryTimers.current.has(sid)) continue;
      const sess = sessions.find((s) => s.id === sid);
      if (!sess) {
        // The parent vanished (archived/deleted) before the fork could run.
        useSessionStore.getState().clearPendingFork(sid);
        clearForkBackoff(sid);
        continue;
      }
      const busy = isSessionBusy(
        sess.status,
        pendingQueueMap[sid]?.length ?? 0,
        pendingQuestionsMap[sid]?.length ?? 0
      );
      if (busy) continue;
      forksInFlight.current.add(sid);
      const label = pendingForksMap[sid].label ?? undefined;
      const switchTo = useSessionStore.getState().activeSessionId === sid;
      void runFork(sid, label, { switchTo })
        .then((result) => {
          if (result === "retry") {
            // The intent may have been cleared (e.g. /reset or /archive) while
            // this in-flight POST was resolving — don't schedule a retry for a
            // fork nobody's waiting on (Vera review nit).
            if (!useSessionStore.getState().pendingForks[sid]) {
              clearForkBackoff(sid);
              return;
            }
            const n = (forkAttempts.current.get(sid) ?? 0) + 1;
            if (n > MAX_FORK_RETRIES) {
              clearForkBackoff(sid);
              useSessionStore.getState().clearPendingFork(sid);
              useSessionStore.getState().addMessage(sid, {
                role: "system",
                type: "notice",
                content:
                  "Couldn't fork — the session stayed busy. Try /fork again.",
                is_error: true,
              });
              return;
            }
            forkAttempts.current.set(sid, n);
            const delay = Math.min(8000, 400 * 2 ** n);
            const t = setTimeout(() => {
              // Only retire the timer if it's still the one we scheduled (a
              // later resolution may have replaced/cleared it).
              if (forkRetryTimers.current.get(sid) === t)
                forkRetryTimers.current.delete(sid);
              setForkRetryTick((v) => v + 1); // re-run this effect
            }, delay);
            forkRetryTimers.current.set(sid, t);
          } else {
            clearForkBackoff(sid); // ok / hard-fail → reset backoff
          }
        })
        .finally(() => forksInFlight.current.delete(sid));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingForksMap, sessions, pendingQueueMap, pendingQuestionsMap, forkRetryTick]);

  // Clear any outstanding fork-retry timers on unmount.
  useEffect(() => {
    const timers = forkRetryTimers.current;
    return () => timers.forEach((t) => clearTimeout(t));
  }, []);

  const forkBanner =
    activeSession?.forked_from_session_id != null ? (
      <div
        className="chat-fork-banner flex items-center gap-2 px-4 py-1.5 shrink-0 border-b border-border bg-primary/5 text-xs text-muted-foreground"
        data-testid="fork-banner"
      >
        <span className="text-primary" aria-hidden>
          ⑂
        </span>
        <span>
          Forked from{" "}
          <span className="font-medium text-foreground">
            {forkParentSession?.name ?? "(deleted session)"}
          </span>
          {activeSession.fork_is_full_copy ? (
            <>{" "}— full copy of the working dir</>
          ) : (
            <>
              {" "}at message{" "}
              <strong>{(activeSession.fork_after_seq ?? -1) + 1}</strong>
            </>
          )}
        </span>
        {forkParentSession && (
          <button
            type="button"
            onClick={openForkParent}
            className="btn-open-fork-parent ml-auto text-primary hover:underline"
          >
            back to parent →
          </button>
        )}
      </div>
    ) : null;

  const delegationBanner =
    activeSession?.parent_session_id ? (
      <div
        className="chat-delegation-banner flex items-center gap-2 px-4 py-1.5 shrink-0 border-b border-border bg-primary/5 text-xs text-muted-foreground"
        data-testid="delegation-banner"
      >
        <span className="text-primary">↑</span>
        <span>
          Delegated from{" "}
          <span className="font-medium text-foreground">
            {parentAgent?.name || "another agent"}
          </span>
          {parentSession && (
            <>
              {" "}(session{" "}
              <code className="font-mono text-[10px]">
                {parentSession.id.slice(0, 8)}
              </code>
              )
            </>
          )}
        </span>
        {parentSession && (
          <button
            type="button"
            onClick={openParent}
            className="btn-open-parent ml-auto text-primary hover:underline"
          >
            Open parent →
          </button>
        )}
      </div>
    ) : null;

  if (!activeSessionId) {
    return (
      <div className="chat-view flex-1 flex flex-col min-h-0">
        {header}
        {delegationBanner}
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
      {forkBanner}
      {delegationBanner}

      {forkDialog && activeSession && (
        <ForkDialog
          sessionId={activeSession.id}
          parentName={activeSession.name || "Session"}
          initialSeq={forkDialog.seq}
          onClose={() => setForkDialog(null)}
          onForked={handleForked}
        />
      )}

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
          {agentLabel} is waiting for your response
        </div>
      )}

      {activeSessionId && (
        <div className="research-card-wrap shrink-0 px-4 empty:hidden">
          <ResearchCard sessionId={activeSessionId} />
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
          {/* `relative` anchors the slash-command menu, which floats just
              above the composer (bottom-full) like the Claude Code CLI. */}
          <div className="relative">
          {showSlashMenu && (
            <SlashCommandMenu
              commands={slashCommands}
              activeIndex={slashIndex}
              onSelect={completeSlash}
              onHoverIndex={setSlashIndex}
              className="absolute inset-x-0 bottom-full mb-2"
            />
          )}
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
              ref={textareaRef}
              value={input}
              onChange={handleInputChange}
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
        </div>
      )}
    </div>
  );
}
