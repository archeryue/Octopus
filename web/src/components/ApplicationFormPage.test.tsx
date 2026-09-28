/**
 * The Applications page — the Create form, and the All tab (sidebar-pins.md
 * §6): every application by section, with open, pin, archive, restore and
 * delete.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ApplicationFormPage } from "./ApplicationFormPage";
import { useSessionStore, type Agent, type Application } from "../stores/sessionStore";

const agent = { id: "ag1", name: "Octo", avatar: "🐙", is_system: true, backend: "claude-code" } as unknown as Agent;

function application(o: Partial<Application> = {}): Application {
  return {
    id: "a1",
    name: "Habit Tracker",
    description: "Track habits",
    icon: "✅",
    agent_id: "ag1",
    session_id: "s1",
    app_dir: "/tmp/apps/habit",
    entrypoint: "index.html",
    status: "ready",
    error: null,
    archived: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    last_built_at: null,
    pinned: true,
    pin_order: 1,
    ...o,
  } as Application;
}

let fetchMock: ReturnType<typeof vi.fn>;

function mount(
  pageTab: "all" | "form" = "form",
  applications: Application[] = []
) {
  useSessionStore.setState({
    token: "tok",
    agents: [agent],
    activeAgentId: "ag1",
    applications,
    mainView: "application-create",
    pageTab,
  });
  return render(<ApplicationFormPage onToggleSidebar={() => {}} />);
}

beforeEach(() => {
  fetchMock = vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ApplicationFormPage", () => {
  it("keeps Start Build disabled until there's a name and a brief", () => {
    mount();
    const submit = screen.getByRole("button", { name: /Start Build/ });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tracker" } });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/What problem/), {
      target: { value: "track things" },
    });
    expect((submit as HTMLButtonElement).disabled).toBe(false);
  });

  it("POSTs the brief and opens the new application", async () => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      String(url).endsWith("/api/applications") && init?.method === "POST"
        ? new Response(JSON.stringify(application({ archived: false })), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Habit Tracker" } });
    fireEvent.change(screen.getByLabelText(/What problem/), {
      target: { value: "track habits" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));

    await waitFor(() =>
      expect(useSessionStore.getState().mainView).toBe("application")
    );
    expect(useSessionStore.getState().activeApplicationId).toBe("a1");
    const body = fetchMock.mock.calls.find(
      ([u, i]) => String(u).endsWith("/api/applications") && (i as RequestInit)?.method === "POST"
    )?.[1] as RequestInit;
    expect(JSON.parse(String(body.body))).toMatchObject({
      name: "Habit Tracker",
      description: "track habits",
      agent_id: "ag1",
    });
  });

  it("lists every application on the All tab, by section", async () => {
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).includes("archived=true")
        ? json([application({ id: "a9", name: "Old Report" })])
        : json([])
    );
    const { container } = mount("all", [
      application({ id: "a1", archived: false, pin_order: 2 }),
      application({ id: "a2", name: "Reader", archived: false, pin_order: 1 }),
      application({ id: "a3", name: "Scratch", archived: false, pinned: false }),
    ]);
    await waitFor(() => expect(screen.getByText("Old Report")).toBeTruthy());
    const names = (section: string) =>
      [...container.querySelectorAll(`.library-${section} .library-name`)].map(
        (n) => n.textContent
      );
    // "In sidebar" follows the sidebar's order, not creation order.
    expect(names("pinned")).toEqual(["Reader", "Habit Tracker"]);
    expect(names("unpinned")).toEqual(["Scratch"]);
    expect(names("archived")).toEqual(["Old Report"]);
    expect(screen.getByText("All 4")).toBeTruthy();
  });

  it("opens, pins and unpins from a row", async () => {
    const scratch = application({ id: "a3", name: "Scratch", archived: false, pinned: false });
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).endsWith("/a3/pin") ? json({ ...scratch, pinned: true, pin_order: 2 }) : json([])
    );
    mount("all", [scratch]);
    fireEvent.click(screen.getByLabelText("Pin Scratch to the sidebar"));
    await waitFor(() =>
      expect(useSessionStore.getState().applications[0].pinned).toBe(true)
    );
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(useSessionStore.getState().mainView).toBe("application");
    expect(useSessionStore.getState().activeApplicationId).toBe("a3");
  });

  it("archives a row into the Archived section", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const live = application({ archived: false });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      String(url).endsWith("/a1/archive") && init?.method === "POST"
        ? json({ ...live, archived: true })
        : json([])
    );
    const { container } = mount("all", [live]);
    fireEvent.click(screen.getByLabelText("Archive Habit Tracker"));
    await waitFor(() =>
      expect(
        container.querySelector(".library-archived .library-name")?.textContent
      ).toBe("Habit Tracker")
    );
    expect(useSessionStore.getState().applications).toEqual([]);
  });

  it("never offers Delete — archive is the only way out", async () => {
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).includes("archived=true")
        ? json([application({ id: "a9", name: "Old Report" })])
        : json([])
    );
    mount("all", [application({ archived: false })]);
    await waitFor(() => expect(screen.getByText("Old Report")).toBeTruthy());
    expect(screen.queryByLabelText(/^Delete /)).toBeNull();
    expect(screen.getByLabelText("Archive Habit Tracker")).toBeTruthy();
  });

  it("restore puts it back pinned and stays on the page", async () => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes("/unarchive") && init?.method === "POST")
        return json(application({ archived: false }));
      if (String(url).includes("archived=true")) return json([application()]);
      return json([]);
    });
    const { container } = mount("all");
    await waitFor(() => expect(screen.getByText("Habit Tracker")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));

    await waitFor(() =>
      expect(useSessionStore.getState().applications.map((a) => a.id)).toEqual(["a1"])
    );
    expect(useSessionStore.getState().mainView).toBe("application-create");
    await waitFor(() =>
      expect(
        container.querySelector(".library-pinned .library-name")?.textContent
      ).toBe("Habit Tracker")
    );
  });

  it("says so plainly when there are none", async () => {
    mount("all");
    await waitFor(() => expect(screen.getByText(/No applications yet/)).toBeTruthy());
    fireEvent.click(screen.getByText(/Describe one/));
    expect(useSessionStore.getState().pageTab).toBe("form");
  });

  it("surfaces a create failure", async () => {
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST"
        ? new Response(JSON.stringify({ detail: "An application named 'x' already exists" }), {
            status: 409,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "d" } });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
    await waitFor(() => expect(screen.getByText(/already exists/)).toBeTruthy());
  });
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
