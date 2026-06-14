import { test, expect, type Page, type APIRequestContext } from "@playwright/test";

// Pure-UI-ish e2e for native deep research (native-deep-research.md §7). We do
// NOT need a working LLM: the `/research` command creates a job row + a
// `research_started` broadcast regardless of model auth, so the ResearchCard
// appears deterministically. This exercises the full command → REST →
// ResearchManager → WS → store → card path. (The pipeline itself degrades fast
// when the backend CLI is logged out; we only assert the card lifecycle, not a
// real report — that's covered by the gated tests/test_research_real.py.)

const TOKEN = "changeme";
const API = "http://localhost:8765/api";
const OWNED = new Set(["Research E2E"]);

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

async function importSession(request: APIRequestContext): Promise<{ id: string }> {
  const res = await request.post(`${API}/sessions/import`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: {
      name: "Research E2E",
      working_dir: "/tmp",
      messages: [{ role: "user", type: "text", content: "hello" }],
    },
  });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

test("the /research command surfaces a live research card", async ({ page, request }) => {
  await importSession(request);
  await login(page);

  await page
    .locator(".session-item .session-name", { hasText: "Research E2E" })
    .click();
  await expect(page.locator(".chat-header h3")).toHaveText("Research E2E");

  // Fire the slash command. A space after the command hides the slash menu, so
  // Enter sends rather than completing a menu item.
  const composer = page.locator(".chat-input-bar textarea");
  await composer.fill("/research what is the capital of France");
  await composer.press("Enter");

  // The ResearchCard appears with the question (driven by the optimistic
  // upsert on the 201 + the research_started WS event) in the running state
  // with a cancel affordance.
  const card = page.locator(".research-card").first();
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card).toContainText("what is the capital of France");
  await expect(card).toHaveAttribute("data-status", "running");

  // Cancel is the fast, deterministic terminal transition (no waiting on the
  // whole pipeline): cancelling the job flips it to "cancelled" over the WS.
  await card.locator(".btn-research-cancel").click();
  await expect(card).toHaveAttribute("data-status", "cancelled", {
    timeout: 20_000,
  });
});
