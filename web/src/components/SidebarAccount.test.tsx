/**
 * The account row's handle.
 *
 * It used to render the access token, on the reasoning that in single-user mode
 * the token *is* the identity. But that row is pinned under the sidebar at all
 * times, so the credential was in every screenshot, every screen share and
 * every glance over a shoulder. These tests pin the two halves of the fix: the
 * handle comes from `/api/auth/identity`, and the token is never on screen.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { SidebarAccount } from "./SidebarAccount";
import { useSessionStore } from "../stores/sessionStore";

const TOKEN = "s3cret-token-value";

function mount() {
  return render(
    <SidebarAccount
      onSignOut={() => {}}
      onOpenSettings={() => {}}
      onOpenArchivedSessions={() => {}}
    />
  );
}

beforeEach(() => {
  useSessionStore.getState().setToken(TOKEN);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({ label: "archeryue" }) }))
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SidebarAccount", () => {
  it("shows the label from the identity route", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("archeryue")).toBeTruthy());
    // The initial tile follows the label, not the token's first character.
    expect(screen.getByText("A")).toBeTruthy();
  });

  it("never renders the token, before or after the label arrives", async () => {
    const { container } = mount();
    expect(container.textContent).not.toContain(TOKEN);
    await waitFor(() => expect(screen.getByText("archeryue")).toBeTruthy());
    expect(container.textContent).not.toContain(TOKEN);
  });

  it("stays generic when the identity route can't be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => null })));
    const { container } = mount();
    await waitFor(() => expect(screen.getByText("signed in")).toBeTruthy());
    expect(container.textContent).not.toContain(TOKEN);
  });

  it("asks again when the token changes, which is what a rotation does", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("archeryue")).toBeTruthy());
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    useSessionStore.getState().setToken("a-rotated-token");
    await waitFor(() =>
      expect(
        (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length
      ).toBeGreaterThan(calls)
    );
  });
});
