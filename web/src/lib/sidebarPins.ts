/** What the sidebar lists (docs/plans/sidebar-pins.md).
 *
 * The sidebar is a set of shortcuts: pinned agents and applications, in the
 * order the user dragged them into. Everything else is on the Agents and
 * Applications pages. The one exception is something unpinned that needs
 * you right now (§5) — it appears after the pinned ones until that passes.
 *
 * Pure functions over store state, so both sidebar sections and their tests
 * read one definition of "shown".
 */

import type {
  Agent,
  Application,
  MainView,
  PendingQuestion,
  SessionInfo,
} from "../stores/sessionStore";

/** Pinned rows, in sidebar order. A row without a position (only possible
 * mid-migration) sorts last rather than first. */
export function pinnedInOrder<T extends { pinned?: boolean; pin_order?: number | null }>(
  rows: T[]
): T[] {
  return rows
    .filter((r) => r.pinned !== false)
    .map((r, i) => ({ r, i }))
    .sort(
      (a, b) =>
        (a.r.pin_order ?? Number.MAX_SAFE_INTEGER) -
          (b.r.pin_order ?? Number.MAX_SAFE_INTEGER) || a.i - b.i
    )
    .map(({ r }) => r);
}

export interface SidebarRows<T> {
  /** The user's shortcuts, in their order. Draggable. */
  pinned: T[];
  /** Unpinned, but needing attention right now (§5). Not draggable. */
  present: T[];
}

/** Agents for the sidebar. An unpinned agent is present while it owns the
 * open session, or any of its sessions is running, waiting on approval, or
 * holding a question for you. An application's own conversations don't
 * count — the rail never shows them (app-agent-access.md §7). */
export function sidebarAgents(
  agents: Agent[],
  state: {
    sessions: SessionInfo[];
    activeSessionId: string | null;
    pendingQuestions: Record<string, PendingQuestion[]>;
  }
): SidebarRows<Agent> {
  const activeAgentId = state.sessions.find(
    (s) => s.id === state.activeSessionId
  )?.agent_id;
  const needsYou = new Set<string>();
  for (const s of state.sessions) {
    if (!s.agent_id || s.origin === "app") continue;
    if (
      s.status === "running" ||
      s.status === "waiting_approval" ||
      (state.pendingQuestions[s.id]?.length ?? 0) > 0
    ) {
      needsYou.add(s.agent_id);
    }
  }
  return {
    pinned: pinnedInOrder(agents),
    present: agents.filter(
      (a) =>
        a.pinned === false && (a.id === activeAgentId || needsYou.has(a.id))
    ),
  };
}

/** Applications for the sidebar. An unpinned application is present while
 * it's open, while it builds, and after a build failed unseen — until it's
 * opened. */
export function sidebarApplications(
  applications: Application[],
  state: {
    mainView: MainView;
    activeApplicationId: string | null;
    unseenFailedApplications: string[];
  }
): SidebarRows<Application> {
  const open =
    state.mainView === "application" ? state.activeApplicationId : null;
  return {
    pinned: pinnedInOrder(applications),
    present: applications.filter(
      (a) =>
        a.pinned === false &&
        (a.id === open ||
          a.status === "building" ||
          state.unseenFailedApplications.includes(a.id))
    ),
  };
}

/** `rows` with the pinned ones renumbered to follow `orderedIds` — the
 * optimistic half of a drag, applied before the server confirms it. */
export function withPinOrder<T extends { id: string; pin_order?: number | null }>(
  rows: T[],
  orderedIds: string[]
): T[] {
  const position = new Map(orderedIds.map((id, i) => [id, i + 1]));
  return rows.map((r) =>
    position.has(r.id) ? { ...r, pin_order: position.get(r.id)! } : r
  );
}
