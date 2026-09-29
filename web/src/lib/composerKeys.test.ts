import { describe, expect, it } from "vitest";

import { isImeComposing } from "./composerKeys";

describe("isImeComposing", () => {
  it("is true while a composition is active (standard flag)", () => {
    expect(isImeComposing({ isComposing: true, keyCode: 13 })).toBe(true);
  });

  it("is true for the older WebKit sentinel — the macOS Safari case", () => {
    // Safari commits a CJK candidate with Enter but reports isComposing:false
    // and keyCode:229. This half is the one that actually fixed the bug.
    expect(isImeComposing({ isComposing: false, keyCode: 229 })).toBe(true);
  });

  it("is false for a plain Enter, so normal send is untouched", () => {
    expect(isImeComposing({ isComposing: false, keyCode: 13 })).toBe(false);
  });

  it("is false when the flags are absent", () => {
    expect(isImeComposing({})).toBe(false);
  });
});
