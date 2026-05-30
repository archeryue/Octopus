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

  it("matches by delegation_id parsed from the sibling tool_result (server truth)", () => {
    // Two delegations to the same target with DIFFERENT requests:
    // by-id matching must pick the one the tool_result names, NOT
    // the one whose request happens to equal the tool_input.request.
    // This is the fan-out invariant Vera flagged: matching by name
    // alone (or even by name+request after a model round-trip
    // mismatch) can bind the card to the wrong delegation.
    // Production delegation_ids are 12-char hex strings (matching the
    // `[A-Za-z0-9]+` regex the card uses to parse the tool_result).
    // Test ids mirror that shape so the regex actually accepts them.
    useSessionStore.getState().upsertDelegation(
      "parent",
      makeDelegation({
        delegation_id: "aaaaaaaaaaaa",
        request: "review the dashboard",
      })
    );
    useSessionStore.getState().upsertDelegation(
      "parent",
      makeDelegation({
        delegation_id: "bbbbbbbbbbbb",
        request: "review the sidebar",
        state: "completed",
        finished_at: "x",
      })
    );
    // Seed the message history: the model's tool_input.request differs
    // from BOTH stored requests (post-round-trip drift), but the
    // tool_result for our toolUseId names the right delegation_id.
    useSessionStore.getState().setMessages("parent", [
      {
        role: "assistant",
        type: "tool_use",
        tool_name: "mcp__ask_agent__ask",
        tool_use_id: "tu-1",
        tool_input: {
          name: "Vera",
          request: "review the sidebar — the EXACT round-trip text differs",
        },
      },
      {
        role: "user",
        type: "tool_result",
        tool_use_id: "tu-1",
        content: "Started delegation `bbbbbbbbbbbb` to Vera. …",
      },
    ]);

    render(
      <AgentDelegationRequestCard
        sessionId="parent"
        toolUseId="tu-1"
        agentName="Vera"
        request="review the sidebar — the EXACT round-trip text differs"
        files={undefined}
      />
    );
    // State of the RIGHT delegation surfaces; the wrong-one is
    // ignored entirely.
    expect(screen.getByText("replied")).toBeInTheDocument();
    // No cancel button — the matched record is `completed`.
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
