import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "telegram-bridge.spec.ts",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: "http://localhost:8000",
  },
  webServer: [
    {
      command: "node e2e/fake-telegram-server.mjs",
      port: 9999,
      reuseExistingServer: true,
      timeout: 5_000,
    },
    {
      command: [
        "cd .. &&",
        "OCTOPUS_TELEGRAM_BOT_TOKEN=test-token",
        "OCTOPUS_TELEGRAM_API_BASE_URL=http://localhost:9999",
        ".venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000",
      ].join(" "),
      port: 8000,
      reuseExistingServer: true,
      timeout: 15_000,
    },
  ],
});
