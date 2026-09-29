/**
 * The Account page is where an install becomes an account and where an admin
 * decides who else gets one (multi-tenancy.md §3, §9).
 *
 * The tests that matter are about *what is shown to whom*: the claim form only
 * before accounts exist, the admin sections only to an admin, and — the one
 * that would be a real incident — no way for an admin to disable themselves
 * and leave the site with nobody who can administer it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AccountPage } from "./AccountPage";
import { useSessionStore } from "../stores/sessionStore";

interface Routes {
  accountsExist: boolean;
  identity: { label: string; user_id: string | null; is_admin: boolean };
  invites?: unknown[];
  users?: unknown[];
  workspace?: unknown;
}

let posts: { path: string; body: unknown }[] = [];

function stub(routes: Routes) {
  posts = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url).replace(`${window.location.origin}/api/auth`, "");
      if (init?.method && init.method !== "GET") {
        posts.push({ path, body: init.body ? JSON.parse(String(init.body)) : null });
        if (path === "/bootstrap") {
          return {
            ok: true,
            json: async () => ({
              token: "issued",
              summary: {
                username: "archer",
                adopted: { sessions: 12, agents: 2 },
                extra_roots: ["/home/start-up/Octopus"],
                agent_memory_moved: true,
              },
            }),
          };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      }
      const body: Record<string, unknown> = {
        "/state": { accounts_exist: routes.accountsExist },
        "/identity": routes.identity,
        "/invites": routes.invites ?? [],
        "/users": routes.users ?? [],
        "/workspace": routes.workspace ?? {
          workspace: "/home/you/.octopus/users/u/workspace",
          extra_roots: [],
          confined: true,
        },
      }[path] as Record<string, unknown>;
      return { ok: true, json: async () => body };
    })
  );
}

beforeEach(() => useSessionStore.getState().setToken("install-token"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AccountPage", () => {
  it("offers to claim an install that has no accounts yet", async () => {
    stub({
      accountsExist: false,
      identity: { label: "archeryue", user_id: null, is_admin: false },
    });
    render(<AccountPage />);
    await waitFor(() => expect(screen.getByText("Claim this install")).toBeTruthy());
    // Nothing else applies yet: there is no password to change and no admin.
    // ("Password" alone would match the claim form's own field.)
    expect(screen.queryByText("Change password")).toBeNull();
    expect(screen.queryByText("Invites")).toBeNull();
  });

  it("reports what the claim moved, and stores the new bearer", async () => {
    stub({
      accountsExist: false,
      identity: { label: "archeryue", user_id: null, is_admin: false },
    });
    render(<AccountPage />);
    await waitFor(() => expect(screen.getByText("Claim this install")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "archer" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByText("Create account"));

    await waitFor(() =>
      expect(screen.getByText("This install now belongs to archer")).toBeTruthy()
    );
    expect(screen.getByText(/12 rows adopted/)).toBeTruthy();
    expect(useSessionStore.getState().token).toBe("issued");
    expect(posts[0]).toEqual({
      path: "/bootstrap",
      body: { username: "archer", password: "pw" },
    });
  });

  it("shows an ordinary account its password and nothing else", async () => {
    stub({
      accountsExist: true,
      identity: { label: "vera", user_id: "u2", is_admin: false },
    });
    render(<AccountPage />);
    await waitFor(() => expect(screen.getByText("Change password")).toBeTruthy());
    expect(screen.queryByText("Claim this install")).toBeNull();
    expect(screen.queryByText("Invites")).toBeNull();
    expect(screen.queryByText("People")).toBeNull();
  });

  it("gives an admin invites and people", async () => {
    stub({
      accountsExist: true,
      identity: { label: "archer", user_id: "u1", is_admin: true },
      invites: [
        {
          code: "inv-live",
          created_at: "2026-09-29T00:00:00Z",
          expires_at: "2026-10-13T00:00:00Z",
          max_uses: 1,
          used_count: 0,
          revoked_at: null,
        },
        {
          code: "inv-dead",
          created_at: "2026-09-01T00:00:00Z",
          expires_at: null,
          max_uses: 1,
          used_count: 1,
          revoked_at: "2026-09-02T00:00:00Z",
        },
      ],
      users: [
        {
          id: "u1",
          username: "archer",
          is_admin: true,
          created_at: "2026-09-01T00:00:00Z",
          disabled_at: null,
        },
        {
          id: "u2",
          username: "vera",
          is_admin: false,
          created_at: "2026-09-20T00:00:00Z",
          disabled_at: null,
        },
      ],
    });
    render(<AccountPage />);
    await waitFor(() => expect(screen.getByText("inv-live")).toBeTruthy());
    // A revoked invite is not a way in, so it is not on the list.
    expect(screen.queryByText("inv-dead")).toBeNull();
    expect(screen.getByText("vera")).toBeTruthy();
  });

  it("gives an admin no way to disable their own account", async () => {
    stub({
      accountsExist: true,
      identity: { label: "archer", user_id: "u1", is_admin: true },
      users: [
        {
          id: "u1",
          username: "archer",
          is_admin: true,
          created_at: "2026-09-01T00:00:00Z",
          disabled_at: null,
        },
        {
          id: "u2",
          username: "vera",
          is_admin: false,
          created_at: "2026-09-20T00:00:00Z",
          disabled_at: null,
        },
      ],
    });
    render(<AccountPage />);
    // Waited on the *other* person: "archer" is also the identity card's
    // heading, so it is on screen before the list arrives.
    await waitFor(() => expect(screen.getByText("vera")).toBeTruthy());
    const toggles = screen.getAllByLabelText("Disable account");
    expect(toggles).toHaveLength(1);
    fireEvent.click(toggles[0]);
    await waitFor(() =>
      expect(posts).toEqual([
        { path: "/users/u2/disabled", body: { disabled: true } },
      ])
    );
  });

  it("sends the current password with the new one", async () => {
    stub({
      accountsExist: true,
      identity: { label: "vera", user_id: "u2", is_admin: false },
    });
    render(<AccountPage />);
    await waitFor(() => expect(screen.getByText("Change password")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Current password"), {
      target: { value: "old" },
    });
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "new" },
    });
    fireEvent.click(screen.getByText("Change password"));
    await waitFor(() =>
      expect(posts).toEqual([
        { path: "/password", body: { current_password: "old", new_password: "new" } },
      ])
    );
  });
});

describe("AccountPage — where you can work", () => {
  const workspace = {
    workspace: "/home/you/.octopus/users/u1/workspace",
    extra_roots: ["/home/you/Octopus"],
    confined: true,
  };

  it("shows the workspace and the directories opened outside it", async () => {
    stub({
      accountsExist: true,
      identity: { label: "archer", user_id: "u1", is_admin: true },
      workspace,
    });
    render(<AccountPage />);
    await waitFor(() =>
      expect(screen.getByText("/home/you/.octopus/users/u1/workspace")).toBeTruthy()
    );
    expect(screen.getByText("/home/you/Octopus")).toBeTruthy();
  });

  it("adds a directory, sending the whole list", async () => {
    stub({
      accountsExist: true,
      identity: { label: "archer", user_id: "u1", is_admin: true },
      workspace,
    });
    render(<AccountPage />);
    await waitFor(() => expect(screen.getByLabelText("Add a directory")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Add a directory"), {
      target: { value: "/home/you/another-repo" },
    });
    fireEvent.click(screen.getByText("Add"));
    await waitFor(() =>
      expect(posts).toContainEqual({
        path: "/users/u1/extra-roots",
        body: { extra_roots: ["/home/you/Octopus", "/home/you/another-repo"] },
      })
    );
  });

  it("removes one by sending the list without it", async () => {
    stub({
      accountsExist: true,
      identity: { label: "archer", user_id: "u1", is_admin: true },
      workspace,
    });
    render(<AccountPage />);
    await waitFor(() =>
      expect(screen.getByLabelText("Stop allowing /home/you/Octopus")).toBeTruthy()
    );
    fireEvent.click(screen.getByLabelText("Stop allowing /home/you/Octopus"));
    await waitFor(() =>
      expect(posts).toContainEqual({
        path: "/users/u1/extra-roots",
        body: { extra_roots: [] },
      })
    );
  });

  it("gives a non-admin no way to open a hole in their own confinement", async () => {
    stub({
      accountsExist: true,
      identity: { label: "vera", user_id: "u2", is_admin: false },
      workspace: { ...workspace, extra_roots: [] },
    });
    render(<AccountPage />);
    await waitFor(() => expect(screen.getByText("Where you can work")).toBeTruthy());
    expect(screen.queryByLabelText("Add a directory")).toBeNull();
    expect(screen.getByText(/An admin can open another directory/)).toBeTruthy();
  });
});
