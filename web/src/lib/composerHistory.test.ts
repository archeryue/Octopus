/**
 * The composer's history state machine.
 *
 * Written against the behaviour a shell has, because that is what was asked
 * for and what fingers already expect: Up walks back, Down walks forward,
 * coming back past the newest restores the unsent draft, and the ends of the
 * list don't fall through to caret movement.
 */

import { describe, expect, it } from "vitest";

import {
  IDLE,
  caretWantsHistory,
  historyFromMessages,
  recallNext,
  recallPrev,
} from "./composerHistory";

const H = ["first", "second", "third"];

describe("historyFromMessages", () => {
  it("keeps the user's own text turns, oldest first", () => {
    expect(
      historyFromMessages([
        { role: "user", type: "text", content: "a" },
        { role: "assistant", type: "text", content: "reply" },
        { role: "user", type: "text", content: "b" },
        { role: "user", type: "tool_result", content: "not a turn" },
      ])
    ).toEqual(["a", "b"]);
  });

  it("collapses consecutive duplicates and drops blanks", () => {
    expect(
      historyFromMessages([
        { role: "user", type: "text", content: "same" },
        { role: "user", type: "text", content: "same" },
        { role: "user", type: "text", content: "   " },
        { role: "user", type: "text", content: "other" },
      ])
    ).toEqual(["same", "other"]);
  });

  it("leaves out turns the user never typed", () => {
    // Delegation replies, bg-task results and scheduled fires all arrive as
    // user-role messages; recalling one would put machine text in the box.
    const injected = (t: string) => t.startsWith("[");
    expect(
      historyFromMessages(
        [
          { role: "user", type: "text", content: "mine" },
          { role: "user", type: "text", content: "[agent-reply:Vera] hi" },
        ],
        injected
      )
    ).toEqual(["mine"]);
  });
});

describe("recallPrev / recallNext", () => {
  it("walks back from the newest and remembers the draft", () => {
    const up1 = recallPrev(H, IDLE, "half-typed")!;
    expect(up1.text).toBe("third");
    expect(up1.state).toEqual({ cursor: 2, draft: "half-typed" });

    const up2 = recallPrev(H, up1.state, up1.text)!;
    expect(up2.text).toBe("second");
  });

  it("stops at the oldest, and still consumes the key", () => {
    let r = recallPrev(H, IDLE, "")!;
    r = recallPrev(H, r.state, r.text)!;
    r = recallPrev(H, r.state, r.text)!;
    expect(r.text).toBe("first");
    const again = recallPrev(H, r.state, r.text);
    expect(again).not.toBeNull();
    expect(again!.text).toBe("first");
    expect(again!.state.cursor).toBe(0);
  });

  it("comes back out to the draft that was there", () => {
    const up = recallPrev(H, IDLE, "my draft")!;
    const down = recallNext(H, up.state, up.text)!;
    expect(down.text).toBe("my draft");
    expect(down.state).toEqual(IDLE);
  });

  it("is not ours with no history, or on Down while not navigating", () => {
    expect(recallPrev([], IDLE, "x")).toBeNull();
    expect(recallNext(H, IDLE, "x")).toBeNull();
  });
});

describe("caretWantsHistory", () => {
  it("is the first line for Up and the last line for Down", () => {
    const v = "one\ntwo";
    expect(caretWantsHistory(v, 0, "up")).toBe(true);
    expect(caretWantsHistory(v, 3, "up")).toBe(true);
    expect(caretWantsHistory(v, 5, "up")).toBe(false); // second line
    expect(caretWantsHistory(v, 7, "down")).toBe(true);
    expect(caretWantsHistory(v, 1, "down")).toBe(false); // more lines below
  });
});
