import { beforeEach, describe, expect, it, vi } from "vitest";

import { handleWsMessage, shouldApplyWsEvent } from "./useWebSocket";
import { useSessionStore } from "../stores/sessionStore";

/** Snapshot-baseline dedup primitive.
 *
 * The full handler is hard to unit-test cleanly because it touches the
 * zustand store + WebSocket lifecycle, but the guard inside it — "is
 * this event's seq already covered by the snapshot we just loaded?" —
 * is the whole bug-fix and is a tiny pure function. Testing it
 * directly is the strongest signal that the dedup works for the race
 * window the seq mechanism is designed to cover.
 */
describe("shouldApplyWsEvent", () => {
  it("applies events without seq (ephemeral status/queue updates)", () => {
    expect(shouldApplyWsEvent(undefined, 5)).toBe(true);
    expect(shouldApplyWsEvent(null, 5)).toBe(true);
  });

  it("applies events when no baseline is set yet (fresh session)", () => {
    expect(shouldApplyWsEvent(0, undefined)).toBe(true);
    expect(shouldApplyWsEvent(7, undefined)).toBe(true);
  });

  it("applies events with seq strictly greater than baseline", () => {
    expect(shouldApplyWsEvent(6, 5)).toBe(true);
    expect(shouldApplyWsEvent(100, 99)).toBe(true);
  });

  it("drops events with seq <= baseline (already in snapshot)", () => {
    expect(shouldApplyWsEvent(5, 5)).toBe(false);
    expect(shouldApplyWsEvent(0, 5)).toBe(false);
    expect(shouldApplyWsEvent(99, 100)).toBe(false);
  });

  it("treats baseline=0 distinctly from baseline=undefined", () => {
    // baseline=0 means "seq 0 is in the snapshot, but seq 1+ are not"
    expect(shouldApplyWsEvent(0, 0)).toBe(false);
    expect(shouldApplyWsEvent(1, 0)).toBe(true);
  });
});

/** Token rotation over the socket (token-rotation.md §3).
 *
 * The rotation itself is one server-side operation; what the client owes the
 * user is to carry on when it's handed a new token, and to ask for one when
 * it isn't.
 */
describe("auth_token_rotated", () => {
  beforeEach(() => {
    useSessionStore.getState().setToken("old-token-1234");
  });

  it("takes the new token so an open tab keeps working", () => {
    handleWsMessage({ type: "auth_token_rotated", token: "a-stronger-token" });
    expect(useSessionStore.getState().token).toBe("a-stronger-token");
    expect(localStorage.getItem("octopus_token")).toBe("a-stronger-token");
  });

  it("signs this client out when the rotation revoked the others", () => {
    // No token in the event means the old one leaked: everyone else proves
    // they have the new one.
    handleWsMessage({ type: "auth_token_rotated", token: null });
    expect(useSessionStore.getState().token).toBe("");
  });
});

/** An agent setting a schedule for itself (schedule-tool.md §6).
 *
 * The server says only that the list moved; the client refetches. Without
 * this, a schedule an agent creates mid-conversation is invisible until the
 * user reloads — which looks exactly like the tool having failed.
 */
describe("schedules_changed", () => {
  beforeEach(() => {
    useSessionStore.getState().setToken("tok");
    useSessionStore.getState().setSchedules([]);
  });

  it("refetches the schedule list into the store", async () => {
    const row = {
      id: "s1",
      agent_id: "a1",
      name: "Morning build check",
      prompt: "check the build",
      cron: "0 9 * * 1-5",
      interval_seconds: null,
      timezone: "America/Los_Angeles",
      recurrence_label: "Weekdays at 09:00",
      enabled: true,
      created_at: "2026-09-21T00:00:00Z",
    };
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toContain("/api/schedules");
      return { ok: true, json: async () => [row] };
    });
    vi.stubGlobal("fetch", fetchMock);

    handleWsMessage({ type: "schedules_changed" });
    await vi.waitFor(() =>
      expect(useSessionStore.getState().schedules).toHaveLength(1)
    );
    expect(useSessionStore.getState().schedules[0].name).toBe(
      "Morning build check"
    );
    expect(fetchMock).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });

  it("leaves the list alone when signed out", async () => {
    useSessionStore.getState().setToken("");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    handleWsMessage({ type: "schedules_changed" });
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
