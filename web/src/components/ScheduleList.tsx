import { useCallback, useEffect, useState } from "react";
import { IconCircle, IconCircleFilled, IconPlus, IconX } from "@tabler/icons-react";
import { useSessionStore, type Schedule } from "../stores/sessionStore";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

export function ScheduleList() {
  const token = useSessionStore((s) => s.token);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const schedules = useSessionStore((s) => s.schedules);
  const setSchedules = useSessionStore((s) => s.setSchedules);

  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [intervalMin, setIntervalMin] = useState("5");
  const [showForm, setShowForm] = useState(false);

  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };

  const fetchSchedules = useCallback(async () => {
    const resp = await fetch("/api/schedules", { headers: { Authorization: `Bearer ${token}` } });
    if (resp.ok) setSchedules(await resp.json());
  }, [token, setSchedules]);

  useEffect(() => {
    if (token) fetchSchedules();
  }, [token, fetchSchedules]);

  const sessionSchedules = schedules.filter((s) => s.session_id === activeSessionId);

  const handleCreate = async () => {
    if (!name.trim() || !prompt.trim() || !activeSessionId) return;
    const interval = Math.max(1, parseInt(intervalMin, 10) || 5) * 60;
    const resp = await fetch("/api/schedules", {
      method: "POST",
      headers,
      body: JSON.stringify({
        session_id: activeSessionId,
        name: name.trim(),
        prompt: prompt.trim(),
        interval_seconds: interval,
      }),
    });
    if (resp.ok) {
      setName("");
      setPrompt("");
      setIntervalMin("5");
      setShowForm(false);
      fetchSchedules();
    }
  };

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

  if (!activeSessionId) return null;

  return (
    <div className="schedule-section shrink-0 pb-6 pt-3">
      <div className="schedule-header group flex h-10 items-center justify-between rounded-lg px-3 hover:bg-sidebar-accent transition-colors">
        <span className="schedule-title text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Schedules
        </span>
        <button
          className="btn-schedule-add inline-flex h-7 w-7 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={() => setShowForm(!showForm)}
          aria-label={showForm ? "Cancel" : "New schedule"}
        >
          {showForm ? <IconX size={16} /> : <IconPlus size={16} />}
        </button>
      </div>

      {showForm && (
        <div className="schedule-form mt-3 rounded-lg border-[0.7px] border-border bg-card p-5 space-y-4">
          <Input
            className="h-9 text-sm"
            placeholder="Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <textarea
            placeholder="Prompt..."
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={2}
            className="flex w-full rounded-lg border-[0.7px] border-gray-400 bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none transition-colors focus:border-primary focus:ring-[3px] focus:ring-primary/10 resize-none"
          />
          <div className="schedule-form-row flex items-center gap-2 text-xs text-muted-foreground">
            <label>Every</label>
            <Input
              type="number"
              min="1"
              value={intervalMin}
              onChange={(e) => setIntervalMin(e.target.value)}
              className="interval-input h-8 w-14 text-center px-2 text-sm"
            />
            <label>min</label>
            <Button
              className="btn btn-create ml-auto"
              size="sm"
              onClick={handleCreate}
            >
              Create
            </Button>
          </div>
        </div>
      )}

      <div className="flex flex-col gap-1.5 mt-3">
        {sessionSchedules.map((sched) => (
          <div
            key={sched.id}
            className={`schedule-item group flex items-center gap-3 rounded-lg px-3 py-3 text-sm text-sidebar-foreground hover:bg-sidebar-accent transition-colors ${
              !sched.enabled ? "disabled opacity-50" : ""
            }`}
          >
            <span className="schedule-name truncate flex-1">{sched.name}</span>
            <span className="schedule-interval text-xs text-sidebar-foreground/60 whitespace-nowrap font-mono">
              {sched.interval_seconds / 60}m
            </span>
            <div className="schedule-item-actions flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
              <button
                className={`btn-toggle ${sched.enabled ? "on" : "off"} inline-flex h-6 w-6 items-center justify-center rounded-md hover:bg-card ${
                  sched.enabled ? "text-primary" : "text-sidebar-foreground/50"
                }`}
                onClick={() => handleToggle(sched)}
                title={sched.enabled ? "Disable" : "Enable"}
              >
                {sched.enabled ? (
                  <IconCircleFilled size={12} />
                ) : (
                  <IconCircle size={12} />
                )}
              </button>
              <button
                className="btn-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/60 hover:bg-destructive/10 hover:text-destructive"
                onClick={() => handleDelete(sched.id)}
                title="Delete"
              >
                <IconX size={14} />
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
