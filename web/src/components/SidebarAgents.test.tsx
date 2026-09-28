/**
 * Renderer tests for the Agents sidebar section — specifically its fold state.
 *
 * The section used to unfold whichever agent it auto-selected on load, so
 * opening Octopus always spilled one agent's sessions into the sidebar before
 * you'd clicked anything. Every agent now starts folded on every load, and
 * unfolding is only ever something the user did.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SidebarAgents } from "./SidebarAgents";
import {
  useSessionStore,
  type Agent,
  type SessionInfo,
} from "../stores/sessionStore";

const agent = (o: Partial<Agent> = {}) =>
  ({
    id: "ag1",
    name: "Octo",
    description: "",
    avatar: "🐙",
    backend: "claude-code",
    credential_id: null,
    is_system: true,
    archived: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...o,
  }) as Agent;

const session = (o: Partial<SessionInfo> = {}) =>
  ({
    id: "s1",
    name: "ideas",
    working_dir: "/tmp",
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    message_count: 0,
    agent_id: "ag1",
    origin: "user",
    backend: "claude-code",
    credential_id: null,
    archived: false,
    ...o,
  }) as SessionInfo;

const AGENTS = [
  agent(),
  agent({ id: "ag2", name: "Researcher", is_system: false }),
];
const SESSIONS = [
  session(),
  session({ id: "s2", name: "notes" }),
  session({ id: "s3", name: "papers", agent_id: "ag2" }),
];

let fetchMock: ReturnType<typeof vi.fn>;

/** Mount with the server already answering — the section is the sidebar's
 * data orchestrator, so it fetches agents/sessions/backends on its own. */
function mount() {
  useSessionStore.setState({
    token: "tok",
    agents: [],
    sessions: [],
    activeAgentId: null,
    activeSessionId: null,
    credentials: [],
    availableBackends: ["claude-code"],
    mainView: "chat",
    sidebarCollapsed: false,
  });
  return render(<SidebarAgents />);
}

const jsonRes = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

