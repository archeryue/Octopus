import { describe, it, expect } from "vitest";
import { isSessionBusy } from "./deferredFork";

describe("isSessionBusy", () => {
  it("is false only when idle with an empty queue and no open questions", () => {
    expect(isSessionBusy("idle", 0, 0)).toBe(false);
  });

  it("is true while a turn is running or waiting on approval", () => {
    expect(isSessionBusy("running", 0, 0)).toBe(true);
    expect(isSessionBusy("waiting_approval", 0, 0)).toBe(true);
  });

  it("is true when messages are still queued (fork waits for full drain)", () => {
    expect(isSessionBusy("idle", 2, 0)).toBe(true);
  });

  it("is true when an AskUserQuestion is open", () => {
    expect(isSessionBusy("idle", 0, 1)).toBe(true);
  });

  it("treats an unknown/undefined status as busy", () => {
    expect(isSessionBusy(undefined, 0, 0)).toBe(true);
  });
});
