import type { SessionInfo } from "../stores/sessionStore";

/** A session plus its fork subtree (session-tree-rewind.md §6.3). */
export interface ForkTreeNode {
  session: SessionInfo;
  children: ForkTreeNode[];
}

export interface ForkTree {
  /** Normal sessions (no `forked_from_session_id`), each with its fork subtree. */
  roots: ForkTreeNode[];
  /** Forks whose parent isn't in the set (parent deleted) — anchored at a
   * top-level "(parent deleted)" group. */
  orphans: ForkTreeNode[];
}

/**
 * Group a flat session list into fork trees. A session is a ROOT when it has no
 * `forked_from_session_id`; a CHILD when its parent is present in the list; an
 * ORPHAN when its parent id is set but absent (parent deleted — §5.5/§6.3).
 * Forks-of-forks nest naturally via recursion; a visited-set guards against a
 * corrupted pointer cycle.
 */
export function buildForkTree(sessions: SessionInfo[]): ForkTree {
  const byId = new Map(sessions.map((s) => [s.id, s]));
  const childrenOf = new Map<string, SessionInfo[]>();
  const roots: SessionInfo[] = [];
  const orphans: SessionInfo[] = [];

  for (const s of sessions) {
    const parent = s.forked_from_session_id;
    if (parent && byId.has(parent)) {
      const list = childrenOf.get(parent);
      if (list) list.push(s);
      else childrenOf.set(parent, [s]);
    } else if (parent) {
      orphans.push(s); // parent set but not present → dangling reference
    } else {
      roots.push(s);
    }
  }

  const build = (s: SessionInfo, seen: Set<string>): ForkTreeNode => {
    seen.add(s.id);
    const kids = (childrenOf.get(s.id) ?? []).filter((c) => !seen.has(c.id));
    return { session: s, children: kids.map((c) => build(c, seen)) };
  };

  return {
    roots: roots.map((s) => build(s, new Set())),
    orphans: orphans.map((s) => build(s, new Set())),
  };
}
