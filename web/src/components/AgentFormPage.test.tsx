/**
 * The Agents page — the Create/Edit form (create, edit, archive) and the All
 * tab (sidebar-pins.md §6): every agent by section, pin toggles, Chat, and
 * restore from the Archived section.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AgentFormPage } from "./AgentFormPage";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";

function agent(o: Partial<Agent> = {}): Agent {
  return {
    id: "ag1",
    name: "Octo",
    description: "",
    avatar: "🐙",
    system_prompt: "",
    model: null,
    credential_id: null,
    backend: "claude-code",
    mcp_servers: ["ask", "bg"],
    tool_allow: "",
    tool_deny: "",
    is_system: true,
    archived: false,
    pinned: true,
    pin_order: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    active_session_count: 0,
    ...o,
  } as Agent;
}

let fetchMock: ReturnType<typeof vi.fn>;

function mount(
  editingAgentId: string | null = null,
  agents: Agent[] = [agent()],
  pageTab: "all" | "form" = "form"
) {
  useSessionStore.setState({
    token: "tok",
    agents,
    sessions: [],
    editingAgentId,
    pageTab,
    credentials: [],
    connectorInstallations: [],
    availableBackends: ["claude-code", "codex"],
    mainView: "agent-form",
  });
  return render(<AgentFormPage onToggleSidebar={() => {}} />);
}

beforeEach(() => {
  fetchMock = vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AgentFormPage", () => {
  it("POSTs a new agent and returns to chat", async () => {
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST"
        ? new Response(JSON.stringify(agent({ id: "ag2", name: "sre-ops", is_system: false })), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    mount(null);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sre-ops" } });
    fireEvent.click(screen.getByRole("button", { name: "Create Agent" }));

    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    expect(useSessionStore.getState().agents.some((a) => a.id === "ag2")).toBe(true);
  });

  it("seeds the form when editing and PATCHes on save", async () => {
    const existing = agent({ id: "ag9", name: "Vera", is_system: false, system_prompt: "review code" });
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "PATCH"
        ? new Response(JSON.stringify({ ...existing, description: "reviewer" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    mount("ag9", [existing]);
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Vera");
    expect((screen.getByLabelText(/System prompt/) as HTMLTextAreaElement).value).toBe(
      "review code"
    );

    fireEvent.change(screen.getByLabelText("One-line role"), {
      target: { value: "reviewer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, i]) => String(u).endsWith("/ag9") && (i as RequestInit)?.method === "PATCH"
        )
      ).toBe(true)
    );
  });

  it("won't offer Archive on the protected system agent", () => {
    // Scoped by class, not by name: the Archived *tab* also says "Archive…".
    const { container } = mount("ag1", [agent()]);
    expect(container.querySelector(".btn-agent-archive")).toBeNull();
  });

  it("archives an ordinary agent and drops it from the sidebar", async () => {
    const vera = agent({ id: "ag9", name: "Vera", is_system: false });
    const { container } = mount("ag9", [vera]);
    fireEvent.click(container.querySelector(".btn-agent-archive")!);
    await waitFor(() =>
      expect(useSessionStore.getState().agents.some((a) => a.id === "ag9")).toBe(false)
    );
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).endsWith("/ag9/archive"))
    ).toBe(true);
  });

  it("lists every agent on the All tab, by section", async () => {
    const retired = agent({ id: "ag8", name: "Retired", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).includes("include_archived=true")
        ? json([agent(), retired])
        : json([])
    );
    const { container } = mount(null, [
      agent(),
      agent({ id: "ag2", name: "Vera", is_system: false, pinned: false, pin_order: 2 }),
      agent({ id: "ag3", name: "Scout", is_system: false, pin_order: 3 }),
    ], "all");

    await waitFor(() => expect(screen.getByText("Retired")).toBeTruthy());
    const names = (section: string) =>
      [...container.querySelectorAll(`.library-${section} .library-name`)].map(
        (n) => n.textContent
      );
    expect(names("pinned")).toEqual(["Octo", "Scout"]);
    expect(names("unpinned")).toEqual(["Vera"]);
    expect(names("archived")).toEqual(["Retired"]);
    // The tab counts live and archived together.
    expect(screen.getByText("All 4")).toBeTruthy();
  });

  it("filters by name and description", () => {
    const { container } = mount(null, [
      agent(),
      agent({ id: "ag2", name: "Vera", description: "code reviewer", is_system: false }),
    ], "all");
    fireEvent.change(screen.getByLabelText("Filter agents"), {
      target: { value: "review" },
    });
    expect(
      [...container.querySelectorAll(".library-name")].map((n) => n.textContent)
    ).toEqual(["Vera"]);
  });

  it("pins and unpins from the All tab; the Default Agent stays pinned", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false, pinned: false });
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).endsWith("/ag2/pin")
        ? json({ ...vera, pinned: true, pin_order: 2 })
        : json([])
    );
    mount(null, [agent(), vera], "all");

    expect(screen.getByLabelText("Octo is always in the sidebar")).toBeTruthy();
    expect(screen.queryByLabelText("Unpin Octo from the sidebar")).toBeNull();

    fireEvent.click(screen.getByLabelText("Pin Vera to the sidebar"));
    await waitFor(() =>
      expect(
        useSessionStore.getState().agents.find((a) => a.id === "ag2")?.pinned
      ).toBe(true)
    );
  });

  it("Chat opens the agent's latest session", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false, pinned: false });
    mount(null, [agent(), vera], "all");
    useSessionStore.setState({
      sessions: [
        session({ id: "old", agent_id: "ag2", created_at: "2026-01-01T00:00:00Z" }),
        session({ id: "new", agent_id: "ag2", created_at: "2026-02-01T00:00:00Z" }),
        // An application's conversation is not the user's chat.
        session({ id: "app", agent_id: "ag2", origin: "app", created_at: "2026-03-01T00:00:00Z" }),
      ],
    });
    fireEvent.click(
      screen.getAllByRole("button", { name: "Chat" })[1]
    );
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe("new"));
    expect(useSessionStore.getState().mainView).toBe("chat");
  });

  it("Chat starts a session when the agent has none", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      String(url).endsWith("/api/agents/ag2/sessions") && init?.method === "POST"
        ? json(session({ id: "fresh", agent_id: "ag2" }), 201)
        : json([])
    );
    mount(null, [agent(), vera], "all");
    fireEvent.click(screen.getAllByRole("button", { name: "Chat" })[1]);
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe("fresh"));
    expect(useSessionStore.getState().sessions.some((s) => s.id === "fresh")).toBe(true);
  });

  it("archives from a row and keeps the page open", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false });
    const { container } = mount(null, [agent(), vera], "all");
    fireEvent.click(screen.getByLabelText("Archive Vera"));
    await waitFor(() =>
      expect(useSessionStore.getState().agents.some((a) => a.id === "ag2")).toBe(false)
    );
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    expect(container.querySelector(".library-archived")).toBeNull(); // the mock archive is empty
    expect(screen.queryByLabelText("Archive Octo")).toBeNull();
  });

  it("restores an archived agent and it comes back pinned", async () => {
    const retired = agent({ id: "ag8", name: "Retired", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive"))
        return json({ ...retired, archived: false, pinned: true, pin_order: 2 });
      if (String(url).includes("include_archived=true")) return json([agent(), retired]);
      return json([]);
    });
    const { container } = mount(null, [agent()], "all");
    await waitFor(() => expect(screen.getByText("Retired")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(useSessionStore.getState().agents.some((a) => a.id === "ag8")).toBe(true)
    );
    // It stays on the All tab and moves to "In sidebar".
    expect(useSessionStore.getState().pageTab).toBe("all");
    await waitFor(() =>
      expect(
        [...container.querySelectorAll(".library-pinned .library-name")].map(
          (n) => n.textContent
        )
      ).toEqual(["Octo", "Retired"])
    );
  });

  it("surfaces a name clash when restoring", async () => {
    const retired = agent({ id: "ag8", name: "Scout", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive"))
        return json({ detail: "An agent named 'Scout' already exists" }, 400);
      if (String(url).includes("include_archived=true")) return json([retired]);
      return json([]);
    });
    mount(null, [agent()], "all");
    await waitFor(() => expect(screen.getByText("Scout")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(screen.getByText(/already exists/)).toBeTruthy());
  });

  it("names the second tab Edit while an agent is open", () => {
    mount("ag1", [agent()]);
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
    cleanup();
    mount(null, [agent()]);
    expect(screen.getByRole("button", { name: "Create" })).toBeTruthy();
  });
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function session(o: Partial<SessionInfo> = {}): SessionInfo {
  return {
    id: "s1",
    name: "Session",
    working_dir: "/tmp",
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    origin: "user",
    agent_id: "ag1",
    ...o,
  } as SessionInfo;
}
