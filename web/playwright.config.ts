import os from "node:os";
import path from "node:path";

import { defineConfig } from "@playwright/test";

// Isolate the e2e backend's per-agent state (canonical memory/ + claude-home/)
// under a temp dir so runs never litter the developer's real
// ~/.octopus/agents (and never leave copied Claude credentials there). Removed
// in global-teardown. Exported so the teardown deletes the exact same path.
export const E2E_AGENTS_DIR = path.join(os.tmpdir(), "octopus-e2e-agents");

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["telegram-bridge.spec.ts"],
  globalTeardown: "./e2e/global-teardown.ts",
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: "http://localhost:5174",
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
  webServer: [
    {
      command:
        "cd .. && .venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8765",
      port: 8765,
      reuseExistingServer: true,
      timeout: 10_000,
      env: {
        ...process.env,
        // The backend spawns the `claude` CLI directly via PATH lookup.
        // ~/.local/bin is the typical install location and may not be on
        // a non-interactive shell's PATH. Prepend it so the e2e server
        // can find the binary without the user having to configure shell.
        PATH: `${process.env.HOME ?? ""}/.local/bin:${process.env.PATH ?? ""}`,
        OCTOPUS_AUTH_TOKEN: "changeme",
        // Tell pydantic-settings the actual uvicorn port (matches the
        // `port: 8765` above and `--port 8765` in the command). The bg
        // MCP server reads settings.port to build OCTOPUS_API_BASE; the
        // default 8000 would have its callback POSTs hit a dead socket
        // and leave the BgTaskChip stuck in "Waiting for bg task…".
        OCTOPUS_PORT: "8765",
        OCTOPUS_TELEGRAM_BOT_TOKEN: "",
        OCTOPUS_DB_PATH: ":memory:",
        // Per-agent memory dirs (docs/plans/memory.md) live under here; keep
        // them out of the developer's real ~/.octopus/agents. Cleaned in
        // e2e/global-teardown.ts.
        OCTOPUS_AGENTS_DIR: E2E_AGENTS_DIR,
        // Short auto-answer window so the AskUserQuestion-timeout e2e
        // fires in seconds instead of minutes. Existing interactive
        // real-CLI tests click within a second of the form appearing,
        // well under this budget.
        OCTOPUS_ASK_USER_QUESTION_TIMEOUT_SECONDS: "12",
      },
    },
    {
      command: "bun dev --port 5174",
      port: 5174,
      reuseExistingServer: true,
      timeout: 10_000,
      env: {
        ...process.env,
        OCTOPUS_API_PORT: "8765",
      },
    },
  ],
});
