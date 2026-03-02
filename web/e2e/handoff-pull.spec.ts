import { test, expect } from "@playwright/test";
import { execFileSync } from "child_process";
import {
  mkdtempSync,
  mkdirSync,
  rmSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "fs";
import { join, dirname } from "path";
import { tmpdir } from "os";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const TOKEN = "changeme";
const API = "http://localhost:8000/api/sessions";
const CLI = [".venv/bin/python", "-m", "server.cli"];
const PROJECT_ROOT = join(__dirname, "..", "..");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface CliResult {
  stdout: string;
  stderr: string;
  exitCode: number;
}

function runCli(args: string[]): CliResult {
  try {
    const stdout = execFileSync(CLI[0], [...CLI.slice(1), ...args], {
      cwd: PROJECT_ROOT,
      encoding: "utf-8",
      timeout: 30_000,
    });
    return { stdout, stderr: "", exitCode: 0 };
  } catch (err: any) {
    return {
      stdout: err.stdout?.toString() ?? "",
      stderr: err.stderr?.toString() ?? "",
      exitCode: err.status ?? 1,
    };
  }
}

function writeTestJsonl(
  dir: string,
  sessionId: string,
  messages: { type: string; role: string; content: any }[]
): string {
  mkdirSync(dir, { recursive: true });

  const lines: string[] = [];
  let parentUuid: string | null = null;

  for (const msg of messages) {
    const lineUuid = crypto.randomUUID();
    lines.push(
      JSON.stringify({
        type: msg.type,
        message: { role: msg.role, content: msg.content },
        uuid: lineUuid,
        parentUuid,
        isSidechain: false,
        userType: "external",
        cwd: "/tmp",
        sessionId,
        version: "2.1.62",
        timestamp: new Date().toISOString(),
      })
    );
    parentUuid = lineUuid;
  }

  const filePath = join(dir, `${sessionId}.jsonl`);
  writeFileSync(filePath, lines.join("\n") + "\n", "utf-8");
  return filePath;
}

async function login(page: any) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".session-list-header")).toBeVisible();
}

async function getSessionByName(
  request: any,
  name: string
): Promise<{ id: string; claude_session_id: string | null } | undefined> {
  const res = await request.get(API, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  const sessions: {
    id: string;
    name: string;
    claude_session_id: string | null;
  }[] = await res.json();
  return sessions.find((s) => s.name === name);
}

// ---------------------------------------------------------------------------
// Temp directory management
// ---------------------------------------------------------------------------

const tempDirs: string[] = [];

function makeTempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "octopus-e2e-"));
  tempDirs.push(dir);
  return dir;
}

// ---------------------------------------------------------------------------
// Cleanup: delete all sessions + temp dirs after suite
// ---------------------------------------------------------------------------

