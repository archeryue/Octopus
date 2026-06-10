/**
 * Renderer tests for CredentialList's re-authorization affordance
 * (harness-credential-reauth.md §6). A credential flagged
 * `needs_reconnect` shows an "expired" badge + a "reauth" button;
 * clicking it kicks off that backend's login flow (carrying the target
 * credential id so the backend re-auths in place). A healthy credential
 * shows neither.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { CredentialList } from "./CredentialList";
import { useSessionStore, type CredentialInfo } from "../stores/sessionStore";

function cred(overrides: Partial<CredentialInfo> = {}): CredentialInfo {
  return {
    id: "c1",
    backend: "claude-code",
    label: "Personal",
    auth_type: "oauth",
    created_at: "2026-06-09T00:00:00Z",
    status: "active",
    token_expires_at: null,
    needs_reconnect: false,
    last_refresh_error_code: null,
    ...overrides,
  } as CredentialInfo;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  useSessionStore.setState({ token: "tok", credentials: [] });
  // Route by (method, url): the mount GET returns whatever the test seeded
  // into the store; the login-start POST returns a stub login id + url.
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.endsWith("/api/credentials") && method === "GET") {
      return new Response(
        JSON.stringify(useSessionStore.getState().credentials),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    }
    if (url.endsWith("/oauth/start")) {
      return new Response(
        JSON.stringify({ login_id: "login-1", device_url: "https://claude.ai/x" }),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    }
    if (url.endsWith("/codex/start")) {
      return new Response(JSON.stringify({ login_id: "login-2" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("CredentialList re-authorization", () => {
  it("shows no reauth affordance for a healthy credential", async () => {
    useSessionStore.setState({ credentials: [cred()] });
    await act(async () => {
      render(<CredentialList />);
    });
    await screen.findByText("Personal");
    expect(screen.queryByTitle("Re-authorize this credential")).toBeNull();
    expect(screen.queryByText("expired")).toBeNull();
  });

  it("renders expired badge + reauth button when needs_reconnect", async () => {
    useSessionStore.setState({
      credentials: [cred({ needs_reconnect: true, last_refresh_error_code: "invalid_credentials" })],
    });
    await act(async () => {
      render(<CredentialList />);
    });
    expect(await screen.findByText("expired")).toBeTruthy();
    expect(screen.getByTitle("Re-authorize this credential")).toBeTruthy();
  });

  it("clicking reauth starts the Claude OAuth flow", async () => {
    useSessionStore.setState({ credentials: [cred({ needs_reconnect: true })] });
    await act(async () => {
      render(<CredentialList />);
    });
    const btn = await screen.findByTitle("Re-authorize this credential");
    await act(async () => {
      fireEvent.click(btn);
    });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) => String(u).endsWith("/oauth/start"))
      ).toBe(true)
    );
  });

  it("clicking reauth on a codex credential starts the device flow", async () => {
    useSessionStore.setState({
      credentials: [cred({ id: "c2", backend: "codex", needs_reconnect: true, label: "ChatGPT" })],
    });
    await act(async () => {
      render(<CredentialList />);
    });
    const btn = await screen.findByTitle("Re-authorize this credential");
    await act(async () => {
      fireEvent.click(btn);
    });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) =>
        String(u).endsWith("/codex/start")
      );
      expect(call).toBeTruthy();
      const body = JSON.parse((call![1] as RequestInit).body as string);
      expect(body.reauth_credential_id).toBe("c2");
      expect(body.label).toBe("ChatGPT");
    });
  });
});
