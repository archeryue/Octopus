/**
 * Renderer tests for AgentDelegationRequestCard. The card lives next
 * to a `mcp__ask_agent__ask` tool_use and surfaces the live delegation
 * state — running / completed / failed / cancelled — by matching
 * (target_agent_name, request) into the store's `delegations` map.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { AgentDelegationRequestCard } from "./AgentDelegationRequestCard";
import { useSessionStore, type Delegation } from "../stores/sessionStore";

function makeDelegation(overrides: Partial<Delegation> = {}): Delegation {
  return {
    delegation_id: "deleg-001",
    sub_session_id: "deleg-001",
    parent_session_id: "parent",
    target_agent_id: "vera-id",
    target_agent_name: "Vera",
    request: "review the readme",
    state: "running",
    created_at: "2026-05-29T00:00:00Z",
    finished_at: null,
    error: null,
    ...overrides,
  };
}

beforeEach(() => {
  useSessionStore.setState({
    token: "tok",
    sessions: [],
    delegations: {},
    activeAgentId: null,
    activeSessionId: null,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("[]", { status: 200 }))
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AgentDelegationRequestCard", () => {
  it("renders 'running' when a matching delegation is in the store", () => {
    useSessionStore.getState().upsertDelegation("parent", makeDelegation());
    render(
      <AgentDelegationRequestCard
        sessionId="parent"
        toolUseId={undefined}
        agentName="Vera"
        request="review the readme"
        files={undefined}
      />
    );
    expect(screen.getByText(/Asked Vera/)).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByTitle(/cancel delegation/i)).toBeInTheDocument();
  });

  it("renders 'replied' on completed state and hides the cancel button", () => {
    useSessionStore
      .getState()
      .upsertDelegation(
        "parent",
        makeDelegation({ state: "completed", finished_at: "x" })
      );
    render(
      <AgentDelegationRequestCard
        sessionId="parent"
        toolUseId={undefined}
        agentName="Vera"
        request="review the readme"
        files={undefined}
      />
    );
    expect(screen.getByText("replied")).toBeInTheDocument();
    expect(screen.queryByTitle(/cancel delegation/i)).toBeNull();
  });

  it("matches by lowercase name (so 'vera' tool_input finds 'Vera' record)", () => {
    useSessionStore.getState().upsertDelegation("parent", makeDelegation());
    render(
      <AgentDelegationRequestCard
        sessionId="parent"
        toolUseId={undefined}
        agentName="vera"
        request="review the readme"
        files={undefined}
      />
    );
    // Same record found despite case mismatch — the card still
    // surfaces the canonical name from the record.
    expect(screen.getByText(/Asked vera/)).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("renders a placeholder state while the matching record is absent", () => {
    render(
      <AgentDelegationRequestCard
        sessionId="parent"
        toolUseId={undefined}
        agentName="Vera"
        request="review the readme"
        files={undefined}
      />
    );
    // No matched record: state defaults to running with the spinner
    // and no delegation-id badge appears.
    expect(screen.getByText(/Asked Vera/)).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.queryByTitle(/cancel delegation/i)).toBeNull();
  });

  it("renders the file list when files were passed", () => {
    render(
      <AgentDelegationRequestCard
        sessionId="parent"
        toolUseId={undefined}
        agentName="Vera"
        request="review"
        files={["a.tsx", "b.tsx"]}
      />
    );
    expect(screen.getByText(/a\.tsx, b\.tsx/)).toBeInTheDocument();
  });

  it("the open-child button switches active session + agent", () => {
    useSessionStore.setState({
      sessions: [
        {
          id: "deleg-001",
          name: "Vera ← Octo",
          working_dir: "/tmp",
          status: "idle",
          created_at: "2026-05-29T00:00:00Z",
          message_count: 0,
          claude_session_id: null,
          credential_id: null,
          agent_id: "vera-id",
          origin: "delegation",
          parent_session_id: "parent",
          delegation_request: "review the readme",
          backend: "claude-code",
          archived: false,
        },
      ],
    });
    useSessionStore.getState().upsertDelegation("parent", makeDelegation());
    render(
      <AgentDelegationRequestCard
        sessionId="parent"
        toolUseId={undefined}
        agentName="Vera"
        request="review the readme"
        files={undefined}
      />
    );
    fireEvent.click(screen.getByTitle(/open vera's session/i));
    expect(useSessionStore.getState().activeSessionId).toBe("deleg-001");
    expect(useSessionStore.getState().activeAgentId).toBe("vera-id");
  });

  it("the cancel button POSTs to the cancel route", async () => {
    useSessionStore.getState().upsertDelegation("parent", makeDelegation());
    const fetchMock = vi.fn(
      async () => new Response("{}", { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <AgentDelegationRequestCard
        sessionId="parent"
        toolUseId={undefined}
        agentName="Vera"
        request="review the readme"
        files={undefined}
      />
    );
    await act(async () => {
      fireEvent.click(screen.getByTitle(/cancel delegation/i));
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(
        "/api/sessions/parent/delegations/deleg-001/cancel"
      ),
      expect.anything()
    );
  });
});
