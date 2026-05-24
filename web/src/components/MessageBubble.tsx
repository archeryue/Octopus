import { useState } from "react";
import { IconChevronDown, IconChevronRight, IconTool, IconFile, IconRobot } from "@tabler/icons-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  useSessionStore,
  type AttachmentMetadata,
  type Message,
} from "../stores/sessionStore";
import { BgTaskChip } from "./BgTaskChip";

// Marker the backend prepends to the synthesized user message it
// injects when a bg task completes. Used to render those messages
// with a distinct "auto" badge so the user knows they didn't type it.
// Source of truth: server/bg_tasks.py render_delivery_prompt.
const BG_TASK_RESULT_PREFIX = "[bg-task-result]";

interface MessageBubbleProps {
  message: Message;
  sessionId: string;
  // Name + avatar of the agent that owns this session, used to label
  // assistant turns. Falls back to a harness-neutral "Assistant" when the
  // session has no owning agent.
  agentName?: string;
  agentAvatar?: string | null;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function AttachmentList({
  attachments,
  sessionId,
}: {
  attachments: AttachmentMetadata[];
  sessionId: string;
}) {
  const token = useSessionStore((s) => s.token);
  return (
    <div className="msg-attachments mt-2 flex flex-wrap gap-2">
      {attachments.map((a) => {
        // Token is appended as a query param because <img src> and
        // <a download> can't carry custom headers. Same auth value as the
        // bearer header — the server accepts either.
        const url = `${window.location.origin}/api/sessions/${encodeURIComponent(
          sessionId
        )}/attachments/${encodeURIComponent(a.id)}?token=${encodeURIComponent(token)}`;
        const isImage = a.mime_type.startsWith("image/");
        if (isImage) {
          return (
            <a
              key={a.id}
              href={url}
              target="_blank"
              rel="noreferrer"
              className="attachment-thumb block rounded-md border border-border overflow-hidden bg-muted/40 hover:border-primary/60 transition-colors"
              title={`${a.filename} (${formatBytes(a.size)})`}
            >
              <img
                src={url}
                alt={a.filename}
                className="block max-h-40 max-w-[16rem] object-contain"
              />
            </a>
          );
        }
        return (
          <a
            key={a.id}
            href={url}
            download={a.filename}
            target="_blank"
            rel="noreferrer"
            className="attachment-chip inline-flex items-center gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-1.5 text-xs text-foreground hover:border-primary/60 transition-colors"
          >
            <IconFile size={14} className="text-muted-foreground shrink-0" />
            <span className="font-medium truncate max-w-[12rem]">{a.filename}</span>
            <span className="text-muted-foreground">{formatBytes(a.size)}</span>
          </a>
        );
      })}
    </div>
  );
}

export function MessageBubble({
  message,
  sessionId,
  agentName,
  agentAvatar,
}: MessageBubbleProps) {
  const assistantLabel = agentName || "Assistant";
  // Mirror the header's avatar treatment: show the agent's emoji, falling
  // back to the default octopus when an agent exists but set no avatar.
  const assistantAvatar = agentName ? agentAvatar || "🐙" : null;
  switch (message.type) {
    case "text":
      if (message.role === "user") {
        const isBgResult =
          typeof message.content === "string" &&
          message.content.startsWith(BG_TASK_RESULT_PREFIX);
        if (isBgResult) {
          return (
            <BgTaskResultMessage
              content={message.content as string}
              sessionId={sessionId}
            />
          );
        }
        return (
          <div className="msg msg-user flex justify-end">
            <div className="max-w-[85%] space-y-1">
              <div className="msg-label text-xs font-semibold text-muted-foreground text-right">
                You
              </div>
              <div className="msg-content inline-block rounded-lg border border-primary/60 bg-card px-4 py-3 text-sm text-foreground whitespace-pre-wrap break-words">
                {message.content}
              </div>
              {message.attachments && message.attachments.length > 0 && (
                <AttachmentList
                  attachments={message.attachments}
                  sessionId={sessionId}
                />
              )}
            </div>
          </div>
        );
      }
      return (
        <div className="msg msg-assistant space-y-1">
          <div className="msg-label flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
            {assistantAvatar && (
              <span aria-hidden className="text-sm leading-none">
                {assistantAvatar}
              </span>
            )}
            <span>{assistantLabel}</span>
          </div>
          <div className="msg-content markdown rounded-lg border border-border bg-card px-4 py-3 text-sm leading-relaxed">
            <Markdown remarkPlugins={[remarkGfm]}>
              {message.content || ""}
            </Markdown>
          </div>
        </div>
      );

    case "tool_use":
      return <ToolUseBlock message={message} sessionId={sessionId} />;

    case "tool_result":
      return <ToolResultBlock message={message} />;

    case "tool_approval_request":
    case "question_request":
      return null; // handled by ToolApproval / QuestionPrompt component

    case "question_answer":
      return (
        <div className="msg msg-user flex justify-end">
          <div className="max-w-[85%] space-y-1">
            <div className="msg-label text-xs font-semibold text-muted-foreground text-right">
              You
            </div>
            <div className="msg-content msg-question-answer inline-block rounded-lg border border-primary/60 bg-card px-4 py-3 text-sm text-foreground italic whitespace-pre-wrap break-words">
              {message.content}
            </div>
          </div>
        </div>
      );

    case "result":
      return (
        <div className="msg msg-system flex justify-center py-1">
          <span className="result-badge text-[10px] font-medium uppercase tracking-wider text-muted-foreground bg-muted px-2 py-0.5 rounded-full">
            Done{message.cost != null ? ` · $${message.cost.toFixed(4)}` : ""}
          </span>
        </div>
      );

    // Ephemeral, client-side system note (e.g. the /schedule command's
    // confirmation or a parse-error hint). Centered pill, not attributed to
    // the user or to Claude. Not persisted — clears on reload.
    case "notice":
      return (
        <div className="msg msg-notice flex justify-center py-1">
          <span
            className={`notice-pill max-w-[85%] rounded-full border px-3 py-1 text-xs ${
              message.is_error
                ? "border-destructive/30 bg-destructive/10 text-destructive"
                : "border-border bg-muted text-muted-foreground"
            }`}
          >
            {message.content}
          </span>
        </div>
      );

    case "error":
      return (
        <div className="msg msg-error space-y-1">
          <div className="msg-label text-xs font-semibold text-destructive">
            Error
          </div>
          <div className="msg-content rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive whitespace-pre-wrap break-words">
            {message.content}
          </div>
        </div>
      );

    default:
      return null;
  }
}

function ToolUseBlock({
  message,
  sessionId,
}: {
  message: Message;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const inputStr = message.tool_input
    ? JSON.stringify(message.tool_input, null, 2)
    : "";
  const preview = (() => {
    const input = message.tool_input;
    if (input && "command" in input) return String(input.command).slice(0, 80);
    if (input && "file_path" in input) return String(input.file_path);
    return null;
  })();

  // The bg MCP tool returns a task_id in its tool_result, but the
  // tool_use itself doesn't carry it (the model generated the
  // tool_use_id before knowing the bg task_id). We find the matching
  // bg task by `bg_started` events stamped with the SAME tool's
  // command text + most-recent-first. Simpler approach: the next
  // tool_result message that follows this tool_use carries the
  // started-at-task_id text. For now: render the chip whenever the
  // tool is bg_run, and the chip itself fetches by id once we have
  // it (via the followup tool_result text). We grab the task id from
  // tool_input.__task_id if the backend injected it OR fall back to
  // matching by command — the bg MCP server returns text like
  // "Started bg task `<id>`" which the tool_result will contain.
  const isBgRun = message.tool_name === "mcp__bg__run";

  return (
    <div className="space-y-1.5">
      <div className="msg msg-tool rounded-lg border border-border bg-card overflow-hidden">
        <button
          type="button"
          className="tool-header w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent/50 transition-colors"
          onClick={() => setExpanded(!expanded)}
        >
          <span className="tool-icon text-muted-foreground shrink-0">
            {expanded ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          </span>
          <IconTool size={14} className="text-primary shrink-0" />
          <span className="tool-name font-medium text-primary shrink-0">
            {message.tool_name}
          </span>
          {preview && (
            <code className="tool-preview truncate text-xs text-muted-foreground font-mono">
              {preview}
            </code>
          )}
        </button>
        {expanded && (
          <pre className="tool-detail border-t border-border bg-muted/40 px-4 py-2.5 text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap break-words max-h-80 overflow-y-auto">
            {inputStr}
          </pre>
        )}
      </div>
      {isBgRun && <BgChipForToolUse sessionId={sessionId} message={message} />}
    </div>
  );
}

/**
 * Bridges a `mcp__bg__run` tool_use → the live BgTaskChip.
 *
 * The model's tool_use doesn't carry the task_id (we mint it
 * server-side). We pull the matching task out of the store by
 * looking for the most recent task whose `command` matches the
 * tool_use's input.command. The bg_started WS event populates the
 * store before this component mounts in normal flow; on snapshot
 * rehydration we still get a match because list_bg_tasks is fetched
 * on session load.
 */
function BgChipForToolUse({
  sessionId,
  message,
}: {
  sessionId: string;
  message: Message;
}) {
  const command =
    message.tool_input && typeof message.tool_input.command === "string"
      ? (message.tool_input.command as string)
      : "";
  const matchedId = useSessionStore((s) => {
    const tasks = s.bgTasks[sessionId] || [];
    const match = [...tasks]
      .reverse()
      .find((t) => t.command === command);
    return match?.id ?? null;
  });
  if (!matchedId) {
    return (
      <div className="octo-bgtask-chip inline-flex items-center gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-1.5 text-xs text-muted-foreground">
        <span>Waiting for bg task to register…</span>
      </div>
    );
  }
  return <BgTaskChip sessionId={sessionId} taskId={matchedId} />;
}

function BgTaskResultMessage({
  content,
  sessionId: _sessionId,
}: {
  content: string;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  // Strip the marker for display — it's only a routing hint, not part
  // of the human-facing text.
  const stripped = content.startsWith(BG_TASK_RESULT_PREFIX)
    ? content.slice(BG_TASK_RESULT_PREFIX.length).trimStart()
    : content;
  // Show only the first line collapsed; that line is the "finished
  // with status …" summary which is the most-important info.
  const firstLine = stripped.split("\n", 1)[0];
  return (
    <div className="msg msg-bg-result flex justify-end">
      <div className="max-w-[85%] space-y-1">
        <div className="msg-label text-xs font-semibold text-muted-foreground text-right flex items-center justify-end gap-1.5">
          <IconRobot size={12} className="text-muted-foreground" />
          <span>Auto · bg-task result</span>
        </div>
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="msg-content w-full text-left inline-block rounded-lg border border-dashed border-border bg-muted/30 px-4 py-3 text-sm text-foreground hover:bg-muted/50 transition-colors"
          title={expanded ? "Collapse" : "Expand full result"}
          aria-expanded={expanded}
        >
          <div className="flex items-start gap-2">
            <span className="text-muted-foreground shrink-0 mt-0.5">
              {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
            </span>
            {expanded ? (
              <pre className="m-0 flex-1 font-mono text-xs whitespace-pre-wrap break-words">
                {stripped}
              </pre>
            ) : (
              <span className="flex-1">{firstLine}</span>
            )}
          </div>
        </button>
      </div>
    </div>
  );
}

function ToolResultBlock({ message }: { message: Message }) {
  const [expanded, setExpanded] = useState(false);
  const content = message.content || "";
  const previewStr =
    typeof content === "string" ? content.slice(0, 120) : String(content);
  const errored = !!message.is_error;

  return (
    <div
      className={`msg msg-tool-result rounded-lg border overflow-hidden ${
        errored
          ? "error border-destructive/50 bg-destructive/5"
          : "border-border bg-card"
      }`}
    >
      <button
        type="button"
        className="tool-header w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent/50 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="tool-icon text-muted-foreground shrink-0">
          {expanded ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
        </span>
        <span
          className={`tool-result-label text-xs font-semibold uppercase tracking-wider shrink-0 ${
            errored ? "text-destructive" : "text-green-700"
          }`}
        >
          {errored ? "Error" : "Result"}
        </span>
        {!expanded && (
          <code className="tool-preview truncate text-xs text-muted-foreground font-mono">
            {previewStr}
          </code>
        )}
      </button>
      {expanded && (
        <pre
          className={`tool-detail border-t px-4 py-2.5 text-xs font-mono whitespace-pre-wrap break-words max-h-80 overflow-y-auto ${
            errored
              ? "border-destructive/30 bg-destructive/5 text-destructive"
              : "border-border bg-muted/40 text-foreground"
          }`}
        >
          {content}
        </pre>
      )}
    </div>
  );
}
