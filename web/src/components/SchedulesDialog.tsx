import { useCallback, useEffect } from "react";
import {
  IconCircle,
  IconCircleFilled,
  IconClock,
  IconX,
} from "@tabler/icons-react";
import { useSessionStore, type Agent, type Schedule } from "../stores/sessionStore";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}

/** Read-only overview of every agent's schedules. Creation happens from chat
 * via the `/schedule` command (see ChatView); this page is where you see them
 * all in one place and toggle/delete them. Grouped by owning agent. */
export function SchedulesDialog({ open, onOpenChange }: Props) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const schedules = useSessionStore((s) => s.schedules);
  const setSchedules = useSessionStore((s) => s.setSchedules);

  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };

  const fetchSchedules = useCallback(async () => {
    const resp = await fetch("/api/schedules", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (resp.ok) setSchedules(await resp.json());
  }, [token, setSchedules]);

  // Refetch each time the overview opens so it reflects schedules created from
  // other chats (or other clients) since the last sidebar load.
  useEffect(() => {
    if (open) fetchSchedules();
  }, [open, fetchSchedules]);

  const handleToggle = async (sched: Schedule) => {
    await fetch(`/api/schedules/${sched.id}`, {
      method: "PATCH",
      headers,
      body: JSON.stringify({ enabled: !sched.enabled }),
    });
    fetchSchedules();
  };

  const handleDelete = async (id: string) => {
    await fetch(`/api/schedules/${id}`, { method: "DELETE", headers });
    fetchSchedules();
  };

  // Group by owning agent; trailing "unknown" bucket catches schedules whose
  // agent isn't in the loaded list (archived agent, etc.) so none vanish.
  const groups: { agent: Agent | null; items: Schedule[] }[] = agents
    .map((a) => ({
      agent: a as Agent | null,
      items: schedules.filter((s) => s.agent_id === a.id),
    }))
    .filter((g) => g.items.length > 0);
  const knownAgentIds = new Set(agents.map((a) => a.id));
  const orphans = schedules.filter((s) => !knownAgentIds.has(s.agent_id));
  if (orphans.length) groups.push({ agent: null, items: orphans });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="schedules-dialog max-w-xl">
        <DialogHeader>
          <DialogTitle>Schedules</DialogTitle>
          <DialogDescription>
            Recurring prompts across all your agents. Create one from any chat
            with{" "}
            <code className="font-mono text-foreground">
              /schedule 30m your prompt
            </code>
            .
          </DialogDescription>
        </DialogHeader>

        {schedules.length === 0 ? (
          <div className="schedules-empty rounded-lg border border-dashed border-border bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground">
            No schedules yet. Type{" "}
            <code className="font-mono text-foreground">/schedule 30m …</code>{" "}
            in any chat to create one.
          </div>
        ) : (
          <div className="schedules-groups space-y-4 max-h-[60vh] overflow-y-auto">
            {groups.map(({ agent, items }, gi) => (
              <div
                key={agent?.id ?? `orphan-${gi}`}
                className="schedule-agent-group"
              >
                <div className="schedule-agent-header flex items-center gap-1.5 px-1 pb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  <span className="text-sm leading-none">
                    {agent?.avatar || "🐙"}
                  </span>
                  <span className="truncate">
                    {agent?.name ?? "Unknown agent"}
                  </span>
                </div>
                <div className="flex flex-col gap-1">
                  {items.map((sched) => (
                    <ScheduleRow
                      key={sched.id}
                      sched={sched}
                      onToggle={handleToggle}
                      onDelete={handleDelete}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function ScheduleRow({
  sched,
  onToggle,
  onDelete,
}: {
  sched: Schedule;
  onToggle: (s: Schedule) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div
      className={`schedule-item group flex items-start gap-2 rounded-lg border border-border bg-card px-3 py-2 transition-colors ${
        !sched.enabled ? "disabled opacity-60" : ""
      }`}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="schedule-name truncate text-sm font-medium text-foreground">
            {sched.name}
          </span>
          <span className="schedule-interval inline-flex items-center gap-1 whitespace-nowrap text-xs text-muted-foreground">
            <IconClock size={11} />
            {sched.recurrence_label || "—"}
          </span>
        </div>
        <div
          className="schedule-prompt mt-0.5 truncate text-xs text-muted-foreground"
          title={sched.prompt}
        >
          {sched.prompt}
        </div>
        {sched.last_run_at && (
          <div className="schedule-lastrun mt-0.5 text-[11px] text-muted-foreground/70">
            Last run {new Date(sched.last_run_at).toLocaleString()}
          </div>
        )}
      </div>
      <div className="schedule-item-actions flex shrink-0 items-center gap-0.5">
        <button
          className={`btn-toggle ${
            sched.enabled ? "on" : "off"
          } inline-flex h-7 w-7 items-center justify-center rounded-md hover:bg-accent ${
            sched.enabled ? "text-primary" : "text-muted-foreground/60"
          }`}
          onClick={() => onToggle(sched)}
          title={sched.enabled ? "Disable" : "Enable"}
        >
          {sched.enabled ? (
            <IconCircleFilled size={13} />
          ) : (
            <IconCircle size={13} />
          )}
        </button>
        <button
          className="btn-delete inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground/60 hover:bg-destructive/10 hover:text-destructive"
          onClick={() => onDelete(sched.id)}
          title="Delete schedule"
        >
          <IconX size={14} />
        </button>
      </div>
    </div>
  );
}
