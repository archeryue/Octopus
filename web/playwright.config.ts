import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["telegram-bridge.spec.ts"],
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
        OCTOPUS_AUTH_TOKEN: "changeme",
        OCTOPUS_TELEGRAM_BOT_TOKEN: "",
        OCTOPUS_DB_PATH: ":memory:",
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
