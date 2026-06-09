import { test, expect, type Page, type APIRequestContext } from "@playwright/test";

// Pure-UI e2e for session tree-rewind / fork (session-tree-rewind.md §6).
// No real LLM turn: we import a parent session, drive the per-message "Fork
// from here" → confirm dialog → create flow, and assert the fork opens with
// its banner + prefilled input and nests under the parent in the sidebar.

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
        // Delete the parent and any forks of it (named "… (fork @N)").
        if (!OWNED.has(s.name) && !s.name.includes("(fork @")) continue;
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

  // Hover the SECOND user message and click its "Fork from here" affordance.
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

  // The sidebar nests the fork under its parent with an "@msg" badge.
  await expect(page.locator(".fork-badge").first()).toBeVisible();
});

test("/fork picker lists user messages and creates a fork", async ({
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

  // Type the /fork slash command and submit it to open the picker. The first
  // Enter is captured by the slash-autocomplete menu (it selects the command
  // into the composer as "/fork "); the second Enter actually sends it.
  const composer = page.locator(".chat-input-bar textarea");
  await composer.fill("/fork");
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
