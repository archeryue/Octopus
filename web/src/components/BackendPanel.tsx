import { useState } from "react";
import { IconChevronRight, IconServer } from "@tabler/icons-react";

import type { Application } from "../stores/sessionStore";

type Backend = NonNullable<Application["backend"]>;

/** What the application's own server process is doing.
 *
 * Only rendered when the app actually declares a backend (an executable
 * `start.sh`), so a static application's UI is unchanged.
 *
 * The log is the point. A backend that won't start is the failure mode this
 * whole feature has to answer for, and "it doesn't work" with nothing to read
 * is what makes that miserable — so the last lines of install/start output are
 * one click away, not in a file on the server.
 */
export function BackendPanel({ backend }: { backend: Backend }) {
  const [open, setOpen] = useState(false);
  if (!backend || backend.state === "absent") return null;

  const tone =
    backend.state === "running"
      ? "text-success"
      : backend.state === "failed"
        ? "text-destructive"
        : "text-muted-foreground";

  const detail =
    backend.state === "running"
      ? `port ${backend.port}${
          backend.uptime_s ? ` · up ${formatUptime(backend.uptime_s)}` : ""
        }`
      : backend.error || "";

  const lines = backend.log_tail ?? [];

  return (
    <div className="backend-panel border-t border-border bg-muted/20 px-4 py-1.5 text-xs">
      <button
        type="button"
        className="backend-summary flex w-full items-center gap-2 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <IconServer size={13} className={tone} />
        <span className={`backend-state font-medium ${tone}`}>
          {backend.state}
        </span>
        <span className="backend-detail truncate text-muted-foreground">
          {detail}
        </span>
        {lines.length > 0 && (
          <IconChevronRight
            size={13}
            className={`ml-auto shrink-0 text-muted-foreground transition-transform ${
              open ? "rotate-90" : ""
            }`}
          />
        )}
      </button>

      {open && lines.length > 0 && (
        <pre className="backend-log mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border-[0.7px] border-border bg-card px-3 py-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
          {lines.join("\n")}
        </pre>
      )}
    </div>
  );
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
