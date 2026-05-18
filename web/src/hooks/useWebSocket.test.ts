import { describe, expect, it } from "vitest";

import { shouldApplyWsEvent } from "./useWebSocket";

/** Snapshot-baseline dedup primitive (future-features #6).
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
