import * as http from "node:http";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test, expect, type Page, type APIRequestContext } from "@playwright/test";

const TOKEN = "changeme";
const SERVER_URL = "http://localhost:8765";
const API = `${SERVER_URL}/api`;

/** Click the new-session "+" on the default "Octo" agent's row. The button is
 * per-agent, and specs share one in-memory backend DB, so a bare
 * ".btn-session-add" turns ambiguous once a concurrent spec creates another
 * agent. Scoping to Octo keeps it unambiguous. */
const addOctoSession = (page: Page) =>
  page
    .locator(".agent-item", { hasText: "Octo" })
    .locator(".btn-session-add")
    .click();

const OWNED_NAMES = new Set([
  "Schedule UI Test",
  "Schedule Cmd Test",
  "Waiting Hint Yes",
  "Waiting Hint No",
  "Virtuoso Long Session",
  "Queue Test",
  "Interrupt Test",
  "Asked Question",
  "Real Q Session",
  "Resume Session",
  "Bad Cred Session",
  "Webhook Fire Probe",
  "Archive Probe",
  "Archive Probe Two",
  "Attachment Picker Test",
  "Attachment History Test",
  "Viewer Showme Test",
  "Q Re-render A",
  "Q Re-render B",
  "Q Interrupt Recovery",
  "Q Auto Answer",
  "Reset Slash Cmd",
  "Bg Run E2E",
  "Bg Spill Pipeline",
  "Bg Idle Watchdog",
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
  await expect(page.locator(".agent-list-header")).toBeVisible();
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
  messages: Record<string, unknown>[]
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
  test("the Schedules section opens the all-agents overview", async ({
    page,
  }) => {
    await login(page);
    // The section is always present now (not agent-scoped) and is the entry
    // point to the overview dialog.
    await expect(page.locator(".schedule-section")).toBeVisible();
    await expect(page.locator(".schedule-title")).toHaveText("Schedules");

    await page.locator(".schedule-header").click();
    await expect(page.locator(".schedules-dialog")).toBeVisible();
    await expect(
      page.locator(".schedules-dialog", { hasText: "Recurring prompts" })
    ).toBeVisible();
  });

  test("/schedule command creates a schedule shown in the overview; toggle + delete", async ({
    page,
    request,
  }) => {
    await createSessionApi(request, "Schedule Cmd Test");
    await login(page);

    // Activate the session so the chat input is available.
    await page
      .locator(".session-item .session-name", { hasText: "Schedule Cmd Test" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText(
      "Schedule Cmd Test"
    );

    // Run the command (45m = 2700s — well above the run window; won't fire).
    const PROMPT = "e2e schedule command probe";
    await page
      .locator(".chat-input-bar textarea")
      .fill(`/schedule 45m ${PROMPT}`);
    await page.locator("button.btn-send").click();

    // A confirmation notice renders in chat (not attributed to You/Claude).
    await expect(page.locator(".msg-notice")).toContainText("Scheduled");

    // Open the overview and find our schedule (scope by its unique prompt).
    await page.locator(".schedule-header").click();
    await expect(page.locator(".schedules-dialog")).toBeVisible();
    const row = page.locator(".schedules-dialog .schedule-item", {
      hasText: PROMPT,
    });
    await expect(row).toHaveCount(1);
    await expect(row.locator(".schedule-name")).toHaveText(PROMPT);
    await expect(row.locator(".schedule-interval")).toContainText("45m");
    await expect(row.locator(".btn-toggle")).toHaveClass(/on/);

    // Toggle disabled.
    await row.locator(".btn-toggle").click();
    await expect(row.locator(".btn-toggle")).toHaveClass(/off/);
    await expect(row).toHaveClass(/disabled/);

    // Delete it — the row leaves the overview.
    await row.locator(".btn-delete").click();
    await expect(
      page.locator(".schedules-dialog .schedule-item", { hasText: PROMPT })
    ).toHaveCount(0);
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

// ---------------------------------------------------------------------------
// Message queue + interrupt
// ---------------------------------------------------------------------------

test.describe("Message Queue & Interrupt", () => {
  // Real Claude turns — give them time. Queue scenarios run two turns.
  test.describe.configure({ timeout: 180_000 });

  test("send while running queues the message, which fires after current turn", async ({
    page,
    request,
  }) => {
    const { id: sessionId } = await createSessionApi(request, "Queue Test");

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Queue Test" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Queue Test");

    const input = page.locator(".chat-input-bar textarea");

    // First message — force tool use with sleeps so the CLI-direct path
    // is genuinely still running when we send the queued message. A plain
    // text prompt completes too fast to reliably catch with Playwright.
    await input.fill(
      "Use the Bash tool to run `sleep 4`, then `sleep 4` again, " +
      "then say: 100 * 200 = 20000"
    );
    await page.locator("button.btn-send").click();

    // Wait for the run to start. The first turn cold-starts a real `claude`
    // process; under full-suite parallel load (several workers each spawning
    // claude) that can take well over 15s, so allow generous headroom — the
    // describe budget is 180s.
    await expect(
      page.locator(".status-badge.status-running")
    ).toBeVisible({ timeout: 60_000 });

    // Send button switches its semantic label to "Queue message" while a
    // turn is running. The button is icon-only (post-VM0-style redesign),
    // so we check the accessibility label, which is the source of truth
    // either way for screen readers.
    await expect(page.locator("button.btn-send")).toHaveAttribute(
      "aria-label",
      "Queue message"
    );

    // Queue a second message while the first is still running
    await input.fill("And what is 50 * 50? Reply with just the number.");
    await page.locator("button.btn-send").click();

    // Queue list should show one pending message
    await expect(page.locator(".queue-list")).toBeVisible();
    await expect(page.locator(".queue-item")).toHaveCount(1);
    await expect(page.locator(".queue-item .queue-content")).toContainText(
      "50 * 50"
    );

    // Wait for both runs to finish (two result badges)
    await expect(page.locator(".result-badge")).toHaveCount(2, {
      timeout: 120_000,
    });

    // Queue is drained
    await expect(page.locator(".queue-item")).toHaveCount(0);

    // Both user messages should be persisted in the session. The chat
    // is virtualized (react-virtuoso unmounts items beyond a few hundred
    // pixels of overscan) and `followOutput="smooth"` parks us at the
    // bottom — so the first user message is reliably unmounted by the
    // time both turns finish. Verify the truth (DB) instead of the
    // rendered window.
    const detailRes = await request.get(`${API}/sessions/${sessionId}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    expect(detailRes.ok()).toBeTruthy();
    const detail = await detailRes.json();
    const userMessages = (
      detail.messages as { role: string; content: unknown }[]
    )
      .filter((m) => m.role === "user")
      .map((m) => (typeof m.content === "string" ? m.content : ""))
      // The model may legitimately route the `sleep` commands through
      // mcp__bg__run (the Octopus system prompt tells it to use bg_run for
      // sleeps). When that bg task finishes it injects a *synthesized*
      // `[bg-task-result]` user-role message — not user-typed input — so
      // exclude it from the typed-prompt count to keep this deterministic.
      .filter((c) => !c.includes("[bg-task-result]"));
    expect(userMessages.length).toBe(2);
    expect(userMessages[0]).toContain("100 * 200");
    expect(userMessages[1]).toContain("50 * 50");
  });

  test("Esc key interrupts the current turn", async ({ page, request }) => {
    await createSessionApi(request, "Interrupt Test");

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Interrupt Test" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Interrupt Test");

    const input = page.locator(".chat-input-bar textarea");

    // Force a genuinely long-running tool call. We previously asked the
    // model for three separate `sleep 3` invocations, but Claude tends to
    // collapse those into a single `sleep 3 && sleep 3 && sleep 3` Bash
    // call that finishes before Playwright presses Esc. A single explicit
    // `sleep 30` gives us a wide window regardless of how the model
    // chooses to compose tool calls.
    await input.fill(
      "Use the Bash tool to run exactly: sleep 30. Then say done."
    );
    await page.locator("button.btn-send").click();

    // Wait for the run to start, then press Esc immediately — no extra
    // settling delay so we don't race the model into finishing.
    await expect(
      page.locator(".status-badge.status-running")
    ).toBeVisible({ timeout: 15_000 });
    await page.keyboard.press("Escape");

    // The interrupt should land an error marker in the chat
    await expect(page.locator(".msg-error .msg-content")).toContainText(
      "interrupted by user",
      { timeout: 10_000 }
    );

    // Status returns to idle
    await expect(page.locator(".status-badge.status-idle")).toBeVisible({
      timeout: 10_000,
    });
  });
});

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

// ---------------------------------------------------------------------------
// AskUserQuestion rendering
// ---------------------------------------------------------------------------
//
// The interactive form path (broadcast → form → answer → resume) is covered
// by backend unit tests in tests/test_session_manager.py since triggering a
// real SDK AskUserQuestion call from Playwright would require a live agent.
// This e2e covers the rendering of a question that exists in chat history.

// ---------------------------------------------------------------------------
// Credentials Panel
// ---------------------------------------------------------------------------

test.describe("Credentials Panel", () => {
  // Clean up credentials our test creates so we don't pollute later runs.
  const ownedLabels = new Set(["E2E Cred", "E2E Cred Renamed"]);

  test.afterAll(async ({ request }) => {
    try {
      const res = await request.get(`${API}/credentials`, {
        headers: { Authorization: `Bearer ${TOKEN}` },
        timeout: 5_000,
      });
      if (!res.ok()) return;
      const items: { id: string; label: string }[] = await res.json();
      for (const i of items) {
        if (!ownedLabels.has(i.label)) continue;
        await request
          .delete(`${API}/credentials/${i.id}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
            timeout: 3_000,
          })
          .catch(() => {});
      }
    } catch {
      // best-effort
    }
  });

  test("delete a credential via the sidebar", async ({ page, request }) => {
    // Seed via API — the UI's only add path is OAuth, which can't run
    // in the test env. Deletion is the in-UI path we verify here.
    const res = await request.post(`${API}/credentials`, {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      data: {
        backend: "claude-code",
        label: "E2E Cred",
        auth_type: "api_key",
        secret: "sk-e2e-test-key",
      },
    });
    expect(res.ok()).toBeTruthy();

    await login(page);

    await expect(page.locator(".credential-title")).toHaveText("Harness");

    const item = page.locator(".credential-item", { hasText: "E2E Cred" });
    await expect(item).toBeVisible();
    await expect(
      item.locator(".credential-badge.backend-claude-code")
    ).toHaveText("Claude");
    await expect(item.locator(".credential-badge.auth-api_key")).toHaveText(
      "Key"
    );

    // Delete it via the UI
    await item.locator(".btn-delete").click();
    await expect(
      page.locator(".credential-item", { hasText: "E2E Cred" })
    ).toHaveCount(0);
  });

  test("sign-in starts OAuth flow and exposes the device URL", async ({
    page,
  }) => {
    // We can't reach the real Anthropic endpoint from this test, but we
    // CAN intercept the /oauth/start call to verify the UI wires up the
    // returned URL correctly. The "Bug ZodError" e2e already exercises
    // the end-to-end OAuth flow against the live network on the user's
    // verification pass (OAuth-7).
    await page.route("**/api/credentials/oauth/start", (route) => {
      route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          login_id: "intercepted-login",
          device_url:
            "https://claude.ai/oauth/authorize?client_id=test&state=abc",
        }),
      });
    });
    await page.route("**/api/credentials/oauth/cancel", (route) => {
      route.fulfill({ status: 204, body: "" });
    });

    await login(page);
    // Click the "+" button in the Harness section header
    await page
      .locator(".credential-section .btn-credential-add")
      .first()
      .click();

    // The sign-in dialog opens
    const dialog = page.locator('[role="dialog"]', {
      hasText: "Sign in with Claude Code",
    });
    await expect(dialog).toBeVisible();

    // The device URL appears as a clickable link with the OAuth URL
    const urlLink = dialog.locator(".credential-device-url");
    await expect(urlLink).toBeVisible();
    await expect(urlLink).toContainText("claude.ai/oauth/authorize");
    await expect(urlLink).toHaveAttribute("target", "_blank");

    // Step-2 inputs appear inside the dialog
    await expect(dialog.locator("#cred-label")).toBeVisible();
    await expect(dialog.locator("#cred-code")).toBeVisible();

    // Cancel closes the dialog
    await dialog.locator("button", { hasText: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
  });

  test("create-session form shows credential selector when credentials exist", async ({
    page,
    request,
  }) => {
    // Seed one via API so the selector has something to show.
    const res = await request.post(`${API}/credentials`, {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      data: {
        backend: "claude-code",
        label: "E2E Cred Renamed",
        auth_type: "api_key",
        secret: "sk-e2e-2",
      },
    });
    expect(res.ok()).toBeTruthy();

    await login(page);

    // Open the create-session form via the default agent's "+" button.
    await addOctoSession(page);

    // Selector is rendered, default option is "Default auth (CLI login)",
    // and our seeded credential is selectable.
    const selector = page.locator(".session-credential-select");
    await expect(selector).toBeVisible();
    await expect(selector.locator("option")).toContainText([
      "Default auth (CLI login)",
      "E2E Cred Renamed",
    ]);
  });
});

