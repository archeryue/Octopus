/**
 * The agent form page — create, edit, archive and (the new half) restore from
 * the Archived tab.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AgentFormPage } from "./AgentFormPage";
import { useSessionStore, type Agent } from "../stores/sessionStore";

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
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    active_session_count: 0,
    ...o,
  } as Agent;
}

let fetchMock: ReturnType<typeof vi.fn>;

function mount(editingAgentId: string | null = null, agents: Agent[] = [agent()]) {
  useSessionStore.setState({
    token: "tok",
    agents,
    editingAgentId,
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

  it("lists archived agents behind the tab and restores one", async () => {
    const retired = agent({ id: "ag8", name: "Retired", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive"))
        return new Response(JSON.stringify({ ...retired, archived: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (String(url).includes("include_archived=true"))
        return new Response(JSON.stringify([agent(), retired]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    mount(null);
    await waitFor(() => expect(screen.getByText(/Archived 1/)).toBeTruthy());
    fireEvent.click(screen.getByText(/Archived 1/));
    expect(screen.getByText("Retired")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(useSessionStore.getState().agents.some((a) => a.id === "ag8")).toBe(true)
    );
    // Restoring drops you into that agent's form, on the Create tab.
    expect(useSessionStore.getState().editingAgentId).toBe("ag8");
  });

  it("surfaces a name clash when restoring", async () => {
    const retired = agent({ id: "ag8", name: "Scout", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive"))
        return new Response(JSON.stringify({ detail: "An agent named 'Scout' already exists" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      if (String(url).includes("include_archived=true"))
        return new Response(JSON.stringify([retired]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    mount(null);
    await waitFor(() => expect(screen.getByText(/Archived 1/)).toBeTruthy());
    fireEvent.click(screen.getByText(/Archived 1/));
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(screen.getByText(/already exists/)).toBeTruthy());
  });
});
