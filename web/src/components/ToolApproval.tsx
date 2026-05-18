import { IconAlertTriangle } from "@tabler/icons-react";
import type { Message } from "../stores/sessionStore";
import { Button } from "./ui/button";

interface Props {
  message: Message;
  onApprove: (toolUseId: string) => void;
  onDeny: (toolUseId: string) => void;
}

export function ToolApproval({ message, onApprove, onDeny }: Props) {
  const toolUseId = message.tool_use_id;
  if (!toolUseId) return null;

  return (
    <div className="msg msg-approval rounded-lg border-[0.7px] border-yellow-200 bg-yellow-50 overflow-hidden">
      <div className="approval-header flex items-center gap-2.5 px-5 py-4 text-sm">
        <IconAlertTriangle size={18} className="text-yellow-700 shrink-0" />
        <span>
          <strong className="text-foreground">{message.tool_name}</strong>{" "}
          <span className="text-muted-foreground">wants to execute:</span>
        </span>
      </div>
      <pre className="approval-detail border-t border-yellow-200 bg-card/60 px-5 py-3.5 text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
        {JSON.stringify(message.tool_input, null, 2)}
      </pre>
      <div className="approval-actions flex gap-2 px-5 py-3 border-t border-yellow-200">
        <Button
          className="btn btn-approve"
          size="sm"
          onClick={() => onApprove(toolUseId)}
        >
          Allow
        </Button>
        <Button
          className="btn btn-deny"
          variant="outline"
          size="sm"
          onClick={() => onDeny(toolUseId)}
        >
          Deny
        </Button>
      </div>
    </div>
  );
}