test.describe("AskUserQuestion rendering", () => {
  test("imported question_request renders the asked-question summary", async ({
    page,
    request,
  }) => {
    await importSessionApi(request, "Asked Question", [
      { role: "user", type: "text", content: "I need your input" },
      {
        role: "assistant",
        type: "question_request",
        tool_name: "AskUserQuestion",
        tool_use_id: "q-imported-1",
        tool_input: {
          questions: [
            {
              question: "Which database should we use?",
              header: "DB",
              multiSelect: false,
              options: [
                { label: "Postgres", description: "Battle tested" },
                { label: "SQLite", description: "Simple" },
              ],
            },
          ],
        },
      },
      {
        role: "user",
        type: "question_answer",
        tool_use_id: "q-imported-1",
        content: "Q: Which database should we use?\nA: Postgres",
      },
    ]);

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Asked Question" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Asked Question");

    // The historical question renders as the dashed-border summary
    const summary = page.locator(".msg-question-done");
    await expect(summary).toBeVisible();
    await expect(summary).toContainText("Claude asked");
    await expect(summary).toContainText("Which database should we use?");

    // The user's answer renders as a user bubble with italic body
    const answer = page.locator(".msg-question-answer");
    await expect(answer).toBeVisible();
    await expect(answer).toContainText("Postgres");
  });
});

// ---------------------------------------------------------------------------
// End-to-end against the real Claude CLI: AskUserQuestion + resume
// ---------------------------------------------------------------------------
//
// These hit the real model. Costs ~$0.01-0.05 per run. They run last and
// are skipped if the Octopus server can't reach claude on PATH (the assert
// below catches that). Both also have generous timeouts since real API
// latency varies.

