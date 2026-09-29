/**
 * Who this browser is signed in as, asked once.
 *
 * It matters that a failure leaves the identity `null` rather than inventing
 * one: `null` means "not known yet", and the sidebar reads it as "show the
 * operator's rows", which is right for an install that has no accounts and for
 * a moment before the answer lands. An invented `is_admin: false` would hide
 * the Monitor page from the operator of a single-user install.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadIdentity } from "./loadIdentity";
import { useSessionStore } from "../stores/sessionStore";

beforeEach(() => {
  useSessionStore.getState().setToken("a-token");
  useSessionStore.getState().setIdentity(null);
});

afterEach(() => vi.unstubAllGlobals());

describe("loadIdentity", () => {
  it("stores what the route answers", async () => {
    const body = { label: "archer", user_id: "u1", is_admin: true };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => body })));
    await loadIdentity();
    expect(useSessionStore.getState().identity).toEqual(body);
  });

  it("sends the bearer", async () => {
    const calls: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        calls.push(init);
        return { ok: true, json: async () => ({ label: "x", user_id: null, is_admin: false }) };
      })
    );
    await loadIdentity();
    expect(
      (calls[0].headers as Record<string, string>).Authorization
    ).toBe("Bearer a-token");
  });

  it("leaves the identity unknown when the route refuses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => null })));
    await loadIdentity();
    expect(useSessionStore.getState().identity).toBeNull();
  });

  it("leaves the identity unknown when the server is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    await loadIdentity();
    expect(useSessionStore.getState().identity).toBeNull();
  });

  it("does nothing without a bearer", async () => {
    useSessionStore.getState().setToken("");
    const spy = vi.fn();
    vi.stubGlobal("fetch", spy);
    await loadIdentity();
    expect(spy).not.toHaveBeenCalled();
  });
});
