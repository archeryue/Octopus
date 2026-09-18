/**
 * Rotating the access token from Settings (token-rotation.md).
 *
 * The token is also the key every stored secret is encrypted with, so this is
 * one server-side operation — the dialog's job is to send it, say what
 * happened, and stay out of the way while the WebSocket hands this tab the
 * new token.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SettingsDialog } from "./SettingsDialog";
import { useSessionStore } from "../stores/sessionStore";

let fetchMock: ReturnType<typeof vi.fn>;

function openSettings() {
  const view = render(<SettingsDialog open onOpenChange={() => {}} />);
  // Radix tabs switch on pointerdown, which jsdom doesn't synthesize.
  const tab = screen.getByRole("tab", { name: /account/i });
  fireEvent.pointerDown(tab);
  fireEvent.mouseDown(tab);
  fireEvent.click(tab);
  return view;
}

beforeEach(() => {
  useSessionStore.setState({ token: "old-token-1234" });
  fetchMock = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          env_files: ["/home/me/.env"],
          reencrypted: { credential_secrets: 2, connector_oauth_clients: 1 },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("rotating the token", () => {
  it("sends the new token with the old one and reports what changed", async () => {
    openSettings();
    fireEvent.change(screen.getByPlaceholderText(/New token/i), {
      target: { value: "a-much-stronger-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: /rotate/i }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) =>
        String(u).endsWith("/api/auth/rotate")
      );
      expect(call).toBeTruthy();
      const init = call![1] as RequestInit;
      // Authenticated with the token being replaced — which is exactly who is
      // allowed to do this.
      expect((init.headers as Record<string, string>).Authorization).toBe(
        "Bearer old-token-1234"
      );
      expect(JSON.parse(init.body as string)).toEqual({
        new_token: "a-much-stronger-token",
        revoke_other_clients: false,
      });
    });

    // A rotation is never a silent success: it says how many secrets were
    // re-keyed and which file was written.
    await waitFor(() =>
      expect(screen.getByText(/3 stored secrets re-encrypted/)).toBeTruthy()
    );
    expect(screen.getByText(/\/home\/me\/\.env/)).toBeTruthy();
  });

  it("can revoke the other devices when the old token leaked", async () => {
    openSettings();
    fireEvent.change(screen.getByPlaceholderText(/New token/i), {
      target: { value: "a-much-stronger-token" },
    });
    fireEvent.click(screen.getByLabelText(/sign out other devices/i));
    fireEvent.click(screen.getByRole("button", { name: /rotate/i }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) =>
        String(u).endsWith("/api/auth/rotate")
      );
      expect(JSON.parse((call![1] as RequestInit).body as string)).toEqual({
        new_token: "a-much-stronger-token",
        revoke_other_clients: true,
      });
    });
  });

  it("shows the server's refusal rather than pretending it worked", async () => {
    fetchMock.mockImplementation(
      async () =>
        new Response(
          JSON.stringify({ detail: "The new token must be at least 12 characters" }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        )
    );
    openSettings();
    fireEvent.change(screen.getByPlaceholderText(/New token/i), {
      target: { value: "short" },
    });
    fireEvent.click(screen.getByRole("button", { name: /rotate/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/must be at least 12 characters/)
      ).toBeTruthy()
    );
    // The client keeps the token it has; the server refused to change it.
    expect(useSessionStore.getState().token).toBe("old-token-1234");
  });

  it("won't send an empty token", () => {
    openSettings();
    expect(
      (screen.getByRole("button", { name: /rotate/i }) as HTMLButtonElement).disabled
    ).toBe(true);
  });
});