test.afterAll(async ({ request }) => {
  // Delete all sessions via API (best-effort, ignore errors from active sessions)
  try {
    const res = await request.get(API, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      timeout: 5_000,
    });
    if (res.ok()) {
      const sessions: { id: string }[] = await res.json();
      for (const s of sessions) {
        try {
          await request.delete(`${API}/${s.id}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
            timeout: 3_000,
          });
        } catch {
          // Session might be running or already deleted by another worker
        }
      }
    }
  } catch {
    // Server might be unavailable, ignore
  }

  // Remove temp directories
  for (const dir of tempDirs) {
    try {
      rmSync(dir, { recursive: true, force: true });
    } catch {
      // ignore cleanup errors
    }
  }
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe("Handoff & Pull CLI", () => {
  test("handoff — imports a local JSONL session into the web UI", async ({
    page,
    request,
  }) => {
    const tmpDir = makeTempDir();
    const sessionId = crypto.randomUUID();

    writeTestJsonl(tmpDir, sessionId, [
      { type: "user", role: "user", content: "Hello from handoff test" },
      {
        type: "assistant",
        role: "assistant",
        content: [{ type: "text", text: "Hi there! This is the assistant." }],
      },
    ]);

    const result = runCli([
      "handoff",
      "--session-id",
      sessionId,
      "--project-dir",
      tmpDir,
      "--name",
      "Handoff E2E",
      "--token",
      TOKEN,
    ]);

    expect(result.exitCode).toBe(0);
    expect(result.stdout).toContain("Session imported:");

    // Verify in web UI
    await login(page);

    await expect(
      page.locator(".session-item .session-name", { hasText: "Handoff E2E" })
    ).toBeVisible();

    // Click into the session
    await page
      .locator(".session-item .session-name", { hasText: "Handoff E2E" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText("Handoff E2E");

    // Verify messages
    await expect(page.locator(".msg-user .msg-content")).toContainText(
      "Hello from handoff test"
    );
    await expect(page.locator(".msg-assistant .msg-content")).toContainText(
      "Hi there! This is the assistant."
    );
  });

  test("pull — generates UUID when no claude_session_id", async ({
    request,
  }) => {
    // Create a session via API (no messages sent, so no claude_session_id)
    const createRes = await request.post(API, {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      data: { name: "Pull No Claude ID" },
    });
    expect(createRes.ok()).toBeTruthy();
    const created = await createRes.json();

    const tmpDir = makeTempDir();

    const result = runCli([
      "pull",
      created.id,
      "--project-dir",
      tmpDir,
      "--token",
      TOKEN,
    ]);

    expect(result.exitCode).toBe(0);
    expect(result.stdout).toContain(
      "No claude_session_id on server"
    );

    // A .jsonl file should exist in the temp dir
    const files = readdirSync(tmpDir).filter((f: string) =>
      f.endsWith(".jsonl")
    );
    expect(files.length).toBe(1);
  });

  test("pull — exports a web session with messages to local JSONL", async ({
    page,
    request,
  }) => {
    test.setTimeout(90_000);

    await login(page);

    // Create session and send a message
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("Pull Test");
    await page
      .locator('.session-create input[placeholder*="Working directory"]')
      .fill("/tmp");
    await page.locator("button.btn-create").click();
    await expect(page.locator(".chat-header h3")).toHaveText("Pull Test");

    const input = page.locator(".chat-input-bar textarea");
    await input.fill("What is 2+2? Reply with just the number.");
    await page.locator("button.btn-send").click();

    // Wait for result badge (indicates response complete)
    await expect(page.locator(".result-badge")).toBeVisible({
      timeout: 60_000,
    });

    // Get session ID via API
    const session = await getSessionByName(request, "Pull Test");
    expect(session).toBeDefined();

    const tmpDir = makeTempDir();

    const result = runCli([
      "pull",
      session!.id,
      "--project-dir",
      tmpDir,
      "--token",
      TOKEN,
    ]);

    expect(result.exitCode).toBe(0);
    expect(result.stdout).toContain("Pulled session with");

    // Read the JSONL file and verify contents
    const files = readdirSync(tmpDir).filter((f: string) =>
      f.endsWith(".jsonl")
    );
    expect(files.length).toBe(1);

    const jsonlContent = readFileSync(join(tmpDir, files[0]), "utf-8");
    const jsonlLines = jsonlContent
      .trim()
      .split("\n")
      .map((l: string) => JSON.parse(l));

    // Should have at least user + assistant lines
    const userLines = jsonlLines.filter((l: any) => l.type === "user");
    const assistantLines = jsonlLines.filter(
      (l: any) => l.type === "assistant"
    );
    expect(userLines.length).toBeGreaterThanOrEqual(1);
    expect(assistantLines.length).toBeGreaterThanOrEqual(1);

    // UUID chain: each line's parentUuid should be the previous line's uuid
    for (let i = 1; i < jsonlLines.length; i++) {
      expect(jsonlLines[i].parentUuid).toBe(jsonlLines[i - 1].uuid);
    }

    // Version should be 2.1.62
    for (const line of jsonlLines) {
      expect(line.version).toBe("2.1.62");
    }
  });

  test("roundtrip — web UI to pull to handoff preserves messages", async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);

    await login(page);

    // Create session and send a message
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("Roundtrip Source");
    await page
      .locator('.session-create input[placeholder*="Working directory"]')
      .fill("/tmp");
    await page.locator("button.btn-create").click();
    await expect(page.locator(".chat-header h3")).toHaveText(
      "Roundtrip Source"
    );

    const input = page.locator(".chat-input-bar textarea");
    await input.fill("What is 3+3? Reply with just the number.");
    await page.locator("button.btn-send").click();

    await expect(page.locator(".result-badge")).toBeVisible({
      timeout: 60_000,
    });

    // Pull to local JSONL
    const session = await getSessionByName(request, "Roundtrip Source");
    expect(session).toBeDefined();

    const tmpDir = makeTempDir();

    const pullResult = runCli([
      "pull",
      session!.id,
      "--project-dir",
      tmpDir,
      "--token",
      TOKEN,
    ]);
    expect(pullResult.exitCode).toBe(0);

    // Find the JSONL file
    const files = readdirSync(tmpDir).filter((f: string) =>
      f.endsWith(".jsonl")
    );
    expect(files.length).toBe(1);
    const jsonlSessionId = files[0].replace(".jsonl", "");

    // Handoff back as a new session
    const handoffResult = runCli([
      "handoff",
      "--session-id",
      jsonlSessionId,
      "--project-dir",
      tmpDir,
      "--name",
      "Roundtrip Re-imported",
      "--token",
      TOKEN,
    ]);
    expect(handoffResult.exitCode).toBe(0);
    expect(handoffResult.stdout).toContain("Session imported:");

    // Verify in web UI
    await page.reload();
    await expect(page.locator(".session-list-header")).toBeVisible();

    await expect(
      page.locator(".session-item .session-name", {
        hasText: "Roundtrip Re-imported",
      })
    ).toBeVisible();

    await page
      .locator(".session-item .session-name", {
        hasText: "Roundtrip Re-imported",
      })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText(
      "Roundtrip Re-imported"
    );

    // Verify original user message is present
    await expect(page.locator(".msg-user .msg-content").first()).toContainText(
      "What is 3+3?"
    );

    // Verify an assistant response exists
    await expect(
      page.locator(".msg-assistant .msg-content").first()
    ).toBeVisible();
  });

  test("cleanup — deletes sessions via API", async ({ request }) => {
    // Create 2 test sessions
    const createdIds: string[] = [];
    for (const name of ["Cleanup A", "Cleanup B"]) {
      const createRes = await request.post(API, {
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
        },
        data: { name },
      });
      const created = await createRes.json();
      createdIds.push(created.id);
    }

    // Verify they exist
    let res = await request.get(API, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    let sessions: { id: string }[] = await res.json();
    const foundIds = sessions.map((s) => s.id);
    for (const id of createdIds) {
      expect(foundIds).toContain(id);
    }

    // Delete only the sessions we created
    for (const id of createdIds) {
      const delRes = await request.delete(`${API}/${id}`, {
        headers: { Authorization: `Bearer ${TOKEN}` },
      });
      expect(delRes.status()).toBe(204);
    }

    // Verify our created sessions are gone
    res = await request.get(API, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    sessions = await res.json();
    const remainingIds = sessions.map((s) => s.id);
    for (const id of createdIds) {
      expect(remainingIds).not.toContain(id);
    }
  });
});
