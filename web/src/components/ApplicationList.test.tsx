/**
 * Renderer tests for the Applications sidebar section (applications.md §7):
 * the seed fetch, status dots, selection, the empty state, and delete.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { ApplicationList } from "./ApplicationList";
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

describe("ApplicationList", () => {
  it("seeds the list from GET /api/applications", async () => {
    fetchMock.mockImplementation(
      async () =>
        new Response(JSON.stringify([application()]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
    );
    render(<ApplicationList />);
    await waitFor(() =>
      expect(useSessionStore.getState().applications).toHaveLength(1)
    );
    expect(screen.getByText("Habit Tracker")).toBeTruthy();
  });

  it("offers the create flow when there are none", () => {
    render(<ApplicationList />);
    fireEvent.click(screen.getByText(/No applications yet/));
    expect(useSessionStore.getState().mainView).toBe("application-create");
  });

  it("the + opens the create pane", () => {
    render(<ApplicationList />);
    fireEvent.click(screen.getByLabelText("New application"));
    expect(useSessionStore.getState().mainView).toBe("application-create");
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it("clicking a row opens it in the main pane", () => {
    useSessionStore.setState({ applications: [application()] });
    render(<ApplicationList />);
    fireEvent.click(screen.getByText("Habit Tracker"));
    expect(useSessionStore.getState().mainView).toBe("application");
    expect(useSessionStore.getState().activeApplicationId).toBe("a1");
  });

  it("marks status with a dot class per state", () => {
    useSessionStore.setState({
      applications: [
        application(),
        application({ id: "a2", name: "Building One", status: "building" }),
        application({ id: "a3", name: "Broken One", status: "failed" }),
      ],
    });
    const { container } = render(<ApplicationList />);
    expect(container.querySelectorAll(".app-status-ready")).toHaveLength(1);
    expect(container.querySelectorAll(".app-status-building")).toHaveLength(1);
    expect(container.querySelectorAll(".app-status-failed")).toHaveLength(1);
  });

  it("delete confirms, drops the row, and DELETEs", async () => {
    useSessionStore.setState({ applications: [application()] });
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<ApplicationList />);

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
    render(<ApplicationList />);

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
    expect(useSessionStore.getState().activeSessionId).toBe("s9");
  });
});
