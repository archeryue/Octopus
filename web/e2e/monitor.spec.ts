/**
 * The Monitor page (docs/plans/polish-2026-09.md §9).
 *
 * The case it exists for is in this repo's own history: the Gmail connector
 * was unavailable for eleven consecutive days, a daily schedule failed on each
 * of them, and nothing counted it. So what matters is that the collected data
 * reaches a human — an empty section must say "nothing recorded" rather than
 * render blank, because a blank panel reads as a clean bill of health.
 *
 * It also carries the only continuous check that §4 B1 stayed done:
 * `mcp_sidecar_count` is sampled from this process's own descendants, and it
 * must be 0 now that the MCP namespaces are served in-process rather than
 * spawned seven-per-session.
 */

import { expect, test, type Page } from "@playwright/test";

const TOKEN = "changeme";

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
}

async function openMonitor(page: Page) {
  await login(page);
  await page.locator("button.btn-manage-monitor").click();
  await expect(page.getByText(/last 24h/)).toBeVisible();
}

test.describe("Monitor", () => {
  test("reaches the page from the sidebar and renders every section", async ({
    page,
  }) => {
    await openMonitor(page);
    for (const title of ["activity", "turns", "top errors", "resources"]) {
      await expect(page.getByText(title, { exact: true })).toBeVisible();
    }
  });

  test("records the browser's own requests, so the page proves the hook", async ({
    page,
  }) => {
    await openMonitor(page);
    // The HTTP hook timed the requests this very page made to load itself.
    await expect(page.locator("tr", { hasText: "http" }).first()).toBeVisible();
  });

  test("no section is silently blank — a table, or an explicit notice", async ({
    page,
  }) => {
    // A blank panel reads as a clean bill of health when it means "nothing
    // recorded", so every section owes the reader one or the other.
    //
    // Asserted per section rather than as "some section is empty": which
    // sections have rows depends on what the rest of the suite happened to do
    // before this spec ran, and a test whose premise is "nothing has happened
    // yet" is only true on the first run of the day. The empty *rendering*
    // itself is pinned in MonitorPage.test.tsx, where it costs nothing.
    await openMonitor(page);
    const sections = page.locator(".monitor-page section");
    // `openMonitor` waits for the header, which renders before the overview
    // fetch returns; the sections arrive with the data. Wait for them — a
    // `.count()` does not retry, so on a slow fetch a page that is merely
    // still loading reads as a page with no sections at all. All four render
    // in one commit, so once the first is up the rest are too.
    await expect(sections.first()).toBeVisible();
    const count = await sections.count();
    expect(count).toBeGreaterThan(0);
    for (let i = 0; i < count; i++) {
      const section = sections.nth(i);
      const rows = await section.locator("table").count();
      const notice = await section
        .getByText(/Nothing recorded in this window/)
        .count();
      const heading = await section.locator("h3").textContent();
      expect(rows + notice, `section "${heading?.trim()}" said nothing`).toBeGreaterThan(0);
    }
  });

  test("changing the window refetches", async ({ page }) => {
    await openMonitor(page);
    await page.getByRole("button", { name: "7d", exact: true }).click();
    await expect(page.getByText(/last 7d/)).toBeVisible();
  });

  test("no MCP sidecars are running — B1 stayed done", async ({ page }) => {
    await openMonitor(page);
    const row = page.locator("tr", { hasText: "mcp_sidecar_count" });
    // The sampler runs on a 60s interval, so on a short run the gauge may not
    // have a reading yet; when it does, it must be zero.
    if (await row.isVisible()) {
      // Last cell is `latest`. Read the cell, not the row: the columns render
      // without separators, so a regex over the row's text concatenates them
      // ("mcp_sidecar_count" + count + avg + peak + latest) and matches the
      // wrong number.
      const latest = await row.locator("td").last().textContent();
      expect(Number(latest?.trim())).toBe(0);
    }
  });
});
