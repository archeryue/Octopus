"""Connector MCP servers (connectors.md §5.3).

One module per kind (github, gmail, …), each served in-process at
`/mcp/<kind>` and mounted once per kind rather than once per installation: the
config key the CLI sees stays per-installation (`gmail_e255c1`, so the tool name
does not move) while the URL is shared, and which installation a call belongs to
comes from its verified scope (polish-2026-09.md §4 B1). Each fetches its access
token from the host's internal /token route at call time. Shared
HTTP/token/truncation helpers live in `_shared`.
"""
