import { mkdirSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { defineConfig } from "@playwright/test";

// Isolate the e2e backend's per-agent state (canonical memory/ + claude-home/)
// under a temp dir so runs never litter the developer's real
// ~/.octopus/agents (and never leave copied Claude credentials there). Removed
// in global-teardown. Exported so the teardown deletes the exact same path.
export const E2E_AGENTS_DIR = path.join(os.tmpdir(), "octopus-e2e-agents");

// Same isolation for agent-built applications (docs/plans/applications.md §9):
// the e2e backend writes app directories under here instead of the
// developer's real ~/.octopus/applications. Removed in global-teardown.
export const E2E_APPLICATIONS_DIR = path.join(
  os.tmpdir(),
  "octopus-e2e-applications",
);

// And for the metrics database. The session DB is `:memory:` per run, but the
// metrics one defaulted to `octopus-metrics.db` in the repo root — so the suite
// wrote into the developer's own metrics file and, worse, inherited the last
// run's rows: "an empty section says so rather than rendering blank" can only be
// asserted against a database that is actually empty. Removed in
// global-teardown.
export const E2E_METRICS_DB = path.join(os.tmpdir(), "octopus-e2e-metrics.db");

// And for everything accounts brought with them (multi-tenancy.md §6): the
// master key the server *creates* on first use, the per-account directory
// tree, and a research job's scratch + report. All three default into
// `~/.octopus`, which on a developer's box is the state directory of the
// install they actually run. Removed in global-teardown.
export const E2E_STATE_DIR = path.join(os.tmpdir(), "octopus-e2e-state");

// Accounts get a backend of their own (accounts.spec.ts). Claiming an install
// is a one-way door — it retires the token every other spec signs in with —
// so it cannot be done to the shared server. A second uvicorn on its own port,
// with its own everything, plus the dev server that proxies to it. Started
// fresh each run rather than reused: a leftover server from a previous run
// would already be claimed, and the spec would pass by testing nothing.
//
// They are started ONLY for a run that can select an accounts test. Two idle
// servers are not free here: with them up, the `@llm` bucket — which drives
// real `claude` processes two at a time — failed a different Chat test on each
// of two runs and passed with them gone. `test:e2e` is therefore the two
// buckets as two invocations (package.json), so the real-CLI half gets a box
// with nothing extra on it.
/** Does this invocation select only `@llm` tests? Then no accounts test can
 * run, and the two servers it needs would be pure load. Any other selection —
 * the default, `--grep-invert @llm`, a file list — may reach accounts.spec.ts,
 * so they start. Read from argv rather than an env flag because a flag can be
 * forgotten, and a forgotten one is a spec that fails for no visible reason. */
function llmOnlyRun(argv: string[]): boolean {
  if (argv.some((a) => a.startsWith("--grep-invert"))) return false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const value = a.startsWith("--grep=")
      ? a.slice("--grep=".length)
      : a.startsWith("-g=")
        ? a.slice("-g=".length)
        : a === "--grep" || a === "-g"
          ? argv[i + 1]
          : undefined;
    if (value !== undefined) return value === "@llm";
  }
  return false;
}

export const E2E_ACCOUNTS = !llmOnlyRun(process.argv.slice(2));
export const E2E_ACCOUNTS_PORT = 8766;
export const E2E_ACCOUNTS_WEB_PORT = 5175;
export const E2E_ACCOUNTS_STATE_DIR = path.join(
  os.tmpdir(),
  "octopus-e2e-accounts",
);

// Made here, at config load, because uvicorn opens its metrics database before
// anything else runs and sqlite will not create the directory around it — the
// server exits "unable to open database file" and the run dies waiting for a
// port. The shared backend avoids this only by keeping its metrics DB loose in
// the temp directory.
if (E2E_ACCOUNTS) mkdirSync(E2E_ACCOUNTS_STATE_DIR, { recursive: true });

