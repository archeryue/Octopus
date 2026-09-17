/**
 * The sub-agent card (native-subagents.md §5).
 *
 * Both CLIs fan work out to short-lived helpers and narrate it while it runs.
 * Without this card the only trace is a tool call that sits there for minutes
 * looking stuck, then a result that appears from nowhere.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { SubagentCard } from "./SubagentCard";
import { useSessionStore, type SubagentRun } from "../stores/sessionStore";

const run = (o: Partial<SubagentRun> = {}): SubagentRun =>
  ({
    task_id: "t1",
    tool_use_id: "tu1",
    status: "running",
    name: "Explore",
    description: "searching the repo",
    prompt: "find the token",
    summary: "",
    tokens: null,
    tool_uses: null,
    duration_ms: null,
    ...o,
  }) as SubagentRun;

beforeEach(() => {
  useSessionStore.setState({ subagents: {} });
});

afterEach(cleanup);

describe("SubagentCard", () => {
  it("renders nothing when there's no run for the tool call", () => {
    // Every tool call mounts one; only the ones that spawned a sub-agent
    // have anything to say.
    const { container } = render(
      <SubagentCard sessionId="s1" toolUseId="tu1" />
    );
    expect(container.firstChild).toBeNull();
  });

  it("says who is working and what they're doing", () => {
    useSessionStore.getState().setSubagents("s1", [
      run({ tokens: 11843, tool_uses: 3, duration_ms: 6100 }),
    ]);
    const { container } = render(
      <SubagentCard sessionId="s1" toolUseId="tu1" />
    );
    expect(container.querySelector(".subagent-name")?.textContent).toBe("Explore");
    expect(container.querySelector(".subagent-description")?.textContent).toBe(
      "searching the repo"
    );
    // Counters climb while it works — the reason to keep watching.
    expect(container.querySelector(".subagent-meta")?.textContent).toContain(
      "3 tools"
    );
    expect(container.querySelector(".subagent-meta")?.textContent).toContain(
      "11.8k tokens"
    );
    expect(container.querySelector(".subagent-spinner")).toBeTruthy();
  });

  it("shows the answer when it lands, with the brief behind it", () => {
    useSessionStore
      .getState()
      .setSubagents("s1", [
        run({ status: "completed", summary: "It's in notes.txt" }),
      ]);
    const { container } = render(
      <SubagentCard sessionId="s1" toolUseId="tu1" />
    );
    expect(container.querySelector(".subagent-completed")).toBeTruthy();
    expect(container.querySelector(".subagent-spinner")).toBeNull();
    expect(screen.getByText("It's in notes.txt")).toBeTruthy();

    // The brief it was given is one click away, not on screen by default.
    expect(screen.queryByText("find the token")).toBeNull();
    fireEvent.click(container.querySelector(".subagent-toggle")!);
    expect(screen.getByText("find the token")).toBeTruthy();
  });

  it("says a failure is a failure", () => {
    useSessionStore.getState().setSubagents("s1", [run({ status: "failed" })]);
    const { container } = render(
      <SubagentCard sessionId="s1" toolUseId="tu1" />
    );
    expect(container.querySelector(".subagent-failed")).toBeTruthy();
    expect(container.querySelector(".subagent-description")?.textContent).toBe(
      "failed"
    );
  });
});

describe("the store's sub-agent state", () => {
  it("merges partial updates instead of blanking what they omit", () => {
    // A status patch carries no name; a summary carries no counters. Each
    // observation is partial by design (native-subagents.md §3).
    const { upsertSubagent } = useSessionStore.getState();
    upsertSubagent("s1", run({ tokens: 500 }));
    upsertSubagent(
      "s1",
      run({ name: "", description: "", prompt: "", status: "completed", summary: "done" })
    );

    const stored = useSessionStore.getState().subagents["s1"]["tu1"];
    expect(stored.status).toBe("completed");
    expect(stored.summary).toBe("done");
    expect(stored.name).toBe("Explore");
    expect(stored.tokens).toBe(500);
  });

  it("keys on the tool call, falling back to the task id", () => {
    const { upsertSubagent } = useSessionStore.getState();
    upsertSubagent("s1", run({ tool_use_id: null, task_id: "t9" }));
    expect(Object.keys(useSessionStore.getState().subagents["s1"])).toEqual(["t9"]);
  });
});
