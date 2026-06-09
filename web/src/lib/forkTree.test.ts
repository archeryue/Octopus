import { describe, it, expect } from "vitest";
import { buildForkTree } from "./forkTree";
import type { SessionInfo } from "../stores/sessionStore";

function sess(id: string, forkedFrom?: string, after?: number): SessionInfo {
  return {
    id,
    name: id,
    working_dir: "/tmp",
    status: "idle",
    created_at: "2026-06-08T00:00:00+00:00",
    message_count: 0,
    origin: forkedFrom ? "fork" : "user",
    backend: "claude-code",
    can_fork: true,
    forked_from_session_id: forkedFrom ?? null,
    fork_after_seq: after ?? null,
    archived: false,
  } as SessionInfo;
}

describe("buildForkTree", () => {
  it("returns flat sessions as roots with no children", () => {
    const roots = buildForkTree([sess("a"), sess("b")]);
    expect(roots.map((r) => r.session.id)).toEqual(["a", "b"]);
    expect(roots.every((r) => r.children.length === 0)).toBe(true);
  });

  it("nests a fork under its parent when the parent is present", () => {
    const roots = buildForkTree([sess("a"), sess("b", "a", 3)]);
    expect(roots).toHaveLength(1);
    expect(roots[0].session.id).toBe("a");
    expect(roots[0].children.map((c) => c.session.id)).toEqual(["b"]);
  });

  it("nests forks of forks", () => {
    const roots = buildForkTree([
      sess("a"),
      sess("b", "a", 2),
      sess("c", "b", 1),
    ]);
    expect(roots[0].children[0].session.id).toBe("b");
    expect(roots[0].children[0].children[0].session.id).toBe("c");
  });

  it("surfaces a fork whose parent is absent as a top-level root", () => {
    // The rewind case: the parent was archived when the fork was created, so
    // it's not in the active list and the fork stands on its own.
    const roots = buildForkTree([sess("b", "gone", 3)]);
    expect(roots.map((r) => r.session.id)).toEqual(["b"]);
    expect(roots[0].children).toHaveLength(0);
  });

  it("does not loop on a corrupted self-parent pointer", () => {
    // b -> b (cycle). Its parent (b) is present, so it's a child of itself and
    // never surfaces as a root; the visited-set stops the recursion.
    const roots = buildForkTree([sess("a"), sess("b", "b")]);
    expect(roots.map((r) => r.session.id)).toEqual(["a"]);
  });
});
