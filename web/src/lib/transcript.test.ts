import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStore, type Message } from "../stores/sessionStore";
import { applyTranscript, loadOlderMessages } from "./transcript";

const msg = (seq: number): Message => ({
  role: "user",
  type: "text",
  content: `msg-${seq}`,
  seq,
});

/** A windowed transcript, and the scroll-back that fills it in
 * (polish-2026-09.md §4 B2).
 *
 * The two ways this path fails are both about the seam between the window and
 * the page before it: a message rendered twice, or a page fetched forever. Both
 * are pinned here, because in the browser they look like ordinary scrolling.
 */
describe("applyTranscript", () => {
  beforeEach(() => {
    useSessionStore.setState({
      token: "t",
      messages: {},
      oldestSeq: {},
      hasMoreMessages: {},
      lastAppliedSeq: {},
    });
  });

  it("sets the transcript, the window and the dedup baseline together", () => {
    applyTranscript("s1", {
      messages: [msg(10), msg(11)],
      oldest_loaded_seq: 10,
      has_more_messages: true,
      next_message_seq: 12,
    });
    const s = useSessionStore.getState();
    expect(s.messages.s1).toHaveLength(2);
    expect(s.oldestSeq.s1).toBe(10);
    expect(s.hasMoreMessages.s1).toBe(true);
    // Anything at or below 11 is already in the messages above.
    expect(s.lastAppliedSeq.s1).toBe(11);
  });

  it("marks a whole transcript as having nothing older", () => {
    applyTranscript("s1", {
      messages: [msg(0)],
      oldest_loaded_seq: 0,
      has_more_messages: false,
      next_message_seq: 1,
    });
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(false);
  });
});

describe("loadOlderMessages", () => {
  beforeEach(() => {
    useSessionStore.setState({
      token: "t",
      messages: { s1: [msg(10), msg(11)] },
      oldestSeq: { s1: 10 },
      hasMoreMessages: { s1: true },
      lastAppliedSeq: {},
    });
  });

  it("asks for the page before what it holds and prepends it", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toContain("/api/sessions/s1/messages?before_seq=10");
      return {
        ok: true,
        json: async () => ({
          messages: [msg(8), msg(9)],
          oldest_loaded_seq: 8,
          has_more_messages: true,
        }),
      };
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await loadOlderMessages("s1")).toBe(2);
    const s = useSessionStore.getState();
    expect(s.messages.s1.map((m) => m.seq)).toEqual([8, 9, 10, 11]);
    expect(s.oldestSeq.s1).toBe(8);
    expect(s.hasMoreMessages.s1).toBe(true);
    vi.unstubAllGlobals();
  });

  it("never renders a message twice when a page overlaps", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        // 10 is already held; only 9 is new.
        json: async () => ({
          messages: [msg(9), msg(10)],
          oldest_loaded_seq: 9,
          has_more_messages: false,
        }),
      }))
    );
    expect(await loadOlderMessages("s1")).toBe(1);
    expect(useSessionStore.getState().messages.s1.map((m) => m.seq)).toEqual([
      9, 10, 11,
    ]);
    vi.unstubAllGlobals();
  });

  it("stops asking when a page adds nothing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          messages: [msg(10)],
          oldest_loaded_seq: 10,
          has_more_messages: true,
        }),
      }))
    );
    expect(await loadOlderMessages("s1")).toBe(0);
    // Believing the server's `has_more` here would poll forever.
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(false);
    vi.unstubAllGlobals();
  });

  it("does not ask when there is nothing older, or no session", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    useSessionStore.setState({ hasMoreMessages: { s1: false } });
    expect(await loadOlderMessages("s1")).toBe(0);
    expect(await loadOlderMessages("")).toBe(0);
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("does not ask twice at once", async () => {
    let resolve!: (v: unknown) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise((r) => {
          resolve = r;
        })
    );
    vi.stubGlobal("fetch", fetchMock);
    const first = loadOlderMessages("s1");
    const second = loadOlderMessages("s1");
    expect(await second).toBe(0);
    resolve({
      ok: true,
      json: async () => ({
        messages: [msg(9)],
        oldest_loaded_seq: 9,
        has_more_messages: false,
      }),
    });
    expect(await first).toBe(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it("leaves the transcript alone when the fetch fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => ({}) })));
    expect(await loadOlderMessages("s1")).toBe(0);
    expect(useSessionStore.getState().messages.s1.map((m) => m.seq)).toEqual([
      10, 11,
    ]);
    // Still true, so the user can try again rather than being told there is
    // nothing older when the request simply failed.
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(true);
    vi.unstubAllGlobals();
  });
});
