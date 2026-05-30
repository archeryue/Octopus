/**
 * Renderer tests for the agent-to-agent delegation event card. Three
 * variants are produced from the parsed turn-injection prefix the
 * DelegationManager prepends:
 *
 *   reply    →  [agent-reply:<name> delegation=<id>]\n<body>
 *   question →  [agent-question:<name> delegation=<id> question_id=<qid>]\n<body>
 *   error    →  [agent-error:<name> delegation=<id> reason=<r>]\n<body>
 *
 * Covers: parser correctness on each variant, rendered headline + state,
 * the question card showing options as TEXT (not buttons) — the
 * principal-chain rule — and the "Open child's session" link wiring.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  AgentDelegationEventCard,
  parseDelegationEvent,
} from "./AgentDelegationEventCard";
import { useSessionStore } from "../stores/sessionStore";

beforeEach(() => {
  useSessionStore.setState({
    token: "tok",
    sessions: [],
    agents: [],
    activeAgentId: null,
    activeSessionId: null,
  });
});

afterEach(() => {
  cleanup();
});

describe("parseDelegationEvent", () => {
  it("recognises a reply prefix and strips it", () => {
    const ev = parseDelegationEvent(
      "[agent-reply:Vera delegation=ab12cd34ef56]\nLooks good to me."
    );
    expect(ev).not.toBeNull();
    expect(ev?.kind).toBe("reply");
    expect(ev?.agentName).toBe("Vera");
    expect(ev?.delegationId).toBe("ab12cd34ef56");
    expect(ev?.body).toBe("Looks good to me.");
  });

  it("recognises a question prefix and captures the question id", () => {
    const ev = parseDelegationEvent(
      "[agent-question:Vera delegation=abc question_id=q-7]\nWhich file should I focus on?\n  - Dashboard.tsx\n  - Sidebar.tsx"
    );
    expect(ev?.kind).toBe("question");
    expect(ev?.questionId).toBe("q-7");
    expect(ev?.body).toContain("Which file should I focus on?");
  });

  it("recognises an error prefix and captures the reason text", () => {
    const ev = parseDelegationEvent(
      "[agent-error:Vera delegation=abc reason=cancelled by caller]\n(child session ended in state 'cancelled')"
    );
    expect(ev?.kind).toBe("error");
    expect(ev?.reason).toBe("cancelled by caller");
  });

  it("returns null when the content doesn't match a prefix", () => {
    expect(parseDelegationEvent("hello there")).toBeNull();
    expect(parseDelegationEvent("")).toBeNull();
    expect(parseDelegationEvent(undefined)).toBeNull();
  });
});

describe("AgentDelegationEventCard", () => {
  it("renders the reply variant with the agent name + body preview", () => {
    const ev = parseDelegationEvent(
      "[agent-reply:Vera delegation=ab12cd34ef56]\nLooks good to me."
    )!;
    render(<AgentDelegationEventCard event={ev} />);
    expect(screen.getByText(/From delegation/)).toBeInTheDocument();
    expect(screen.getByText("Vera")).toBeInTheDocument();
    expect(screen.getByText(/replied/)).toBeInTheDocument();
    // The body shows up either collapsed (first line) or expanded.
    expect(screen.getByText(/Looks good to me\./)).toBeInTheDocument();
  });

  it("renders the question variant with options as TEXT (not buttons)", () => {
    const body =
      "Question 1: Which file?\n  - Dashboard.tsx\n  - Sidebar.tsx\n  (single-choice; pass the chosen label as `choice`.)";
    const ev = parseDelegationEvent(
      `[agent-question:Vera delegation=abc question_id=q-7]\n${body}`
    )!;
    render(<AgentDelegationEventCard event={ev} />);
    expect(screen.getByText(/is asking/)).toBeInTheDocument();
    // Options must NOT render as <button>s — the human shouldn't be
    // answering directly; Octo's model is. We confirm by checking the
    // expanded body's <pre> carries the labels as plain text and no
    // button bears the option label.
    expect(screen.getByText(/Dashboard\.tsx/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Dashboard\.tsx/ })
    ).toBeNull();
    // The "decide" hint to the parent agent surfaces with the
    // actual MCP tool name (`mcp__ask_agent__answer`) — was
    // `answer_agent_question` for one release; Vera caught the
    // mismatch with the real exported tool name.
    expect(
      screen.getByText(/mcp__ask_agent__answer/i)
    ).toBeInTheDocument();
  });

  it("renders the error variant with the reason inline", () => {
    const ev = parseDelegationEvent(
      "[agent-error:Vera delegation=abc reason=cancelled by caller]\n(child session ended in state 'cancelled')"
    )!;
    render(<AgentDelegationEventCard event={ev} />);
    expect(screen.getByText(/ended with an error/)).toBeInTheDocument();
    expect(screen.getByText(/cancelled by caller/)).toBeInTheDocument();
  });

  it("toggles body expansion when the header is clicked (reply is collapsed by default)", () => {
    const ev = parseDelegationEvent(
      "[agent-reply:Vera delegation=ab12cd34ef56]\nLine one.\nLine two."
    )!;
    render(<AgentDelegationEventCard event={ev} />);
    const button = screen.getAllByRole("button")[0];
    // Reply starts collapsed: only the first line is shown as preview.
    expect(screen.queryByText(/Line two\./)).toBeNull();
    fireEvent.click(button);
    expect(screen.getByText(/Line two\./)).toBeInTheDocument();
  });

  it("offers 'Open child session' when the child is in archivedSessions only (post-auto-archive)", () => {
    // After DelegationManager.auto_archive_scheduled_session fires on
    // terminal delivery, the child row lives in archivedSessions, not
    // sessions. Vera caught a version of the code where the open
    // button gated on `sessions.find(...)` only — archived children
    // were unopenable from the terminal event card. Now the lookup
    // resolves from BOTH stores.
    useSessionStore.setState({
      sessions: [],
      archivedSessions: [
        {
          id: "ab12cd34ef56",
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
          archived: true,
        },
      ],
    });
    const ev = parseDelegationEvent(
      "[agent-reply:Vera delegation=ab12cd34ef56]\nDone."
    )!;
    render(<AgentDelegationEventCard event={ev} />);
    // Reply variant is collapsed by default — expand to expose the
    // footer, then assert the button is present and the click
    // navigates into the archived child.
    fireEvent.click(screen.getAllByRole("button")[0]);
    const openBtn = screen.getByRole("button", {
      name: /open vera's session/i,
    });
    expect(openBtn).toBeInTheDocument();
    fireEvent.click(openBtn);
    expect(useSessionStore.getState().activeSessionId).toBe(
      "ab12cd34ef56"
    );
    expect(useSessionStore.getState().activeAgentId).toBe("vera-id");
  });

  it("offers an 'Open child session' link when the child session is in the store", () => {
    useSessionStore.setState({
      sessions: [
        {
          id: "ab12cd34ef56",
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
    const ev = parseDelegationEvent(
      "[agent-reply:Vera delegation=ab12cd34ef56]\nDone."
    )!;
    render(<AgentDelegationEventCard event={ev} />);
    // Reply variant is collapsed by default — expand so the footer
    // (which carries the Open button) renders.
    fireEvent.click(screen.getAllByRole("button")[0]);
    const openBtn = screen.getByRole("button", {
      name: /open vera's session/i,
    });
    expect(openBtn).toBeInTheDocument();
    fireEvent.click(openBtn);
    expect(useSessionStore.getState().activeSessionId).toBe(
      "ab12cd34ef56"
    );
    expect(useSessionStore.getState().activeAgentId).toBe("vera-id");
  });
});
