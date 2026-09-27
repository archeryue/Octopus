/**
 * Shell-style history for the composer: ArrowUp walks back through what you
 * sent, ArrowDown walks forward, and coming back past the newest entry restores
 * whatever you were typing when you started.
 *
 * Pure on purpose — the interesting part is the state machine (when a key is
 * *ours* versus when the textarea should get it), and that is much easier to
 * pin here than through a rendered component.
 */

export interface HistoryState {
  /** Index into the history list, or null when editing the live draft. */
  cursor: number | null;
  /** What was in the box when navigation started, restored on the way back. */
  draft: string;
}

export const IDLE: HistoryState = { cursor: null, draft: "" };

export interface Recall {
  state: HistoryState;
  text: string;
}

/**
 * The user's own sent messages, oldest first — what ArrowUp walks back through.
 *
 * Only real typed turns: an agent reply injected as a user-role message (a
 * delegation reply, a background-task result, a scheduled fire) is something
 * the user never typed, and recalling it would be nonsense. Consecutive
 * duplicates collapse, the way a shell's history does.
 */
export function historyFromMessages(
  messages: { role?: string; type?: string; content?: unknown }[],
  isInjected: (text: string) => boolean = () => false
): string[] {
  const out: string[] = [];
  for (const m of messages) {
    if (m.role !== "user" || m.type !== "text") continue;
    const text = typeof m.content === "string" ? m.content : "";
    if (!text.trim() || isInjected(text)) continue;
    if (out[out.length - 1] === text) continue;
    out.push(text);
  }
  return out;
}

/** ArrowUp. `null` means "not ours" — let the textarea have the key. */
export function recallPrev(
  history: string[],
  state: HistoryState,
  current: string
): Recall | null {
  if (history.length === 0) return null;
  if (state.cursor === null) {
    // Starting out: remember the draft so ArrowDown can bring it back.
    const cursor = history.length - 1;
    return { state: { cursor, draft: current }, text: history[cursor] };
  }
  if (state.cursor === 0) {
    // At the oldest. Consume the key rather than letting the caret jump —
    // a shell stays put here too, and falling through would move the caret
    // to the top of a recalled multi-line message unexpectedly.
    return { state, text: current };
  }
  const cursor = state.cursor - 1;
  return { state: { ...state, cursor }, text: history[cursor] };
}

/** ArrowDown. `null` means "not ours". */
export function recallNext(
  history: string[],
  state: HistoryState,
  _current: string
): Recall | null {
  if (state.cursor === null) return null; // not navigating; normal caret move
  if (state.cursor >= history.length - 1) {
    return { state: IDLE, text: state.draft }; // back out to the draft
  }
  const cursor = state.cursor + 1;
  return { state: { ...state, cursor }, text: history[cursor] };
}

/**
 * Whether ArrowUp/ArrowDown at this caret position should mean "history"
 * rather than "move the caret".
 *
 * The rule is the one editors use: the key is history's only while the caret is
 * on the first line (Up) or the last line (Down), so a recalled multi-line
 * message stays navigable with the same keys.
 */
export function caretWantsHistory(
  value: string,
  caret: number,
  direction: "up" | "down"
): boolean {
  const before = value.slice(0, caret);
  const after = value.slice(caret);
  return direction === "up" ? !before.includes("\n") : !after.includes("\n");
}
