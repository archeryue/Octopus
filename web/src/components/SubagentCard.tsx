import { useState } from "react";
import {
  IconChevronDown,
  IconChevronRight,
  IconCircleCheck,
  IconAlertTriangle,
  IconUsersGroup,
} from "@tabler/icons-react";

import { useSessionStore } from "../stores/sessionStore";

/** A sub-agent the model spawned inside this turn (native-subagents.md §5).
 *
 * Both CLIs can fan work out to a short-lived helper — Claude Code's `Task`,
 * Codex's `spawn_agent` — and both narrate it while it runs. Without this the
 * only trace is a tool call that sits there for minutes looking stuck, and a
 * result that appears from nowhere when it's over.
 *
 * Renders nothing when there's no live state for the call: after a server
 * restart the sub-agent is gone anyway, and the tool call plus its result are
 * still in the transcript, so an empty card would add nothing.
 */
export function SubagentCard({
  sessionId,
  toolUseId,
}: {
  sessionId: string;
  toolUseId?: string | null;
}) {
  const run = useSessionStore((s) =>
    toolUseId ? s.subagents[sessionId]?.[toolUseId] : undefined
  );
  const [open, setOpen] = useState(false);
  if (!run) return null;

  const running = run.status === "running";
  const failed = run.status === "failed";
  const meta = [
    run.tool_uses ? `${run.tool_uses} tool${run.tool_uses === 1 ? "" : "s"}` : "",
    run.tokens ? `${formatTokens(run.tokens)} tokens` : "",
    run.duration_ms ? formatDuration(run.duration_ms) : "",
  ].filter(Boolean);

  return (
    <div
      className={`subagent-card subagent-${run.status} rounded-lg border-[0.7px] px-3 py-2 ${
        failed
          ? "border-destructive/40 bg-destructive/5"
          : running
            ? "border-primary-100 bg-primary-50/50"
            : "border-gray-300 bg-gray-50"
      }`}
    >
      <div className="flex items-center gap-2 text-[12.5px]">
        {running ? (
          <span className="subagent-spinner inline-block size-3 shrink-0 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
        ) : failed ? (
          <IconAlertTriangle size={14} className="shrink-0 text-destructive" />
        ) : (
          <IconCircleCheck size={14} className="shrink-0 text-success" />
        )}
        <IconUsersGroup size={13} className="shrink-0 text-gray-700" />
        <span className="subagent-name shrink-0 font-semibold text-gray-900">
          {run.name || "sub-agent"}
        </span>
        {/* What it is doing right now — the reason this card exists. */}
        <span className="subagent-description truncate text-gray-700">
          {running
            ? run.description || "working"
            : failed
              ? "failed"
              : "done"}
        </span>
        {meta.length > 0 && (
          <span className="subagent-meta ml-auto shrink-0 font-mono text-[10.5px] text-gray-700">
            {meta.join(" · ")}
          </span>
        )}
      </div>

      {(run.summary || run.prompt) && (
        <button
          type="button"
          className="subagent-toggle mt-1.5 flex w-full items-start gap-1.5 text-left text-[12px] text-gray-800"
          onClick={() => setOpen((v) => !v)}
        >
          <span className="shrink-0 text-gray-600">
            {open ? (
              <IconChevronDown size={13} />
            ) : (
              <IconChevronRight size={13} />
            )}
          </span>
          <span className={`subagent-summary ${open ? "" : "line-clamp-2"}`}>
            {run.summary || run.prompt}
          </span>
        </button>
      )}

      {open && run.summary && run.prompt && (
        <div className="subagent-prompt mt-1.5 border-t border-gray-300 pt-1.5 text-[11.5px] leading-relaxed text-gray-700">
          <span className="font-mono text-[10.5px] uppercase tracking-wide text-gray-600">
            brief
          </span>
          <p className="mt-0.5 whitespace-pre-wrap break-words">{run.prompt}</p>
        </div>
      )}
    </div>
  );
}

function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}
