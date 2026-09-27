/**
 * When a message happened, revealed on hover.
 *
 * The case for it is a turn that ran for twenty minutes: the transcript says
 * what happened in order but not *when*, so "did that tool call take four
 * minutes or forty" was unanswerable after the fact. A clock time answers it
 * without a column of timestamps down the page, which is why this is hover-only
 * rather than always on.
 *
 * Absolutely positioned so revealing it never reflows the transcript — a
 * timestamp that pushed the message down on hover would make the thing it labels
 * move away from the cursor. **Inside** its own row's box, not in the gap above
 * it: floated above, it bled into the previous message (so it appeared to label
 * the wrong one) and the first row's label was clipped by the scroller, which is
 * what "the agent's messages have no time" actually was.
 *
 * The corner it occupies is kept clear by every shape that could reach it — the
 * user bubble's rewind affordance, the assistant's prose, and the tool and
 * tool-result headers all reserve room on the right (`pr-12`). Reserving beats
 * floating over: the one thing worse than no timestamp is a timestamp sitting on
 * top of the button you were aiming at.
 *
 * And it is *inset* a few pixels rather than flush with the row's edge, because
 * the tool shapes are bordered cards that begin at the row's top: flush, the
 * chip's own background sat on their 1px stroke and broke the outline of the box
 * it was labelling. Inset, it floats inside the card, clear of both the stroke
 * and the rounded corner.
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
      className="message-time pointer-events-auto absolute right-2 top-0.5 z-10 rounded bg-white/85 px-1 font-mono text-[10px] leading-4 text-gray-600 opacity-0 transition-opacity group-hover/msg:opacity-100"
      dateTime={iso}
      title={at.toLocaleString()}
    >
      {formatMessageTime(iso)}
    </time>
  );
}
