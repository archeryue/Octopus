import { useCallback, useEffect, useState } from "react";
import { IconActivity, IconRefresh } from "@tabler/icons-react";
import { useSessionStore } from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";

const API = window.location.origin;

const WINDOWS = ["1h", "24h", "7d", "30d"] as const;
type Window = (typeof WINDOWS)[number];

type Row = Record<string, string | number | null>;
type Overview = {
  window: string;
  counts: Row[];
  turns: Row[];
  errors: Row[];
  resources: Row[];
};

/** Column labels the raw SQL aliases do not read well as. */
const HEADINGS: Record<string, string> = {
  n: "count",
  ok_n: "ok",
  avg_ms: "avg",
  max_ms: "max",
  p50_ms: "p50",
  p95_ms: "p95",
  p99_ms: "p99",
  last_seen: "last seen",
  error_code: "error",
  installation: "connector",
};

function heading(key: string): string {
  return HEADINGS[key] ?? key.replace(/_/g, " ");
}

function cell(key: string, value: string | number | null): string {
  if (value === null || value === undefined) return "—";
  if (key === "last_seen" && typeof value === "number") {
    return new Date(value * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }
  if (key.endsWith("_ms") && typeof value === "number") {
    return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
  }
  return String(value);
}

/** One report as a table. Empty says so, because a blank panel reads as
 *  "nothing wrong" when it means "nothing recorded". */
function Table({ title, rows }: { title: string; rows: Row[] }) {
  if (!rows?.length) {
    return (
      <section className="mb-6">
        <h3 className="mb-1.5 text-xs font-medium uppercase tracking-wide text-gray-600">
          {title}
        </h3>
        <p className="text-sm text-gray-500">Nothing recorded in this window.</p>
      </section>
    );
  }
  const cols = Object.keys(rows[0]);
  return (
    <section className="mb-6">
      <h3 className="mb-1.5 text-xs font-medium uppercase tracking-wide text-gray-600">
        {title}
      </h3>
      <div className="overflow-x-auto rounded-md border border-gray-200">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50 text-left">
              {cols.map((c) => (
                <th key={c} className="px-3 py-1.5 font-medium text-gray-700">
                  {heading(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-b border-gray-100 last:border-0">
                {cols.map((c) => (
                  <td
                    key={c}
                    className={`px-3 py-1.5 ${
                      typeof r[c] === "number" ? "font-mono tabular-nums" : ""
                    }`}
                  >
                    {cell(c, r[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function MonitorPage({ onToggleSidebar }: { onToggleSidebar?: () => void }) {
  const token = useSessionStore((s) => s.token);
  const [window_, setWindow] = useState<Window>("24h");
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/monitor/overview?window=${window_}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        // 503 is the honest "no metrics file yet" case, not a failure.
        const body = await res.json().catch(() => ({}));
        setError(body.detail ?? `Request failed (${res.status})`);
        setData(null);
        return;
      }
      setData((await res.json()) as Overview);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [token, window_]);

  useEffect(() => {
    void load();
  }, [load]);

  const turnCount = data?.counts.find((c) => c.kind === "turn")?.n ?? 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        icon={<IconActivity size={15} />}
        crumbs={["Monitor"]}
        meta={<>{Number(turnCount).toLocaleString()} turns · last {window_}</>}
        actions={
          <div className="flex items-center gap-1.5">
            {WINDOWS.map((w) => (
              <button
                key={w}
                onClick={() => setWindow(w)}
                className={`rounded px-2 py-0.5 text-xs ${
                  w === window_
                    ? "bg-gray-200 font-medium text-gray-900"
                    : "text-gray-600 hover:bg-gray-100"
                }`}
              >
                {w}
              </button>
            ))}
            <button
              onClick={() => void load()}
              aria-label="Refresh"
              className="rounded p-1 text-gray-600 hover:bg-gray-100"
            >
              <IconRefresh size={14} />
            </button>
          </div>
        }
        onToggleSidebar={onToggleSidebar}
      />

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {error && (
          <p className="mb-4 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">
            {error}
          </p>
        )}
        {loading && !data && <p className="text-sm text-gray-500">Loading…</p>}
        {data && (
          <>
            <Table title="activity" rows={data.counts} />
            <Table title="turns" rows={data.turns} />
            <Table title="top errors" rows={data.errors} />
            <Table title="resources" rows={data.resources} />
            <p className="text-xs text-gray-500">
              Anything not shown here is a query away —{" "}
              <code className="font-mono">octopus monitor --since {window_}</code>, or
              sqlite3 against octopus-metrics.db. Kept 30 days.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