export default defineConfig({
  testDir: "./e2e",
  globalTeardown: "./e2e/global-teardown.ts",
  timeout: 30_000,
  retries: 0,
  // Every spec drives ONE shared backend (a single uvicorn with an in-memory
  // DB) plus ONE Vite dev server, and each @llm turn spawns a real `claude`
  // process with its four MCP-server children. Playwright's default (half the
  // logical cores — 8 here) piles that many browsers, unbundled dev-server
  // page loads and concurrent model turns onto the box, and specs start
  // failing at `page.goto("/")` because the login page never finishes
  // loading. Capped so the suite is deterministic — and it costs nothing:
  // 8 workers took 4.2-4.4 min with 3 failures, 2 workers 2.6 min with none,
  // because the timeouts and worker restarts an overloaded run produces cost
  // far more than the parallelism saves.
  //
  // The same argument goes one step further for the `@llm` half, which
  // `test:e2e:llm` runs with `--workers=1`: two concurrent real-CLI turns on
  // this box time out about one run in three — a different test each time,
  // each passing alone — and serially the bucket is *faster* anyway (4.2 min
  // against 5.5). The pure-UI half keeps two workers, where they do help
  // (45 s).
  workers: 2,
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
        OCTOPUS_DB_PATH: ":memory:",
        // Not the repo's octopus-metrics.db: see E2E_METRICS_DB above.
        OCTOPUS_METRICS_DB_PATH: E2E_METRICS_DB,
        // Per-agent memory dirs (docs/plans/memory.md) live under here; keep
        // them out of the developer's real ~/.octopus/agents. Cleaned in
        // e2e/global-teardown.ts.
        OCTOPUS_AGENTS_DIR: E2E_AGENTS_DIR,
        // Applications built by the e2e suite land here, not in the
        // developer's real ~/.octopus/applications.
        OCTOPUS_APPLICATIONS_DIR: E2E_APPLICATIONS_DIR,
        // Short auto-answer window so the AskUserQuestion-timeout e2e
        // fires in seconds instead of minutes. Existing interactive
        // real-CLI tests click within a second of the form appearing,
        // well under this budget.
        OCTOPUS_ASK_USER_QUESTION_TIMEOUT_SECONDS: "12",
        // Not the developer's own `~/.octopus`: see E2E_STATE_DIR above.
        OCTOPUS_MASTER_KEY_FILE: path.join(E2E_STATE_DIR, "master.key"),
        OCTOPUS_USERS_ROOT: path.join(E2E_STATE_DIR, "users"),
        OCTOPUS_RESEARCH_DIR: path.join(E2E_STATE_DIR, "research"),
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
    // Only for a run that can select an accounts test; see E2E_ACCOUNTS.
    ...(E2E_ACCOUNTS
      ? [
          {
            command:
              "cd .. && .venv/bin/uvicorn server.main:app --host 0.0.0.0 --port " +
              `${E2E_ACCOUNTS_PORT}`,
            port: E2E_ACCOUNTS_PORT,
            reuseExistingServer: false,
            timeout: 10_000,
            env: {
              ...process.env,
              PATH: `${process.env.HOME ?? ""}/.local/bin:${process.env.PATH ?? ""}`,
              OCTOPUS_AUTH_TOKEN: "changeme",
              OCTOPUS_PORT: `${E2E_ACCOUNTS_PORT}`,
              OCTOPUS_DB_PATH: ":memory:",
              OCTOPUS_METRICS_DB_PATH: path.join(
                E2E_ACCOUNTS_STATE_DIR,
                "metrics.db",
              ),
              OCTOPUS_AGENTS_DIR: path.join(E2E_ACCOUNTS_STATE_DIR, "agents"),
              OCTOPUS_APPLICATIONS_DIR: path.join(
                E2E_ACCOUNTS_STATE_DIR,
                "applications",
              ),
              OCTOPUS_ATTACHMENTS_DIR: path.join(
                E2E_ACCOUNTS_STATE_DIR,
                "attachments",
              ),
              OCTOPUS_DEFAULT_WORKING_DIR: path.join(
                E2E_ACCOUNTS_STATE_DIR,
                "workspace",
              ),
              OCTOPUS_MASTER_KEY_FILE: path.join(
                E2E_ACCOUNTS_STATE_DIR,
                "master.key",
              ),
              OCTOPUS_USERS_ROOT: path.join(E2E_ACCOUNTS_STATE_DIR, "users"),
              OCTOPUS_RESEARCH_DIR: path.join(
                E2E_ACCOUNTS_STATE_DIR,
                "research",
              ),
            },
          },
          {
            command: `bun dev --port ${E2E_ACCOUNTS_WEB_PORT}`,
            port: E2E_ACCOUNTS_WEB_PORT,
            reuseExistingServer: true,
            timeout: 10_000,
            env: {
              ...process.env,
              OCTOPUS_API_PORT: `${E2E_ACCOUNTS_PORT}`,
              // Its own dependency-optimizer cache; see `cacheDir` in
              // vite.config.ts. Kept in node_modules rather than the run's temp
              // state, so it survives between runs instead of cold-starting each
              // time.
              VITE_CACHE_DIR: "node_modules/.vite-e2e-accounts",
            },
          },
        ]
      : []),
  ],
});
