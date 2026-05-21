import { test, expect, type Page } from "@playwright/test";

// Connectors UI against the real backend. No OAuth client creds are set in the
// e2e env, so the registered kinds (GitHub, Gmail) come back as *unavailable* —
// which is exactly what lets us assert the catalog + availability gating
// end-to-end without driving a live third-party OAuth flow.

const TOKEN = "changeme";

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
}

test.describe("Connectors", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("the sidebar has a Connectors section", async ({ page }) => {
    await expect(
      page.locator(".connector-header", { hasText: "Connectors" })
    ).toBeVisible();
  });

  test("the catalog lists GitHub + Gmail, disabled when unconfigured", async ({
    page,
  }) => {
    await page.locator(".btn-connector-add").click();
    await expect(page.locator(".connector-catalog")).toBeVisible();

    const github = page.locator(".connector-catalog-item", { hasText: "GitHub" });
    const gmail = page.locator(".connector-catalog-item", { hasText: "Gmail" });
    await expect(github).toBeVisible();
    await expect(gmail).toBeVisible();

    // No OAuth client id/secret in env → Connect is disabled with a hint.
    await expect(github.locator(".btn-connector-connect")).toBeDisabled();
    await expect(github).toContainText("OAuth client");
  });

  test("Agent settings shows the per-agent connectors section", async ({
    page,
  }) => {
    // Select Octo first so its settings open in edit mode (avoids the
    // fresh-load race where no agent is active yet → "new agent" mode).
    const octo = page.locator(".agent-item", { hasText: "Octo" });
    await octo.click();
    await expect(octo).toHaveClass(/active/);

    await page.locator(".btn-account").click();
    await page.locator(".menu-agent-settings").click();
    await expect(page.locator(".agent-settings")).toBeVisible();
    await expect(page.locator("#agent-name")).toHaveValue("Octo");

    // With nothing installed, the section prompts to add one in the sidebar.
    await expect(page.locator(".agent-connectors")).toContainText(
      "No connectors installed"
    );
  });
});
