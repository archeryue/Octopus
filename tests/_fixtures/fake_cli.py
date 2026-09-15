#!/usr/bin/env python3
"""Tiny fake CLI for SubprocessJsonlBackend tests.

Behavior controlled by argv. Used by tests/test_backends_subprocess.py so we
don't depend on a real `claude` binary being installed in the test env.

Supported modes (first argv after the script name):
  emit-lines   : Print the following argv items as separate stdout lines, flush, exit.
  echo-stdin   : Read JSON frames from stdin and echo each one's uuid + content
                 back as a stdout line, so a test can see what the run engine
                 actually wrote. Exit on EOF.
  fail-exit    : Print one line, then exit with non-zero status.
  bad-json     : Print "{not json", then "{\"type\":\"good\"}", then exit.
  sleep-then   : Sleep N seconds (argv[2]), then print "{\"type\":\"woke\"}", then exit.
"""

import json
import sys
import time


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "emit-lines"

    if mode == "echo-stdin":
        # One line per frame read, carrying the frame's uuid. Used to prove the
        # engine writes the prompt to stdin and that every frame uuid is
        # distinct (a repeated uuid is silently dropped by the real CLI).
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            sys.stdout.write(
                json.dumps(
                    {
                        "type": "frame_seen",
                        "uuid": frame.get("uuid"),
                        "content": frame.get("message", {}).get("content"),
                    }
                )
                + "\n"
            )
            # One frame is one turn, so end it the way a real CLI does. The
            # process stays alive for the next frame.
            sys.stdout.write(json.dumps({"type": "result"}) + "\n")
            sys.stdout.flush()
        return 0

    if mode == "emit-lines":
        for line in sys.argv[2:]:
            sys.stdout.write(line + "\n")
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
