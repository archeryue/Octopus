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
    const { roots, orphans } = buildForkTree([sess("a"), sess("b")]);
    expect(roots.map((r) => r.session.id)).toEqual(["a", "b"]);
    expect(roots.every((r) => r.children.length === 0)).toBe(true);
    expect(orphans).toHaveLength(0);
  });

  it("nests a fork under its parent", () => {
    const { roots } = buildForkTree([sess("a"), sess("b", "a", 3)]);
    expect(roots).toHaveLength(1);
    expect(roots[0].session.id).toBe("a");
    expect(roots[0].children.map((c) => c.session.id)).toEqual(["b"]);
  });

  it("nests forks of forks", () => {
    const { roots } = buildForkTree([
      sess("a"),
      sess("b", "a", 2),
      sess("c", "b", 1),
    ]);
    expect(roots[0].children[0].session.id).toBe("b");
    expect(roots[0].children[0].children[0].session.id).toBe("c");
  });

  it("buckets forks whose parent is absent as orphans", () => {
    const { roots, orphans } = buildForkTree([sess("b", "gone", 3)]);
    expect(roots).toHaveLength(0);
    expect(orphans.map((o) => o.session.id)).toEqual(["b"]);
  });

  it("does not loop on a corrupted self-parent pointer", () => {
    // b -> b (cycle). It's not a root (parent set) and parent IS present →
    // it becomes its own child; the visited-set stops the recursion.
    const { roots, orphans } = buildForkTree([sess("a"), sess("b", "b")]);
    // b's parent (b) is present, so it's a child of itself — not a root/orphan.
    expect(roots.map((r) => r.session.id)).toEqual(["a"]);
    expect(orphans).toHaveLength(0);
  });
});
