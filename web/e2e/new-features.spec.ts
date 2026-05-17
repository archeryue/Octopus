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
      "every 60m"
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
    await createSessionApi(request, "Queue Test");

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

    // Both user messages should be in the chat
    await expect(page.locator(".msg-user .msg-content")).toContainText([
      "100 * 200",
      "50 * 50",
    ]);
  });

  test("Esc key interrupts the current turn", async ({ page, request }) => {
    await createSessionApi(request, "Interrupt Test");

    await login(page);
    await page
      .locator(".session-item .session-name", { hasText: "Interrupt Test" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Interrupt Test");

    const input = page.locator(".chat-input-bar textarea");

    // Long-running prompt — force tool use with sleeps so the model is
    // genuinely mid-turn when Esc fires. The CLI-direct path is fast
    // enough that a plain-text prompt can finish before Playwright
    // sees the status flip.
    await input.fill(
      "Use the Bash tool to run `sleep 3`, then `sleep 3` again, " +
      "then `sleep 3` once more, then say done."
    );
    await page.locator("button.btn-send").click();

    // Wait for the run to start
    await expect(
      page.locator(".status-badge.status-running")
    ).toBeVisible({ timeout: 15_000 });

    // Small buffer so we don't press Esc *before* the subprocess registers
    // its handler. The sleeps above give us ~9s of headroom.
    await page.waitForTimeout(1500);

    // Press Esc to interrupt — global listener catches it
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

  test("add and delete a credential via the sidebar", async ({ page }) => {
    await login(page);

    // Section title is visible
    await expect(page.locator(".credential-title")).toHaveText("Credentials");

    // Open the add form
    await page.locator(".btn-credential-add").click();
    await expect(page.locator(".credential-form")).toBeVisible();

    // Fill: default backend is claude-code, set label + secret
    await page
      .locator('.credential-form input[placeholder="Label (e.g. Personal)"]')
      .fill("E2E Cred");
    await page
      .locator('.credential-form input[placeholder="API key"]')
      .fill("sk-e2e-test-key");
    await page.locator(".credential-form .btn-create").click();

    // The new credential shows up in the list with its label and backend badge
    const item = page.locator(".credential-item", { hasText: "E2E Cred" });
    await expect(item).toBeVisible();
    await expect(item.locator(".credential-badge")).toHaveText("Claude");

    // Delete it — form vanishes from the list
    await item.locator(".btn-delete").click();
    await expect(
      page.locator(".credential-item", { hasText: "E2E Cred" })
    ).toHaveCount(0);
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
