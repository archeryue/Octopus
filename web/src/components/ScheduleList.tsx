import { useCallback, useEffect } from "react";
import { IconClock } from "@tabler/icons-react";
import { useSessionStore } from "../stores/sessionStore";

/** Sidebar "Schedules" section. No longer a per-agent create form — creation
 * moved to the `/schedule` chat command. This is now just the entry point to
 * the all-agents Schedules overview (SchedulesDialog, owned by App). It loads
 * the schedule list once so the header can show a live count. */
export function ScheduleList({ onOpen }: { onOpen: () => void }) {
  const token = useSessionStore((s) => s.token);
  const schedules = useSessionStore((s) => s.schedules);
  const setSchedules = useSessionStore((s) => s.setSchedules);

  const fetchSchedules = useCallback(async () => {
    const resp = await fetch("/api/schedules", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (resp.ok) setSchedules(await resp.json());
  }, [token, setSchedules]);

  useEffect(() => {
    if (token) fetchSchedules();
  }, [token, fetchSchedules]);

  const count = schedules.length;

  return (
    <div className="schedule-section shrink-0">
      <button
        type="button"
        className="schedule-header group flex h-8 w-full items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors"
        onClick={onOpen}
        title="View all schedules"
      >
        <span className="schedule-title text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Schedules
        </span>
        <span className="schedule-count flex items-center gap-1 text-xs text-sidebar-foreground/60 group-hover:text-sidebar-foreground transition-colors">
          {count > 0 && <span className="tabular-nums">{count}</span>}
          {/* h-6 w-6 box mirrors the "+" buttons on the other sections so the
              icon lands at the same x/y as theirs. */}
          <span className="inline-flex h-6 w-6 items-center justify-center">
            <IconClock size={14} />
          </span>
        </span>
      </button>
    </div>
  );
}
