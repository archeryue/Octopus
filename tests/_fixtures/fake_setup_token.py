#!/usr/bin/env python3
"""Fake `claude setup-token` for OAuthLoginManager tests.

Mimics enough of the real CLI's behavior to exercise the orchestrator
without depending on the network or the real claude binary:

  1. Prints an authorize URL on stdout.
  2. Reads a line from stdin (the user's pasted code).
  3. Prints the resulting token on stdout.
  4. Exits 0.

Behavior modes (first argv):
  ok          — default; emits URL, reads code, emits token, exits 0
  no-url      — exits before printing a URL (simulates spawn failure)
  bad-code    — reads code; if code != "good-code", emits an error and
                exits 1; otherwise emits token
  hang-token  — emits URL + reads code, then sleeps forever without
                emitting a token (simulates a hung subprocess)
"""

import sys
import time


URL = "https://claude.ai/oauth/authorize?client_id=fake&state=abc&code_challenge=xyz"
TOKEN = "sk-ant-fake-12345-abcdefghijklmnopqrst"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

    if mode == "no-url":
        sys.stderr.write("oauth init failed\n")
        sys.stderr.flush()
        return 2

    sys.stdout.write(f"Open this URL in your browser to log in:\n  {URL}\n\nPaste code here: ")
    sys.stdout.flush()

    if mode == "ok" or mode == "bad-code" or mode == "hang-token":
        code = sys.stdin.readline().strip()
        if mode == "bad-code" and code != "good-code":
            sys.stdout.write("\nInvalid code — token exchange failed.\n")
            sys.stdout.flush()
            return 1
        if mode == "hang-token":
            # Read code but never emit a token; sleep until killed.
            time.sleep(60)
            return 0
        sys.stdout.write(f"\nYour token: {TOKEN}\nExport it as ANTHROPIC_API_KEY.\n")
        sys.stdout.flush()
        return 0

    sys.stderr.write(f"unknown mode: {mode}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
