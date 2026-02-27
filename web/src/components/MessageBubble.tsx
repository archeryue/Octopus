import { useState } from "react";
import Markdown from "react-markdown";
import type { Message } from "../stores/sessionStore";

export function MessageBubble({ message }: { message: Message }) {
  switch (message.type) {
    case "text":
      return message.role === "user" ? (
        <div className="msg msg-user">
          <div className="msg-label">You</div>
          <div className="msg-content">{message.content}</div>
        </div>
      ) : (
        <div className="msg msg-assistant">
          <div className="msg-label">Claude</div>
          <div className="msg-content markdown">
            <Markdown>{message.content || ""}</Markdown>
          </div>
        </div>
      );

    case "tool_use":
      return <ToolUseBlock message={message} />;

    case "tool_result":
      return <ToolResultBlock message={message} />;

    case "tool_approval_request":
      return null; // handled by ToolApproval component

    case "result":
      return (
        <div className="msg msg-system">
          <span className="result-badge">
            Done{message.cost != null ? ` · $${message.cost.toFixed(4)}` : ""}
          </span>
        </div>
      );

    case "error":
      return (
        <div className="msg msg-error">
          <div className="msg-label">Error</div>
          <div className="msg-content">{message.content}</div>
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

  return (
    <div className="msg msg-tool">
      <div className="tool-header" onClick={() => setExpanded(!expanded)}>
        <span className="tool-icon">{expanded ? "▼" : "▶"}</span>
        <span className="tool-name">{message.tool_name}</span>
        {message.tool_input && "command" in message.tool_input && (
          <code className="tool-preview">
            {String(message.tool_input.command).slice(0, 60)}
          </code>
        )}
        {message.tool_input && "file_path" in message.tool_input && (
          <code className="tool-preview">
            {String(message.tool_input.file_path)}
          </code>
        )}
      </div>
      {expanded && (
        <pre className="tool-detail">{inputStr}</pre>
      )}
    </div>
  );
}

function ToolResultBlock({ message }: { message: Message }) {
  const [expanded, setExpanded] = useState(false);
  const content = message.content || "";
  const preview =
    typeof content === "string" ? content.slice(0, 100) : String(content);

  return (
    <div className={`msg msg-tool-result ${message.is_error ? "error" : ""}`}>
      <div className="tool-header" onClick={() => setExpanded(!expanded)}>
        <span className="tool-icon">{expanded ? "▼" : "▶"}</span>
        <span className="tool-result-label">
          {message.is_error ? "Error" : "Result"}
        </span>
        {!expanded && <code className="tool-preview">{preview}</code>}
      </div>
      {expanded && (
        <pre className="tool-detail">{content}</pre>
      )}
    </div>
  );
}