beforeEach(() => {
  fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const u = String(url);
    if (u.includes("/api/agents")) return jsonRes(AGENTS);
    if (u.includes("/api/sessions")) return jsonRes(SESSIONS);
    if (u.includes("/api/backends")) return jsonRes({ available: ["claude-code"] });
    return jsonRes([]);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SidebarAgents fold state", () => {
  it("renders every agent folded on load — no session list is spilled open", async () => {
    const { container } = mount();
    await waitFor(() => expect(screen.getByText("Octo")).toBeTruthy());
    expect(screen.getByText("Researcher")).toBeTruthy();

    expect(container.querySelectorAll(".session-rail")).toHaveLength(0);
    expect(container.querySelectorAll(".session-item")).toHaveLength(0);
    expect(screen.queryByText("ideas")).toBeNull();
    // Every caret still points right.
    const carets = container.querySelectorAll(".agent-fold");
    expect(carets).toHaveLength(2);
    carets.forEach((c) => expect(c.className).not.toContain("rotate-90"));
  });

  it("still selects a default agent — it just doesn't unfold it", async () => {
    const { container } = mount();
    await waitFor(() =>
      expect(useSessionStore.getState().activeAgentId).toBe("ag1")
    );
    expect(container.querySelectorAll(".session-item")).toHaveLength(0);
  });

  it("unfolds on click, and folds again on a second click", async () => {
    const { container } = mount();
    await waitFor(() => expect(screen.getByText("Octo")).toBeTruthy());

    const octo = screen.getByText("Octo").closest(".agent-item") as HTMLElement;
    fireEvent.click(octo);

    await waitFor(() =>
      expect(container.querySelectorAll(".session-item")).toHaveLength(2)
    );
    // Only the clicked agent opened; the other agent's session stays hidden.
    expect(screen.getByText("ideas")).toBeTruthy();
    expect(screen.queryByText("papers")).toBeNull();
    expect(
      (container.querySelector(".agent-fold") as HTMLElement).className
    ).toContain("rotate-90");

    fireEvent.click(octo);
    await waitFor(() =>
      expect(container.querySelectorAll(".session-item")).toHaveLength(0)
    );
  });

  it("expands the whole sidebar when an agent is clicked on the icon rail", async () => {
    const { container } = mount();
    await waitFor(() => expect(screen.getByText("Researcher")).toBeTruthy());
    useSessionStore.getState().setSidebarCollapsed(true);

    const row = screen
      .getByText("Researcher")
      .closest(".agent-item") as HTMLElement;
    fireEvent.click(row);

    // A fold toggle would be invisible from the rail, so the click means
    // "show me this agent": the sidebar opens and the agent comes with it.
    await waitFor(() =>
      expect(useSessionStore.getState().sidebarCollapsed).toBe(false)
    );
    expect(screen.getByText("papers")).toBeTruthy();
    expect(useSessionStore.getState().activeAgentId).toBe("ag2");

    // And now that it's expanded, clicking again folds as it always did.
    fireEvent.click(row);
    await waitFor(() =>
      expect(container.querySelectorAll(".session-item")).toHaveLength(0)
    );
    expect(useSessionStore.getState().sidebarCollapsed).toBe(false);
  });

  it("keeps an application's own agent conversations out of the rail", async () => {
    // An app that chats with an agent all day would otherwise bury the
    // user's sessions — and make the agent look permanently busy for work
    // nobody started (app-agent-access.md §7).
    fetchMock.mockImplementation(async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.includes("/api/agents")) return jsonRes(AGENTS);
      if (u.includes("/api/sessions"))
        return jsonRes([
          ...SESSIONS,
          session({
            id: "c1",
            name: "SmartReader — 09:12",
            origin: "app",
            app_id: "app1",
            status: "running",
          }),
        ]);
      if (u.includes("/api/backends")) return jsonRes({ available: ["claude-code"] });
      return jsonRes([]);
    });
    const { container } = mount();
    await waitFor(() => expect(screen.getByText("Octo")).toBeTruthy());

    // Not even as a running badge on the agent row.
    expect(container.querySelector(".agent-running")).toBeNull();

    fireEvent.click(screen.getByText("Octo").closest(".agent-item") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelectorAll(".session-item")).toHaveLength(2)
    );
    expect(screen.queryByText("SmartReader — 09:12")).toBeNull();
  });

  it("unfolds the agent whose new-session + was pressed", async () => {
    const { container } = mount();
    await waitFor(() => expect(screen.getByText("Researcher")).toBeTruthy());

    const row = screen
      .getByText("Researcher")
      .closest(".agent-item") as HTMLElement;
    fireEvent.click(row.querySelector(".btn-session-add") as HTMLElement);

    await waitFor(() =>
      expect(container.querySelector(".session-create")).toBeTruthy()
    );
    expect(screen.getByText("papers")).toBeTruthy();
    // Octo stayed folded.
    expect(screen.queryByText("ideas")).toBeNull();
  });
});

