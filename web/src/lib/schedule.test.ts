import { describe, expect, it } from "vitest";
import {
  deriveScheduleName,
  formatScheduleInterval,
  parseScheduleCommand,
  parseScheduleInterval,
} from "./schedule";

describe("parseScheduleInterval", () => {
  it("parses unit suffixes", () => {
    expect(parseScheduleInterval("30s")).toBe(30);
    expect(parseScheduleInterval("15m")).toBe(900);
    expect(parseScheduleInterval("2h")).toBe(7200);
    expect(parseScheduleInterval("1d")).toBe(86400);
  });

  it("treats a bare number as minutes", () => {
    expect(parseScheduleInterval("5")).toBe(300);
    expect(parseScheduleInterval("45")).toBe(2700);
  });

  it("is case-insensitive and trims", () => {
    expect(parseScheduleInterval("  2H ")).toBe(7200);
  });

  it("rejects junk and non-positive values", () => {
    expect(parseScheduleInterval("soon")).toBeNull();
    expect(parseScheduleInterval("m")).toBeNull();
    expect(parseScheduleInterval("0")).toBeNull();
    expect(parseScheduleInterval("1.5h")).toBeNull();
  });
});

describe("formatScheduleInterval", () => {
  it("collapses to the largest whole unit", () => {
    expect(formatScheduleInterval(86400)).toBe("1d");
    expect(formatScheduleInterval(7200)).toBe("2h");
    expect(formatScheduleInterval(2700)).toBe("45m");
    expect(formatScheduleInterval(90)).toBe("90s");
  });
});

describe("deriveScheduleName", () => {
  it("uses the first line", () => {
    expect(deriveScheduleName("Check my email\nfor anything urgent")).toBe(
      "Check my email"
    );
  });

  it("caps long prompts with an ellipsis", () => {
    const long = "a".repeat(80);
    const name = deriveScheduleName(long);
    expect(name.endsWith("…")).toBe(true);
    expect(name.length).toBeLessThanOrEqual(48);
  });

  it("falls back when the prompt is blank", () => {
    expect(deriveScheduleName("   ")).toBe("Scheduled task");
  });
});

describe("parseScheduleCommand", () => {
  it("parses interval + prompt", () => {
    const r = parseScheduleCommand("30m summarize my unread email");
    expect(r).toEqual({
      ok: true,
      intervalSeconds: 1800,
      prompt: "summarize my unread email",
      name: "summarize my unread email",
    });
  });

  it("requires both an interval and a prompt", () => {
    expect(parseScheduleCommand("30m").ok).toBe(false);
    expect(parseScheduleCommand("").ok).toBe(false);
  });

  it("reports an unreadable interval", () => {
    const r = parseScheduleCommand("whenever do the thing");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain("whenever");
  });

  it("enforces the 1-minute floor", () => {
    const r = parseScheduleCommand("30s too fast");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain("Minimum");
  });
});
