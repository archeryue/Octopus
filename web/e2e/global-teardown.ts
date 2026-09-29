import { rmSync } from "node:fs";

import {
  E2E_AGENTS_DIR,
  E2E_APPLICATIONS_DIR,
  E2E_METRICS_DB,
  E2E_ACCOUNTS_STATE_DIR,
  E2E_STATE_DIR,
} from "../playwright.config";

// Remove the isolated per-agent state tree the e2e backend wrote (memory/ +
// claude-home/ per test agent), so e2e runs never accumulate state — or stray
// copied Claude credentials — on disk.
export default function globalTeardown(): void {
  rmSync(E2E_AGENTS_DIR, { recursive: true, force: true });
  // Same for the application directories agents built during the run
  // (docs/plans/applications.md §9).
  rmSync(E2E_APPLICATIONS_DIR, { recursive: true, force: true });
  // And the metrics database, so the next run starts with nothing recorded —
  // which is the premise one of the Monitor tests asserts against.
  for (const suffix of ["", "-wal", "-shm"]) {
    rmSync(`${E2E_METRICS_DB}${suffix}`, { force: true });
  }
  // The master key, the per-account directories and the research output the
  // run produced — none of which belong in a developer's real ~/.octopus.
  rmSync(E2E_STATE_DIR, { recursive: true, force: true });
  // Everything the accounts backend wrote: its own agents, applications,
  // workspace, master key and per-account directories.
  rmSync(E2E_ACCOUNTS_STATE_DIR, { recursive: true, force: true });
}
