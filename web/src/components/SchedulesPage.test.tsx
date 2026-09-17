/**
 * The Schedules page's run history.
 *
 * "It ran at 07:18" is half an answer — what it *did* is in the session that
 * fire ran in, so the row opens it (native scheduling parity, VM0 links its
 * schedules to `lastRunId` for the same reason).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SchedulesPage } from "./SchedulesPage";
import { useSessionStore, type Schedule } from "../stores/sessionStore";

const schedule = (o: Partial<Schedule> = {}): Schedule =>
  ({
    id: "sch1",
    agent_id: "ag1",
    name: "Daily digest",
    prompt: "summarize the inbox",
    interval_seconds: null,
    cron: "18 7 * * *",
    timezone: "America/Los_Angeles",
    recurrence_label: "Every day at 7:18 AM",
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    last_run_at: new Date().toISOString(),
    origin_session_id: null,
    run_at: null,
    next_run_at: null,
    last_run_session_id: null,
    ...o,
  }) as Schedule;

function mount(schedules: Schedule[]) {
  useSessionStore.setState({
    token: "tok",
    schedules,
    agents: [
      { id: "ag1", name: "Octo", avatar: "🐙" } as unknown as ReturnType<
        typeof useSessionStore.getState
      >["agents"][number],
    ],
    sessions: [],
    activeSessionId: null,
  });
  return render(<SchedulesPage onToggleSidebar={() => {}} />);
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("[]", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
    )
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SchedulesPage run history", () => {
  it("opens the session a run happened in", async () => {
    const { container } = mount([
      schedule({ last_run_session_id: "sess-42" }),
    ]);
    // The card starts collapsed; open it to reach the run history.
    fireEvent.click(screen.getByText("Daily digest"));

    const row = container.querySelector(".schedule-last-run");
    expect(row).toBeTruthy();
    fireEvent.click(row!);

    await waitFor(() =>
      expect(useSessionStore.getState().activeSessionId).toBe("sess-42")
    );
  });

  it("leaves the row inert when the fire predates the link", () => {
    // Rows that ran before the column existed still say when they ran; they
    // just have nothing to open.
    const { container } = mount([schedule({ last_run_session_id: null })]);
    fireEvent.click(screen.getByText("Daily digest"));
    expect(container.querySelector(".schedule-last-run")).toBeNull();
    expect(screen.getByText(/last run/)).toBeTruthy();
  });
});
