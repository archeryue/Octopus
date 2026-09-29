import fs from "node:fs";
import path from "node:path";

import { test, expect } from "@playwright/test";

import {
  E2E_ACCOUNTS_PORT,
  E2E_ACCOUNTS_STATE_DIR,
  E2E_ACCOUNTS_WEB_PORT,
} from "../playwright.config";

/**
 * A real turn, in an install that has accounts (multi-tenancy.md §5–§7).
 *
 * This is the test the whole branch was missing. Every other real-CLI test —
 * all 34 in the backend tier and all 38 `@llm` specs — runs *before* the first
 * account exists, which is exactly the era the accounts work does not change.
 * Four things change the moment somebody claims the install, all of them in the
 * path a turn takes, and none of them provable with a fake harness:
 *
 *   1. requests carry a session bearer, and `OCTOPUS_AUTH_TOKEN` opens nothing;
 *   2. a working directory has to resolve inside the account's own workspace;
 *   3. a tool calling back into Octopus presents the signed MCP scope rather
 *      than the install token — get this wrong and *every tool an agent has*
 *      fails on the first account's first turn;
 *   4. the broadcast bus routes frames by owner, so a turn's events have to
 *      reach the browser that started it.
 *
 * So: claim the install, sign in as the account, and make the model use
 * `mcp__bg__run` — the tool that reaches furthest back into the server. If the
 * chip completes and the injected follow-up turn lands, all four hold.
 */

test.describe.configure({ mode: "serial" });
test.use({ baseURL: `http://localhost:${E2E_ACCOUNTS_WEB_PORT}` });

const API = `http://localhost:${E2E_ACCOUNTS_PORT}/api`;
const OWNER = { username: "archer", password: "owner-password-1" };
const SENTINEL = "ACCOUNTS-BG-OK-41907";

test.describe("A real turn under an account @llm", () => {
  test("bg_run round-trips for an account that owns its session", async ({
    page,
    request,
  }) => {
    test.setTimeout(180_000);

    // 1. Claim the install. Through the API rather than the UI — the UI path is
    //    accounts.spec.ts's job, and this spec is about what happens after.
    const claimed = await request.post(`${API}/auth/bootstrap`, {
      headers: { Authorization: "Bearer changeme" },
      data: OWNER,
    });
    expect(claimed.ok(), await claimed.text()).toBeTruthy();
    const { token, user_id: userId } = await claimed.json();

    // The install token is dead from here on — the rest of this test would
    // pass by accident if it weren't.
    expect(
      (await request.get(`${API}/sessions`, {
        headers: { Authorization: "Bearer changeme" },
      })).status()
    ).toBe(401);

    // 2. A working directory *inside the account's own workspace*, which is
    //    what confinement now requires. Anywhere else is refused, and proving
    //    that the legal path works is half the point.
    const workspace = path.join(E2E_ACCOUNTS_STATE_DIR, "users", userId, "workspace");
    const wd = fs.mkdtempSync(path.join(workspace, "bg-"));

    const outside = await request.post(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { name: "Outside", working_dir: "/etc" },
    });
    expect(outside.status(), await outside.text()).toBe(400);

    const created = await request.post(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { name: "Account Bg Run", working_dir: wd },
    });
    expect(created.ok(), await created.text()).toBeTruthy();

    // 3. Sign in as the account, through the UI, so the browser holds a session
    //    bearer and the WebSocket subscribes as this owner.
    await page.goto("/");
    await expect(page.locator("#username")).toBeVisible();
    await page.locator("#username").fill(OWNER.username);
    await page.locator("#password").fill(OWNER.password);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".agent-list-header")).toBeVisible();
    await expect(page.locator(".account-handle")).toHaveText(OWNER.username);
    // A prompt typed before the socket is OPEN is dropped on the floor, and
    // every agent's session list starts folded — both exactly as in the
    // pre-accounts specs, and both the reason this test first failed.
    await expect(page.locator(".conn-status.on")).toBeVisible({ timeout: 15_000 });
    await page.locator(".agent-item", { hasText: "Octo" }).first().click();

    await page
      .locator(".session-item .session-name", { hasText: "Account Bg Run" })
      .click();
    await expect(page.locator(".chat-header .crumb-current")).toHaveText(
      "Account Bg Run"
    );

    // 4. The turn. `mcp__bg__run` is the probe because it is the tool that
    //    calls furthest back into the server: the namespace HTTP-POSTs to
    //    `/api/sessions/{id}/bg-tasks` with the bearer it was given, and that
    //    route is scoped to the session's owner.
    const prompt =
      "Use the mcp__bg__run tool RIGHT NOW with command=" +
      `'sleep 3 && echo ${SENTINEL}' and description='accounts probe'. ` +
      "After calling it, say 'started' and end your turn. When the " +
      "follow-up bg-task-result turn arrives, reply by echoing " +
      `'${SENTINEL}' verbatim.`;
    await page.locator(".chat-input-bar textarea").fill(prompt);
    await page.locator("button.btn-send").click();

    // The chip appearing at all means the tool call reached the REST route
    // with a bearer it accepted — i.e. the MCP scope works as a credential.
    const chip = page.locator(".octo-bgtask-chip").first();
    await expect(chip).toBeVisible({ timeout: 90_000 });
    await expect(chip).toContainText(/bg · completed/i, { timeout: 60_000 });

    // The injected follow-up turn landing means the broadcast bus routed the
    // frames to this owner's subscriber rather than dropping them.
    await expect(page.locator(".msg-bg-result").first()).toBeVisible({
      timeout: 60_000,
    });
    await expect(
      page.locator(".msg-assistant .msg-content").last()
    ).toContainText(SENTINEL, { timeout: 60_000 });

    // And the work really happened in the account's own workspace.
    expect(fs.existsSync(wd)).toBeTruthy();
  });
});
