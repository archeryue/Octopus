import { test, expect, type Page, type APIRequestContext } from "@playwright/test";

const TOKEN = "changeme";
const SERVER_URL = "http://localhost:8765";
const API = `${SERVER_URL}/api`;

const OWNED_NAMES = new Set([
  "Schedule UI Test",
  "Waiting Hint Yes",
  "Waiting Hint No",
  "Virtuoso Long Session",
]);

test.afterAll(async ({ request }) => {
  try {
    const res = await request.get(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      timeout: 5_000,
    });
    if (res.ok()) {
      const sessions: { id: string; name: string }[] = await res.json();
      for (const s of sessions) {
        if (!OWNED_NAMES.has(s.name)) continue;
        await request
          .delete(`${API}/sessions/${s.id}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
            timeout: 3_000,
          })
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
  await expect(page.locator(".session-list-header")).toBeVisible();
}

async function createSessionApi(
  request: APIRequestContext,
  name: string
): Promise<{ id: string }> {
  const res = await request.post(`${API}/sessions`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: { name, working_dir: "/tmp" },
  });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

async function importSessionApi(
  request: APIRequestContext,
  name: string,
  messages: { role: string; type: string; content: string }[]
): Promise<{ id: string }> {
  const res = await request.post(`${API}/sessions/import`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: { name, working_dir: "/tmp", messages },
  });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

// ---------------------------------------------------------------------------
// Scheduled Tasks UI
// ---------------------------------------------------------------------------

test.describe("Scheduled Tasks UI", () => {
  test("schedule section is hidden when no session is active", async ({ page }) => {
    await login(page);
    // ScheduleList component returns null when no activeSessionId
    await expect(page.locator(".schedule-section")).toHaveCount(0);
  });

  test("create, toggle, and delete a schedule via the sidebar", async ({
    page,
    request,
  }) => {
    await createSessionApi(request, "Schedule UI Test");

    await login(page);

    // Activate the session
    await page
      .locator(".session-item .session-name", { hasText: "Schedule UI Test" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Schedule UI Test");

    // Schedule section should now be visible
    await expect(page.locator(".schedule-section")).toBeVisible();
    await expect(page.locator(".schedule-title")).toHaveText("Schedules");

    // No schedules yet
    await expect(page.locator(".schedule-item")).toHaveCount(0);

    // Open create form
    await page.locator(".btn-schedule-add").click();
    await expect(page.locator(".schedule-form")).toBeVisible();

    // Fill (60 min = 3600s, well above the run window so it won't fire)
    await page
      .locator('.schedule-form input[placeholder="Name"]')
      .fill("Hourly Check");
    await page
      .locator('.schedule-form textarea[placeholder="Prompt..."]')
      .fill("Summarize the day");
    await page.locator(".schedule-form .interval-input").fill("60");

    // Submit (scope to schedule-form to avoid SessionList's btn-create)
    await page.locator(".schedule-form button.btn-create").click();

    // Schedule item appears
    await expect(page.locator(".schedule-item")).toHaveCount(1);
    await expect(page.locator(".schedule-item .schedule-name")).toHaveText(
      "Hourly Check"
    );
    await expect(page.locator(".schedule-item .schedule-interval")).toHaveText(
      "every 60m"
    );
    await expect(page.locator(".schedule-item .btn-toggle")).toHaveClass(/on/);

    // Toggle disabled
    await page.locator(".schedule-item .btn-toggle").click();
    await expect(page.locator(".schedule-item .btn-toggle")).toHaveClass(/off/);
    await expect(page.locator(".schedule-item")).toHaveClass(/disabled/);

    // Delete
    await page.locator(".schedule-item .btn-delete").click();
    await expect(page.locator(".schedule-item")).toHaveCount(0);
  });
});

// ---------------------------------------------------------------------------
// Interactive Input Hint
// ---------------------------------------------------------------------------

test.describe("Interactive Input Hint", () => {
  test("shows waiting-hint when last assistant message ends with '?'", async ({
    page,
    request,
  }) => {
    await importSessionApi(request, "Waiting Hint Yes", [
      { role: "user", type: "text", content: "Set me up" },
      {
        role: "assistant",
        type: "text",
        content: "Which database should we use?",
      },
    ]);

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Waiting Hint Yes" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Waiting Hint Yes");

    await expect(page.locator(".waiting-hint")).toBeVisible();
    await expect(page.locator(".waiting-hint")).toContainText(
      "waiting for your response"
    );
  });

  test("hides waiting-hint when last assistant message is a statement", async ({
    page,
    request,
  }) => {
    await importSessionApi(request, "Waiting Hint No", [
      { role: "user", type: "text", content: "Hi" },
      {
        role: "assistant",
        type: "text",
        content: "Hello! All systems are nominal.",
      },
    ]);

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Waiting Hint No" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Waiting Hint No");

    // Confirm the assistant message is rendered, then assert the hint is absent
    await expect(page.locator(".msg-assistant .msg-content")).toContainText(
      "All systems are nominal"
    );
    await expect(page.locator(".waiting-hint")).toHaveCount(0);
  });
});

// ---------------------------------------------------------------------------
// Virtualized chat (react-virtuoso)
// ---------------------------------------------------------------------------

test.describe("Virtualized Chat", () => {
  test("renders long conversation through the Virtuoso scroller and pins to bottom", async ({
    page,
    request,
  }) => {
    const PAIRS = 60; // 120 messages total
    const messages: { role: string; type: string; content: string }[] = [];
    for (let i = 0; i < PAIRS; i++) {
      messages.push({ role: "user", type: "text", content: `user-${i}` });
      messages.push({
        role: "assistant",
        type: "text",
        content: `assistant-${i}`,
      });
    }

    await importSessionApi(request, "Virtuoso Long Session", messages);

    await login(page);
    await page
      .locator(".session-item .session-name", {
        hasText: "Virtuoso Long Session",
      })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText(
      "Virtuoso Long Session"
    );

    // Virtuoso decorates its scroller container with this attribute
    await expect(page.locator("[data-virtuoso-scroller]")).toBeVisible();

    // The bottom item should be rendered and visible (followOutput on mount)
    await expect(
      page.locator(".msg-assistant .msg-content").last()
    ).toContainText(`assistant-${PAIRS - 1}`);

    // Virtualization: rendered .msg count should be far less than 2*PAIRS.
    // (Virtuoso only mounts visible items + a viewport buffer.)
    const rendered = await page.locator(".chat-messages .msg").count();
    expect(rendered).toBeGreaterThan(0);
    expect(rendered).toBeLessThan(2 * PAIRS);
  });
});
