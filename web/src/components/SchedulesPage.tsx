import { useCallback, useEffect } from "react";
import { IconClock, IconTrash } from "@tabler/icons-react";
import { useSessionStore, type Agent, type Schedule } from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";

const API = window.location.origin;

/** Format the next fire the way the design's header does: "next 09:00". */
function formatNext(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const time = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return d.toDateString() === new Date().toDateString()
    ? time
    : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  if (sameDay) return `Today ${time}`;
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `Yesterday ${time}`;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

/** The Schedules manage page — one card per schedule in the main area.
 *
 * Replaces the old overview dialog. Each card leads with the name and its
 * recurrence in mono (the cron string is the truth; the label is the reading
 * of it), then the two things you actually want when auditing a schedule: the
 * agent it runs as, and the prompt it will send. Creation still happens from
 * chat — `/schedule 30m …` — because that's where you know what you want run.
 */
export function SchedulesPage({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const schedules = useSessionStore((s) => s.schedules);
  const setSchedules = useSessionStore((s) => s.setSchedules);
  const showChat = useSessionStore((s) => s.showChat);

  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };

  const fetchSchedules = useCallback(async () => {
    const resp = await fetch(`${API}/api/schedules`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (resp.ok) setSchedules(await resp.json());
  }, [token, setSchedules]);

  useEffect(() => {
    fetchSchedules();
  }, [fetchSchedules]);

  const toggle = async (sched: Schedule) => {
    await fetch(`${API}/api/schedules/${sched.id}`, {
      method: "PATCH",
      headers,
      body: JSON.stringify({ enabled: !sched.enabled }),
    });
    fetchSchedules();
  };

  const remove = async (sched: Schedule) => {
    if (!window.confirm(`Delete the schedule "${sched.name}"?`)) return;
    await fetch(`${API}/api/schedules/${sched.id}`, {
      method: "DELETE",
      headers,
    });
    fetchSchedules();
  };

  const agentById = (id: string): Agent | undefined =>
    agents.find((a) => a.id === id);
  const soonest = schedules
    .filter((s) => s.enabled && s.next_run_at)
    .map((s) => s.next_run_at as string)
    .sort()[0];

  return (
    <div className="schedules-page flex min-h-0 flex-1 flex-col">
      <PageHeader
        crumbs={["Schedules"]}
        meta={
          <>
            {schedules.length} schedule{schedules.length === 1 ? "" : "s"}
            {soonest && ` · next ${formatNext(soonest)}`}
          </>
        }
        onToggleSidebar={onToggleSidebar}
      />

      <div className="page-body">
        {schedules.length === 0 ? (
          <div className="schedules-empty card px-6 py-12 text-center">
            <IconClock size={22} className="mx-auto mb-3 text-gray-600" />
            <p className="text-sm text-gray-900">No schedules yet.</p>
            <p className="mt-1.5 text-[13px] text-gray-700">
              Type{" "}
              <code className="font-mono text-primary">/schedule 30m …</code> in
              any chat to create one.
            </p>
            <button
              type="button"
              className="btn-schedules-to-chat mt-4 text-[13px] font-medium text-primary hover:underline"
              onClick={showChat}
            >
              Back to chat →
            </button>
          </div>
        ) : (
          <div className="schedules-list flex flex-col gap-3.5">
            {schedules.map((sched) => {
              const agent = agentById(sched.agent_id);
              return (
                <div key={sched.id} className={`schedule-item card overflow-hidden ${sched.enabled ? "" : "disabled"}`}>
                  <div className="flex items-start gap-3.5 px-5 py-4">
                    <span className="tile tile-lg tile-blue mt-0.5">
                      <IconClock size={15} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="schedule-name truncate text-[15px] font-semibold text-gray-950">
                        {sched.name}
                      </div>
                      <div className="schedule-interval mt-1 flex flex-wrap items-center gap-2 font-mono text-[11.5px] text-gray-700">
                        {sched.cron && <span>{sched.cron}</span>}
                        <span className="text-gray-800">
                          {sched.recurrence_label}
                        </span>
                        {sched.timezone && (
                          <span className="text-primary-300">{sched.timezone}</span>
                        )}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2.5">
                      <span
                        className={`pill ${
                          sched.enabled ? "pill-success" : "pill-neutral"
                        }`}
                      >
                        {sched.enabled && <span className="dot" />}
                        {sched.enabled ? "Active" : "Paused"}
                      </span>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={sched.enabled}
                        aria-label={sched.enabled ? "Pause schedule" : "Resume schedule"}
                        className={`btn-toggle ${
                          sched.enabled ? "on" : "off"
                        } relative h-6 w-11 shrink-0 rounded-full transition-colors ${
                          sched.enabled ? "bg-primary" : "bg-gray-400"
                        }`}
                        onClick={() => toggle(sched)}
                      >
                        <span
                          className={`absolute top-0.5 size-5 rounded-full bg-white shadow transition-all ${
                            sched.enabled ? "left-[22px]" : "left-0.5"
                          }`}
                        />
                      </button>
                      <button
                        type="button"
                        className="btn-delete inline-flex size-8 items-center justify-center rounded-lg text-gray-600 transition-colors hover:bg-danger-bg hover:text-destructive"
                        onClick={() => remove(sched)}
                        title="Delete schedule"
                        aria-label={`Delete ${sched.name}`}
                      >
                        <IconTrash size={15} />
                      </button>
                    </div>
                  </div>

                  <div className="grid gap-x-8 gap-y-4 border-t border-gray-300 px-5 py-4 md:grid-cols-2">
                    <div>
                      <div className="text-[12.5px] text-gray-700">Target Agent</div>
                      <div className="mt-1.5 flex items-center gap-2">
                        <span className="tile tile-plain">
                          {agent?.avatar || "🐙"}
                        </span>
                        <span className="truncate text-[13.5px] text-gray-900">
                          {agent?.name ?? "Unknown agent"}
                        </span>
                      </div>
                      <div className="mt-4 text-[12.5px] text-gray-700">
                        First message
                      </div>
                      <div className="schedule-prompt mt-1.5 rounded-lg border border-gray-300 bg-gray-50 px-3.5 py-2.5 text-[13px] leading-relaxed text-gray-900">
                        {sched.prompt}
                      </div>
                    </div>
                    <div>
                      <div className="text-[12.5px] text-gray-700">Recent runs</div>
                      <div className="mt-1.5 flex flex-col">
                        {sched.last_run_at ? (
                          <div className="flex items-center gap-2.5 border-b border-gray-200 py-2 last:border-0">
                            <span className="inline-block size-1.5 shrink-0 rounded-full bg-success" />
                            <span className="text-[13px] text-gray-900">
                              {formatWhen(sched.last_run_at)}
                            </span>
                            <span className="ml-auto font-mono text-[11px] text-gray-700">
                              last run
                            </span>
                          </div>
                        ) : (
                          <div className="py-2 text-[13px] text-gray-700">
                            Hasn't run yet
                          </div>
                        )}
                        {sched.next_run_at && (
                          <div className="flex items-center gap-2.5 py-2">
                            <span className="inline-block size-1.5 shrink-0 rounded-full bg-primary-200" />
                            <span className="text-[13px] text-gray-900">
                              {formatWhen(sched.next_run_at)}
                            </span>
                            <span className="ml-auto font-mono text-[11px] text-gray-700">
                              next
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
