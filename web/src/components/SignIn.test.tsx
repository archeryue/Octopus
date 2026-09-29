/**
 * The sign-in screen has to ask the right question for the era it is in
 * (multi-tenancy.md §9).
 *
 * Before the first account exists there is no username to ask for and the way
 * in is the install's own token; after it, a token is not a way in at all.
 * Getting that wrong strands somebody outside their own box, so both halves
 * are pinned here — including the default it renders *before* `/api/auth/state`
 * answers, which is what the existing e2e suite drives.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SignIn } from "./SignIn";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A `fetch` double that answers `/api/auth/state` and records every POST. */
function stubFetch({
  accountsExist,
  post,
}: {
  accountsExist: boolean;
  post?: (path: string, body: unknown) => { ok: boolean; body: unknown };
}) {
  const calls: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url).replace(window.location.origin, "");
      if (!init || init.method !== "POST") {
        return { ok: true, json: async () => ({ accounts_exist: accountsExist }) };
      }
      const body = JSON.parse(String(init.body));
      calls.push({ path, body });
      const reply = post?.(path, body) ?? { ok: true, body: { token: "issued" } };
      return { ok: reply.ok, status: reply.ok ? 200 : 401, json: async () => reply.body };
    })
  );
  return calls;
}

describe("SignIn", () => {
  beforeEach(() => stubFetch({ accountsExist: false }));

  it("asks for a token on an install with no accounts", async () => {
    render(<SignIn onSignedIn={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Token")).toBeTruthy());
    expect(screen.queryByLabelText("Username")).toBeNull();
  });

  it("stores the token as typed, without asking the server for one", async () => {
    const seen: string[] = [];
    render(<SignIn onSignedIn={(t) => seen.push(t)} />);
    fireEvent.change(screen.getByLabelText("Token"), {
      target: { value: "  changeme  " },
    });
    fireEvent.click(screen.getByText("Connect"));
    expect(seen).toEqual(["changeme"]);
  });

  it("asks for a username and password once an account exists", async () => {
    stubFetch({ accountsExist: true });
    render(<SignIn onSignedIn={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeTruthy());
    expect(screen.queryByLabelText("Token")).toBeNull();
  });

  it("exchanges the password for the bearer the server issues", async () => {
    const calls = stubFetch({ accountsExist: true });
    const seen: string[] = [];
    render(<SignIn onSignedIn={(t) => seen.push(t)} />);
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "archer" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByText("Sign in"));
    await waitFor(() => expect(seen).toEqual(["issued"]));
    expect(calls).toEqual([
      { path: "/api/auth/login", body: { username: "archer", password: "pw" } },
    ]);
    // The password itself is never what gets stored.
    expect(seen[0]).not.toBe("pw");
  });

  it("shows the server's reason when sign-in is refused", async () => {
    stubFetch({
      accountsExist: true,
      post: () => ({ ok: false, body: { detail: "Too many attempts" } }),
    });
    render(<SignIn onSignedIn={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeTruthy());
    fireEvent.click(screen.getByText("Sign in"));
    await waitFor(() => expect(screen.getByText("Too many attempts")).toBeTruthy());
  });

  it("registers with an invite code", async () => {
    const calls = stubFetch({ accountsExist: true });
    const seen: string[] = [];
    render(<SignIn onSignedIn={(t) => seen.push(t)} />);
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeTruthy());
    fireEvent.click(screen.getByText("I have an invite code"));
    fireEvent.change(screen.getByLabelText("Invite code"), {
      target: { value: "code-1" },
    });
    fireEvent.change(screen.getByLabelText("Choose a username"), {
      target: { value: "vera" },
    });
    fireEvent.change(screen.getByLabelText("Choose a password"), {
      target: { value: "pw" },
    });
    fireEvent.click(screen.getByText("Create account"));
    await waitFor(() => expect(seen).toEqual(["issued"]));
    expect(calls[0]).toEqual({
      path: "/api/auth/register",
      body: { invite_code: "code-1", username: "vera", password: "pw" },
    });
  });

  it("leaves the token form up when the server can't be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    render(<SignIn onSignedIn={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Token")).toBeTruthy());
  });
});
