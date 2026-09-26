import { rmSync } from "node:fs";

import {
  E2E_AGENTS_DIR,
  E2E_APPLICATIONS_DIR,
  E2E_METRICS_DB,
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
}
