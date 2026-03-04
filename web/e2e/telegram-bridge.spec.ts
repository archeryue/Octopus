/**
 * E2E tests for the Telegram bridge.
 *
 * Uses a fake Telegram Bot API server (fake-telegram-server.mjs) started
 * by playwright.bridge.config.ts. The bridge polls the fake server for
 * updates; we push simulated Telegram messages via the control API and
 * verify responses.
 */

import { test, expect } from "@playwright/test";

const TOKEN = "changeme";
const API = "http://localhost:8000/api/sessions";
const CONTROL = "http://localhost:9999/control";
const CHAT_ID = 12345;

/** Push a simulated Telegram message from a user. */
async function pushUpdate(text: string, chatId = CHAT_ID) {
  await fetch(`${CONTROL}/push-update`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}

/** Get all sendMessage calls the bridge made to the fake Telegram API. */
async function getSentMessages(): Promise<
  { chat_id: number; text: string; parse_mode?: string }[]
> {
  const res = await fetch(`${CONTROL}/messages`);
  return res.json();
}

/** Poll until sent messages match a predicate, or timeout. */
async function pollMessages(
  predicate: (
    msgs: { chat_id: number; text: string; parse_mode?: string }[]
  ) => boolean,
  timeoutMs = 45_000
): Promise<{ chat_id: number; text: string; parse_mode?: string }[]> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const msgs = await getSentMessages();
    if (predicate(msgs)) return msgs;
    await new Promise((r) => setTimeout(r, 500));
  }
  // One final check
  const msgs = await getSentMessages();
  if (predicate(msgs)) return msgs;
  throw new Error(
    `pollMessages timed out after ${timeoutMs}ms. Messages: ${JSON.stringify(msgs)}`
  );
}

// Reset fake server state before each test
test.beforeEach(async () => {
  await fetch(`${CONTROL}/reset`, { method: "DELETE" });
});

// Clean up sessions after all tests
test.afterAll(async ({ request }) => {
  const res = await request.get(API, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  if (res.ok()) {
    const sessions: { id: string }[] = await res.json();
    for (const s of sessions) {
      await request.delete(`${API}/${s.id}`, {
        headers: { Authorization: `Bearer ${TOKEN}` },
      });
    }
  }
});

test.describe("Telegram Bridge", () => {
  test("bridge is running (health check)", async ({ request }) => {
    const res = await request.get("http://localhost:8000/health");
    expect(res.ok()).toBeTruthy();
  });

  test("/help command returns available commands", async () => {
    await pushUpdate("/help");

    const msgs = await pollMessages(
      (m) => m.some((msg) => msg.text.includes("/new"))
    );

    const helpMsg = msgs.find((m) => m.text.includes("/new"));
    expect(helpMsg).toBeDefined();
    expect(helpMsg!.text).toContain("/sessions");
    expect(helpMsg!.text).toContain("/switch");
    expect(helpMsg!.text).toContain("/help");
  });

  test("/new creates a session", async ({ request }) => {
    await pushUpdate("/new E2E Bridge Test");

    // Wait for bridge to respond with session creation confirmation
    const msgs = await pollMessages(
      (m) => m.some((msg) => msg.text.includes("E2E Bridge Test"))
    );

    const confirmMsg = msgs.find((m) => m.text.includes("E2E Bridge Test"));
    expect(confirmMsg).toBeDefined();
    expect(confirmMsg!.chat_id).toBe(CHAT_ID);

    // Verify session exists via REST API
    const sessionsRes = await request.get(API, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    const sessions: { id: string; name: string }[] =
      await sessionsRes.json();
    const bridgeSession = sessions.find((s) => s.name === "E2E Bridge Test");
    expect(bridgeSession).toBeDefined();
  });

  test("message flows to Claude and response comes back", async ({
    request,
  }) => {
    // Create a session first
    await pushUpdate("/new Claude Flow Test");
    await pollMessages(
      (m) => m.some((msg) => msg.text.includes("Claude Flow Test"))
    );

    // Reset messages so we only see the Claude response
    await fetch(`${CONTROL}/reset`, { method: "DELETE" });

    // Send a message to Claude
    await pushUpdate("What is 2+2? Reply with just the number.");

    // Wait for Claude's response — the bridge should send back
    // at least a text response and a result/done message
    const msgs = await pollMessages(
      (m) =>
        m.some(
          (msg) =>
            msg.text.includes("Done") || msg.text.includes("Error")
        ),
      45_000
    );

    // Should have received some messages back
    expect(msgs.length).toBeGreaterThan(0);

    // Verify session has messages via REST API
    const sessionsRes = await request.get(API, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    const sessions: { id: string; name: string; message_count: number }[] =
      await sessionsRes.json();
    const session = sessions.find((s) => s.name === "Claude Flow Test");
    expect(session).toBeDefined();
    expect(session!.message_count).toBeGreaterThan(0);
  });

  test("/sessions lists existing sessions", async () => {
    // Create a session first
    await pushUpdate("/new List Test Session");
    await pollMessages(
      (m) => m.some((msg) => msg.text.includes("List Test Session"))
    );
    await fetch(`${CONTROL}/reset`, { method: "DELETE" });

    // Ask for session list
    await pushUpdate("/sessions");

    const msgs = await pollMessages(
      (m) => m.some((msg) => msg.text.includes("List Test Session"))
    );

    const listMsg = msgs.find((m) => m.text.includes("List Test Session"));
    expect(listMsg).toBeDefined();
    expect(listMsg!.text).toContain("(current)");
  });
});
