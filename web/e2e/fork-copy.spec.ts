import { test, expect, type Page, type APIRequestContext } from "@playwright/test";
import { mkdtempSync, writeFileSync, rmSync, existsSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

// Pure-UI e2e for the /fork copy-dir duplicate (session-fork-copy.md). No real
// LLM turn: we import a parent session pointing at a SMALL temp working dir,
// run `/fork copy`, and assert a NEW session appears + becomes active with the
// "full copy" banner while the ORIGINAL session stays put (not archived).
//
// /fork triggers a real backend copytree into ~/.octopus/fork/, so afterAll
// deletes both sessions and removes the copied + source dirs.

const TOKEN = "changeme";
const API = "http://localhost:8765/api";
const OWNED = new Set(["Fork-Copy E2E Parent", "copy"]);
const cleanupDirs: string[] = [];

test.afterAll(async ({ request }) => {
  try {
    const res = await request.get(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      params: { include_archived: "true" },
      timeout: 5_000,
    });
    if (res.ok()) {
      const sessions: { id: string; name: string; working_dir: string }[] =
        await res.json();
      for (const s of sessions) {
        if (!OWNED.has(s.name)) continue;
        if (s.working_dir?.includes("/.octopus/fork/")) cleanupDirs.push(s.working_dir);
        await request
          .delete(`${API}/sessions/${s.id}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
            timeout: 3_000,
          })
          .catch(() => {});
      }
    }
  } catch {
    /* best-effort */
  }
  for (const d of cleanupDirs) {
    try {
      if (existsSync(d)) rmSync(d, { recursive: true, force: true });
    } catch {
      /* best-effort */
    }
  }
});

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

async function importParent(
  request: APIRequestContext,
  workingDir: string
): Promise<{ id: string }> {
  const res = await request.post(`${API}/sessions/import`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: {
      name: "Fork-Copy E2E Parent",
      working_dir: workingDir,
      messages: [
        { role: "user", type: "text", content: "first question" },
        { role: "assistant", type: "text", content: "first answer" },
      ],
    },
  });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

test("/fork duplicates onto a copied dir; the original session stays", async ({
  page,
  request,
}) => {
  // A small, real working dir so the backend copytree is cheap and safe.
  const srcDir = mkdtempSync(join(tmpdir(), "octo-forkcopy-"));
  cleanupDirs.push(srcDir);
  writeFileSync(join(srcDir, "hello.txt"), "original\n");

  await importParent(request, srcDir);
  await login(page);

  await page
    .locator(".session-item .session-name", { hasText: "Fork-Copy E2E Parent" })
    .click();
  await expect(page.locator(".chat-header h3")).toHaveText("Fork-Copy E2E Parent");

  // Type "/fork copy" and send. With a space after the command the slash menu
  // hides, so a single Enter sends the line.
  const composer = page.locator(".chat-input-bar textarea");
  await composer.fill("/fork copy");
  await composer.press("Enter");

  // The new fork opens with the "full copy" banner (no "@message N" badge).
  await expect(page.locator('[data-testid="fork-banner"]')).toBeVisible();
  await expect(page.locator('[data-testid="fork-banner"]')).toContainText(
    "full copy of the working dir"
  );
  await expect(page.locator(".chat-header h3")).toHaveText("copy");

  // The ORIGINAL parent is untouched — still listed alongside the fork.
  // (Exact matches: "copy" is a substring of "Fork-Copy E2E Parent", so a
  // loose hasText would match both and trip strict mode.)
  await expect(
    page.locator(".session-item .session-name").filter({ hasText: "Fork-Copy E2E Parent" })
  ).toBeVisible();
  await expect(
    page.locator(".session-item .session-name").getByText("copy", { exact: true })
  ).toBeVisible();
});
