/**
 * The account row's handle.
 *
 * It used to render the access token, on the reasoning that in single-user mode
 * the token *is* the identity. But that row is pinned under the sidebar at all
 * times, so the credential was in every screenshot, every screen share and
 * every glance over a shoulder. These tests pin the two halves of the fix: the
 * handle is the identity's label, and the token is never on screen.
 *
 * The *fetch* lives in `lib/loadIdentity` now, tested there — two components
 * need the answer, so one of them asking for it was one answer too many.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

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
  useSessionStore.getState().setIdentity(null);
});

afterEach(cleanup);

describe("SidebarAccount", () => {
  it("shows the label from the identity", () => {
    useSessionStore
      .getState()
      .setIdentity({ label: "archeryue", user_id: "u1", is_admin: false });
    mount();
    expect(screen.getByText("archeryue")).toBeTruthy();
    // The initial tile follows the label, not the token's first character.
    expect(screen.getByText("A")).toBeTruthy();
  });

  it("never renders the token, before or after the label arrives", () => {
    const { container } = mount();
    expect(container.textContent).not.toContain(TOKEN);
    useSessionStore
      .getState()
      .setIdentity({ label: "archeryue", user_id: "u1", is_admin: false });
    expect(container.textContent).not.toContain(TOKEN);
  });

  it("stays generic when the identity isn't known", () => {
    const { container } = mount();
    expect(screen.getByText("signed in")).toBeTruthy();
    expect(container.textContent).not.toContain(TOKEN);
  });
});
