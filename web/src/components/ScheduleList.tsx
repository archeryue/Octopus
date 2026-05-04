import { useCallback, useEffect, useState } from "react";
import { useSessionStore, type Schedule } from "../stores/sessionStore";

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
    <div className="schedule-section">
      <div className="schedule-header">
        <span className="schedule-title">Schedules</span>
        <button className="btn-schedule-add" onClick={() => setShowForm(!showForm)}>
          {showForm ? "−" : "+"}
        </button>
      </div>

      {showForm && (
        <div className="schedule-form">
          <input
            placeholder="Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <textarea
            placeholder="Prompt..."
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={2}
          />
          <div className="schedule-form-row">
            <label>Every</label>
            <input
              type="number"
              min="1"
              value={intervalMin}
              onChange={(e) => setIntervalMin(e.target.value)}
              className="interval-input"
            />
            <label>min</label>
            <button className="btn btn-create" onClick={handleCreate}>
              Create
            </button>
          </div>
        </div>
      )}

      {sessionSchedules.map((sched) => (
        <div key={sched.id} className={`schedule-item ${!sched.enabled ? "disabled" : ""}`}>
          <div className="schedule-item-info">
            <span className="schedule-name">{sched.name}</span>
            <span className="schedule-interval">
              every {sched.interval_seconds / 60}m
            </span>
          </div>
          <div className="schedule-item-actions">
            <button
              className={`btn-toggle ${sched.enabled ? "on" : "off"}`}
              onClick={() => handleToggle(sched)}
              title={sched.enabled ? "Disable" : "Enable"}
            >
              {sched.enabled ? "●" : "○"}
            </button>
            <button
              className="btn-delete"
              onClick={() => handleDelete(sched.id)}
              title="Delete"
            >
              ×
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
