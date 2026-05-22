#!/usr/bin/env python3
"""Fake `codex login --device-auth` for CodexLoginManager tests.

Prints the same URL + one-time-code shape the real codex CLI emits, then
behaves per `CODEX_FAKE_LOGIN_MODE`:

  success (default): write `auth.json` into $CODEX_HOME and exit 0 (as codex
                     does once the user authorizes in the browser).
  fail:              print an error and exit 1.
  hang:              sleep so the manager can exercise cancel().

The args (`login --device-auth`) are ignored — the mode env var drives it.
"""

import json
import os
import pathlib
import sys
import time


def main() -> int:
    # Mirror the real device-auth output (ANSI colors omitted; the scraper
    # strips them anyway).
    print("Follow these steps to sign in with ChatGPT using device code authorization:")
    print("1. Open this link in your browser and sign in to your account")
    print("   https://auth.openai.com/codex/device")
    print("2. Enter this one-time code (expires in 15 minutes)")
    print("   TEST-CODE9")
    sys.stdout.flush()

    mode = os.environ.get("CODEX_FAKE_LOGIN_MODE", "success")
    if mode == "hang":
        time.sleep(30)
        return 0
    if mode == "fail":
        time.sleep(0.05)
        print("device authorization failed")
        return 1

    # success — write auth.json into CODEX_HOME, like a completed login.
    time.sleep(0.05)
    home = os.environ.get("CODEX_HOME")
    if home:
        p = pathlib.Path(home)
        p.mkdir(parents=True, exist_ok=True)
        (p / "auth.json").write_text(
            json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "fake"}})
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
