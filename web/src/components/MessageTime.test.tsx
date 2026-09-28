/**
 * The hover timestamp (feature: "show me when a message happened").
 *
 * Two things are worth pinning: the format switches to include the date once
 * the message isn't from today (a 20-minute turn crossing midnight otherwise
 * reads as out of order), and a message with no stored time renders *nothing*
 * rather than "Invalid Date" — which is the common case for every message
 * written before the column existed.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { MessageTime, formatMessageTime } from "./MessageTime";

afterEach(cleanup);

describe("formatMessageTime", () => {
  const now = new Date("2026-09-26T14:00:00");

  it("is just the clock for a message from today", () => {
    const iso = new Date("2026-09-26T09:05:00").toISOString();
    expect(formatMessageTime(iso, now)).toBe("09:05");
  });

  it("carries the date once it isn't today", () => {
    const iso = new Date("2026-09-25T23:58:00").toISOString();
    const out = formatMessageTime(iso, now);
    expect(out).toContain("23:58");
    expect(out).toMatch(/Sep|09/);
  });

  it("is empty rather than 'Invalid Date' for a value that isn't one", () => {
    expect(formatMessageTime("not-a-time", now)).toBe("");
  });
});

describe("MessageTime", () => {
  it("renders a <time> with the full stamp in its title", () => {
    // The component formats against the real clock, so the message has to be
    // from the real today — a fixed date only passes on the day it names.
    const today = new Date();
    today.setHours(9, 5, 0, 0);
    const iso = today.toISOString();
    const { container } = render(<MessageTime iso={iso} />);
    const el = container.querySelector("time");
    expect(el).toBeTruthy();
    expect(el?.getAttribute("datetime")).toBe(iso);
    expect(el?.getAttribute("title")).toBeTruthy();
    expect(el?.textContent).toBe("09:05");
  });

  it("renders nothing when the message has no stored time", () => {
    for (const iso of [undefined, null, "", "nonsense"]) {
      const { container } = render(<MessageTime iso={iso} />);
      expect(container.querySelector("time")).toBeNull();
      cleanup();
    }
  });
});
