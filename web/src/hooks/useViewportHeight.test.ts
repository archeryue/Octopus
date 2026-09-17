/**
 * The height the app fills (`--app-h`) and where it starts (`--app-top`).
 *
 * The bug being guarded: an app sized straight from the visual viewport stops
 * short of the bottom of the screen after the keyboard animates away, because
 * iOS can leave a stale, smaller height behind. On a phone that shows up as a
 * band of dead page under the composer.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

import { useViewportHeight } from "./useViewportHeight";

const INNER = 844;

type FakeVV = {
  height: number;
  offsetTop: number;
  addEventListener: (t: string, f: () => void) => void;
  removeEventListener: (t: string, f: () => void) => void;
  fire: () => void;
};

let vv: FakeVV;

function fakeVisualViewport(height: number, offsetTop = 0): FakeVV {
  const listeners: (() => void)[] = [];
  return {
    height,
    offsetTop,
    addEventListener: (_t, f) => listeners.push(f),
    removeEventListener: () => {},
    fire: () => listeners.forEach((f) => f()),
  };
}

const appH = () =>
  document.documentElement.style.getPropertyValue("--app-h");
const appTop = () =>
  document.documentElement.style.getPropertyValue("--app-top");

beforeEach(() => {
  vi.useFakeTimers();
  window.innerHeight = INNER;
  vv = fakeVisualViewport(INNER);
  Object.defineProperty(window, "visualViewport", {
    configurable: true,
    value: vv,
  });
  document.body.innerHTML = '<textarea id="composer"></textarea>';
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  document.documentElement.style.removeProperty("--app-h");
  document.documentElement.style.removeProperty("--app-top");
});

describe("useViewportHeight", () => {
  it("fills the window when nothing is being typed into", () => {
    renderHook(() => useViewportHeight());
    expect(appH()).toBe(`${INNER}px`);
    expect(appTop()).toBe("0px");
  });

  it("ignores a stale visual viewport once the keyboard is gone", () => {
    // Exactly the iOS behaviour that left a dead band under the composer:
    // the keyboard has closed, nothing is focused, and the visual viewport
    // still reports the shrunken height.
    renderHook(() => useViewportHeight());
    vv.height = INNER - 90;
    act(() => vv.fire());
    expect(appH()).toBe(`${INNER}px`);
  });

  it("follows the visual viewport while a field is focused", () => {
    renderHook(() => useViewportHeight());
    const box = document.getElementById("composer") as HTMLTextAreaElement;
    box.focus();
    vv.height = INNER - 336; // keyboard up
    vv.offsetTop = 0;
    act(() => vv.fire());
    expect(appH()).toBe(`${INNER - 336}px`);
  });

  it("re-measures after the keyboard finishes animating away", () => {
    // The resize that matters is the one at the end of the animation, and
    // iOS doesn't reliably send it — so a blur schedules its own.
    renderHook(() => useViewportHeight());
    const box = document.getElementById("composer") as HTMLTextAreaElement;
    box.focus();
    vv.height = INNER - 336;
    act(() => vv.fire());
    expect(appH()).toBe(`${INNER - 336}px`);

    box.blur();
    act(() => {
      window.dispatchEvent(new Event("focusout"));
      vi.advanceTimersByTime(700);
    });
    expect(appH()).toBe(`${INNER}px`);
    expect(appTop()).toBe("0px");
  });
});
