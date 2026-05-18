import { useState } from "react";
import { IconChevronDown, IconChevronRight, IconTool } from "@tabler/icons-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Message } from "../stores/sessionStore";

export function MessageBubble({ message }: { message: Message }) {
  switch (message.type) {
    case "text":
      return message.role === "user" ? (
        <div className="msg msg-user flex justify-end">
          <div className="max-w-[85%] space-y-1">
            <div className="msg-label text-xs font-semibold text-muted-foreground text-right">
              You
            </div>
            <div className="msg-content inline-block rounded-lg bg-primary px-7 py-5 text-sm text-primary-foreground whitespace-pre-wrap break-words">
              {message.content}
            </div>
          </div>
        </div>
      ) : (
        <div className="msg msg-assistant space-y-1">
          <div className="msg-label text-xs font-semibold text-muted-foreground">
            Claude
          </div>
          <div className="msg-content markdown rounded-lg border border-border bg-card px-7 py-5 text-sm leading-7">
            <Markdown remarkPlugins={[remarkGfm]}>
              {message.content || ""}
            </Markdown>
          </div>
        </div>
      );

    case "tool_use":
      return <ToolUseBlock message={message} />;

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
            <div className="msg-content msg-question-answer inline-block rounded-lg bg-primary/80 px-7 py-5 text-sm text-primary-foreground italic whitespace-pre-wrap break-words">
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

    case "error":
      return (
        <div className="msg msg-error space-y-1">
          <div className="msg-label text-xs font-semibold text-destructive">
            Error
          </div>
          <div className="msg-content rounded-lg border border-destructive/40 bg-destructive/10 px-7 py-5 text-sm text-destructive whitespace-pre-wrap break-words">
            {message.content}
          </div>
        </div>
      );

    default:
      return null;
  }
}

function ToolUseBlock({ message }: { message: Message }) {
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

  return (
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
        <span className="tool-name font-medium text-foreground shrink-0">
          {message.tool_name}
        </span>
        {preview && (
          <code className="tool-preview truncate text-xs text-muted-foreground font-mono">
            {preview}
          </code>
        )}
      </button>
      {expanded && (
        <pre className="tool-detail border-t border-border bg-muted/40 px-6 py-4 text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap break-words max-h-80 overflow-y-auto">
          {inputStr}
        </pre>
      )}
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
            errored ? "text-destructive" : "text-muted-foreground"
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
          className={`tool-detail border-t px-6 py-4 text-xs font-mono whitespace-pre-wrap break-words max-h-80 overflow-y-auto ${
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
