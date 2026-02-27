import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: "http://localhost:5173",
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
        "cd .. && .venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000",
      port: 8000,
      reuseExistingServer: true,
      timeout: 10_000,
    },
    {
      command: "bun dev --port 5173",
      port: 5173,
      reuseExistingServer: true,
      timeout: 10_000,
    },
  ],
});
