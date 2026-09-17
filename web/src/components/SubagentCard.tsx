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
  const steps = run.steps ?? [];
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
      <button
        type="button"
        className="subagent-summary-row flex w-full items-center gap-2 text-left text-[12.5px]"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? "Hide what it's doing" : "See what it's doing"}
      >
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
        <span className="shrink-0 text-gray-600">
          {open ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
        </span>
      </button>

      {/* The answer it gave, always in reach — one line closed, whole when
        * opened. While it's still working there is no answer yet, so the
        * closed card shows the step it's on (above) and nothing else. */}
      {!open && run.summary && (
        <p className="subagent-summary mt-1.5 line-clamp-2 text-[12px] text-gray-800">
          {run.summary}
        </p>
      )}

      {open && (
        <div className="subagent-detail mt-2 space-y-2 border-t border-gray-300 pt-2">
          {run.summary && (
            <div>
              <Label>answer</Label>
              <p className="subagent-summary mt-0.5 whitespace-pre-wrap break-words text-[12px] leading-relaxed text-gray-900">
                {run.summary}
              </p>
            </div>
          )}

          {/* What it has actually been doing. One line per step, newest last
            * — the thing a spinner and a token count can't tell you. */}
          {steps.length > 0 && (
            <div>
              <Label>
                {running ? `doing now · ${steps.length} steps` : `${steps.length} steps`}
              </Label>
              <ol className="subagent-steps mt-0.5 space-y-0.5">
                {steps.map((step, i) => (
                  <li
                    key={`${i}-${step}`}
                    className={`subagent-step flex gap-1.5 text-[11.5px] leading-relaxed ${
                      running && i === steps.length - 1
                        ? "font-medium text-gray-900"
                        : "text-gray-700"
                    }`}
                  >
                    <span className="shrink-0 font-mono text-[10px] text-gray-600">
                      {i + 1}
                    </span>
                    <span className="break-words">{step}</span>
                  </li>
                ))}
              </ol>
            </div>
          )}

          {run.prompt && (
            <div>
              <Label>brief</Label>
              <p className="subagent-prompt mt-0.5 whitespace-pre-wrap break-words text-[11.5px] leading-relaxed text-gray-700">
                {run.prompt}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** The small caps label the expanded panel groups by. */
function Label({ children }: { children: React.ReactNode }) {
  return (
    <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-gray-600">
      {children}
    </span>
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
