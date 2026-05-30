/**
 * End-to-end coverage of agent-to-agent delegation (agent-collaboration.md).
 *
 * This is the deterministic counterpart to `tests/test_delegations_real.py`
 * — that suite verifies the chain primitive against the real `claude` CLI,
 * but its question-loop / 3-hop cases can't exercise the full MCP HTTP
 * shim because there's no live FastAPI in the unit-test process. This
 * spec runs against Playwright's auto-started backend (with the real
 * `claude` binary) so the `mcp__ask_agent__*` tools actually reach
 * the delegations routes.
 *
 * What this spec covers end-to-end:
 *   1. A real LLM call to `mcp__ask_agent__ask` spawns Vera's child
 *      session, the request card renders inline with the tool_use, and
 *      transitions running → replied.
 *   2. Vera's reply lands as a `[agent-reply:Vera delegation=…]` turn
 *      injection in Octo's chat, rendered as an
 *      `AgentDelegationEventCard`.
 *   3. The "Open Vera's session" link navigates to the child session,
 *      and the "Delegated from Octo" banner appears on the child
 *      header.
 *   4. The sidebar surfaces the hidden delegation pill on Vera (the
 *      child session is `origin='delegation'` so it's hidden behind
 *      `showDelegations=false` by default).
 */

import { test, expect, type Page, type APIRequestContext } from "@playwright/test";

const TOKEN = "changeme";
const SERVER_URL = "http://localhost:8765";
const API = `${SERVER_URL}/api`;

const OWNED_AGENTS = new Set(["E2E DelegTarget"]);
const OWNED_SESSIONS = new Set(["Delegation E2E"]);

