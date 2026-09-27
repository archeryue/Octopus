/**
 * When a message happened, revealed on hover.
 *
 * The case for it is a turn that ran for twenty minutes: the transcript says
 * what happened in order but not *when*, so "did that tool call take four
 * minutes or forty" was unanswerable after the fact. A clock time answers it
 * without a column of timestamps down the page, which is why this is hover-only
 * rather than always on.
 *
 * Absolutely positioned in the gap above its row so revealing it never reflows
 * the transcript — a timestamp that pushed the message down on hover would make
 * the thing it labels move away from the cursor.
 *
 * `null` for messages written before the column existed (there was nothing to
 * backfill from), and for anything not yet persisted.
 */

/** `14:32`, or `Sep 25 14:32` when it isn't today. Local time, because the
 *  reader is local; the full stamp goes in the `title`. */
export function formatMessageTime(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const clock = at.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const sameDay =
    at.getFullYear() === now.getFullYear() &&
    at.getMonth() === now.getMonth() &&
    at.getDate() === now.getDate();
  if (sameDay) return clock;
  const day = at.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${day} ${clock}`;
}

export function MessageTime({ iso }: { iso?: string | null }) {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return (
    <time
      className="message-time pointer-events-auto absolute right-1 -top-2 z-10 rounded bg-white/85 px-1 font-mono text-[10px] leading-4 text-gray-600 opacity-0 transition-opacity group-hover/msg:opacity-100"
      dateTime={iso}
      title={at.toLocaleString()}
    >
      {formatMessageTime(iso)}
    </time>
  );
}
