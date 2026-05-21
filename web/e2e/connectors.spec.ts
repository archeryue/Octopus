import { test, expect, type Page } from "@playwright/test";

// Connectors UI against the real backend. No real third-party OAuth is needed:
// configuring an OAuth client (or creating a custom connector) just flips a
// connector to "available" — which is the browser-only setup flow we verify.

const TOKEN = "changeme";
const API = "http://localhost:8765/api";

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
}

// Keep the shared in-memory backend clean between tests (configuring a built-in
// or adding a custom connector persists for the whole server run).
test.afterEach(async ({ request }) => {
  const headers = { Authorization: `Bearer ${TOKEN}` };
  for (const kind of ["github", "gmail"]) {
    await request
      .delete(`${API}/connectors/${kind}/oauth-client`, { headers })
      .catch(() => {});
  }
  const cat = await request.get(`${API}/connectors/catalog`, { headers });
  if (cat.ok()) {
    for (const c of (await cat.json()) as { kind: string; custom: boolean }[]) {
      if (c.custom) {
        await request
          .delete(`${API}/connectors/custom/${c.kind}`, { headers })
          .catch(() => {});
      }
    }
  }
});

test.describe("Connectors", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("the sidebar has a Connectors section", async ({ page }) => {
    await expect(
      page.locator(".connector-header", { hasText: "Connectors" })
    ).toBeVisible();
  });

  test("the catalog lists GitHub + Gmail with a Set up action", async ({
    page,
  }) => {
    await page.locator(".btn-connector-add").click();
    await expect(page.locator(".connector-catalog")).toBeVisible();
    const github = page.locator(".connector-catalog-item", { hasText: "GitHub" });
    await expect(github).toBeVisible();
    await expect(
      page.locator(".connector-catalog-item", { hasText: "Gmail" })
    ).toBeVisible();
    // Unconfigured → a Set up button (not a Connect button).
    await expect(github.locator(".btn-connector-setup")).toBeVisible();
  });

  test("setting up a built-in connector in-browser makes it connectable", async ({
    page,
  }) => {
    await page.locator(".btn-connector-add").click();
    const github = page.locator(".connector-catalog-item", { hasText: "GitHub" });
    await github.locator(".btn-connector-setup").click();

    // Setup form: shows the redirect URI to register + client id/secret fields.
    await expect(page.locator("#setup-client-id")).toBeVisible();
    await expect(
      page.locator("code", { hasText: "/api/connectors/oauth/callback" })
    ).toBeVisible();
    await page.locator("#setup-client-id").fill("test-client-id");
    await page.locator("#setup-client-secret").fill("test-secret");
    await page.locator(".btn-connector-save-client").click();

    // Back at the catalog, GitHub is now available → Connect is shown.
    await expect(
      page
        .locator(".connector-catalog-item", { hasText: "GitHub" })
        .locator(".btn-connector-connect")
    ).toBeVisible();
  });

  test("adding and removing a custom connector in-browser", async ({ page }) => {
    await page.locator(".btn-connector-add").click();
    await page.locator(".btn-connector-add-custom").click();

    await page.locator("#cc-kind").fill("linear");
    await page.locator("#cc-name").fill("Linear");
    await page.locator("#cc-auth").fill("https://linear.app/oauth/authorize");
    await page.locator("#cc-token").fill("https://api.linear.app/oauth/token");
    await page.locator("#cc-api").fill("https://api.linear.app");
    await page.locator("#cc-cid").fill("cid");
    await page.locator("#cc-csec").fill("csec");
    await page.locator(".btn-connector-save-custom").click();

    // Appears in the catalog as a custom (available, connectable) connector.
    const linear = page.locator(".connector-catalog-item", { hasText: "Linear" });
    await expect(linear).toBeVisible();
    await expect(linear).toContainText("custom");
    await expect(linear.locator(".btn-connector-connect")).toBeVisible();

    // Remove it.
    await linear.locator(".btn-connector-remove").click();
    await expect(
      page.locator(".connector-catalog-item", { hasText: "Linear" })
    ).toHaveCount(0);
  });

  test("Agent settings shows the per-agent connectors section", async ({
    page,
  }) => {
    const octo = page.locator(".agent-item", { hasText: "Octo" });
    await octo.click();
    await expect(octo).toHaveClass(/active/);

    await page.locator(".btn-account").click();
    await page.locator(".menu-agent-settings").click();
    await expect(page.locator(".agent-settings")).toBeVisible();
    await expect(page.locator("#agent-name")).toHaveValue("Octo");
    await expect(page.locator(".agent-connectors")).toContainText(
      "No connectors installed"
    );
  });
});
