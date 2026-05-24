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

type InlineButton = { text: string; callback_data?: string };
type SentMessage = {
  chat_id: number;
  text: string;
  parse_mode?: string;
  reply_markup?: { inline_keyboard?: InlineButton[][] };
};

/** Push a simulated Telegram message from a user. */
async function pushUpdate(text: string, chatId = CHAT_ID) {
  await fetch(`${CONTROL}/push-update`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}

/** Get all sendMessage calls the bridge made to the fake Telegram API. */
async function getSentMessages(): Promise<SentMessage[]> {
  const res = await fetch(`${CONTROL}/messages`);
  return res.json();
}

/** Flatten every inline-keyboard button across all sent messages. */
function allButtons(msgs: SentMessage[]): InlineButton[] {
  return msgs.flatMap((m) => m.reply_markup?.inline_keyboard?.flat() ?? []);
}

/** Poll until sent messages match a predicate, or timeout. */
async function pollMessages(
  predicate: (msgs: SentMessage[]) => boolean,
  timeoutMs = 45_000
): Promise<SentMessage[]> {
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

    // Wait for Claude's response. The chat is in quiet mode (the default),
    // so the bridge forwards only the agent's natural-language reply — the
    // "Done ($cost)" footer and tool activity are suppressed.
    const msgs = await pollMessages(
      (m) =>
        m.some((msg) => msg.text?.includes("4") || msg.text?.includes("Error")),
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

  test("/sessions lists sessions as tappable switch buttons", async () => {
    // Create a session first
    await pushUpdate("/new List Test Session");
    await pollMessages(
      (m) => m.some((msg) => msg.text.includes("List Test Session"))
    );
    await fetch(`${CONTROL}/reset`, { method: "DELETE" });

    // Ask for session list — rendered as inline buttons, one per session.
    await pushUpdate("/sessions");

    const msgs = await pollMessages((m) =>
      allButtons(m).some((b) => b.text.includes("List Test Session"))
    );

    const button = allButtons(msgs).find((b) =>
      b.text.includes("List Test Session")
    );
    expect(button).toBeDefined();
    // The just-created session is the sticky one, so it's marked current.
    expect(button!.text).toContain("✓");
    // Tapping it switches via a switch:<id> callback.
    expect(button!.callback_data).toMatch(/^switch:/);
  });

  test("/quiet and /verbose toggle confirmations", async () => {
    await pushUpdate("/verbose");
    let msgs = await pollMessages((m) =>
      m.some((msg) => msg.text.includes("Verbose mode"))
    );
    expect(msgs.find((m) => m.text.includes("Verbose mode"))).toBeDefined();

    await fetch(`${CONTROL}/reset`, { method: "DELETE" });

    await pushUpdate("/quiet");
    msgs = await pollMessages((m) =>
      m.some((msg) => msg.text.includes("Quiet mode"))
    );
    expect(msgs.find((m) => m.text.includes("Quiet mode"))).toBeDefined();
  });
});
