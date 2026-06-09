import type { SessionInfo } from "../stores/sessionStore";

/** A session plus its fork subtree (session-tree-rewind.md §6.3). */
export interface ForkTreeNode {
  session: SessionInfo;
  children: ForkTreeNode[];
}

/**
 * Group a flat session list into fork trees. A session is a CHILD when its
 * `forked_from_session_id` parent is present in the list; otherwise it's a
 * ROOT — whether it never forked, or its parent is absent.
 *
 * In the rewind flow a fork's parent is archived the instant the fork is
 * created, so the parent is absent and the fork surfaces as a plain top-level
 * session, indistinguishable from the original it replaced (session-tree-
 * rewind.md — rewind, not branch). The nesting still re-forms if the parent is
 * later unarchived. Forks-of-forks nest via recursion; a visited-set guards a
 * corrupted pointer cycle.
 */
export function buildForkTree(sessions: SessionInfo[]): ForkTreeNode[] {
  const byId = new Map(sessions.map((s) => [s.id, s]));
  const childrenOf = new Map<string, SessionInfo[]>();
  const roots: SessionInfo[] = [];

  for (const s of sessions) {
    const parent = s.forked_from_session_id;
    if (parent && byId.has(parent)) {
      const list = childrenOf.get(parent);
      if (list) list.push(s);
      else childrenOf.set(parent, [s]);
    } else {
      roots.push(s); // no parent, or parent archived/deleted → top-level
    }
  }

  const build = (s: SessionInfo, seen: Set<string>): ForkTreeNode => {
    seen.add(s.id);
    const kids = (childrenOf.get(s.id) ?? []).filter((c) => !seen.has(c.id));
    return { session: s, children: kids.map((c) => build(c, seen)) };
  };

  return roots.map((s) => build(s, new Set()));
}
