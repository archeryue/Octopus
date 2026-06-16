import { test, expect, type Page, type APIRequestContext } from "@playwright/test";

// Pure-UI e2e for session tree-rewind / fork (session-rewind.md §6).
// No real LLM turn: we import a parent session, drive the per-message "Fork
// from here" → confirm dialog → create flow, and assert the fork opens with
// its banner + prefilled input. A fork is a rewind, not a branch: it inherits
// the parent's name and the parent is archived, so the fork surfaces in the
// sidebar as a plain top-level session (no nesting, no "@msg" badge).

const TOKEN = "changeme";
const API = "http://localhost:8765/api";
const OWNED = new Set(["Fork E2E Parent"]);

test.afterAll(async ({ request }) => {
  try {
    const res = await request.get(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      params: { include_archived: "true" },
      timeout: 5_000,
    });
    if (res.ok()) {
      const sessions: { id: string; name: string }[] = await res.json();
      for (const s of sessions) {
        // The parent and its forks all carry the same name now (a fork inherits
        // the parent's name), so one membership check covers both.
        if (!OWNED.has(s.name)) continue;
        await request
          .delete(`${API}/sessions/${s.id}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
            timeout: 3_000,
          })
          .catch(() => {});
      }
    }
  } catch {
    /* best-effort */
  }
});

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

async function importParent(request: APIRequestContext): Promise<{ id: string }> {
  const res = await request.post(`${API}/sessions/import`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: {
      name: "Fork E2E Parent",
      working_dir: "/tmp",
      messages: [
        { role: "user", type: "text", content: "first question about auth" },
        { role: "assistant", type: "text", content: "here is the auth answer" },
        { role: "user", type: "text", content: "second question about tests" },
        { role: "assistant", type: "text", content: "here is the test answer" },
      ],
    },
  });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

test("fork from a user message opens a prefilled, banner-marked branch", async ({
  page,
  request,
}) => {
  await importParent(request);
  await login(page);

  await page
    .locator(".session-item .session-name", { hasText: "Fork E2E Parent" })
    .click();
  await expect(page.locator(".chat-header h3")).toHaveText("Fork E2E Parent");

  // Hover the SECOND user message and click its "Rewind to here" affordance.
  const secondUserMsg = page.locator(".msg-user").nth(1);
  await expect(secondUserMsg).toContainText("second question about tests");
  await secondUserMsg.hover();
  await secondUserMsg.locator('[data-testid="fork-from-here"]').click();

  // The confirm dialog appears (the side-effect summary may be empty here).
  await expect(page.locator('[data-testid="fork-dialog"]')).toBeVisible();
  await expect(page.locator('[data-testid="fork-confirm"]')).toBeVisible();

  await page.locator(".btn-create-fork").click();

  // The fork opens: banner names the parent + branch point, and the composer
  // is prefilled with the rewound user message's text.
  await expect(page.locator('[data-testid="fork-banner"]')).toBeVisible();
  await expect(page.locator('[data-testid="fork-banner"]')).toContainText(
    "Fork E2E Parent"
  );
  await expect(page.locator(".chat-input-bar textarea")).toHaveValue(
    "second question about tests"
  );

  // Rewind, not branch: the parent is archived and the fork takes its place as
  // a plain top-level session — it keeps the parent's name and shows no "@msg"
  // fork badge anywhere in the active list.
  await expect(
    page
      .locator(".session-item .session-name", { hasText: "Fork E2E Parent" })
      .first()
  ).toBeVisible();
  await expect(page.locator(".fork-badge")).toHaveCount(0);
});

test("/rewind picker lists user messages and creates a branch", async ({
  page,
  request,
}) => {
  await importParent(request);
  await login(page);

  await page
    .locator(".session-item .session-name", { hasText: "Fork E2E Parent" })
    .first()
    .click();
  await expect(page.locator(".chat-header h3")).toHaveText("Fork E2E Parent");

  // Type the /rewind slash command and submit it to open the picker. The first
  // Enter is captured by the slash-autocomplete menu (it selects the command
  // into the composer as "/rewind "); the second Enter actually sends it.
  const composer = page.locator(".chat-input-bar textarea");
  await composer.fill("/rewind");
  await composer.press("Enter");
  await composer.press("Enter");

  await expect(page.locator('[data-testid="fork-picker"]')).toBeVisible();
  // Picker rows are user messages only — pick the first one.
  const firstRow = page.locator("[data-fork-seq]").first();
  await expect(firstRow).toContainText("first question about auth");
  await firstRow.click();

  await expect(page.locator('[data-testid="fork-confirm"]')).toBeVisible();
  await page.locator(".btn-create-fork").click();

  await expect(page.locator('[data-testid="fork-banner"]')).toBeVisible();
  await expect(page.locator(".chat-input-bar textarea")).toHaveValue(
    "first question about auth"
  );
});
