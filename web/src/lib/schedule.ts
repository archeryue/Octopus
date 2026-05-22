/** Parsing + formatting helpers for the `/schedule` chat command and the
 * Schedules overview. Kept framework-free so they're trivially unit-tested.
 *
 * Command shape: `/schedule <interval> <prompt…>` — the first whitespace
 * token is the interval, everything after it is the prompt. The schedule's
 * display name is derived from the prompt (the backend has no separate name
 * concept worth a second token in chat). */

const MIN_INTERVAL_SECONDS = 60; // mirrors server CreateScheduleRequest (ge=60)

export const SCHEDULE_USAGE =
  'Usage: /schedule <interval> <prompt> — e.g. "/schedule 30m check my email". ' +
  "Interval units: s, m, h, d (a bare number means minutes; minimum 1m).";

/** Parse a single interval token (`30s`, `15m`, `2h`, `1d`, or bare `30` =
 * minutes) into whole seconds. Returns null on anything unparseable. */
export function parseScheduleInterval(token: string): number | null {
  const m = /^(\d+)(s|m|h|d)?$/i.exec(token.trim());
  if (!m) return null;
  const n = parseInt(m[1], 10);
  if (!Number.isFinite(n) || n <= 0) return null;
  const unit = (m[2] || "m").toLowerCase();
  const mult = unit === "s" ? 1 : unit === "m" ? 60 : unit === "h" ? 3600 : 86400;
  return n * mult;
}

/** Render seconds back to the most compact whole unit (`3600` → `"1h"`). */
export function formatScheduleInterval(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

/** A schedule name derived from its prompt: first line, trimmed, capped. */
export function deriveScheduleName(prompt: string): string {
  const firstLine = prompt.split("\n", 1)[0].trim();
  if (!firstLine) return "Scheduled task";
  if (firstLine.length <= 48) return firstLine;
  return firstLine.slice(0, 47).trimEnd() + "…";
}

export type ParsedScheduleCommand =
  | { ok: true; intervalSeconds: number; prompt: string; name: string }
  | { ok: false; error: string };

/** Parse the argument string that follows `/schedule` (already stripped of
 * the command word). */
export function parseScheduleCommand(args: string): ParsedScheduleCommand {
  const m = /^(\S+)\s+([\s\S]+)$/.exec(args.trim());
  if (!m) return { ok: false, error: SCHEDULE_USAGE };
  const intervalSeconds = parseScheduleInterval(m[1]);
  if (intervalSeconds === null)
    return {
      ok: false,
      error: `Couldn't read "${m[1]}" as an interval. ${SCHEDULE_USAGE}`,
    };
  if (intervalSeconds < MIN_INTERVAL_SECONDS)
    return { ok: false, error: "Minimum interval is 1m (60s)." };
  const prompt = m[2].trim();
  if (!prompt) return { ok: false, error: SCHEDULE_USAGE };
  return {
    ok: true,
    intervalSeconds,
    prompt,
    name: deriveScheduleName(prompt),
  };
}
