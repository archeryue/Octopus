/**
 * Live status chip for a cross-turn background task.
 *
 * Rendered inside ToolUseBlock when the tool is `mcp__bg__run`. The
 * tool_use itself shows the model's request (command + description);
 * the chip shows the LIVE state (running spinner, completed/failed
 * badge, exit code, last-line output preview) and exposes a Cancel
 * button while the task is running.
 *
 * State source: zustand `bgTasks[sessionId]`, populated by the WS
 * handler from `bg_started` / `bg_completed` events. Full output is
 * fetched on demand via `GET /api/sessions/{id}/bg-tasks/{task_id}`
 * when the user opens the popover (we don't ship 200 KB on every
 * WS event).
 */

import { useEffect, useState } from "react";
import {
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconExclamationCircle,
  IconHandStop,
  IconLoader2,
  IconX,
} from "@tabler/icons-react";

import { useSessionStore, type BgTask } from "../stores/sessionStore";
import { cn } from "../lib/utils";

interface Props {
  sessionId: string;
  taskId: string;
}

const STATUS_LABEL: Record<BgTask["status"], string> = {
  pending: "queued",
  running: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
  interrupted: "interrupted",
};

function StatusIcon({ status }: { status: BgTask["status"] }) {
  if (status === "running" || status === "pending") {
    return <IconLoader2 size={14} className="animate-spin text-primary" />;
  }
  if (status === "completed") {
    return <IconCheck size={14} className="text-green-700" />;
  }
  if (status === "cancelled" || status === "interrupted") {
    return <IconHandStop size={14} className="text-muted-foreground" />;
  }
  return <IconExclamationCircle size={14} className="text-destructive" />;
}

function lastLine(text: string): string {
  const trimmed = text.replace(/\s+$/, "");
  const lines = trimmed.split("\n");
  return lines[lines.length - 1]?.slice(0, 120) || "";
}

export function BgTaskChip({ sessionId, taskId }: Props) {
  const token = useSessionStore((s) => s.token);
  const upsertBgTask = useSessionStore((s) => s.upsertBgTask);
  const task = useSessionStore((s) =>
    (s.bgTasks[sessionId] || []).find((t) => t.id === taskId)
  );
  const [expanded, setExpanded] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  // When the chip is first rendered (e.g. from chat history that
  // pre-existed the WS connection), we may not have the task in the
  // store. Fetch it once so the chip has *something* to show.
  useEffect(() => {
    if (task) return;
    const url = `${window.location.origin}/api/sessions/${encodeURIComponent(
      sessionId
    )}/bg-tasks/${encodeURIComponent(taskId)}`;
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data) upsertBgTask(sessionId, data as BgTask);
      })
      .catch(() => {});
  }, [sessionId, taskId, token, task, upsertBgTask]);

  // When expanded, we want the full stdout/stderr — refetch to pick up
  // bytes that didn't ride the WS event.
  useEffect(() => {
    if (!expanded) return;
    const url = `${window.location.origin}/api/sessions/${encodeURIComponent(
      sessionId
    )}/bg-tasks/${encodeURIComponent(taskId)}`;
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data) upsertBgTask(sessionId, data as BgTask);
      })
      .catch(() => {});
  }, [expanded, sessionId, taskId, token, upsertBgTask]);

  if (!task) {
    return (
      <div className="octo-bgtask-chip mt-2 inline-flex items-center gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-1.5 text-xs text-muted-foreground">
        <IconLoader2 size={12} className="animate-spin" />
        <span>Loading bg task {taskId}…</span>
      </div>
    );
  }

  const isRunning = task.status === "running" || task.status === "pending";

  const cancel = async () => {
    if (cancelling) return;
    setCancelling(true);
    try {
      await fetch(
        `${window.location.origin}/api/sessions/${encodeURIComponent(
          sessionId
        )}/bg-tasks/${encodeURIComponent(taskId)}/cancel`,
        { method: "POST", headers: { Authorization: `Bearer ${token}` } }
      );
    } catch {
      // best effort
    } finally {
      setCancelling(false);
    }
  };

  const headerStyle: Record<BgTask["status"], string> = {
    pending: "border-border bg-muted/40",
    running: "border-primary/40 bg-primary/5",
    completed: "border-green-700/40 bg-green-50",
    failed: "border-destructive/40 bg-destructive/5",
    cancelled: "border-border bg-muted/40",
    interrupted: "border-border bg-muted/40",
  };

  const lastOut = lastLine(task.stdout) || lastLine(task.stderr);

  return (
    <div
      className={cn(
        "octo-bgtask-chip mt-2 rounded-md border overflow-hidden",
        headerStyle[task.status]
      )}
    >
      <div className="flex items-center gap-2 px-2.5 py-1.5 text-xs">
        {/* The expand-toggle is its own button — siblings (exit-code
         * badge, cancel button) live outside so we don't nest <button>
         * inside <button>, which is invalid HTML and breaks a11y. */}
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="flex flex-1 min-w-0 items-center gap-2 text-left hover:bg-accent/40 -mx-1 px-1 py-0.5 rounded transition-colors"
          aria-expanded={expanded}
          aria-label={expanded ? "Collapse bg task" : "Expand bg task"}
        >
          <span className="text-muted-foreground shrink-0">
            {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
          </span>
          <StatusIcon status={task.status} />
          <span className="font-semibold uppercase tracking-wider text-[10px] shrink-0">
            bg · {STATUS_LABEL[task.status]}
          </span>
          {task.description && (
            <span className="truncate text-foreground">{task.description}</span>
          )}
          {!task.description && (
            <code className="truncate font-mono text-muted-foreground">
              {task.command}
            </code>
          )}
        </button>
        {task.exit_code !== null && task.exit_code !== 0 && (
          <span className="shrink-0 rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-mono text-destructive">
            exit {task.exit_code}
          </span>
        )}
        {isRunning && (
          <button
            type="button"
            onClick={cancel}
            disabled={cancelling}
            className="shrink-0 inline-flex items-center gap-1 rounded border border-border bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-foreground hover:border-destructive/50 transition-colors disabled:opacity-50"
            title="Cancel this background task"
          >
            <IconX size={10} />
            Cancel
          </button>
        )}
      </div>
      {!expanded && lastOut && (
        <div className="border-t border-border/60 px-3 py-1 font-mono text-[11px] text-muted-foreground truncate">
          {lastOut}
        </div>
      )}
      {expanded && (
        <div className="border-t border-border/60 bg-card/60 px-3 py-2 space-y-2">
          <div className="text-[11px] text-muted-foreground">
            <span className="font-semibold">command</span>{" "}
            <code className="font-mono">{task.command}</code>
          </div>
          {task.stdout && (
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-1">
                stdout
                {task.truncated && (
                  <span className="ml-2 text-destructive">(truncated)</span>
                )}
              </div>
              <pre className="m-0 max-h-64 overflow-auto rounded bg-muted/40 px-2 py-1.5 text-[11px] font-mono whitespace-pre-wrap break-words">
                {task.stdout}
              </pre>
            </div>
          )}
          {task.stderr && (
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wider text-destructive mb-1">
                stderr
              </div>
              <pre className="m-0 max-h-64 overflow-auto rounded bg-destructive/5 px-2 py-1.5 text-[11px] font-mono whitespace-pre-wrap break-words text-destructive">
                {task.stderr}
              </pre>
            </div>
          )}
          {!task.stdout && !task.stderr && task.status !== "running" && (
            <div className="text-[11px] text-muted-foreground italic">
              (no output)
            </div>
          )}
        </div>
      )}
    </div>
  );
}
