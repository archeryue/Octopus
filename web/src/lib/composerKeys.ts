/** Is this keydown the IME committing/navigating a composition, not a real key?
 *
 * CJK input (Chinese/Japanese/Korean) works by building a candidate from
 * several keystrokes and then committing it — and the commit is usually Enter.
 * If the composer treats that Enter as "send", the half-composed message goes
 * out early. Safari on macOS is the case that bit a user typing Chinese.
 *
 * The composing keydown is flagged one of two ways, and both must be checked:
 *   - `isComposing` — the standard `KeyboardEvent` property; and
 *   - `keyCode === 229` — the older WebKit sentinel ("IME is processing"),
 *     which is what some macOS Safari versions send on the commit Enter while
 *     leaving `isComposing` false. Dropping this half silently reintroduces
 *     the bug on exactly the browser that had it.
 */
export function isImeComposing(e: {
  isComposing?: boolean;
  keyCode?: number;
}): boolean {
  return e.isComposing === true || e.keyCode === 229;
}
