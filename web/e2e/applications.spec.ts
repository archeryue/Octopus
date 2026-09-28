/**
 * End-to-end coverage of applications (docs/plans/applications.md).
 *
 * Two buckets:
 *
 *  - "Applications UI" — pure UI, no model. The sidebar section, the create
 *    pane, its validation, and returning to chat.
 *  - "Applications @llm" — the whole point of the feature, against the real
 *    `claude` CLI: describe an app → an agent builds it in its own directory →
 *    the built page renders in the main pane's iframe → ask for a change from
 *    that same pane → the change shows up → delete it.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const TOKEN = "changeme";
const SERVER_URL = "http://localhost:8765";
const API = `${SERVER_URL}/api`;

const OWNED_APPS = new Set(["E2E Hello App"]);

type AppRow = {
  id: string;
  name: string;
  status: string;
  session_id: string | null;
  app_dir: string;
};

const headers = { Authorization: `Bearer ${TOKEN}` };
const jsonHeaders = { ...headers, "Content-Type": "application/json" };

test.afterAll(async ({ request }) => {
  try {
    // Live and archived: the UI archives rather than deletes, so a finished
    // run leaves its app in the archive.
    const lists = await Promise.all(
      [{}, { archived: "true" }].map((params) =>
        request.get(`${API}/applications`, { headers, params, timeout: 5_000 })
      )
    );
    for (const res of lists) {
      if (!res.ok()) continue;
      for (const app of (await res.json()) as AppRow[]) {
        if (!OWNED_APPS.has(app.name)) continue;
        await request
          .delete(`${API}/applications/${app.id}`, { headers, timeout: 5_000 })
          .catch(() => {});
      }
    }
    // Build sessions outlive their application by design — clean ours up.
    const sRes = await request.get(`${API}/sessions`, { headers, timeout: 5_000 });
    if (sRes.ok()) {
      const sessions: { id: string; name: string }[] = await sRes.json();
      for (const s of sessions) {
        if (!/^Build: E2E /.test(s.name)) continue;
        await request
          .delete(`${API}/sessions/${s.id}`, { headers, timeout: 3_000 })
          .catch(() => {});
      }
    }
  } catch {
    // best-effort
  }
});

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

async function getApp(request: APIRequestContext, name: string): Promise<AppRow> {
  const res = await request.get(`${API}/applications`, { headers });
  expect(res.ok()).toBeTruthy();
  const app = ((await res.json()) as AppRow[]).find((a) => a.name === name);
  expect(app, `application ${name} should exist`).toBeTruthy();
  return app!;
}

// ---------------------------------------------------------------------------
// Pure UI
// ---------------------------------------------------------------------------

test.describe("Applications UI", () => {
  test("the sidebar section opens the create pane and validates it", async ({
    page,
  }) => {
    await login(page);

    const section = page.locator(".application-section");
    await expect(section).toBeVisible();
    await expect(section.locator("h2")).toHaveText("Applications");

    await page.locator(".btn-application-add").click();
    const pane = page.locator(".application-create");
    await expect(pane).toBeVisible();
    // Chat is hidden, not unmounted — it keeps its composer draft/scroll.
    await expect(page.locator(".main-pane")).toHaveClass(/hidden/);

    // "Start Build" stays disabled until there's both a name and a brief.
    const submit = page.locator("button.btn-application-create");
    await expect(submit).toBeDisabled();
    await page.locator("#app-name").fill("Draft Only");
    await expect(submit).toBeDisabled();
    await page.locator("#app-description").fill("Something");
    await expect(submit).toBeEnabled();

    // The agent picker is populated from the agent list.
    await expect(page.locator("#app-agent option")).not.toHaveCount(0);

    // The All tab lists every application — archived ones included — and is
    // where one comes back from (sidebar-pins.md §6).
    await page.locator(".btn-tab-all").click();
    await expect(page.locator(".library-empty, .item-library")).toBeVisible();
    await page.locator(".btn-tab-create").click();

    // Closing returns the pane to chat without creating anything.
    await page.locator(".btn-application-create-close").click();
    await expect(pane).toHaveCount(0);
    await expect(page.locator(".main-pane")).not.toHaveClass(/hidden/);
    await expect(
      page.locator(".application-item", { hasText: "Draft Only" })
    ).toHaveCount(0);
  });
});

// ---------------------------------------------------------------------------
// The real loop
// ---------------------------------------------------------------------------

test.describe("Applications @llm", () => {
  test.describe.configure({ timeout: 420_000 });

  test("an agent builds an app, it renders in the pane, and changes land", async ({
    page,
    request,
  }) => {
    await login(page);

    // --- create -----------------------------------------------------------
    await page.locator(".btn-application-add").click();
    await page.locator("#app-name").fill("E2E Hello App");
    await page.locator("#app-icon").fill("👋");
    await page
      .locator("#app-description")
      .fill(
        "A single static page whose only visible content is one <h1> element " +
          "containing the exact text HELLO OCTOPUS. No other text, no images, " +
          "no JavaScript. Keep it to a single index.html file."
      );
    await page.locator("button.btn-application-create").click();

    // The pane switches to the application immediately, showing the build.
    const view = page.locator(".application-view");
    await expect(view).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".application-title")).toHaveText("E2E Hello App");
    await expect(view.locator(".app-status-building").first()).toBeVisible();

    // The sidebar has it too, with a live status dot.
    const row = page.locator(".application-item", { hasText: "E2E Hello App" });
    await expect(row).toBeVisible();

    // --- the agent builds it ----------------------------------------------
    // The frame only mounts once the entrypoint exists on disk and the build
    // turn has ended (applications.md §4).
    const frame = page.locator("iframe.application-frame");
    await expect(frame).toBeVisible({ timeout: 300_000 });
    await expect(view.locator(".app-status-ready").first()).toBeVisible();
    // The sidebar row shows a dot only while something is unfinished — a
    // ready app is just a row, per the design.
    await expect(row.locator(".app-status-building")).toHaveCount(0);
    await expect(row.locator(".app-status-failed")).toHaveCount(0);

    await expect(
      page.frameLocator("iframe.application-frame").locator("h1")
    ).toContainText("HELLO OCTOPUS", { timeout: 30_000 });

    // The row on disk points at a real directory under the applications root.
    const app = await getApp(request, "E2E Hello App");
    expect(app.status).toBe("ready");
    expect(app.session_id).toBeTruthy();
    expect(app.app_dir).toContain("octopus-e2e-applications");

    // --- the app is served, and only to an authenticated caller -----------
    const served = await request.get(`${SERVER_URL}/apps/${app.id}/`, { headers });
    expect(served.ok()).toBeTruthy();
    expect(await served.text()).toContain("HELLO OCTOPUS");
    expect(served.headers()["cache-control"]).toBe("no-store");

    const anonymous = await request.get(`${SERVER_URL}/apps/${app.id}/`, {
      headers: {},
    });
    expect(anonymous.status()).toBe(401);

    // --- the app can talk to an agent -------------------------------------
    // The whole point of the agent API (app-agent-access.md): a running app
    // asks one of the user's own agents a question and gets prose back. Driven
    // through the real server here, exactly as the page's `fetch` would.
    const asked = await request.post(
      `${SERVER_URL}/apps/${app.id}/agent/ask`,
      {
        headers,
        data: {
          message:
            "What is the codeword in the document? Reply with the codeword " +
            "alone and nothing else.",
          context: "The agreed codeword is MARMALADE9981.",
        },
        timeout: 180_000,
      }
    );
    expect(asked.ok()).toBeTruthy();
    const answer = await asked.json();
    expect(answer.reply).toContain("MARMALADE9981");

    // It happened in a conversation owned by the app — and that conversation
    // stays out of the sidebar's agent rails.
    const threads = await (
      await request.get(`${SERVER_URL}/apps/${app.id}/agent/conversations`, {
        headers,
      })
    ).json();
    expect(threads.map((t: { id: string }) => t.id)).toContain(
      answer.conversation_id
    );
    // Unfold the owning agent so the assertion is about a rail that's
    // actually rendering sessions: the build session shows, the app's own
    // conversation doesn't.
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await expect(
      page.locator('.session-item:has-text("Build: E2E Hello App")')
    ).toBeVisible();
    await expect(
      page.locator(`.session-item:has-text("${threads[0].title}")`)
    ).toHaveCount(0);

    // --- ask for a change from the app pane -------------------------------
    // No composer bar under the page any more: it's the Iterate popover, which
    // also carries the link into the build session.
    await expect(page.locator(".application-compose")).toHaveCount(0);
    await page.locator(".btn-application-iterate").click();
    await page
      .locator(".application-request-input")
      .fill("Change the h1 text to exactly GOODBYE OCTOPUS. Change nothing else.");
    await page.locator("button.btn-application-request").click();

    // Back to building, then ready again — and the frame reloads itself.
    await expect(view.locator(".app-status-building").first()).toBeVisible({
      timeout: 30_000,
    });
    await expect(view.locator(".app-status-ready").first()).toBeVisible({
      timeout: 300_000,
    });
    await expect(
      page.frameLocator("iframe.application-frame").locator("h1")
    ).toContainText("GOODBYE OCTOPUS", { timeout: 30_000 });

    // The change ran in the SAME build session — no second session spawned.
    const after = await getApp(request, "E2E Hello App");
    expect(after.session_id).toBe(app.session_id);

    // --- the build session is a normal session ----------------------------
    await page.locator(".btn-application-iterate").click();
    await page.locator(".btn-application-open-session").click();
    await expect(page.locator(".application-view")).toHaveCount(0);
    await expect(page.locator(".chat-header")).toContainText("Build: E2E Hello App");

    // --- pins (sidebar-pins.md) --------------------------------------------
    // A new application is pinned. Unpinning it from the sidebar removes the
    // shortcut, not the app: it keeps serving, and the All tab still lists
    // it. (It would stay in the sidebar while it's the one open, so step off
    // it first.)
    await page.locator(".btn-application-add").click();
    await page.locator(".btn-tab-all").click();
    await row.hover();
    await row.locator(".btn-application-unpin").click();
    await expect(row).toHaveCount(0);
    expect(
      (await request.get(`${SERVER_URL}/apps/${app.id}/`, { headers })).status()
    ).toBe(200);
    const loose = page.locator(".library-unpinned .library-row", {
      hasText: "E2E Hello App",
    });
    await expect(loose).toBeVisible();
    await loose.locator(".btn-library-pin").click();
    await expect(row).toBeVisible();

    // --- archive ----------------------------------------------------------
    // The UI never deletes an application (sidebar-pins.md §6): archiving
    // from the All tab moves it to the Archived section with its files kept,
    // and Restore would bring it back exactly as it was.
    const libraryRow = page.locator(`.library-row[data-id="${app.id}"]`);
    page.once("dialog", (d) => d.accept());
    await libraryRow.locator(".btn-library-archive").click();
    await expect(page.locator(".library-archived", { hasText: "E2E Hello App" })).toBeVisible();
    await expect(row).toHaveCount(0);
    await expect(libraryRow.locator(".btn-library-delete")).toHaveCount(0);
    const archivedRow = await request.get(`${API}/applications/${app.id}`, { headers });
    expect((await archivedRow.json()).archived).toBe(true);
    // The build conversation is untouched either way.
    const session = await request.get(`${API}/sessions/${app.session_id}`, {
      headers: jsonHeaders,
    });
    expect(session.ok()).toBeTruthy();
  });
});
