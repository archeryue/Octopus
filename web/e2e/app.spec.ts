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
  "Mobile Drawer",
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
    await expect(page.locator(".chat-header .crumb-current")).toHaveText(
      "E2E Test Session"
    );
  });

  test("shows empty chat view when no session selected", async ({ page }) => {
    // The empty canvas is the mark plus one line of guidance — no wordmark.
    await expect(page.locator(".chat-empty svg")).toBeVisible();
    await expect(page.locator(".chat-empty")).toContainText(
      "Pick a session on the left"
    );
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

test.describe("Chat @llm", () => {
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
    // Working dir is an override now — unfold the create row's extras first.
    await page.locator(".btn-session-advanced").click();
    await page.locator(".session-working-dir").fill("/tmp");
    await page.locator("button.btn-create").click();
    await expect(page.locator(".chat-header .crumb-current")).toHaveText("Chat Test");
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

    // …and entirely off-screen when closed. It used to sit at -260px while
    // being 270px wide, leaving a 10px sliver of sidebar on every screen.
    await page.locator(".sidebar-overlay").click();
    await expect(page.locator(".sidebar")).not.toHaveClass(/open/);
    // Polled, because the class comes off when the slide-out *starts*.
    await expect
      .poll(async () => {
        const box = await page.locator(".sidebar").boundingBox();
        return box ? box.x + box.width : -1;
      })
      .toBeLessThanOrEqual(0.5);
  });

  test("the drawer puts itself away when you pick a session", async ({
    page,
    request,
  }) => {
    // The worst phone bug this suite guards: picking something left the
    // drawer open, so every navigation ended with the thing you'd just
    // chosen hidden behind the menu you chose it from (mobile.md §2).
    const created = await request.post(API, {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      data: { name: "Mobile Drawer", working_dir: "/tmp" },
    });
    expect(created.ok()).toBeTruthy();

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();

    await page.locator(".btn-menu").first().click();
    await expect(page.locator(".sidebar")).toHaveClass(/open/);

    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page
      .locator(".session-item", { hasText: "Mobile Drawer" })
      .first()
      .click();

    await expect(page.locator(".sidebar")).not.toHaveClass(/open/);
    await expect(page.locator(".sidebar-overlay")).toHaveCount(0);
    await expect(page.locator(".chat-header .crumb-current")).toContainText(
      "Mobile Drawer"
    );
  });

  test("nothing scrolls sideways, and no field is small enough to zoom iOS", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    // The login field is the first thing a phone shows.
    expect(
      await page
        .locator('input[type="password"]')
        .evaluate((el) => parseFloat(getComputedStyle(el).fontSize))
    ).toBeGreaterThanOrEqual(16);

    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".conn-status.on")).toBeVisible({
      timeout: 10_000,
    });

    // A phone that scrolls sideways feels broken even when nothing is
    // actually cut off.
    const doc = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(doc.scrollWidth).toBe(doc.clientWidth);

    // Under 16px, iOS zooms on focus and never zooms back.
    const composer = page.locator(".chat-input-bar textarea").first();
    if (await composer.count()) {
      expect(
        await composer.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))
      ).toBeGreaterThanOrEqual(16);
    }
  });
});

/** The sidebar folds down to an icon rail.
 *
 * The handle is hover-revealed, so the test drives the mouse rather than
 * clicking a locator blind: `pointer-events: none` while hidden means a click
 * that skipped the hover would never land, which is exactly the guarantee
 * worth pinning down (the button overhangs the main pane).
 */
test.describe("Foldable sidebar", () => {
  const EDGE_Y = 400;

  test("hover reveals the handle, and the rail keeps its icons", async ({
    page,
  }) => {
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".sidebar")).toHaveCSS("width", "270px");

    const handle = page.locator(".btn-sidebar-toggle");
    await expect(handle).toHaveCSS("opacity", "0");

    // The strip lives in the sidebar's last 14px — x=263 is on it.
    await page.mouse.move(263, EDGE_Y);
    await expect(handle).toHaveCSS("opacity", "1");
    await expect(handle).toHaveAttribute("aria-label", "Collapse sidebar");

    await handle.click();
    await expect(page.locator(".sidebar")).toHaveClass(/collapsed/);
    await expect(page.locator(".sidebar")).toHaveCSS("width", "56px");

    // Words are gone; the icons that replace them are not.
    await expect(page.locator(".brand-name")).toBeHidden();
    await expect(page.locator(".manage-label").first()).toBeHidden();
    await expect(page.locator(".agent-name").first()).toBeHidden();
    await expect(page.locator(".agent-avatar").first()).toBeVisible();
    await expect(page.locator(".btn-account")).toBeVisible();

    // The rail still navigates — an icon is a row, not a decoration.
    await page.locator(".btn-manage-schedules").click();
    await expect(page.locator(".schedules-page")).toBeVisible();

    // Folding is a preference, so it survives a reload.
    await page.reload();
    await expect(page.locator(".sidebar")).toHaveClass(/collapsed/);

    // And the handle, now at the rail's edge, puts it back.
    await page.mouse.move(49, EDGE_Y);
    const expand = page.locator(".btn-sidebar-toggle");
    await expect(expand).toHaveAttribute("aria-label", "Expand sidebar");
    await expand.click();
    await expect(page.locator(".sidebar")).not.toHaveClass(/collapsed/);
    await expect(page.locator(".brand-name")).toBeVisible();
  });

  test("clicking an agent on the rail opens the sidebar with it", async ({
    page,
  }) => {
    await page.goto("/");
    await page.locator('input[type="password"]').fill(TOKEN);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".agent-item").first()).toBeVisible();

    await page.mouse.move(263, EDGE_Y);
    await page.locator(".btn-sidebar-toggle").click();
    await expect(page.locator(".sidebar")).toHaveClass(/collapsed/);

    // From the rail there is no fold to toggle, so the click means "show me
    // this agent": the sidebar comes back with the agent already open.
    // (Matched by position, not by name — the name is display:none on the
    // rail, so a `hasText` locator would stop resolving mid-test.)
    const agent = page.locator(".agent-item").first();
    await agent.click();
    await expect(page.locator(".sidebar")).not.toHaveClass(/collapsed/);
    await expect(agent.locator(".agent-fold")).toHaveClass(/rotate-90/);
  });
});
