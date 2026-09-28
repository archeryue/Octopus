/**
 * Renderer tests for the Applications sidebar section (the console design's
 * workspace half): the seed fetch, selection, status dots, the empty state,
 * and pins (sidebar-pins.md) — only pinned apps, in their order, plus an
 * unpinned one while it needs you.
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
    pinned: true,
    pin_order: 1,
    ...overrides,
  } as Application;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  useSessionStore.setState({
    token: "tok",
    applications: [],
    activeApplicationId: null,
    mainView: "chat",
    unseenFailedApplications: [],
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

  it("lists only pinned applications, in pin order", () => {
    useSessionStore.setState({
      applications: [
        application({ id: "a1", name: "First Made", pin_order: 2 }),
        application({ id: "a2", name: "Moved Up", pin_order: 1 }),
        application({ id: "a3", name: "Not Pinned", pinned: false }),
      ],
    });
    const { container } = render(<SidebarApplications />);
    expect(
      [...container.querySelectorAll(".application-name")].map((n) => n.textContent)
    ).toEqual(["Moved Up", "First Made"]);
  });

  it("unpin POSTs and takes the row out of the sidebar", async () => {
    useSessionStore.setState({ applications: [application()] });
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).endsWith("/a1/unpin")
        ? new Response(JSON.stringify(application({ pinned: false })), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200 })
    );
    render(<SidebarApplications />);
    fireEvent.click(screen.getByLabelText("Unpin Habit Tracker from the sidebar"));
    await waitFor(() => expect(screen.queryByText("Habit Tracker")).toBeNull());
    // Still a live application — only the shortcut went.
    expect(useSessionStore.getState().applications).toHaveLength(1);
  });

  it("the sidebar has no delete — that lives on the Applications page", () => {
    useSessionStore.setState({ applications: [application()] });
    render(<SidebarApplications />);
    expect(screen.queryByLabelText("Delete Habit Tracker")).toBeNull();
  });

  it("an unpinned app shows while open, building, or failed unseen", () => {
    useSessionStore.setState({
      applications: [
        application({ id: "a1", name: "Quiet", pinned: false }),
        application({ id: "a2", name: "Open", pinned: false }),
        application({ id: "a3", name: "Building", pinned: false, status: "building" }),
        application({ id: "a4", name: "Broke", pinned: false, status: "failed" }),
        application({ id: "a5", name: "Broke Seen", pinned: false, status: "failed" }),
      ],
      mainView: "application",
      activeApplicationId: "a2",
      unseenFailedApplications: ["a4"],
    });
    const { container } = render(<SidebarApplications />);
    const shown = [...container.querySelectorAll(".application-item.unpinned")].map(
      (n) => n.querySelector(".application-name")?.textContent
    );
    expect(shown).toEqual(["Open", "Building", "Broke"]);
    // Each offers the way to make it stay.
    expect(screen.getByLabelText("Pin Open to the sidebar")).toBeTruthy();
  });

  it("opening an app clears its unseen failure", () => {
    useSessionStore.setState({
      applications: [application({ pinned: false, status: "failed" })],
      unseenFailedApplications: ["a1"],
    });
    render(<SidebarApplications />);
    fireEvent.click(screen.getByText("Habit Tracker"));
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
  });

  it("the + opens the Create tab", () => {
    useSessionStore.setState({ pageTab: "all" });
    render(<SidebarApplications />);
    fireEvent.click(screen.getByLabelText("New application"));
    expect(useSessionStore.getState().pageTab).toBe("form");
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
