/**
 * Renderer tests for the Applications sidebar section (the console design's
 * workspace half): the seed fetch, selection, status dots and the empty state.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SidebarApplications } from "./SidebarApplications";
import { useSessionStore, type Application } from "../stores/sessionStore";

function application(overrides: Partial<Application> = {}): Application {
  return {
    id: "a1",
    name: "Habit Tracker",
    description: "Track habits",
    icon: "✅",
    agent_id: "ag1",
    session_id: "s1",
    app_dir: "/tmp/apps/habit-tracker",
    entrypoint: "index.html",
    status: "ready",
    error: null,
    archived: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    last_built_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  useSessionStore.setState({
    token: "tok",
    applications: [],
    activeApplicationId: null,
    mainView: "chat",
  });
  fetchMock = vi.fn(async () => new Response("[]", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SidebarApplications", () => {
  it("seeds the list from GET /api/applications", async () => {
    fetchMock.mockImplementation(
      async () =>
        new Response(JSON.stringify([application()]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
    );
    render(<SidebarApplications />);
    await waitFor(() =>
      expect(useSessionStore.getState().applications).toHaveLength(1)
    );
    expect(screen.getByText("Habit Tracker")).toBeTruthy();
  });

  it("asks only for live applications — archived ones live behind the tab", async () => {
    render(<SidebarApplications />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const urls = fetchMock.mock.calls.map(([u]) => String(u));
    expect(urls.some((u) => u.endsWith("/api/applications"))).toBe(true);
    expect(urls.some((u) => u.includes("archived=true"))).toBe(false);
  });

  it("the + opens the create pane", () => {
    render(<SidebarApplications />);
    fireEvent.click(screen.getByLabelText("New application"));
    expect(useSessionStore.getState().mainView).toBe("application-create");
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it("offers the create flow when there are none", () => {
    render(<SidebarApplications />);
    fireEvent.click(screen.getByText(/No applications yet/));
    expect(useSessionStore.getState().mainView).toBe("application-create");
  });

  it("clicking a row opens it in the main pane", () => {
    useSessionStore.setState({ applications: [application()] });
    render(<SidebarApplications />);
    fireEvent.click(screen.getByText("Habit Tracker"));
    expect(useSessionStore.getState().mainView).toBe("application");
    expect(useSessionStore.getState().activeApplicationId).toBe("a1");
  });

  it("only unfinished applications carry a status dot", () => {
    useSessionStore.setState({
      applications: [
        application(),
        application({ id: "a2", name: "Building One", status: "building" }),
        application({ id: "a3", name: "Broken One", status: "failed" }),
      ],
    });
    const { container } = render(<SidebarApplications />);
    // A ready app needs no dot — the row itself says it's fine.
    expect(container.querySelectorAll(".app-status-ready")).toHaveLength(0);
    expect(container.querySelectorAll(".app-status-building")).toHaveLength(1);
    expect(container.querySelectorAll(".app-status-failed")).toHaveLength(1);
  });

  it("marks the open application as selected", () => {
    useSessionStore.setState({
      applications: [application()],
      activeApplicationId: "a1",
      mainView: "application",
    });
    const { container } = render(<SidebarApplications />);
    expect(container.querySelector(".application-item.active")).toBeTruthy();
  });

  it("delete confirms, drops the row, and DELETEs", async () => {
    useSessionStore.setState({ applications: [application()] });
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<SidebarApplications />);

    fireEvent.click(screen.getByLabelText("Delete Habit Tracker"));
    expect(useSessionStore.getState().applications).toHaveLength(0);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, init]) =>
            String(u).endsWith("/api/applications/a1") &&
            (init as RequestInit | undefined)?.method === "DELETE"
        )
      ).toBe(true)
    );
  });

  it("delete does nothing when the confirm is declined", () => {
    useSessionStore.setState({ applications: [application()] });
    vi.stubGlobal("confirm", vi.fn(() => false));
    render(<SidebarApplications />);

    fireEvent.click(screen.getByLabelText("Delete Habit Tracker"));
    expect(useSessionStore.getState().applications).toHaveLength(1);
    expect(
      fetchMock.mock.calls.some(
        ([, init]) => (init as RequestInit | undefined)?.method === "DELETE"
      )
    ).toBe(false);
  });

  it("deleting the open application returns the pane to chat", () => {
    useSessionStore.setState({
      applications: [application()],
      activeApplicationId: "a1",
      mainView: "application",
    });
    useSessionStore.getState().removeApplication("a1");
    expect(useSessionStore.getState().mainView).toBe("chat");
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it("selecting a session leaves the application view", () => {
    useSessionStore.setState({
      activeApplicationId: "a1",
      mainView: "application",
    });
    useSessionStore.getState().setActiveSessionId("s9");
    expect(useSessionStore.getState().mainView).toBe("chat");
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });
});
