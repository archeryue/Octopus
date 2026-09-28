/**
 * Sidebar pins, end to end (docs/plans/sidebar-pins.md). Pure UI, no model.
 *
 * The sidebar lists pinned agents in the user's order; the Agents page's All
 * tab lists every agent. Covered here in a real browser because the parts
 * that matter are physical: a hover button, a drag, a keyboard reorder, and
 * the order surviving a reload. The application half of the same flow runs
 * in applications.spec.ts, where a real application exists to pin.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const TOKEN = "changeme";
const API = "http://localhost:8765/api";
const headers = { Authorization: `Bearer ${TOKEN}` };

const PIN_A = "E2E Pin Alpha";
const PIN_B = "E2E Pin Beta";
const PIN_C = "E2E Pin Gamma";
const OWNED = new Set([PIN_A, PIN_B, PIN_C]);

type AgentRow = { id: string; name: string; pinned: boolean; pin_order: number };

async function agents(request: APIRequestContext): Promise<AgentRow[]> {
  const res = await request.get(`${API}/agents`, { headers });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

async function ensureAgent(request: APIRequestContext, name: string): Promise<AgentRow> {
  const existing = (await agents(request)).find((a) => a.name === name);
  if (existing) {
    if (!existing.pinned) {
      await request.post(`${API}/agents/${existing.id}/pin`, { headers });
    }
    return existing;
  }
  const res = await request.post(`${API}/agents`, {
    headers: { ...headers, "Content-Type": "application/json" },
    data: { name },
  });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

/** The agents the sidebar shows, top to bottom. */
async function sidebarNames(page: Page): Promise<string[]> {
  return page.locator(".agent-list-items .agent-name").allTextContents();
}

/** Only this spec's agents, in sidebar order — other specs share the server. */
async function ownOrder(page: Page): Promise<string[]> {
  return (await sidebarNames(page)).filter((n) => OWNED.has(n));
}

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
}

/** The Agents page's All tab: the section's "+", then All. */
async function openAllAgents(page: Page) {
  await page.locator(".btn-agent-add").click();
  await page.locator(".btn-tab-all").click();
  await expect(page.locator(".item-library")).toBeVisible();
}

const row = (page: Page, name: string) =>
  page.locator(".agent-list-items .agent-item", { hasText: name });

test.beforeEach(async ({ request }) => {
  // A fixed starting point: all three pinned, in A, B, C order.
  const rows = [];
  for (const name of [PIN_A, PIN_B, PIN_C]) rows.push(await ensureAgent(request, name));
  const pinned = (await agents(request))
    .filter((a) => a.pinned && !OWNED.has(a.name))
    .sort((x, y) => x.pin_order - y.pin_order)
    .map((a) => a.id);
  await request.put(`${API}/agents/pin-order`, {
    headers: { ...headers, "Content-Type": "application/json" },
    data: { ids: [...pinned, ...rows.map((r) => r.id)] },
  });
});

test.afterAll(async ({ request }) => {
  for (const a of await agents(request).catch(() => [] as AgentRow[])) {
    if (OWNED.has(a.name)) {
      await request.post(`${API}/agents/${a.id}/archive`, { headers }).catch(() => {});
    }
  }
});

