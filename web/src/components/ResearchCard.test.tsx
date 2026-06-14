/**
 * Renderer tests for ResearchCard (native-deep-research.md §7): live phase
 * while running, a result line on completion, an error line on failure, and a
 * cancel button that POSTs only while running.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ResearchCard } from "./ResearchCard";
import { useSessionStore, type ResearchJob } from "../stores/sessionStore";

function job(overrides: Partial<ResearchJob> = {}): ResearchJob {
  return {
    id: "r1",
    session_id: "s1",
    question: "What is X?",
    status: "running",
    phase: "search",
    ...overrides,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  useSessionStore.setState({ token: "tok", research: {} });
  fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ResearchCard", () => {
  it("renders nothing with no jobs", () => {
    const { container } = render(<ResearchCard sessionId="s1" />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the running phase + a cancel button", () => {
    useSessionStore.setState({ research: { s1: [job({ phase: "verify" })] } });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.getByText("What is X?")).toBeTruthy();
    expect(screen.getByText(/Verifying claims/)).toBeTruthy();
    expect(screen.getByTitle("Cancel research")).toBeTruthy();
  });

  it("shows a result line and no cancel when completed", () => {
    useSessionStore.setState({
      research: { s1: [job({ status: "completed", phase: "done", verified: 3, sources: ["u"] })] },
    });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.getByText(/Report delivered below/)).toBeTruthy();
    expect(screen.getByText(/3 verified findings/)).toBeTruthy();
    expect(screen.queryByTitle("Cancel research")).toBeNull();
  });

  it("shows the error on failure", () => {
    useSessionStore.setState({
      research: { s1: [job({ status: "failed", error: "boom" })] },
    });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.getByText("boom")).toBeTruthy();
  });

  it("ignores a partial upsert for an unknown id (no malformed card)", () => {
    // A progress/completed event arriving without a prior research_started
    // (missed after reconnect / in a 2nd tab) must NOT create a card with no
    // question/status (Vera review).
    useSessionStore.getState().upsertResearch("s1", { id: "ghost", phase: "verify" });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.queryByText(/Verifying claims/)).toBeNull();
    expect(useSessionStore.getState().research["s1"] ?? []).toHaveLength(0);
  });

  it("cancel POSTs to the cancel route", async () => {
    useSessionStore.setState({ research: { s1: [job()] } });
    render(<ResearchCard sessionId="s1" />);
    fireEvent.click(screen.getByTitle("Cancel research"));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) =>
          String(u).endsWith("/api/sessions/s1/research/r1/cancel")
        )
      ).toBe(true)
    );
  });
});