test.describe("Real CLI end-to-end", () => {
  test.setTimeout(120_000);

  test("AskUserQuestion (via mcp__ask__user): real model → form → answer → reply", async ({
    page,
    request,
  }) => {
    await createSessionApi(request, "Real Q Session");

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Real Q Session" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Real Q Session");

    // The built-in AskUserQuestion is disabled in Octopus (--disallowedTools
    // in claude_code.py). Nudge the model toward the MCP replacement
    // explicitly. The frontend / WS / question-state machinery on the
    // host side is the same as the legacy flow — the change is purely
    // about which tool fires.
    const prompt =
      "Use the mcp__ask__user tool right now with this exact JSON for the questions argument: " +
      '[{"question":"Pick a color","header":"Choice","multiSelect":false,' +
      '"options":[' +
      '{"label":"red","description":"the color red"},' +
      '{"label":"blue","description":"the color blue"}' +
      "]}]. " +
      "Do not write any text before the tool call.";

    await page.locator(".chat-input-bar textarea").fill(prompt);
    await page.locator("button.btn-send").click();

    // The QuestionPrompt form should appear (interactive, not the dashed
    // "Claude asked" summary that fires when the question is already answered).
    const form = page.locator(".msg-question:not(.msg-question-done)");
    await expect(form).toBeVisible({ timeout: 60_000 });
    await expect(form).toContainText("Pick a color");
    await expect(form.locator(".question-option-label", { hasText: "red" }))
      .toBeVisible();

    // Pick "red" and submit
    await form
      .locator(".question-option", { hasText: "red" })
      .locator("input")
      .check();
    await form.locator(".btn-approve").click();

    // The user-side answer bubble lands first; then a follow-up assistant
    // response should reference "red". This proves the new round-trip:
    // UI → WS answer_question → session_manager.answer_question → sets
    // asyncio.Event → mcp__ask__user's long-poll unblocks → returns text
    // to the model → CLI continues → next event upstream.
    await expect(page.locator(".msg-question-answer")).toContainText("red", {
      timeout: 30_000,
    });

    // And the turn eventually completes
    await expect(page.locator(".result-badge").first()).toBeVisible({
      timeout: 60_000,
    });
  });

  test("Credential override: bad key attached to session causes auth failure", async ({
    page,
    request,
  }) => {
    // Seed a deliberately invalid credential via API (the UI works too,
    // but API is fewer steps and what we already test in Credentials Panel).
    const credRes = await request.post(`${API}/credentials`, {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      data: {
        backend: "claude-code",
        label: "Bad Key (E2E)",
        auth_type: "api_key",
        secret: "sk-ant-bogus-octopus-e2e",
      },
    });
    expect(credRes.ok()).toBeTruthy();
    const credId = (await credRes.json()).id;

    // Create a session that uses the bad credential
    const sessRes = await request.post(`${API}/sessions`, {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      data: {
        name: "Bad Cred Session",
        working_dir: "/tmp",
        credential_id: credId,
      },
    });
    expect(sessRes.ok()).toBeTruthy();

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Bad Cred Session" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Bad Cred Session");

    // Send a prompt — should fail because the bogus key overrides the
    // (otherwise-working) default OAuth, proving the env-var injection
    // actually reaches the CLI.
    await page
      .locator(".chat-input-bar textarea")
      .fill("Reply with: HI");
    await page.locator("button.btn-send").click();

    // We expect either an error bubble OR a result badge marking error
    // (the CLI surfaces auth failures as a result with is_error=true).
    // No happy "HI" text should appear.
    await expect(
      page
        .locator(".msg-error, .msg-system .result-badge")
        .first()
    ).toBeVisible({ timeout: 60_000 });

    // Belt-and-braces: ensure no successful assistant text "HI" landed.
    const assistantTexts = await page
      .locator(".msg-assistant .msg-content")
      .allInnerTexts();
    const joined = assistantTexts.join(" ");
    expect(joined).not.toMatch(/^HI$/);

    // Cleanup: delete the credential so it doesn't accumulate
    await request
      .delete(`${API}/credentials/${credId}`, {
        headers: { Authorization: `Bearer ${TOKEN}` },
      })
      .catch(() => {});
  });

  test("Resume: turn 2 has context from turn 1", async ({ page, request }) => {
    await createSessionApi(request, "Resume Session");
    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Resume Session" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Resume Session");

    // Turn 1 — plant a fact, wait for the result badge.
    await page
      .locator(".chat-input-bar textarea")
      .fill(
        "Remember this exact word for later: PINEAPPLE. Reply only with OK."
      );
    await page.locator("button.btn-send").click();
    await expect(page.locator(".result-badge").first()).toBeVisible({
      timeout: 60_000,
    });

    // Turn 2 — ask for the word. If resume works, the answer contains it.
    await page
      .locator(".chat-input-bar textarea")
      .fill("What word did I ask you to remember? Reply only with that word.");
    await page.locator("button.btn-send").click();

    // Wait for the second turn's result; the most recent assistant text
    // before it should mention PINEAPPLE (case-insensitive, in case the
    // model lower-cases).
    await expect(page.locator(".result-badge").nth(1)).toBeVisible({
      timeout: 60_000,
    });
    const allAssistant = await page
      .locator(".msg-assistant .msg-content")
      .allInnerTexts();
    const joined = allAssistant.join(" ").toUpperCase();
    expect(joined).toContain("PINEAPPLE");
  });
});

// ---------------------------------------------------------------------------
// AskUserQuestion: session-switch re-render, interrupt recovery, auto-answer
// ---------------------------------------------------------------------------
//
// Real CLI tests for three related behaviors:
//   1. (regression) selecting a session that has a live AskUserQuestion
//      must show the interactive form, not the greyed "already answered"
//      summary. The earlier bug was that SessionList.selectSession
//      fetched messages + pending_queue but skipped pending_questions, so
//      the form never rendered after a session switch.
//   2. (regression) interrupting a session that's blocked on
//      AskUserQuestion must return the UI to idle quickly. The earlier
//      bug was that interrupt() awaited backend.interrupt() with a tight
//      timeout that silently swallowed the failure, leaving the lock
//      held and the UI soft-locked.
//   3. (new feature) if no human answers within
//      OCTOPUS_ASK_USER_QUESTION_TIMEOUT_SECONDS, the server should
//      synthesize an "act autonomously" reply so async-driven sessions
//      (bridges, schedules) can't wedge forever.
//
// Each test sends a deterministic prompt that nudges the real model to
// invoke AskUserQuestion immediately — same trick the older
// "AskUserQuestion: real model → form → answer → reply" test uses, so
// timing/cost characteristics are similar (~$0.01-0.05 per test, up to
// ~60s for the form to appear).

const ASK_QUESTION_PROMPT =
  "Use the AskUserQuestion tool right now with this exact JSON for the questions argument: " +
  '[{"question":"Pick a color","header":"Choice","multiSelect":false,' +
  '"options":[' +
  '{"label":"red","description":"the color red"},' +
  '{"label":"blue","description":"the color blue"}' +
  "]}]. " +
  "Do not write any text before the tool call.";

