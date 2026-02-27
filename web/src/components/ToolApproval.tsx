import type { Message } from "../stores/sessionStore";

interface Props {
  message: Message;
  onApprove: (toolUseId: string) => void;
  onDeny: (toolUseId: string) => void;
}

export function ToolApproval({ message, onApprove, onDeny }: Props) {
  const toolUseId = message.tool_use_id;
  if (!toolUseId) return null;

  return (
    <div className="msg msg-approval">
      <div className="approval-header">
        <span className="approval-icon">⚠</span>
        <strong>{message.tool_name}</strong> wants to execute:
      </div>
      <pre className="approval-detail">
        {JSON.stringify(message.tool_input, null, 2)}
      </pre>
      <div className="approval-actions">
        <button className="btn btn-approve" onClick={() => onApprove(toolUseId)}>
          Allow
        </button>
        <button className="btn btn-deny" onClick={() => onDeny(toolUseId)}>
          Deny
        </button>
      </div>
    </div>
  );
}
