import { describe, expect, it } from "vitest";

import type { Agent, Application, SessionInfo } from "../stores/sessionStore";
import {
  pinnedInOrder,
  sidebarAgents,
  sidebarApplications,
  withPinOrder,
} from "./sidebarPins";

const agent = (o: Partial<Agent>) => ({ id: "a", pinned: true, pin_order: 1, ...o }) as Agent;
const app = (o: Partial<Application>) =>
  ({ id: "x", pinned: true, pin_order: 1, status: "ready", ...o }) as Application;
const session = (o: Partial<SessionInfo>) =>
  ({ id: "s", agent_id: "a", status: "idle", origin: "user", ...o }) as SessionInfo;

describe("pinnedInOrder", () => {
  it("keeps pinned rows, by position; a missing position sorts last", () => {
    const rows = [
      agent({ id: "c", pin_order: 3 }),
      agent({ id: "none", pin_order: null }),
      agent({ id: "a", pin_order: 1 }),
      agent({ id: "off", pinned: false, pin_order: 0 }),
      agent({ id: "b", pin_order: 2 }),
    ];
    expect(pinnedInOrder(rows).map((r) => r.id)).toEqual(["a", "b", "c", "none"]);
  });

  it("treats a row that predates the field as pinned", () => {
    expect(pinnedInOrder([{ id: "old" } as Agent]).map((r) => r.id)).toEqual(["old"]);
  });
});

describe("sidebarAgents", () => {
  const base = { sessions: [], activeSessionId: null, pendingQuestions: {} };

  it("an unpinned agent is absent at rest", () => {
    const rows = sidebarAgents([agent({ id: "q", pinned: false })], base);
    expect(rows).toEqual({ pinned: [], present: [] });
  });

  it("present while running, waiting, asking, or owning the open session", () => {
    const agents = ["run", "wait", "ask", "open", "idle", "app"].map((id) =>
      agent({ id, pinned: false })
    );
    const rows = sidebarAgents(agents, {
      sessions: [
        session({ id: "1", agent_id: "run", status: "running" }),
        session({ id: "2", agent_id: "wait", status: "waiting_approval" }),
        session({ id: "3", agent_id: "ask" }),
        session({ id: "4", agent_id: "open" }),
        session({ id: "5", agent_id: "idle" }),
        session({ id: "6", agent_id: "app", status: "running", origin: "app" }),
      ],
      activeSessionId: "4",
      pendingQuestions: { "3": [{ question_id: "q", questions: [] }] },
    });
    expect(rows.present.map((a) => a.id)).toEqual(["run", "wait", "ask", "open"]);
  });

  it("a pinned agent is never also present", () => {
    const rows = sidebarAgents([agent({ id: "p" })], {
      ...base,
      sessions: [session({ id: "1", agent_id: "p", status: "running" })],
    });
    expect(rows.pinned.map((a) => a.id)).toEqual(["p"]);
    expect(rows.present).toEqual([]);
  });
});

describe("sidebarApplications", () => {
  it("present while open, building, or failed unseen", () => {
    const apps = [
      app({ id: "open", pinned: false }),
      app({ id: "build", pinned: false, status: "building" }),
      app({ id: "fail", pinned: false, status: "failed" }),
      app({ id: "seen", pinned: false, status: "failed" }),
      app({ id: "quiet", pinned: false }),
    ];
    const rows = sidebarApplications(apps, {
      mainView: "application",
      activeApplicationId: "open",
      unseenFailedApplications: ["fail"],
    });
    expect(rows.present.map((a) => a.id)).toEqual(["open", "build", "fail"]);
  });

  it("an app is only 'open' while the application view shows it", () => {
    const rows = sidebarApplications([app({ id: "x", pinned: false })], {
      mainView: "chat",
      activeApplicationId: "x",
      unseenFailedApplications: [],
    });
    expect(rows.present).toEqual([]);
  });
});

describe("withPinOrder", () => {
  it("renumbers the named rows and leaves the rest alone", () => {
    const rows = [
      agent({ id: "a", pin_order: 1 }),
      agent({ id: "b", pin_order: 2 }),
      agent({ id: "u", pinned: false, pin_order: 7 }),
    ];
    const out = withPinOrder(rows, ["b", "a"]);
    expect(out.map((r) => [r.id, r.pin_order])).toEqual([
      ["a", 2],
      ["b", 1],
      ["u", 7],
    ]);
    expect(rows[0].pin_order).toBe(1); // not mutated
  });
});
