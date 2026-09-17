import { useState } from "react";
import { IconServer } from "@tabler/icons-react";

import type { Application } from "../stores/sessionStore";
import { Popover } from "./ui/popover";

type Backend = NonNullable<Application["backend"]>;

/** What the application's own server process is doing.
 *
 * Only rendered when the app actually declares a backend (an executable
 * `start.sh`), so a static application's chrome is unchanged.
 *
 * It lives in the header rather than in a strip under the page: the app gets
 * the whole pane (app-agent-access.md §7). The state word is always visible
 * because a failed backend has to be noticeable without hunting; the port,
 * the uptime, the reason and the log are one click away. The log is the
 * point — a dead backend with nothing to read is the failure mode this whole
 * feature exists to avoid.
 */
export function BackendPanel({ backend }: { backend: Backend }) {
  const [open, setOpen] = useState(false);
  if (!backend || backend.state === "absent") return null;

  const tone =
    backend.state === "running"
      ? "text-success"
      : backend.state === "failed"
        ? "text-destructive"
        : "text-gray-700";

  const detail =
    backend.state === "running"
      ? `port ${backend.port}${
          backend.uptime_s ? ` · up ${formatUptime(backend.uptime_s)}` : ""
        }`
      : backend.error || "no output yet";

  const lines = backend.log_tail ?? [];

  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      label="Backend"
      panelClassName="w-[380px]"
      trigger={
        <button
          type="button"
          className="backend-panel backend-summary inline-flex h-8 items-center gap-1.5 rounded-lg px-2 transition-colors hover:bg-gray-100"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          title="Backend process"
        >
          <IconServer size={14} className={tone} />
          <span className={`backend-state text-[12px] font-medium ${tone}`}>
            {backend.state}
          </span>
        </button>
      }
    >
      <div className="backend-detail text-xs text-gray-800">{detail}</div>
      {lines.length > 0 && (
        <pre className="backend-log mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg border-[0.7px] border-border bg-gray-50 px-3 py-2 font-mono text-[11px] leading-relaxed text-gray-800">
          {lines.join("\n")}
        </pre>
      )}
    </Popover>
  );
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
