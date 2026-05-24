import { rmSync } from "node:fs";

import { E2E_AGENTS_DIR } from "../playwright.config";

// Remove the isolated per-agent state tree the e2e backend wrote (memory/ +
// claude-home/ per test agent), so e2e runs never accumulate state — or stray
// copied Claude credentials — on disk.
export default function globalTeardown(): void {
  rmSync(E2E_AGENTS_DIR, { recursive: true, force: true });
}
