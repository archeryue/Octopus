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
    <div className="msg msg-approval rounded-lg border-2 border-yellow-400/60 bg-yellow-400/5 overflow-hidden">
      <div className="approval-header flex items-center gap-2 px-3 py-2 bg-yellow-400/10 text-sm">
        <IconAlertTriangle size={16} className="text-yellow-400 shrink-0" />
        <span>
          <strong className="text-foreground">{message.tool_name}</strong>{" "}
          <span className="text-muted-foreground">wants to execute:</span>
        </span>
      </div>
      <pre className="approval-detail border-t border-yellow-400/20 bg-muted/30 px-3 py-2 text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
        {JSON.stringify(message.tool_input, null, 2)}
      </pre>
      <div className="approval-actions flex gap-2 px-3 py-2 border-t border-yellow-400/20">
        <Button
          className="btn btn-approve bg-emerald-600 hover:bg-emerald-700 text-white"
          size="sm"
          onClick={() => onApprove(toolUseId)}
        >
          Allow
        </Button>
        <Button
          className="btn btn-deny bg-destructive hover:bg-destructive/90 text-destructive-foreground"
          size="sm"
          onClick={() => onDeny(toolUseId)}
        >
          Deny
        </Button>
      </div>
    </div>
  );
}