describe("SidebarAgents pins (sidebar-pins.md)", () => {
  function mountWith(agents: Agent[], sessions: SessionInfo[], extra = {}) {
    fetchMock.mockImplementation(async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.includes("/api/agents")) return jsonRes(agents);
      if (u.includes("/api/sessions")) return jsonRes(sessions);
      if (u.includes("/api/backends")) return jsonRes({ available: ["claude-code"] });
      return jsonRes([]);
    });
    useSessionStore.setState({
      token: "tok",
      agents,
      sessions,
      activeAgentId: null,
      activeSessionId: null,
      credentials: [],
      availableBackends: ["claude-code"],
      mainView: "chat",
      sidebarCollapsed: false,
      pendingQuestions: {},
      ...extra,
    });
    return render(<SidebarAgents />);
  }

  const names = (container: HTMLElement) =>
    [...container.querySelectorAll(".agent-name")].map((n) => n.textContent);

  it("lists only pinned agents, in pin order", async () => {
    const { container } = mountWith(
      [
        agent({ pinned: true, pin_order: 2 }),
        agent({ id: "ag2", name: "Researcher", is_system: false, pinned: true, pin_order: 1 }),
        agent({ id: "ag3", name: "Quiet", is_system: false, pinned: false, pin_order: 3 }),
      ],
      []
    );
    await waitFor(() => expect(names(container)).toEqual(["Researcher", "Octo"]));
  });

  it("an unpinned agent shows, dimmed, while it's running, asking, or open", async () => {
    const agents = [
      agent({ pinned: true, pin_order: 1 }),
      agent({ id: "ag2", name: "Runner", is_system: false, pinned: false }),
      agent({ id: "ag3", name: "Asker", is_system: false, pinned: false }),
      agent({ id: "ag4", name: "Opened", is_system: false, pinned: false }),
      agent({ id: "ag5", name: "Quiet", is_system: false, pinned: false }),
      agent({ id: "ag6", name: "AppBusy", is_system: false, pinned: false }),
    ];
    const sessions = [
      session({ id: "r", agent_id: "ag2", status: "running" }),
      session({ id: "q", agent_id: "ag3" }),
      session({ id: "o", agent_id: "ag4" }),
      session({ id: "z", agent_id: "ag5" }),
      // An application's conversation running doesn't count.
      session({ id: "ap", agent_id: "ag6", status: "running", origin: "app" }),
    ];
    const { container } = mountWith(agents, sessions, {
      activeSessionId: "o",
      pendingQuestions: { q: [{ question_id: "x", questions: [] }] },
    });
    await waitFor(() =>
      expect(names(container)).toEqual(["Octo", "Runner", "Asker", "Opened"])
    );
    expect(container.querySelectorAll(".agent-item.unpinned")).toHaveLength(3);
    // The agent that is only here because its session is open is unfolded,
    // so the open session is visible under it.
    await waitFor(() => expect(screen.getByText("ideas")).toBeTruthy());
  });

  it("unpin POSTs; the Default Agent offers no unpin", async () => {
    const agents = [
      agent({ pinned: true, pin_order: 1 }),
      agent({ id: "ag2", name: "Researcher", is_system: false, pinned: true, pin_order: 2 }),
    ];
    const { container } = mountWith(agents, []);
    fetchMock.mockImplementation(async (url: RequestInfo | URL) =>
      String(url).endsWith("/ag2/unpin")
        ? jsonRes({ ...agents[1], pinned: false })
        : jsonRes([])
    );
    await waitFor(() => expect(names(container)).toEqual(["Octo", "Researcher"]));
    expect(screen.queryByLabelText("Unpin Octo from the sidebar")).toBeNull();

    fireEvent.click(screen.getByLabelText("Unpin Researcher from the sidebar"));
    await waitFor(() => expect(names(container)).toEqual(["Octo"]));
    // Still a live agent — only the shortcut went.
    expect(useSessionStore.getState().agents.map((a) => a.id)).toEqual(["ag1", "ag2"]);
  });

  it("the session button archives — nothing in the sidebar hard-deletes", async () => {
    const agents = [agent({ pinned: true, pin_order: 1 })];
    const sessions = [session({ id: "s1", name: "ideas" })];
    const { container } = mountWith(agents, sessions, {
      activeSessionId: "s1",
      archivedSessions: [],
    });
    fetchMock.mockImplementation(async () => new Response(null, { status: 204 }));
    // A pinned agent starts folded; unfold it to reach the session row.
    fireEvent.click(screen.getByText("Octo"));
    fireEvent.click(await screen.findByLabelText("Archive ideas"));

    await waitFor(() => expect(useSessionStore.getState().sessions).toEqual([]));
    const calls = fetchMock.mock.calls.map(([u, i]) => [
      String(u),
      (i as RequestInit | undefined)?.method,
    ]);
    expect(calls).toContainEqual([
      expect.stringContaining("/api/sessions/s1/archive?replace=false"),
      "POST",
    ]);
    expect(calls.some(([, m]) => m === "DELETE")).toBe(false);
    // It's kept among the archived, so it can still be opened and restored.
    expect(useSessionStore.getState().archivedSessions.map((x) => x.id)).toEqual(["s1"]);
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(container.querySelector(".btn-delete")).toBeNull();
  });
});
