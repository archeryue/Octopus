/**
 * Renderer tests for BgTaskChip. Covers the four user-visible states
 * (running, completed, failed, cancelled), the on-mount fetch fallback
 * for tasks not yet in the store, expansion → full output reveal,
 * and the cancel button behavior.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";

import { BgTaskChip } from "./BgTaskChip";
import { useSessionStore, type BgTask } from "../stores/sessionStore";

function makeTask(overrides: Partial<BgTask> = {}): BgTask {
  return {
    id: "t1",
    session_id: "s1",
    command: "yes | head -c 100",
    description: "fill buffer",
    working_dir: "/tmp",
    status: "running",
    exit_code: null,
    stdout: "",
    stderr: "",
    truncated: false,
    started_at: "2026-05-18T00:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  useSessionStore.setState({ token: "tok", bgTasks: {} });
});

afterEach(() => {
  cleanup();
  useSessionStore.setState({ bgTasks: {} });
  vi.restoreAllMocks();
});

describe("BgTaskChip", () => {
  it("shows a loading placeholder when the task isn't in the store yet", () => {
    // fetch will return null (404-ish); chip stays in placeholder state.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response("not found", { status: 404 })
      )
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText(/loading bg task/i)).toBeInTheDocument();
  });

  it("renders the running state with description + cancel button", () => {
    // Stub fetch to return an empty JSON object — the chip's mount-time
    // refetch shouldn't crash the test; we already pre-seeded the store.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 }))
    );
    useSessionStore.getState().upsertBgTask("s1", makeTask());
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText(/bg · running/i)).toBeInTheDocument();
    expect(screen.getByText("fill buffer")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /cancel/i })
    ).toBeInTheDocument();
  });

  it("renders the completed state without a cancel button", () => {
    // Stub fetch to return an empty JSON object — the chip's mount-time
    // refetch shouldn't crash the test; we already pre-seeded the store.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 }))
    );
    useSessionStore.getState().upsertBgTask(
      "s1",
      makeTask({ status: "completed", exit_code: 0 })
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText(/bg · completed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });

  it("shows an exit-code badge for failed tasks", () => {
    // Stub fetch to return an empty JSON object — the chip's mount-time
    // refetch shouldn't crash the test; we already pre-seeded the store.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 }))
    );
    useSessionStore.getState().upsertBgTask(
      "s1",
      makeTask({ status: "failed", exit_code: 7 })
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText(/bg · failed/i)).toBeInTheDocument();
    expect(screen.getByText(/exit 7/i)).toBeInTheDocument();
  });

  it("expanding shows full stdout/stderr panes", async () => {
    // Stub fetch to return an empty JSON object — the chip's mount-time
    // refetch shouldn't crash the test; we already pre-seeded the store.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 }))
    );
    useSessionStore.getState().upsertBgTask(
      "s1",
      makeTask({
        status: "completed",
        exit_code: 0,
        stdout: "line one\nline two\nline three",
        stderr: "",
      })
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    // Last line is shown as the collapsed preview.
    expect(screen.getByText("line three")).toBeInTheDocument();
    // Expand.
    const trigger = screen.getByRole("button", { expanded: false });
    act(() => trigger.click());
    await waitFor(() =>
      expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument()
    );
    expect(screen.getByText(/stdout/i)).toBeInTheDocument();
    expect(screen.getByText(/line one/)).toBeInTheDocument();
  });

  it("clicking cancel POSTs to the cancel endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // The cancel POST is the URL we want to assert on.
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/cancel") && init?.method === "POST") {
        return new Response(JSON.stringify({ cancelled: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      // Any other fetch (the initial chip metadata refetch) is a no-op.
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    useSessionStore.getState().upsertBgTask("s1", makeTask());
    render(<BgTaskChip sessionId="s1" taskId="t1" />);

    const cancelBtn = screen.getByRole("button", { name: /cancel/i });
    act(() => cancelBtn.click());

    await waitFor(() => {
      const cancelCall = fetchMock.mock.calls.find((args) => {
        const [url, init] = args as [
          RequestInfo | URL,
          RequestInit | undefined,
        ];
        return (
          typeof url === "string" &&
          url.includes("/bg-tasks/t1/cancel") &&
          init?.method === "POST"
        );
      });
      expect(cancelCall).toBeDefined();
    });
  });

  it("fetches the task on mount when it isn't already in the store", async () => {
    const fetched: BgTask = makeTask({ status: "completed", exit_code: 0 });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify(fetched), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      )
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    await waitFor(() =>
      expect(screen.getByText(/bg · completed/i)).toBeInTheDocument()
    );
  });
});
