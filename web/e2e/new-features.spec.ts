import * as http from "node:http";
import { test, expect, type Page, type APIRequestContext } from "@playwright/test";

const TOKEN = "changeme";
const SERVER_URL = "http://localhost:8765";
const API = `${SERVER_URL}/api`;

const OWNED_NAMES = new Set([
  "Schedule UI Test",
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
  await expect(page.locator(".session-list-header")).toBeVisible();
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
  test("schedule section is hidden when no session is active", async ({ page }) => {
    await login(page);
    // ScheduleList component returns null when no activeSessionId
    await expect(page.locator(".schedule-section")).toHaveCount(0);
  });

  test("create, toggle, and delete a schedule via the sidebar", async ({
    page,
    request,
  }) => {
    await createSessionApi(request, "Schedule UI Test");

    await login(page);

    // Activate the session
    await page
      .locator(".session-item .session-name", { hasText: "Schedule UI Test" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Schedule UI Test");

    // Schedule section should now be visible
    await expect(page.locator(".schedule-section")).toBeVisible();
    await expect(page.locator(".schedule-title")).toHaveText("Schedules");

    // No schedules yet
    await expect(page.locator(".schedule-item")).toHaveCount(0);

    // Open create form
    await page.locator(".btn-schedule-add").click();
    await expect(page.locator(".schedule-form")).toBeVisible();

    // Fill (60 min = 3600s, well above the run window so it won't fire)
    await page
      .locator('.schedule-form input[placeholder="Name"]')
      .fill("Hourly Check");
    await page
      .locator('.schedule-form textarea[placeholder="Prompt..."]')
      .fill("Summarize the day");
    await page.locator(".schedule-form .interval-input").fill("60");

    // Submit (scope to schedule-form to avoid SessionList's btn-create)
    await page.locator(".schedule-form button.btn-create").click();

    // Schedule item appears
    await expect(page.locator(".schedule-item")).toHaveCount(1);
    await expect(page.locator(".schedule-item .schedule-name")).toHaveText(
      "Hourly Check"
    );
    await expect(page.locator(".schedule-item .schedule-interval")).toHaveText(
      "60m"
    );
    await expect(page.locator(".schedule-item .btn-toggle")).toHaveClass(/on/);

    // Toggle disabled
    await page.locator(".schedule-item .btn-toggle").click();
    await expect(page.locator(".schedule-item .btn-toggle")).toHaveClass(/off/);
    await expect(page.locator(".schedule-item")).toHaveClass(/disabled/);

    // Delete
    await page.locator(".schedule-item .btn-delete").click();
    await expect(page.locator(".schedule-item")).toHaveCount(0);
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

    // Wait for the run to start
    await expect(
      page.locator(".status-badge.status-running")
    ).toBeVisible({ timeout: 15_000 });

    // Send button should now read "Queue"
    await expect(page.locator("button.btn-send")).toHaveText("Queue");

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
      .map((m) => (typeof m.content === "string" ? m.content : ""));
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

    // Open the create-session form via the Sessions section's "+" button.
    await page.locator(".btn-session-add").click();

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

  test("AskUserQuestion: real model → form → answer → reply", async ({
    page,
    request,
  }) => {
    await createSessionApi(request, "Real Q Session");

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Real Q Session" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Real Q Session");

    // Nudge the model to invoke AskUserQuestion directly with our chosen
    // options, so the form is deterministic. The schema requires `description`
    // on every option, so we spell them out fully.
    const prompt =
      "Use the AskUserQuestion tool right now with this exact JSON for the questions argument: " +
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
    // response should reference "red" in some way (the model usually
    // acknowledges or restates the chosen option). We assert on the
    // answer bubble — that proves the round-trip completed (UI → WS →
    // session_manager.answer_question → backend.answer_question →
    // control_response → CLI → next event from model).
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
// Settings dialog (future-features #7)
// ---------------------------------------------------------------------------

test.describe("Settings dialog", () => {
  test("opens via gear, renders General / Account / Notifications tabs", async ({
    page,
  }) => {
    await login(page);

    await expect(page.locator(".btn-settings")).toBeVisible();
    await page.locator(".btn-settings").click();
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

    // The old session 404s now (hidden / archived).
    const oldRes = await request.get(`${API}/sessions/${imp.id}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    expect(oldRes.status()).toBe(404);
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
    await page.locator(".btn-settings").click();
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

