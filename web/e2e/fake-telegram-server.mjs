/**
 * Fake Telegram Bot API server for E2E testing.
 *
 * Bot API routes (called by TelegramBridge):
 *   POST /bot<token>/getMe          - bot identity
 *   GET  /bot<token>/getUpdates     - queued updates (waits up to 2s if empty)
 *   POST /bot<token>/sendMessage    - records the call
 *   POST /bot<token>/sendChatAction - no-op success
 *   POST /bot<token>/answerCallbackQuery    - no-op success
 *   POST /bot<token>/editMessageReplyMarkup - no-op success
 *
 * Control routes (called by the test):
 *   POST   /control/push-update  - add update to getUpdates queue
 *   GET    /control/messages     - get all recorded sendMessage calls
 *   DELETE /control/reset        - clear all state
 */

import { createServer } from "node:http";

const PORT = 9999;

let updateQueue = [];
let updateIdCounter = 1;
let sentMessages = [];
let pendingPolls = []; // waiting getUpdates requests

function resetState() {
  updateQueue = [];
  sentMessages = [];
  updateIdCounter = 1;
  // Resolve any pending polls with empty result
  for (const resolve of pendingPolls) {
    resolve({ ok: true, result: [] });
  }
  pendingPolls = [];
}

function readBody(req) {
  return new Promise((resolve) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      try {
        resolve(JSON.parse(body));
      } catch {
        resolve({});
      }
    });
  });
}

function respond(res, data, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;

  // --- Control routes ---
  if (path === "/control/push-update" && req.method === "POST") {
    const body = await readBody(req);
    const update = {
      update_id: updateIdCounter++,
      message: {
        message_id: updateIdCounter,
        from: { id: body.chat_id || 12345, is_bot: false, first_name: "Test" },
        chat: { id: body.chat_id || 12345, type: "private" },
        date: Math.floor(Date.now() / 1000),
        text: body.text || "",
      },
    };
    updateQueue.push(update);

    // Wake up any pending polls
    for (const resolve of pendingPolls) {
      const updates = updateQueue.splice(0);
      resolve({ ok: true, result: updates });
    }
    pendingPolls = [];

    return respond(res, { ok: true, update_id: update.update_id });
  }

  if (path === "/control/messages" && req.method === "GET") {
    return respond(res, sentMessages);
  }

  if (path === "/control/reset" && req.method === "DELETE") {
    resetState();
    return respond(res, { ok: true });
  }

  // --- Bot API routes ---
  // Match /bot<token>/<method>
  const botMatch = path.match(/^\/bot[^/]+\/(\w+)$/);
  if (!botMatch) {
    return respond(res, { error: "Not found" }, 404);
  }

  const method = botMatch[1];

  if (method === "getMe") {
    return respond(res, {
      ok: true,
      result: {
        id: 123456789,
        is_bot: true,
        first_name: "TestBot",
        username: "octopus_test_bot",
      },
    });
  }

  if (method === "getUpdates") {
    // If there are queued updates, return immediately
    if (updateQueue.length > 0) {
      const updates = updateQueue.splice(0);
      return respond(res, { ok: true, result: updates });
    }

    // Otherwise wait up to 2 seconds for an update
    const result = await new Promise((resolve) => {
      pendingPolls.push(resolve);
      setTimeout(() => {
        const idx = pendingPolls.indexOf(resolve);
        if (idx !== -1) {
          pendingPolls.splice(idx, 1);
          resolve({ ok: true, result: [] });
        }
      }, 2000);
    });

    return respond(res, result);
  }

  if (method === "sendMessage") {
    const body = await readBody(req);
    sentMessages.push({
      method: "sendMessage",
      chat_id: body.chat_id,
      text: body.text,
      parse_mode: body.parse_mode,
      reply_markup: body.reply_markup,
      timestamp: Date.now(),
    });
    return respond(res, {
      ok: true,
      result: { message_id: updateIdCounter++, chat: { id: body.chat_id } },
    });
  }

  if (
    method === "sendChatAction" ||
    method === "answerCallbackQuery" ||
    method === "editMessageReplyMarkup"
  ) {
    return respond(res, { ok: true, result: true });
  }

  // Unknown method — return success anyway
  return respond(res, { ok: true, result: {} });
});

server.listen(PORT, () => {
  console.log(`Fake Telegram Bot API running on http://localhost:${PORT}`);
});
