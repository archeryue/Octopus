# Codex CLI protocol notes (`codex exec --json`)

Recorded against **codex-cli 0.132.0** (`~/.nvm/versions/node/v22.16.0/bin/codex`),
confirmed on a **live, logged-in ChatGPT subscription** (2026-05-19, Phase C of
`plans/codex-backend.md`). Sibling of `cli-protocol-notes.md` (the Claude CLI).
The normalizer lives in `server/backends/codex.py`; the fake CLI that scripts
these shapes is `tests/_fixtures/fake_codex_cli.py`.

## Spawn command

`CodexBackend.build_args` produces (exec-level flags MUST precede the `resume`
subcommand; `--` precedes the prompt):

```
codex exec --json \
  --dangerously-bypass-approvals-and-sandbox \   # analog of claude --dangerously-skip-permissions
  --skip-git-repo-check \
  -C <abs_working_dir> \
  -c developer_instructions="<TOML-quoted system prompt>" \
  -c mcp_servers.<key>.command="<python>" \       # one block per MCP server (bg/ask)
  -c mcp_servers.<key>.args=["-m","server.mcp_servers.<name>"] \
  -c mcp_servers.<key>.env.<VAR>="<value>" \
  [-m <model>] \
  [resume <thread_id>] \
  -- <prompt>
```

- **Auth:** `CODEX_HOME` (defaults to `~/.codex`) holds `auth.json` and is where
  codex persists its own token refresh. We keep it the stable per-credential /
  host dir and inject MCP per-session via `-c` overrides (NOT a per-session
  config.toml) so the per-session callback env stays per-session while refresh
  still persists. Host `codex login` (option A) works with `CODEX_HOME` unset.
- The real binary **accepts this exact argv** (verified: it parses past
  arguments into execution).

## Event stream (`--json`, one JSON object per stdout line)

All shapes below are **verbatim from live runs**. `→` is the `BackendEvent` we
emit.

| Event | → BackendEvent |
|---|---|
| `{"type":"thread.started","thread_id":"019e…"}` | `session_started` (`session_id = thread_id`), emitted early so the resume id is captured before `result` |
| `{"type":"turn.started"}` | ignored |
| `{"type":"item.completed","item":{"id","type":"agent_message","text":…}}` | `text` (note: agent_message arrives only on `item.completed`; multiple per turn possible) |
| `{"type":"item.*","item":{"type":"reasoning","text":…}}` | `thinking` |
| `item.started` `command_execution` `{command, status:"in_progress", exit_code:null}` | `tool_use` (tool=`Bash`, `tool_use_id=item.id`, `input={command}`). `command` is the full `/bin/bash -lc '…'` |
| `item.completed` `command_execution` `{aggregated_output, exit_code, status:"completed"}` | `tool_result` (`content = aggregated_output`, `is_error = exit_code not in (0,null)`) |
| `item.started` `mcp_tool_call` `{server,tool,arguments,result:null,status:"in_progress"}` | `tool_use` (tool=`mcp__<server>__<tool>`, `input=arguments`) |
| `item.completed` `mcp_tool_call` `{server,tool,arguments,result:{content:[{type:"text",text}], structured_content:{result}},error}` | `tool_result` (`content` = flattened text, `is_error = error is not None`) |
| `item.*` `file_edit`/`file_write`/`file_read` `{path,diff?}` | `tool_use` Edit/Write/Read on `started`; `tool_result` (diff) on `completed` |
| `item.completed` `file_change` `{changes:[{kind,path}]}` | `text` summary |
| `{"type":"turn.completed","usage":{input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens}}` | `result` (`cost=None` — Codex reports **tokens, not USD**), then `_close_stream()` |
| `{"type":"turn.failed","error":…}` | `result` (`is_error=True`) |
| `{"type":"error","message"\|"error":…}` | `error` (`is_error=True`) |

### Key confirmations (resolved `codex-backend.md` §12 open questions)

1. **Event schema** — matches VM0's parser; no drift on codex 0.132.0. The one
   addition VM0 didn't cover: the **`mcp_tool_call`** item type (below).
2. **MCP via `-c mcp_servers.*`** — **honored.** During exploratory testing
   (against the since-removed `viewer` MCP server), codex launched the stdio
   server, the model invoked it, and the per-server `env` reached the
   subprocess. The mechanism stands; only the test target changed.
3. **Tool name** — codex reports `server` + `tool` separately in the event; we
   render `mcp__<server>__<tool>` to match Claude's scheme and our
   `developer_instructions`. The model successfully called the tool referenced
   in `developer_instructions` → instructions land.
4. **`developer_instructions`** — applied; the model knew about the injected MCP
   tool from it.

### Real captures (for reference)

Text turn:
```json
{"type":"thread.started","thread_id":"019e443d-b13d-7a43-aaa8-09072fd83957"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"hello world"}}
{"type":"turn.completed","usage":{"input_tokens":11175,"cached_input_tokens":1920,"output_tokens":20,"reasoning_output_tokens":12}}
```

MCP tool call (this capture predates the viewer-MCP removal, but the shape is
what matters — it's identical for any `mcp_servers.<key>` we register today):
```json
{"type":"item.completed","item":{"id":"item_1","type":"mcp_tool_call","server":"viewer","tool":"show_file","arguments":{"path":"hello.txt"},"result":{"content":[{"type":"text","text":"Opened hello.txt (text, 14 bytes) in the viewer. The user can see it now."}],"structured_content":{"result":"Opened hello.txt …"}},"error":null,"status":"completed"}}
```

## Still open

- **Login UI / flow decision** (`codex-backend.md` §10 #1): host `codex login`
  (option A, works today) vs an in-app `--device-auth` flow (option B). Not
  built — product decision.
- The `test_backend_codex_real.py` suite (gated on `codex` + `~/.codex/auth.json`)
  is the standing guard against version drift in the shapes above.