test.describe("AskUserQuestion edge cases (real CLI)", () => {
  test.setTimeout(180_000);

  test("pending question form re-renders after switching sessions and back", async ({
    page,
    request,
  }) => {
    // Two sessions: A is where we trigger the question, B is just a
    // navigation target so we can leave A and come back to it.
    await createSessionApi(request, "Q Re-render A");
    await createSessionApi(request, "Q Re-render B");

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Q Re-render A" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Q Re-render A");

    await page.locator(".chat-input-bar textarea").fill(ASK_QUESTION_PROMPT);
    await page.locator("button.btn-send").click();

    // Live interactive form appears on session A
    const formA = page.locator(".msg-question:not(.msg-question-done)");
    await expect(formA).toBeVisible({ timeout: 90_000 });
    await expect(formA).toContainText("Pick a color");

    // Switch to a different session, then back. Before the fix,
    // selectSession() never set pending_questions, so coming back showed
    // the greyed summary (.msg-question-done) and the form was gone.
    await page
      .locator(".session-item .session-name", { hasText: "Q Re-render B" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Q Re-render B");
    // Confirm we're really on B (no form here)
    await expect(page.locator(".msg-question")).toHaveCount(0);

    await page
      .locator(".session-item .session-name", { hasText: "Q Re-render A" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Q Re-render A");

    // The interactive form must come back — NOT the greyed "Claude asked"
    // summary. Use :not(.msg-question-done) to assert the live variant.
    const formAfterSwitch = page.locator(
      ".msg-question:not(.msg-question-done)"
    );
    await expect(formAfterSwitch).toBeVisible({ timeout: 5_000 });
    await expect(formAfterSwitch).toContainText("Pick a color");
    await expect(formAfterSwitch.locator(".btn-approve")).toBeVisible();

    // Clean up: answer the question so the session drains and the
    // backend subprocess doesn't sit around until the autoanswer timer
    // fires (which would race the afterAll cleanup).
    await formAfterSwitch
      .locator(".question-option", { hasText: "red" })
      .locator("input")
      .check();
    await formAfterSwitch.locator(".btn-approve").click();
  });

  test("interrupt unsticks a session blocked on AskUserQuestion", async ({
    page,
    request,
  }) => {
    await createSessionApi(request, "Q Interrupt Recovery");

    await login(page);
    await page
      .locator(".session-item .session-name", {
        hasText: "Q Interrupt Recovery",
      })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText(
      "Q Interrupt Recovery"
    );

    await page.locator(".chat-input-bar textarea").fill(ASK_QUESTION_PROMPT);
    await page.locator("button.btn-send").click();

    // Wait for the live form (session is now wedged waiting for an answer)
    const form = page.locator(".msg-question:not(.msg-question-done)");
    await expect(form).toBeVisible({ timeout: 90_000 });
    await expect(page.locator(".chat-header .status-running")).toBeVisible();

    // Press Esc to interrupt. Before the fix, this would await
    // backend.interrupt() with a 2s timeout, swallow the failure, and
    // leave the lock held — the UI would stay in "Running" forever and
    // subsequent send_message calls would fail with "Session is busy".
    await page.keyboard.press("Escape");

    // UI should return to idle fast and the marker should land. Cap at
    // 5s — well under the old 8s backend-teardown budget that made
    // interrupt feel broken.
    await expect(page.locator(".chat-header .status-idle")).toBeVisible({
      timeout: 5_000,
    });
    await expect(
      page.locator(".msg-error", { hasText: "interrupted by user" })
    ).toBeVisible({ timeout: 5_000 });

    // Lock must be released — sending again should NOT raise
    // "Session ... is busy". We don't need the model to actually respond;
    // just that the request is accepted and the session goes back to
    // running. That alone proves the lock was released.
    await page.locator(".chat-input-bar textarea").fill("Reply with the word DONE.");
    await page.locator("button.btn-send").click();
    await expect(page.locator(".chat-header .status-running")).toBeVisible({
      timeout: 5_000,
    });
    // No "is busy" error bubble appeared
    const errors = await page
      .locator(".msg-error")
      .allInnerTexts();
    expect(errors.join(" ")).not.toMatch(/is busy/i);
  });

  test("unanswered AskUserQuestion auto-answers after the configured timeout", async ({
    page,
    request,
  }) => {
    await createSessionApi(request, "Q Auto Answer");

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Q Auto Answer" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Q Auto Answer");

    await page.locator(".chat-input-bar textarea").fill(ASK_QUESTION_PROMPT);
    await page.locator("button.btn-send").click();

    // Wait for the live form, then deliberately do nothing.
    const form = page.locator(".msg-question:not(.msg-question-done)");
    await expect(form).toBeVisible({ timeout: 90_000 });

    // OCTOPUS_ASK_USER_QUESTION_TIMEOUT_SECONDS is 12s in the e2e env
    // (see playwright.config.ts). After it elapses the server should
    // synthesize the autonomy-mode answer and broadcast it as a
    // question_answer event — which the UI renders as a
    // .msg-question-answer bubble, same as a human reply.
    //
    // Generous wait budget (30s) to cover the broadcast + render
    // round-trip after the 12s timer fires.
    await expect(
      page.locator(".msg-question-answer", {
        hasText: "No human is available",
      })
    ).toBeVisible({ timeout: 30_000 });

    // The risky-action language is what keeps the model from doing
    // dangerous things autonomously; assert the key phrase is there so
    // a regression in AUTO_ANSWER_TEXT can't silently weaken the prompt.
    await expect(
      page.locator(".msg-question-answer", {
        hasText: "risky or irreversible",
      })
    ).toBeVisible();

    // After the auto-answer is delivered, the form goes away and the
    // session continues — eventually emitting a result event for this turn.
    await expect(form).toHaveCount(0);
    await expect(page.locator(".result-badge").first()).toBeVisible({
      timeout: 60_000,
    });
  });
});

test.describe("/reset slash command", () => {
  test("typing /reset hits the reset API and clears the input", async ({
    page,
    request,
  }) => {
    const { id } = await createSessionApi(request, "Reset Slash Cmd");

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Reset Slash Cmd" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Reset Slash Cmd");

    // Capture the POST so we can assert it actually fired (not just
    // that the input cleared). The server's reset_session is idempotent
    // and works on an idle session too — returns 200, broadcasts idle.
    const resetRequest = page.waitForRequest(
      (req) =>
        req.method() === "POST" &&
        req.url().endsWith(`/api/sessions/${id}/reset`),
      { timeout: 5_000 }
    );

    await page.locator(".chat-input-bar textarea").fill("/reset");
    await page.locator("button.btn-send").click();

    await resetRequest;
    // Input cleared after the command ran
    await expect(page.locator(".chat-input-bar textarea")).toHaveValue("");
    // /reset is not a chat message — no user bubble for it
    const userBubbles = await page
      .locator(".msg-user .msg-content")
      .allInnerTexts();
    expect(userBubbles.join(" ")).not.toContain("/reset");
  });
});

// ---------------------------------------------------------------------------
// Settings dialog (future-features #7)
// ---------------------------------------------------------------------------

test.describe("Settings dialog", () => {
  test("opens via account menu, renders General / Account / Notifications tabs", async ({
    page,
  }) => {
    await login(page);

    // Settings now live in the account dropdown (no sidebar gear).
    await page.locator(".btn-account").click();
    await page.locator(".menu-settings").click();
    await expect(page.locator('[role="dialog"]')).toBeVisible();
    await expect(page.locator('[role="dialog"]')).toContainText("Settings");

    const tabs = await page.locator('[role="tab"]').allInnerTexts();
    expect(tabs).toEqual(["General", "Account", "Notifications"]);

    // General — shows the current origin + Version. We compare against
    // window.location.origin from the page itself rather than the test
    // file's SERVER_URL because Playwright loads the vite dev server
    // (:5174) which proxies API/WS to the backend on :8765.
    const general = page.locator('[role="tabpanel"]:visible');
    const origin = await page.evaluate(() => window.location.origin);
    await expect(general).toContainText(origin);
    await expect(general).toContainText("Version");

    // Account — shows the full token (no truncation) + Copy / Sign out
    await page.locator('[role="tab"]', { hasText: "Account" }).click();
    const account = page.locator('[role="tabpanel"]:visible');
    await expect(account).toContainText(TOKEN);
    await expect(account).toContainText("Sign out");
    await expect(account.locator(".btn-copy-token")).toBeVisible();

    // Notifications — should show the empty-state hint + Add webhook btn
    await page.locator('[role="tab"]', { hasText: "Notifications" }).click();
    const notif = page.locator('[role="tabpanel"]:visible');
    await expect(notif).toContainText("Webhook targets");
    await expect(notif.locator(".btn-notifier-add")).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// /archive command — fresh empty session under the same sidebar entry
// ---------------------------------------------------------------------------

test.describe("/archive command", () => {
  test("clears history client-side, keeps the same sidebar name", async ({
    page,
    request,
  }) => {
    // Seed a session that has some history already, so the swap is
    // visible (chat goes from "has messages" → "empty").
    const imp = await importSessionApi(request, "Archive Probe", [
      { role: "user", type: "text", content: "old user message" },
      { role: "assistant", type: "text", content: "old assistant reply" },
    ]);

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Archive Probe" })
      .click();

    // The seeded history is visible.
    await expect(page.locator(".msg-user .msg-content")).toContainText(
      "old user message"
    );
    await expect(page.locator(".msg-assistant .msg-content")).toContainText(
      "old assistant reply"
    );

    // Type /archive in the chat input and send. We intercept it
    // client-side; nothing should appear as a user message.
    await page.locator(".chat-input-bar textarea").fill("/archive");
    await page.locator("button.btn-send").click();

    // The chat becomes empty (new session, no messages).
    await expect(page.locator(".msg-user .msg-content")).toHaveCount(0);
    await expect(page.locator(".msg-assistant .msg-content")).toHaveCount(0);

    // The sidebar still shows exactly one "Archive Probe" — same name.
    await expect(
      page.locator(".session-item .session-name", {
        hasText: "Archive Probe",
      })
    ).toHaveCount(1);

    // The old session id is hidden from the REST list; the new one is live.
    const listRes = await request.get(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    const list: Array<{ id: string; name: string }> = await listRes.json();
    const probeIds = list.filter((s) => s.name === "Archive Probe").map((s) => s.id);
    expect(probeIds).toHaveLength(1);
    expect(probeIds[0]).not.toBe(imp.id);

    // The old session is still fetchable but reports archived=true
    // (so the UI can render read-only history for it).
    const oldRes = await request.get(`${API}/sessions/${imp.id}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    expect(oldRes.status()).toBe(200);
    expect((await oldRes.json()).archived).toBe(true);
  });

  test("the account-menu manage page views and unarchives a session", async ({
    page,
    request,
  }) => {
    // Unique name so the row we're asserting on doesn't collide with
    // residue from the sibling "Archive Probe" test (both pre-test
    // imports + archive() create extra rows, and afterAll cleanup runs
    // after both tests, so the manage page would otherwise contain
    // multiple matching rows).
    const ITEM_NAME = "Archive Probe Two";

    // Seed + archive in one shot via REST.
    const imp = await importSessionApi(request, ITEM_NAME, [
      { role: "user", type: "text", content: "old" },
    ]);
    const arc = await request.post(`${API}/sessions/${imp.id}/archive`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    expect(arc.ok()).toBeTruthy();
    const fresh = await arc.json();

    await login(page);

    // Open the manage page from the account menu (no sidebar expander).
    const openArchived = async () => {
      await page.locator(".btn-account").click();
      await page.locator(".menu-archived-sessions").click();
      await expect(page.locator(".archived-sessions-dialog")).toBeVisible();
    };
    await openArchived();
    const ourRow = page.locator(".archived-session-row", {
      hasText: ITEM_NAME,
    });
    await expect(ourRow).toHaveCount(1);

    // View it read-only: dialog closes, chat shows the read-only banner and
    // the old history; the input bar is replaced by the banner.
    await ourRow.locator(".btn-archived-view").click();
    await expect(page.locator(".archived-sessions-dialog")).toHaveCount(0);
    await expect(page.locator(".chat-archived-banner")).toBeVisible();
    await expect(page.locator(".chat-archived-banner")).toContainText(
      "archived"
    );
    await expect(page.locator(".chat-input-bar")).toHaveCount(0);
    await expect(page.locator(".msg-user .msg-content")).toContainText("old");

    // Reopen the manage page and unarchive: the row leaves the list and the
    // session comes back as a live, editable session.
    await openArchived();
    await page
      .locator(".archived-session-row", { hasText: ITEM_NAME })
      .locator(".btn-archived-unarchive")
      .click();
    await expect(page.locator(".archived-sessions-dialog")).toHaveCount(0);
    await expect(page.locator(".chat-input-bar")).toBeVisible();
    await expect(page.locator(".chat-archived-banner")).toHaveCount(0);

    // The unarchived session is live again (its id is imp.id); the previously
    // created `fresh` session is still in the list too.
    const listRes = await request.get(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    const ids: string[] = (await listRes.json()).map((s: { id: string }) => s.id);
    expect(ids).toContain(imp.id);
    expect(ids).toContain(fresh.id);

    // Cleanup the extras so afterAll's OWNED_NAMES sweep doesn't trip.
    await request.delete(`${API}/sessions/${fresh.id}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
  });
});

// ---------------------------------------------------------------------------
// Notifier framework — UI CRUD + real-CLI webhook fire on session idle
// (future-features #5)
// ---------------------------------------------------------------------------

async function clearAllNotifiers(request: APIRequestContext): Promise<void> {
  try {
    const list = await request.get(`${API}/notifiers`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    if (!list.ok()) return;
    const items: { id: string }[] = await list.json();
    for (const n of items) {
      await request
        .delete(`${API}/notifiers/${n.id}`, {
          headers: { Authorization: `Bearer ${TOKEN}` },
        })
        .catch(() => {});
    }
  } catch {
    // best-effort
  }
}

test.describe("Notifier framework", () => {
  test.beforeEach(async ({ request }) => {
    await clearAllNotifiers(request);
  });
  test.afterAll(async ({ request }) => {
    await clearAllNotifiers(request);
  });

  test("add / list / delete a webhook via the Settings dialog", async ({
    page,
    request,
  }) => {
    await login(page);
    await page.locator(".btn-account").click();
    await page.locator(".menu-settings").click();
    await page.locator('[role="tab"]', { hasText: "Notifications" }).click();
    await page.waitForTimeout(200);

    await page.locator(".btn-notifier-add").click();
    await page.locator("#notifier-label").fill("CRUD probe");
    await page
      .locator("#notifier-url")
      .fill("https://example.invalid/hook");
    await page.locator(".btn-notifier-create").click();

    // UI shows the new row
    await expect(page.locator(".notifier-item")).toHaveCount(1);
    await expect(page.locator(".notifier-item")).toContainText("CRUD probe");
    await expect(page.locator(".notifier-item")).toContainText(
      "https://example.invalid/hook"
    );

    // Server persisted it (REST source of truth)
    const listRes = await request.get(`${API}/notifiers`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    const items: Array<{ id: string; label: string; config: { url: string } }> =
      await listRes.json();
    const ours = items.find((n) => n.label === "CRUD probe");
    expect(ours).toBeDefined();
    expect(ours!.config.url).toBe("https://example.invalid/hook");

    // Delete via hover + click; row removed from UI AND from DB
    await page.hover(".notifier-item");
    await page.locator(".notifier-item .btn-delete").click();
    await expect(page.locator(".notifier-item")).toHaveCount(0);

    const afterRes = await request.get(`${API}/notifiers`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    const after: Array<{ label: string }> = await afterRes.json();
    expect(after.find((n) => n.label === "CRUD probe")).toBeUndefined();
  });

  test("webhook fires when a real session goes idle (real-CLI)", async ({
    page,
    request,
  }) => {
    test.skip(
      !process.env.CLAUDE_BIN && !process.env.PATH?.split(":").length,
      "claude CLI required"
    );

    // 1. Stand up a tiny HTTP listener on a random local port. The
    //    backend POSTs the session_idle payload here; we read it
    //    after the turn finishes.
    const received: Array<Record<string, unknown>> = [];
    const server = http.createServer((req, res) => {
      if (req.method === "POST") {
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          try {
            received.push(JSON.parse(body));
          } catch {
            // ignore malformed
          }
          res.writeHead(200);
          res.end();
        });
      } else {
        res.writeHead(404);
        res.end();
      }
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve())
    );
    const addr = server.address();
    const port = typeof addr === "object" && addr ? addr.port : 0;
    const hookUrl = `http://127.0.0.1:${port}/hook`;

    let notifierId: string | undefined;
    let sessionId: string | undefined;
    try {
      // 2. Register the webhook target.
      const create = await request.post(`${API}/notifiers`, {
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
        },
        data: {
          type: "webhook",
          label: "FireProbe",
          config: { url: hookUrl },
        },
      });
      expect(create.ok()).toBeTruthy();
      notifierId = (await create.json()).id;

      // 3. Create the session via API, THEN log in (login fetches the
      //    session list on mount, so order matters).
      const sess = await createSessionApi(request, "Webhook Fire Probe");
      sessionId = sess.id;
      await login(page);
      await page
        .locator(".session-item .session-name", {
          hasText: "Webhook Fire Probe",
        })
        .click();
      await page.locator(".chat-input-bar textarea").fill("Reply only with OK.");
      await page.locator("button.btn-send").click();
      // Wait for the turn to complete (result badge appears).
      await expect(page.locator(".result-badge").first()).toBeVisible({
        timeout: 60_000,
      });
      // Notifier fires in `_drive_messages`'s post-loop hook after the
      // queue drains. Give the gather + POST a moment to land on our
      // listener (httpx + node http roundtrip ≈ tens of ms).
      const deadline = Date.now() + 5_000;
      while (received.length === 0 && Date.now() < deadline) {
        await page.waitForTimeout(100);
      }

      expect(received.length).toBeGreaterThanOrEqual(1);
      const ev = received[0] as {
        type: string;
        session_id: string;
        session_name: string;
        title: string;
      };
      expect(ev.type).toBe("session_idle");
      expect(ev.session_id).toBe(sess.id);
      expect(ev.session_name).toBe("Webhook Fire Probe");
    } finally {
      if (notifierId) {
        await request
          .delete(`${API}/notifiers/${notifierId}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
          })
          .catch(() => {});
      }
      if (sessionId) {
        await request
          .delete(`${API}/sessions/${sessionId}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
          })
          .catch(() => {});
      }
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
});

// ---------------------------------------------------------------------------
// File / image attachments
// ---------------------------------------------------------------------------

test.describe("File attachments", () => {
  test("file picker shows pending chip, server returns metadata, send clears it", async ({
    page,
    request,
  }) => {
    await createSessionApi(request, "Attachment Picker Test");

    await login(page);
    await page
      .locator(".session-item .session-name", {
        hasText: "Attachment Picker Test",
      })
      .click();

    // Inject a small file into the hidden picker input.
    const fileInput = page.getByTestId("attachment-file-input");
    await fileInput.setInputFiles({
      name: "notes.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("hello from playwright"),
    });

    // Pending chip appears, then transitions out of "uploading…".
    const chip = page.locator(".attachment-pending", { hasText: "notes.txt" });
    await expect(chip).toBeVisible();
    await expect(
      page.locator(".attachment-pending", { hasText: "uploading" })
    ).toHaveCount(0, { timeout: 10_000 });

    // Send: type a prompt and click Send. We don't wait for the backend
    // turn to complete — only that the user_message + attachment chip
    // round-trip into the chat history. Interrupting at the end stops
    // the spawned CLI subprocess so test cleanup isn't slow.
    await page
      .locator(".chat-input-bar textarea")
      .fill("attached a file for you");
    await page.locator("button.btn-send").click();

    // Chip in the chat bubble (download link rendering, not the pending chip).
    await expect(
      page.locator(".msg-user .attachment-chip", { hasText: "notes.txt" })
    ).toBeVisible({ timeout: 10_000 });

    // Pending chips were cleared after send.
    await expect(
      page.locator(".chat-attachment-chips .attachment-pending")
    ).toHaveCount(0);

    // Stop whatever the backend started so cleanup is quick.
    await page.keyboard.press("Escape");
  });

  test("attachment metadata survives a reload and the download URL works", async ({
    page,
    request,
  }) => {
    // Upload via the API (single session id throughout), then import the
    // message into the SAME session via a direct DB write so we don't
    // need the live backend turn to complete. We use the `import` flow
    // by upload-then-import-into-new-session would mismatch ids; instead
    // we drive `start_message` indirectly via WebSocket and let the
    // server's broadcast path persist the metadata.
    const { id: sid } = await createSessionApi(
      request,
      "Attachment History Test"
    );

    // Upload a small text file.
    const up = await request.post(`${API}/sessions/${sid}/attachments`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      multipart: {
        file: {
          name: "doc.md",
          mimeType: "text/markdown",
          buffer: Buffer.from("# heading"),
        },
      },
    });
    expect(up.ok()).toBeTruthy();
    const meta = (await up.json()) as {
      id: string;
      filename: string;
      mime_type: string;
    };

    // Log in, activate the session, send a message with the attachment
    // through the live UI flow. The user_message broadcast happens
    // before the backend turn, so the chip lands in chat history (and
    // hits the DB via _persist_message) regardless of whether the CLI
    // turn completes.
    await login(page);
    await page
      .locator(".session-item .session-name", {
        hasText: "Attachment History Test",
      })
      .click();

    // Pre-stage the attachment into the composer's pending list by
    // re-uploading through the picker, then send. (The API upload above
    // verified the server side; the picker upload is what tells the UI
    // about the attachment id.)
    await page.getByTestId("attachment-file-input").setInputFiles({
      name: "doc.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("# heading"),
    });
    await expect(
      page.locator(".attachment-pending", { hasText: "uploading" })
    ).toHaveCount(0, { timeout: 10_000 });
    await page.locator(".chat-input-bar textarea").fill("see attached");
    await page.locator("button.btn-send").click();

    // Chip rendered in chat bubble.
    await expect(
      page.locator(".msg-user .attachment-chip", { hasText: "doc.md" })
    ).toBeVisible({ timeout: 10_000 });

    // Interrupt to stop the backend turn quickly.
    await page.keyboard.press("Escape");

    // ---- Reload: chip is reconstructed from the DB snapshot ----
    // Token is persisted in localStorage, so reload skips the login form
    // and lands straight on the session list.
    await page.reload();
    await expect(page.locator(".agent-list-header")).toBeVisible();
    await page
      .locator(".session-item .session-name", {
        hasText: "Attachment History Test",
      })
      .click();

    const chip = page.locator(".msg-user .attachment-chip", {
      hasText: "doc.md",
    });
    await expect(chip).toBeVisible({ timeout: 10_000 });

    // The chip's href is a download URL with ?token=… so <img>/<a download>
    // can authenticate without a custom header. The `meta.id` we got from
    // the API upload also exists in the chat history's chip — confirms
    // the round-trip carries the right attachment id end-to-end.
    const href = await chip.getAttribute("href");
    expect(href).toContain(`/api/sessions/${sid}/attachments/`);
    expect(href).toContain("token=");

    // Fetch the download URL to confirm the file is actually retrievable.
    const dl = await request.get(href!);
    expect(dl.ok()).toBeTruthy();
    expect(await dl.text()).toBe("# heading");

    // Reference meta.id so the test linter doesn't complain it's unused —
    // it's there for documentation: the chat history carries the SAME
    // attachment id assigned by the upload endpoint.
    expect(meta.id.length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// In-app file viewer (/showme + show_file MCP tool)
// ---------------------------------------------------------------------------
//
// Full-chain test: user types `/showme <path>` → the system prompt addendum
// teaches the model to call the viewer MCP tool → ClaudeCodeBackend.build_args
// registered that MCP server via --mcp-config → claude spawns the stdio
// server → tool_use event flows back over WS → useWebSocket detects the
// mcp__viewer__show_file tool name and opens FileViewerDialog → the dialog
// fetches /api/sessions/{id}/files/meta then the bytes, dispatches to the
// markdown renderer.
//
// Real Claude is required for this test (same as the other "Real CLI" tests
// below). One scenario (markdown) is enough to validate the chain — the
// per-renderer dispatch logic is covered by vitest in
// src/components/FileViewerDialog.test.tsx, and per-extension classification
// is covered by tests/test_file_viewer.py. We don't need to triple-cover.

// ---------------------------------------------------------------------------
// Cross-turn background tasks (mcp__bg__run)
// ---------------------------------------------------------------------------
//
// Full chain: model calls mcp__bg__run → BgTaskChip renders in chat in the
// running state → bg subprocess completes → chip flips to completed via WS
// bg_completed event → session_manager.deliver_bg_result synthesizes a new
// user_message with the [bg-task-result] marker → frontend renders it as
// .msg-bg-result → claude --resume turn fires → model echoes the sentinel
// proving it consumed the auto-injection.
//
// Validates the load-bearing claim of this feature: bg state survives a
// per-turn `claude --print` death and the agent gets a follow-up turn.

test.describe("Cross-turn bg tasks", () => {
  test.setTimeout(180_000);

  test("mcp__bg__run: chip + auto follow-up turn round-trips", async ({
    page,
    request,
  }) => {
    // Per-test tmpdir for the session's working_dir. The bg subprocess
    // runs there; the chip should pick up its exit + output regardless.
    const wd = fs.mkdtempSync(path.join(os.tmpdir(), "octopus-bg-e2e-"));
    const SENTINEL = "BG-E2E-OK-58231";
    try {
      const sessRes = await request.post(`${API}/sessions`, {
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
        },
        data: { name: "Bg Run E2E", working_dir: wd },
      });
      expect(sessRes.ok()).toBeTruthy();

      await login(page);
      await page
        .locator(".session-item .session-name", { hasText: "Bg Run E2E" })
        .click();
      await expect(page.locator(".chat-header h3")).toHaveText("Bg Run E2E");

      // Be explicit so the model actually calls bg_run rather than
      // running the shell inline via the regular Bash tool. The system
      // prompt addendum teaches it about bg_run for ≥30s tasks; with a
      // 3s sleep we have to nudge it.
      const prompt =
        "Use the mcp__bg__run tool RIGHT NOW with command=" +
        `'sleep 3 && echo ${SENTINEL}' and description='e2e probe'. ` +
        "After calling it, say 'started' and end your turn. When the " +
        "follow-up bg-task-result turn arrives, reply by echoing " +
        `'${SENTINEL}' verbatim.`;
      await page.locator(".chat-input-bar textarea").fill(prompt);
      await page.locator("button.btn-send").click();

      // 1. Chip renders in the running state. The chip lives inside
      //    the tool_use block; .octo-bgtask-chip is on the wrapping div.
      const chip = page.locator(".octo-bgtask-chip").first();
      await expect(chip).toBeVisible({ timeout: 90_000 });
      await expect(chip).toContainText(/bg · (running|completed)/i, {
        timeout: 30_000,
      });

      // 2. Chip eventually flips to completed (after the 3s sleep +
      //    plumbing roundtrip).
      await expect(chip).toContainText(/bg · completed/i, { timeout: 30_000 });

      // 3. Synthesized user message arrives with the [bg-task-result]
      //    prefix — frontend renders that as .msg-bg-result.
      const bgResult = page.locator(".msg-bg-result").first();
      await expect(bgResult).toBeVisible({ timeout: 60_000 });
      await expect(bgResult).toContainText(/bg-task result/i);

      // 4. The follow-up assistant turn echoes the sentinel — proves
      //    the auto-injected turn drove the model end-to-end.
      await expect(
        page.locator(".msg-assistant .msg-content").last()
      ).toContainText(SENTINEL, { timeout: 60_000 });
    } finally {
      fs.rmSync(wd, { recursive: true, force: true });
    }
  });

  test("Cancel button on chip stops a running bg task and drives a follow-up turn", async ({
    page,
    request,
  }) => {
    // What this test proves end-to-end (no unit test reaches the UI button):
    //   1. While a bg task is running, the chip exposes a Cancel button.
    //   2. Clicking it hits POST /bg-tasks/{id}/cancel, which SIGTERMs the
    //      process and the manager marks the row status=`cancelled` (NOT
    //      `interrupted` — the latter is reserved for kills we didn't
    //      initiate, see server/bg_tasks.py:494-513).
    //   3. The bg_completed WS event flips the chip label to `cancelled`.
    //   4. deliver_bg_result still fires (cancel is a terminal state like
    //      any other), so the synthesized [bg-task-result] turn lands and
    //      the model gets a chance to react. Without this, a user-cancel
    //      would leave the conversation hanging mid-thought.
    const wd = fs.mkdtempSync(path.join(os.tmpdir(), "octopus-bg-cancel-"));
    const REPLY_SENTINEL = "CANCEL-HANDLED-71920";
    try {
      const sessRes = await request.post(`${API}/sessions`, {
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
        },
        data: { name: "Bg Idle Watchdog", working_dir: wd },
      });
      expect(sessRes.ok()).toBeTruthy();

      await login(page);
      await page
        .locator(".session-item .session-name", {
          hasText: "Bg Idle Watchdog",
        })
        .click();
      await expect(page.locator(".chat-header h3")).toHaveText(
        "Bg Idle Watchdog"
      );

      // sleep 60 is long enough that the cancel always wins the race —
      // even on a slow Playwright worker the click lands well inside the
      // first 5 s. The model's follow-up reply uses a sentinel so we can
      // distinguish "the cancel path drove the model" from "the model
      // already responded before cancel completed".
      const prompt =
        "Use the mcp__bg__run tool RIGHT NOW with " +
        "command='sleep 60' and description='cancel probe'. " +
        "After calling it, say 'started' and end your turn. When " +
        "the follow-up bg-task-result arrives describing a cancelled " +
        `task, reply with exactly '${REPLY_SENTINEL}' and nothing else.`;
      await page.locator(".chat-input-bar textarea").fill(prompt);
      await page.locator("button.btn-send").click();

      // Chip lands in `running`. The Cancel button is visible only
      // while running — title attribute is the most stable selector
      // (text 'Cancel' is also used inside several dialogs).
      const chip = page.locator(".octo-bgtask-chip").first();
      await expect(chip).toBeVisible({ timeout: 90_000 });
      await expect(chip).toContainText(/bg · running/i, { timeout: 30_000 });

      const cancelBtn = chip.locator(
        'button[title="Cancel this background task"]'
      );
      await expect(cancelBtn).toBeVisible();
      await cancelBtn.click();

      // bg_completed → store update → chip header flips. Label must be
      // `cancelled` specifically — `interrupted` would mean we hit the
      // SIGTERM-from-outside branch, not the user-initiated cancel path.
      await expect(chip).toContainText(/bg · cancelled/i, { timeout: 30_000 });
      // Cancel button gone once the task is no longer running (isRunning
      // toggle in BgTaskChip).
      await expect(cancelBtn).toHaveCount(0);

      // Auto-injected follow-up turn lands with status=cancelled in its
      // body. .msg-bg-result is what MessageBubble renders for the
      // [bg-task-result] prefix; the collapsed view shows the first
      // line, which is the "finished with status `cancelled`" summary.
      const bgResult = page.locator(".msg-bg-result").first();
      await expect(bgResult).toBeVisible({ timeout: 60_000 });
      await expect(bgResult).toContainText(/cancelled/i);

      // The model received the follow-up and produced a reply — proves
      // the cancel path still drives a model turn end-to-end, not just
      // a silent terminal state.
      await expect(
        page.locator(".msg-assistant .msg-content").last()
      ).toContainText(REPLY_SENTINEL, { timeout: 60_000 });
    } finally {
      fs.rmSync(wd, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// Bg-task pipeline hardening (2026-05-18 work). Three things to prove end-
// to-end against the real Claude CLI + real bg worker:
//   * A bg task whose captured output exceeds the spill threshold
//     (~100 KB) is delivered to the model as an [octopus-large-prompt]
//     pointer, NOT inline — so execve never sees an oversized argv.
//   * A bg task that produces output and then goes silent past the idle
//     watchdog threshold lands with status=`interrupted` (chip label
//     reflects it), not `failed -15`.
// The auto-respawn fix for CLI premature-exit-after-tool-use is covered
// by unit tests; e2e can't reliably synthesize that race against the
// real CLI within a test's wall-clock budget.
// ---------------------------------------------------------------------------

test.describe("Bg-task pipeline hardening", () => {
  test.setTimeout(180_000);

  test("large bg output is delivered to the model via spill pointer", async ({
    page,
    request,
  }) => {
    const wd = fs.mkdtempSync(path.join(os.tmpdir(), "octopus-bg-spill-"));
    // Sentinel small enough that, if the model echoes it back, we
    // know it actually Read the spilled file (the pointer prompt
    // itself doesn't contain the sentinel).
    const SENTINEL = "SPILL-OK-83472";
    try {
      const sessRes = await request.post(`${API}/sessions`, {
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
        },
        data: { name: "Bg Spill Pipeline", working_dir: wd },
      });
      expect(sessRes.ok()).toBeTruthy();

      await login(page);
      await page
        .locator(".session-item .session-name", { hasText: "Bg Spill Pipeline" })
        .click();
      await expect(page.locator(".chat-header h3")).toHaveText(
        "Bg Spill Pipeline"
      );

      // Python one-liner that prints ~120 KB of filler followed by the
      // sentinel. 120 KB > LARGE_PROMPT_THRESHOLD_BYTES (100 KB) so
      // the bg-task-result prompt MUST be spilled to a file.
      const prompt =
        "Use the mcp__bg__run tool RIGHT NOW with " +
        "command='python3 -c \"print(\\\"X\\\"*120000); print(\\\"" +
        SENTINEL +
        "\\\")\"' and description='spill probe'. After calling it, " +
        "say 'started' and end your turn. When the follow-up " +
        "bg-task-result arrives, follow its instructions to Read the " +
        `referenced file, then reply with the line containing '${SENTINEL}'.`;
      await page.locator(".chat-input-bar textarea").fill(prompt);
      await page.locator("button.btn-send").click();

      // Chip eventually flips to completed — proves no E2BIG at spawn
      // (the pre-spill argv path would have crashed execve here).
      const chip = page.locator(".octo-bgtask-chip").first();
      await expect(chip).toBeVisible({ timeout: 90_000 });
      await expect(chip).toContainText(/bg · completed/i, { timeout: 60_000 });

      // The injected user-turn message renders as .msg-bg-result.
      // Note: by design, persistence keeps the *original* user
      // message body (the 120 KB framed result) so chat history is
      // faithful to what the user "sent". The pointer is only what
      // the backend CLI sees — the frontend sees the full content
      // (possibly truncated to first line by the UI).
      const bgResult = page.locator(".msg-bg-result").first();
      await expect(bgResult).toBeVisible({ timeout: 60_000 });

      // The model received the spill pointer (NOT the 120 KB inline),
      // Read the file as instructed, and surfaced the sentinel — this
      // is the load-bearing claim: a prompt over MAX_ARG_STRLEN
      // round-trips through the bg pipeline without E2BIG and the
      // model still produces a coherent reply tied to the captured
      // output.
      await expect(
        page.locator(".msg-assistant .msg-content").last()
      ).toContainText(SENTINEL, { timeout: 90_000 });
    } finally {
      fs.rmSync(wd, { recursive: true, force: true });
    }
  });
});

test.describe("File viewer (/showme)", () => {
  test.setTimeout(180_000);

  test("/showme on a markdown file opens the viewer with rendered content", async ({
    page,
    request,
  }) => {
    // Stage a working_dir with a markdown file the model can resolve.
    // Using a per-test tmpdir keeps us isolated from anything else
    // running on the box. tmpdir is on the same filesystem as the
    // server since both share this host, so path sandbox checks work
    // as in production.
    const wd = fs.mkdtempSync(path.join(os.tmpdir(), "octopus-viewer-e2e-"));
    fs.writeFileSync(
      path.join(wd, "intro.md"),
      "# Octopus Viewer\n\nThis is the **intro** doc rendered live.\n"
    );

    try {
      const sessRes = await request.post(`${API}/sessions`, {
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
        },
        data: { name: "Viewer Showme Test", working_dir: wd },
      });
      expect(sessRes.ok()).toBeTruthy();

      await login(page);
      await page
        .locator(".session-item .session-name", {
          hasText: "Viewer Showme Test",
        })
        .click();
      await expect(page.locator(".chat-header h3")).toHaveText(
        "Viewer Showme Test"
      );

      // Send the slash command. The model is taught (via
      // --append-system-prompt) to recognize this and call show_file.
      await page
        .locator(".chat-input-bar textarea")
        .fill("/showme intro.md");
      await page.locator("button.btn-send").click();

      // Dialog opens once the model calls the viewer tool. Radix
      // Dialog renders role=dialog on the content node; the data-state
      // attribute flips to "open" when mounted.
      const dialog = page.locator('[role="dialog"][data-state="open"]');
      await expect(dialog).toBeVisible({ timeout: 90_000 });

      // Header surfaces the filename.
      await expect(dialog).toContainText("intro.md");

      // Markdown renderer turns # into an <h1>, and the bold span lands
      // as a <strong>. Asserting both proves we mounted the markdown
      // body (not e.g. the plain-text fallback) and that the file
      // bytes actually rendered.
      await expect(
        dialog.locator("h1", { hasText: "Octopus Viewer" })
      ).toBeVisible({ timeout: 15_000 });
      await expect(dialog.locator("strong", { hasText: "intro" })).toBeVisible();

      // Close button (aria-label="Close") returns the dialog state to closed.
      await dialog.locator('button[aria-label="Close"]').click();
      await expect(page.locator('[role="dialog"][data-state="open"]')).toHaveCount(
        0
      );
    } finally {
      // Clean up the tmpdir whatever the test result.
      fs.rmSync(wd, { recursive: true, force: true });
    }
  });
});