test.afterAll(async ({ request }) => {
  const headers = { Authorization: `Bearer ${TOKEN}` };
  try {
    const sRes = await request.get(`${API}/sessions`, { headers });
    if (sRes.ok()) {
      const sessions: { id: string; name: string }[] = await sRes.json();
      for (const s of sessions) {
        // Clean up the user-typed session AND any delegation child
        // sessions we spawned along the way (their names auto-derive
        // as "<target> ← <parent>").
        if (
          OWNED_SESSIONS.has(s.name) ||
          /←\s*Octo$/.test(s.name)
        ) {
          await request
            .delete(`${API}/sessions/${s.id}`, { headers })
            .catch(() => {});
        }
      }
    }
    const aRes = await request.get(`${API}/agents`, { headers });
    if (aRes.ok()) {
      const agents: { id: string; name: string }[] = await aRes.json();
      for (const a of agents) {
        if (OWNED_AGENTS.has(a.name)) {
          await request
            .post(`${API}/agents/${a.id}/archive`, { headers })
            .catch(() => {});
        }
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
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

async function ensureAgent(
  request: APIRequestContext,
  name: string
): Promise<{ id: string; name: string }> {
  const headers = {
    Authorization: `Bearer ${TOKEN}`,
    "Content-Type": "application/json",
  };
  const list = await request.get(`${API}/agents`, { headers });
  if (list.ok()) {
    const existing = ((await list.json()) as {
      id: string;
      name: string;
    }[]).find((a) => a.name === name);
    if (existing) return existing;
  }
  const res = await request.post(`${API}/agents`, {
    headers,
    data: {
      name,
      model: "haiku",
      // Be permissive: the system prompt nudges Vera to reply tersely
      // so the reply-injection assertion has a stable target string.
      system_prompt:
        "You are a terse assistant. Reply only with what the caller " +
        "asked for — no preamble, no closing remarks.",
    },
  });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

test.describe("Agent-to-agent delegation @llm", () => {
  // Three real LLM hops can take ~45-90 s; allow plenty of headroom
  // so the test never flakes on transient latency.
  test.setTimeout(240_000);
  // Haiku is occasionally non-deterministic about invoking
  // mcp__ask_agent__ask under load — when the spec runs as the last
  // step of the full e2e suite (~3 min in), the model has sometimes
  // declined the tool call. The full chain itself is solid (passes
  // reliably in isolation in ~20 s); one retry rides over that LLM
  // duck without papering over a real defect.
  test.describe.configure({ retries: 1 });

  test("ask_agent → reply card + Open child + Delegated-from banner", async ({
    page,
    request,
  }) => {
    const SENTINEL = "VERAPONG";
    await ensureAgent(request, "E2E DelegTarget");

    // Create the parent session under the default Octo via REST so
    // the test starts in a known state (no chance of the form view
    // intercepting our typed prompt).
    const sessRes = await request.post(`${API}/sessions`, {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      data: { name: "Delegation E2E", working_dir: "/tmp" },
    });
    expect(sessRes.ok()).toBeTruthy();

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Delegation E2E" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Delegation E2E");

    // Force the model to invoke `mcp__ask_agent__ask` directly. The
    // request to Vera asks for the exact sentinel so the [agent-reply]
    // injection has a stable string to assert against.
    const prompt =
      "Use the `mcp__ask_agent__ask` tool RIGHT NOW with " +
      'name="E2E DelegTarget" and ' +
      `request="Reply with exactly the 8 characters ${SENTINEL} and ` +
      'nothing else. No prose, no punctuation.". ' +
      "After calling the tool say 'asked' and end your turn. When " +
      `the follow-up [agent-reply:...] turn arrives, repeat ${SENTINEL} ` +
      "verbatim and stop.";

    await page.locator(".chat-input-bar textarea").fill(prompt);
    await page.locator("button.btn-send").click();

    // 1. The inline delegation-request card appears next to the
    //    tool_use block once the model invokes ask_agent.
    const requestCard = page.locator(".agent-delegation-request").first();
    await expect(requestCard).toBeVisible({ timeout: 120_000 });
    await expect(requestCard).toContainText(/Asked E2E DelegTarget/i);

    // 2. The request card transitions to "replied" once Vera's
    //    [agent-reply] turn injection lands. Match by data attribute
    //    so we don't depend on the badge label text.
    await expect(requestCard).toHaveAttribute(
      "data-delegation-state",
      "completed",
      { timeout: 120_000 }
    );

    // 3. The injected reply turn renders as an event card. The
    //    msg-agent-delegation-event wrapper holds both the
    //    data-delegation-kind label and the .agent-delegation-card
    //    body as siblings — filter the wrapper by the kind label
    //    then descend to the card.
    const replyEvent = page
      .locator(".msg-agent-delegation-event")
      .filter({ has: page.locator('[data-delegation-kind="reply"]') })
      .first();
    await expect(replyEvent).toBeVisible({ timeout: 60_000 });
    const replyCard = replyEvent.locator(".agent-delegation-card");
    // Reply cards collapse by default — expand to reveal the body
    // and the open-child button before assertions.
    await replyCard.locator("button").first().click();
    await expect(replyEvent).toContainText(SENTINEL);

    // 4. The "Open <target>'s session" button on the reply card
    //    navigates into the child session and the "Delegated from
    //    Octo" banner appears in the header.
    const openBtn = replyEvent.getByRole("button", {
      name: /open e2e delegtarget's session/i,
    });
    await openBtn.click();

    const banner = page.locator('[data-testid="delegation-banner"]');
    await expect(banner).toBeVisible({ timeout: 5_000 });
    await expect(banner).toContainText(/Delegated from/);
    await expect(banner).toContainText(/Octo/);
    await expect(banner.getByRole("button", { name: /open parent/i })).toBeVisible();

    // 5. Clicking "Open parent" returns to Octo's session.
    await banner.getByRole("button", { name: /open parent/i }).click();
    await expect(page.locator(".chat-header h3")).toHaveText("Delegation E2E");

    // 6. Sidebar: the new delegation session is hidden by default.
    //    Under the target agent's row the "+1 delegations hidden"
    //    pill surfaces. Click it to flip the global toggle, the
    //    delegation session appears with its subtask marker.
    const targetAgentRow = page.locator(".agent-item", {
      hasText: "E2E DelegTarget",
    });
    // Expand the agent so its session list is mounted.
    await targetAgentRow.click();
    const hiddenPill = targetAgentRow
      .locator("..")
      .locator(".delegation-toggle", { hasText: /delegation/ });
    await expect(hiddenPill).toBeVisible({ timeout: 10_000 });
    await hiddenPill.click();
    // After toggle, a session under E2E DelegTarget marked as a
    // delegation appears. The delegation marker icon hangs off the
    // session row.
    const delegSessionRow = targetAgentRow
      .locator("..")
      .locator(".session-item")
      .filter({ has: page.locator(".delegation-marker") })
      .first();
    await expect(delegSessionRow).toBeVisible({ timeout: 5_000 });
  });
});
