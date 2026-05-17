# Known Bugs To Fix

Bugs we've discovered but deferred. Each entry should list the symptom,
where it surfaces, what we know about the cause, and what a fix probably
looks like.

---

## 1. `can_use_tool` control_response format is out of date for Claude Code 2.x

**Status**: **FIXED** (commit pending). Backend now sends
`{"behavior": "allow", "updatedInput": …}` / `{"behavior": "deny", "message": …}`
instead of the legacy `{"allow": …, "reason": …}`. Real-CLI tests now run
clean with a tripwire that fails on any `ZodError` or `Tool permission
request failed` text in tool_results or stderr.

Kept below as a record of the bug for future reference.

**Original status**: known, working-but-fragile. Existing tests pass. Reproduces when
the user calls `AskUserQuestion` directly inside Claude Code (the in-IDE
`/loop` host emits the same ZodError as our backend would).

**Symptom**

The CLI's input validator rejects our `control_response` payload with:

```
ZodError: [
  {
    "code": "invalid_union",
    "errors": [
      [
        { "code": "invalid_value", "values": ["allow"], "path": ["behavior"],
          "message": "Invalid input: expected \"allow\"" },
        { "expected": "record", "code": "invalid_type", "path": ["updatedInput"],
          "message": "Invalid input: expected record, received undefined" }
      ],
      [
        { "code": "invalid_value", "values": ["deny"], "path": ["behavior"],
          "message": "Invalid input: expected \"deny\"" },
        { "expected": "string", "code": "invalid_type", "path": ["message"],
          "message": "Invalid input: expected string, received undefined" }
      ]
    ],
    ...
  }
]
```

When this triggers, the tool call's `tool_result` carries
`"Tool permission request failed: ZodError: ..."` and Claude visibly
falls back to retrying or text mode.

**Where**

`server/backends/claude_code.py`:
- `_send_control_response_success` / `_handle_can_use_tool` /
  `answer_question` all build the inner `response` dict in the old shape:
  `{"allow": True}` or `{"allow": False, "reason": ...}`.

**Cause**

The Claude Code Python SDK (now retired) used:
- Allow: `{"allow": True, "input": <updated_input>?}`
- Deny:  `{"allow": False, "reason": <message>}`

The current CLI (v2.1.143) expects a different union:
- Allow: `{"behavior": "allow", "updatedInput": <dict>}`  (`updatedInput`
  appears to be required, not optional)
- Deny:  `{"behavior": "deny",  "message": <string>}`

The old shape happens to work for normal tools we always allow — the CLI is
tolerant enough that the response satisfies whichever schema variant matches
first. But for tools where the CLI emits the strict modern validator (like
`AskUserQuestion`), the union check fails and we get the ZodError above.

**Fix sketch**

In `claude_code.py`:

```python
async def _send_control_response_allow(self, request_id, updated_input=None):
    await self._send_control_response_success(
        request_id,
        {"behavior": "allow", "updatedInput": updated_input or {}},
    )

async def _send_control_response_deny(self, request_id, message):
    await self._send_control_response_success(
        request_id,
        {"behavior": "deny", "message": message},
    )
```

Then route every existing `{"allow": ...}` call through these helpers.
`answer_question` still uses the deny path (we deliver the answer as the
deny `message`) until / unless we switch to the MCP-tool replacement
described in `future-features.md` #7.

**Verification after fix**

1. Update `tests/test_backend_claude_code.py::test_permission_callback_invoked_directly`
   to look for `"behavior": "allow"` instead of `"allow": true`.
2. Re-run `tests/test_backend_claude_code_real.py` — the real CLI should
   stop logging ZodErrors (visible by adding a stderr-tail check) and the
   AskUserQuestion test should stop showing the "retry" path on flaky runs.
3. Re-run the full Playwright suite for one extra signal.

**Why deferred**

Doesn't block the current task (OAuth login). The user-visible impact is
limited to occasional retries inside AskUserQuestion flows, which we
recover from. Worth fixing before relying more heavily on the control
protocol.
