import { test, expect, type Page } from "@playwright/test";

const TOKEN = "changeme";
const API = "http://localhost:8765/api/sessions";

/** Click the new-session "+" on the default "Octo" agent's row. The button is
 * per-agent now, and specs share one in-memory backend DB, so a bare
 * ".btn-session-add" turns ambiguous (strict-mode violation) the moment a
 * concurrent spec has created another agent. Scoping to Octo is unambiguous. */
const addOctoSession = (page: Page) =>
  page
    .locator(".agent-item", { hasText: "Octo" })
    .locator(".btn-session-add")
    .click();

// Names of sessions this spec creates — only delete these to avoid
// disturbing sessions in-flight on parallel worker processes.
const OWNED_NAMES = new Set([
  "E2E Test Session",
  "To Delete",
  "Chat Test",
]);

// Clean up only sessions created by this spec
test.afterAll(async ({ request }) => {
  const res = await request.get(API, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  if (res.ok()) {
    const sessions: { id: string; name: string }[] = await res.json();
    for (const s of sessions) {
      if (OWNED_NAMES.has(s.name)) {
        await request
          .delete(`${API}/${s.id}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
          })
          .catch(() => {});
      }
    }
  }
});

test.describe("Login", () => {
  test("shows login screen when no token", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("h1")).toHaveText("Octopus");
    await expect(page.locator('input[type="password"]')).toBeVisible();
  });

  test("rejects empty token", async ({ page }) => {
    await page.goto("/");
    const btn = page.locator("button.btn-login");
    await btn.click();
    // Should still be on login screen
    await expect(page.locator("h1")).toHaveText("Octopus");
  });

  test("logs in with valid token", async ({ page }) => {
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();
    // Should see the main app layout
    await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
  });
});

test.describe("Session Management", () => {
  test.beforeEach(async ({ page }) => {
    // Login first
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".agent-list-header")).toBeVisible();
  });

  test("creates a new session", async ({ page }) => {
    await addOctoSession(page);
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("E2E Test Session");
    await page.locator("button.btn-create").click();

    // Session should appear in the list (use last to avoid stale sessions)
    await expect(
      page.locator(".session-item .session-name").last()
    ).toHaveText("E2E Test Session");
    // Should be selected (active)
    await expect(page.locator(".session-item.active")).toBeVisible();
    // Chat header should show session name
    await expect(page.locator(".chat-header h3")).toHaveText(
      "E2E Test Session"
    );
  });

  test("shows empty chat view when no session selected", async ({ page }) => {
    await expect(page.locator(".chat-empty h2")).toHaveText("Octopus");
  });

  test("deletes a session", async ({ page }) => {
    // Create a session first
    await addOctoSession(page);
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("To Delete");
    await page.locator("button.btn-create").click();

    // Locate by name (not .last()) — other tests in the same server run
    // may have left sessions in the in-memory DB, so positional matching
    // is unreliable.
    const target = page.locator(".session-item", { hasText: "To Delete" });
    await expect(target).toBeVisible();

    // Click to make sure it's active (the create flow already auto-selects
    // it, but be explicit so we don't depend on side effects of creation).
    await target.click();
    await target.hover();
    await target.locator(".btn-delete").click();

    // The "To Delete" entry should vanish from the list
    await expect(target).toHaveCount(0);
  });
});

test.describe("Chat", () => {
  // Claude SDK initialization can take >60s; the global 30s timeout is too short.
  test.describe.configure({ timeout: 120_000 });

  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".agent-list-header")).toBeVisible();

    // Create a session
    await addOctoSession(page);
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("Chat Test");
    await page
      .locator('.session-create input[placeholder*="Working directory"]')
      .fill("/tmp");
    await page.locator("button.btn-create").click();
    await expect(page.locator(".chat-header h3")).toHaveText("Chat Test");
  });

  test("shows connection status", async ({ page }) => {
    await expect(page.locator(".conn-status")).toBeVisible();
  });

  test("sends a message and receives response", async ({ page }) => {
    // Type and send a simple message
    const input = page.locator(".chat-input-bar textarea");
    await input.fill("What is 2+2? Reply with just the number.");
    await page.locator("button.btn-send").click();

    // User message should appear
    await expect(page.locator(".msg-user .msg-content")).toContainText(
      "What is 2+2?"
    );

    // Wait for assistant response (up to 30s)
    await expect(page.locator(".msg-assistant .msg-content")).toBeVisible({
      timeout: 30_000,
    });

    // Should have some content
    const assistantText = await page
      .locator(".msg-assistant .msg-content")
      .first()
      .textContent();
    expect(assistantText).toBeTruthy();

    // Result badge should appear
    await expect(page.locator(".result-badge")).toBeVisible({ timeout: 30_000 });
  });

  test("send with Enter key", async ({ page }) => {
    const input = page.locator(".chat-input-bar textarea");
    await input.fill("Say hello");
    await input.press("Enter");

    // User message should appear
    await expect(page.locator(".msg-user .msg-content")).toContainText(
      "Say hello"
    );

    // Wait for response to complete so it doesn't interfere with the next test
    await expect(page.locator(".result-badge")).toBeVisible({ timeout: 30_000 });
  });

  test("disables input while running", async ({ page }) => {
    const input = page.locator(".chat-input-bar textarea");
    await input.fill("What is 1+1? Reply with just the number.");
    await page.locator("button.btn-send").click();

    // Input should be disabled while Claude is processing
    // (this may be brief, so we check right away)
    // Wait for response to complete
    await expect(page.locator(".result-badge")).toBeVisible({ timeout: 30_000 });

    // After completion, input should be enabled again
    await expect(input).toBeEnabled();
  });
});

test.describe("WebSocket Connection", () => {
  test("shows connected status after login", async ({ page }) => {
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();

    await expect(page.locator(".conn-status.on")).toBeVisible({
      timeout: 5_000,
    });
    await expect(page.locator(".conn-status")).toContainText("Connected");
  });
});

test.describe("Responsive Layout", () => {
  test("shows hamburger menu on mobile", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();

    // Hamburger should be visible on mobile
    await expect(page.locator(".btn-menu")).toBeVisible();

    // Sidebar should be hidden
    await expect(page.locator(".sidebar")).not.toHaveClass(/open/);

    // Click hamburger to open sidebar
    await page.locator(".btn-menu").click();
    await expect(page.locator(".sidebar")).toHaveClass(/open/);

    // Overlay should be visible
    await expect(page.locator(".sidebar-overlay")).toBeVisible();
  });
});
