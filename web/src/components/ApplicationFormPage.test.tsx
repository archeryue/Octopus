/**
 * The application create page — including the Archived tab, which is where an
 * archived app comes back from (there is no catalog; the "market" is your own
 * shelf).
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
    ...o,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

function mount() {
  useSessionStore.setState({
    token: "tok",
    agents: [agent],
    activeAgentId: "ag1",
    applications: [],
    mainView: "application-create",
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

  it("lists archived applications behind the Archived tab", async () => {
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).includes("archived=true")
        ? new Response(JSON.stringify([application()]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    mount();
    await waitFor(() => expect(screen.getByText(/Archived 1/)).toBeTruthy());
    fireEvent.click(screen.getByText(/Archived 1/));
    expect(screen.getByText("Habit Tracker")).toBeTruthy();
  });

  it("restore puts it back and opens it", async () => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes("/unarchive") && init?.method === "POST")
        return new Response(JSON.stringify(application({ archived: false })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (String(url).includes("archived=true"))
        return new Response(JSON.stringify([application()]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    mount();
    await waitFor(() => expect(screen.getByText(/Archived 1/)).toBeTruthy());
    fireEvent.click(screen.getByText(/Archived 1/));
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));

    await waitFor(() =>
      expect(useSessionStore.getState().applications.map((a) => a.id)).toEqual(["a1"])
    );
    expect(useSessionStore.getState().mainView).toBe("application");
  });

  it("says so plainly when nothing is archived", async () => {
    mount();
    fireEvent.click(screen.getByText("Archived"));
    await waitFor(() => expect(screen.getByText(/Nothing archived/)).toBeTruthy());
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
