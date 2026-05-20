import { test, expect, type Page } from "@playwright/test";

const TOKEN = "changeme";
const AGENTS_API = "http://localhost:8765/api/agents";
const SESSIONS_API = "http://localhost:8765/api/sessions";

// Agent names this spec creates — cleaned up in afterAll so reruns against
// the same in-memory server don't accumulate (and unique-name create works).
const OWNED_AGENTS = new Set(["E2E Researcher", "E2E Persisted"]);
const OWNED_SESSIONS = new Set(["Agent Thread"]);

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
}

/** Open the active agent's settings via the account menu (no sidebar gear). */
async function openAgentSettings(page: Page) {
  await page.locator(".btn-account").click();
  await page.locator(".menu-agent-settings").click();
  await expect(page.locator(".agent-settings")).toBeVisible();
}

test.afterAll(async ({ request }) => {
  const headers = { Authorization: `Bearer ${TOKEN}` };
  // Delete owned sessions first.
  const sRes = await request.get(SESSIONS_API, { headers });
  if (sRes.ok()) {
    for (const s of (await sRes.json()) as { id: string; name: string }[]) {
      if (OWNED_SESSIONS.has(s.name)) {
        await request.delete(`${SESSIONS_API}/${s.id}`, { headers }).catch(() => {});
      }
    }
  }
  // Archive owned agents (archive works whether or not they have sessions).
  const aRes = await request.get(AGENTS_API, { headers });
  if (aRes.ok()) {
    for (const a of (await aRes.json()) as { id: string; name: string }[]) {
      if (OWNED_AGENTS.has(a.name)) {
        await request
          .post(`${AGENTS_API}/${a.id}/archive`, { headers })
          .catch(() => {});
      }
    }
  }
});

test.describe("Agents", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("the Default Agent is present", async ({ page }) => {
    await expect(
      page.locator(".agent-item .agent-name", { hasText: "Octo" })
    ).toBeVisible();
  });

  test("creates an agent and a session under it", async ({ page }) => {
    await page.locator(".btn-agent-add").click();
    await page.locator("#agent-name").fill("E2E Researcher");
    await page.locator("#agent-prompt").fill("You research diligently.");
    await page.locator(".btn-agent-save").click();

    // The new agent appears and becomes active.
    const agentRow = page.locator(".agent-item", { hasText: "E2E Researcher" });
    await expect(agentRow).toBeVisible();
    await agentRow.click();
    await expect(agentRow).toHaveClass(/active/);

    // Create a session under this agent (+ lives on the agent's own row).
    await agentRow.locator(".btn-session-add").click();
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("Agent Thread");
    await page.locator("button.btn-create").click();

    await expect(
      page.locator(".session-item .session-name", { hasText: "Agent Thread" })
    ).toBeVisible();
    // Chat header shows both the agent and the session.
    await expect(page.locator(".chat-header h3")).toHaveText("Agent Thread");
    await expect(page.locator(".chat-agent")).toContainText("E2E Researcher");
  });

  test("edits an agent's system prompt and it persists", async ({ page }) => {
    // Create the agent.
    await page.locator(".btn-agent-add").click();
    await page.locator("#agent-name").fill("E2E Persisted");
    await page.locator("#agent-prompt").fill("first prompt");
    await page.locator(".btn-agent-save").click();

    const agentRow = page.locator(".agent-item", { hasText: "E2E Persisted" });
    await expect(agentRow).toBeVisible();

    // Make it the active agent, then edit via the account menu (no gear).
    await agentRow.click();
    await expect(agentRow).toHaveClass(/active/);

    await openAgentSettings(page);
    await expect(page.locator("#agent-prompt")).toHaveValue("first prompt");
    await page.locator("#agent-prompt").fill("second prompt — edited");
    await page.locator(".btn-agent-save").click();

    // Reopen again — the edit persisted (proves PATCH + store upsert).
    await openAgentSettings(page);
    await expect(page.locator("#agent-prompt")).toHaveValue(
      "second prompt — edited"
    );
  });

  test("the Default Agent cannot be archived from settings", async ({ page }) => {
    const def = page.locator(".agent-item", { hasText: "Octo" });
    // Select Octo, then open its settings from the account menu.
    await def.click();
    await expect(def).toHaveClass(/active/);
    await openAgentSettings(page);
    await expect(page.locator(".agent-settings #agent-name")).toHaveValue("Octo");
    // is_system agents expose no "Archive agent" button.
    await expect(
      page.locator(".agent-settings button", { hasText: "Archive agent" })
    ).toHaveCount(0);
  });
});
