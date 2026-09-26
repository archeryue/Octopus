/**
 * The Monitor page.
 *
 * The case it exists for: a connector that fails every day must be visible as
 * data, not only as a log line nobody greps (docs/plans/polish-2026-09.md §9).
 * So the tests that matter are the ones where something IS wrong and the page
 * says so — and the one where nothing has been recorded yet, which must read as
 * "nothing recorded" rather than as a clean bill of health.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { MonitorPage } from "./MonitorPage";
import { useSessionStore } from "../stores/sessionStore";

const overview = (o: Record<string, unknown> = {}) => ({
  window: "24h",
  counts: [{ kind: "turn", n: 12, failures: 1 }],
  turns: [
    {
      backend: "claude-code",
      n: 12,
      ok_n: 11,
      avg_ms: 4200.0,
      max_ms: 91000.0,
      p50_ms: 3100.0,
      p95_ms: 88000.0,
      p99_ms: 91000.0,
    },
  ],
  errors: [{ kind: "connector_call", error_code: "connector_unavailable", n: 11, last_seen: 1758800000 }],
  resources: [{ metric: "mcp_sidecar_count", n: 60, avg: 0, peak: 0, latest: 0 }],
  ...o,
});

function mockFetch(payload: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: async () => payload,
  } as Response);
}

beforeEach(() => {
  useSessionStore.setState({ token: "t" });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MonitorPage", () => {
  it("shows the turn count and the window in the header", async () => {
    vi.stubGlobal("fetch", mockFetch(overview()));
    render(<MonitorPage />);
    await waitFor(() => expect(screen.getByText(/12 turns/)).toBeTruthy());
    expect(screen.getByText(/last 24h/)).toBeTruthy();
  });

  it("surfaces a persistently failing connector", async () => {
    vi.stubGlobal("fetch", mockFetch(overview()));
    render(<MonitorPage />);
    await waitFor(() =>
      expect(screen.getByText("connector_unavailable")).toBeTruthy()
    );
    // The kind is named too, so the row says WHAT failed, not just how it failed.
    expect(screen.getByText("connector_call")).toBeTruthy();
    // 11 appears in more than one table (also turns.ok_n), so assert the row
    // rather than the bare number.
    const row = screen.getByText("connector_unavailable").closest("tr");
    expect(row?.textContent).toContain("11");
  });

  it("keeps the latency tail visible rather than averaging it away", async () => {
    vi.stubGlobal("fetch", mockFetch(overview()));
    render(<MonitorPage />);
    // p99 of 91s must render as its own column, not be hidden behind the mean.
    await waitFor(() => expect(screen.getByText("p99")).toBeTruthy());
    expect(screen.getAllByText("91.0s").length).toBeGreaterThan(0);
  });

  it("reports an empty section as 'nothing recorded', never as blank", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(overview({ counts: [], turns: [], errors: [], resources: [] }))
    );
    render(<MonitorPage />);
    await waitFor(() =>
      expect(screen.getAllByText(/Nothing recorded in this window/).length).toBe(4)
    );
  });

  it("explains a 503 instead of showing an error", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ detail: "No metrics yet — the store is created when the server starts." }, false, 503)
    );
    render(<MonitorPage />);
    await waitFor(() => expect(screen.getByText(/No metrics yet/)).toBeTruthy());
  });

  it("refetches when the window changes", async () => {
    const f = mockFetch(overview());
    vi.stubGlobal("fetch", f);
    render(<MonitorPage />);
    await waitFor(() => expect(f).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText("7d"));
    await waitFor(() => expect(f).toHaveBeenCalledTimes(2));
    expect(String(f.mock.calls[1][0])).toContain("window=7d");
  });

  it("sends the bearer token", async () => {
    const f = mockFetch(overview());
    vi.stubGlobal("fetch", f);
    render(<MonitorPage />);
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(f.mock.calls[0][1].headers.Authorization).toBe("Bearer t");
  });
});
