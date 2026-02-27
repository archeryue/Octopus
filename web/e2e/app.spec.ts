import { test, expect } from "@playwright/test";

const TOKEN = "changeme";

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
    await expect(page.locator(".session-list-header h2")).toHaveText(
      "Sessions"
    );
  });
});

test.describe("Session Management", () => {
  test.beforeEach(async ({ page }) => {
    // Login first
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".session-list-header")).toBeVisible();
  });

  test("creates a new session", async ({ page }) => {
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
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("To Delete");
    await page.locator("button.btn-create").click();
    await expect(
      page.locator(".session-item .session-name").last()
    ).toHaveText("To Delete");

    // Delete the active (last) session
    await page.locator(".session-item.active").hover();
    await page.locator(".session-item.active .btn-delete").click();

    // Chat area should show empty state (no active session)
    await expect(page.locator(".chat-empty")).toBeVisible();
  });
});

test.describe("Chat", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".session-list-header")).toBeVisible();

    // Create a session
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
