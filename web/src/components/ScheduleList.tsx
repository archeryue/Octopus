import { useCallback, useEffect, useState } from "react";
import { IconCircle, IconCircleFilled, IconX } from "@tabler/icons-react";
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
    <div className="schedule-section border-t border-border">
      <div className="schedule-header flex items-center justify-between px-4 py-2">
        <span className="schedule-title text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Schedules
        </span>
        <button
          className="btn-schedule-add inline-flex h-6 w-6 items-center justify-center rounded-md text-base text-primary hover:bg-accent"
          onClick={() => setShowForm(!showForm)}
          aria-label={showForm ? "Cancel" : "New schedule"}
        >
          {showForm ? "−" : "+"}
        </button>
      </div>

      {showForm && (
        <div className="schedule-form px-3 pb-3 space-y-2">
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
            className="flex w-full rounded-md border border-border bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none transition-colors focus:border-ring focus:ring-2 focus:ring-ring/30 resize-none"
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

      <div>
        {sessionSchedules.map((sched) => (
          <div
            key={sched.id}
            className={`schedule-item flex items-center justify-between px-4 py-2 border-b border-border text-sm ${
              !sched.enabled ? "disabled opacity-50" : ""
            }`}
          >
            <div className="schedule-item-info flex items-center gap-2 min-w-0">
              <span className="schedule-name truncate text-foreground">{sched.name}</span>
              <span className="schedule-interval text-xs text-muted-foreground whitespace-nowrap">
                every {sched.interval_seconds / 60}m
              </span>
            </div>
            <div className="schedule-item-actions flex items-center gap-1">
              <button
                className={`btn-toggle ${sched.enabled ? "on" : "off"} inline-flex h-6 w-6 items-center justify-center rounded-md hover:bg-accent ${
                  sched.enabled ? "text-primary" : "text-muted-foreground"
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
                className="btn-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
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