test.describe("Sidebar pins", () => {
  test("unpin from the sidebar, find it on All, pin it back", async ({ page, request }) => {
    await login(page);
    await expect.poll(() => ownOrder(page)).toEqual([PIN_A, PIN_B, PIN_C]);

    // Unpin from the row's hover control.
    await row(page, PIN_B).hover();
    await row(page, PIN_B).locator(".btn-agent-unpin").click();
    await expect(row(page, PIN_B)).toHaveCount(0);
    // Unpinned is not archived: the agent is still live.
    const beta = (await agents(request)).find((a) => a.name === PIN_B)!;
    expect(beta.pinned).toBe(false);

    // The Agents page's All tab lists it under "Not in sidebar".
    await openAllAgents(page);
    await expect(page.locator(".item-library")).toBeVisible();
    const loose = page.locator(".library-unpinned .library-row", { hasText: PIN_B });
    await expect(loose).toBeVisible();

    // Pinning it back puts it at the bottom of the sidebar.
    await loose.locator(".btn-library-pin").click();
    await expect(
      page.locator(".library-pinned .library-row", { hasText: PIN_B })
    ).toBeVisible();
    await expect.poll(() => ownOrder(page)).toEqual([PIN_A, PIN_C, PIN_B]);
  });

  test("the Default Agent can't be unpinned", async ({ page }) => {
    await login(page);
    const octo = page.locator(".agent-list-items .agent-item", { hasText: "Octo" }).first();
    await octo.hover();
    await expect(octo.locator(".btn-agent-unpin")).toHaveCount(0);

    await openAllAgents(page);
    await expect(
      page.locator(".library-row", { hasText: "Octo" }).locator(".btn-library-pin.locked")
    ).toBeVisible();
  });

  test("drag reorders, and the order survives a reload", async ({ page }) => {
    await login(page);
    await expect.poll(() => ownOrder(page)).toEqual([PIN_A, PIN_B, PIN_C]);

    // Carry Gamma above Alpha: press, clear the 6px activation distance,
    // then travel in steps so the sortable sees the pointer pass over rows.
    const from = (await row(page, PIN_C).boundingBox())!;
    const to = (await row(page, PIN_A).boundingBox())!;
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
    await page.mouse.down();
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2 - 10, {
      steps: 5,
    });
    await page.mouse.move(to.x + to.width / 2, to.y + 2, { steps: 15 });
    await page.mouse.up();

    await expect.poll(() => ownOrder(page)).toEqual([PIN_C, PIN_A, PIN_B]);
    // A drag is not a click: nothing unfolded.
    await expect(row(page, PIN_C)).not.toHaveClass(/expanded/);

    await page.reload();
    await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
    await expect.poll(() => ownOrder(page)).toEqual([PIN_C, PIN_A, PIN_B]);
  });

  test("the keyboard reorders too", async ({ page }) => {
    await login(page);
    await expect.poll(() => ownOrder(page)).toEqual([PIN_A, PIN_B, PIN_C]);

    // Keys paced like a person's. The sortable ignores an arrow that lands
    // in the first instant after Space lifts the row (it moves the row but
    // never works out what it's over), and a key pressed the instant after
    // an arrow drops before the move registers. Real keystrokes are far
    // further apart; each step also waits for what a screen reader says back.
    const said = (text: string) =>
      expect(page.locator("[aria-live]", { hasText: text })).toHaveCount(1);
    const pause = () => page.waitForTimeout(300);
    await row(page, PIN_B).focus();
    await page.keyboard.press("Space");
    await said(`Picked up ${PIN_B}`);
    await pause();
    await page.keyboard.press("ArrowDown");
    await said(`${PIN_B} moved to position`);
    await pause();
    await page.keyboard.press("Space");
    await said(`${PIN_B} dropped at position`);

    await expect.poll(() => ownOrder(page)).toEqual([PIN_A, PIN_C, PIN_B]);
  });

  test("an unpinned agent opened from All shows while its session is open", async ({
    page,
    request,
  }) => {
    const all = await agents(request);
    const gamma = all.find((a) => a.name === PIN_C)!;
    const alpha = all.find((a) => a.name === PIN_A)!;
    await request.post(`${API}/agents/${gamma.id}/unpin`, { headers });
    await request.post(`${API}/agents/${alpha.id}/sessions`, {
      headers: { ...headers, "Content-Type": "application/json" },
      data: { name: "E2E Pin Thread" },
    });

    await login(page);
    await expect(row(page, PIN_C)).toHaveCount(0);

    await openAllAgents(page);
    await page
      .locator(".library-unpinned .library-row", { hasText: PIN_C })
      .locator(".btn-library-chat")
      .click();

    // Chat on an agent with no sessions starts one and opens it; the agent
    // joins the sidebar, dimmed and unfolded, while that session is open.
    await expect(page.locator(".chat-header")).toBeVisible();
    await expect(row(page, PIN_C)).toHaveClass(/unpinned/);
    await expect(row(page, PIN_C)).toHaveClass(/expanded/);
    await expect(
      page.locator(".agent-group", { hasText: PIN_C }).locator(".session-item.active")
    ).toBeVisible();

    // Opening another agent's session: Gamma has nothing holding it in the
    // sidebar any more, so it goes back to living on the All page only.
    await row(page, PIN_A).click();
    await page
      .locator(".agent-group", { hasText: PIN_A })
      .locator(".session-item", { hasText: "E2E Pin Thread" })
      .click();
    await expect(row(page, PIN_C)).toHaveCount(0);

    // Clean up the sessions this test made.
    const sessions = (await (
      await request.get(`${API}/sessions`, { headers })
    ).json()) as { id: string; agent_id: string }[];
    for (const s of sessions) {
      const owner = (await agents(request)).find((a) => a.id === s.agent_id);
      if (owner && OWNED.has(owner.name)) {
        await request.delete(`${API}/sessions/${s.id}`, { headers }).catch(() => {});
      }
    }
  });
});
