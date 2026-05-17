#!/usr/bin/env python3
"""Tiny fake CLI for SubprocessJsonlBackend tests.

Behavior controlled by argv. Used by tests/test_backends_subprocess.py so we
don't depend on a real `claude` binary being installed in the test env.

Supported modes (first argv after the script name):
  emit-lines   : Print the following argv items as separate stdout lines, flush, exit.
  echo-stdin   : Read each stdin line, echo it back to stdout prefixed with "echo:". Exit on EOF.
  fail-exit    : Print one line, then exit with non-zero status.
  bad-json     : Print "{not json", then "{\"type\":\"good\"}", then exit.
  sleep-then   : Sleep N seconds (argv[2]), then print "{\"type\":\"woke\"}", then exit.
"""

import json
import sys
import time


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "emit-lines"

    if mode == "emit-lines":
        for line in sys.argv[2:]:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        return 0

    if mode == "echo-stdin":
        for line in sys.stdin:
            line = line.rstrip("\n")
            payload = {"type": "echo", "content": line}
            sys.stdout.write(json.dumps(payload) + "\n")
            sys.stdout.flush()
        return 0

    if mode == "fail-exit":
        sys.stdout.write(json.dumps({"type": "before-exit"}) + "\n")
        sys.stdout.flush()
        sys.stderr.write("boom\n")
        sys.stderr.flush()
        return 3

    if mode == "bad-json":
        sys.stdout.write("{not json\n")
        sys.stdout.write(json.dumps({"type": "good"}) + "\n")
        sys.stdout.flush()
        return 0

    if mode == "sleep-then":
        time.sleep(float(sys.argv[2]))
        sys.stdout.write(json.dumps({"type": "woke"}) + "\n")
        sys.stdout.flush()
        return 0

    sys.stderr.write(f"unknown mode: {mode}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
