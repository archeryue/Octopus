"""Connector MCP servers (connectors.md §5.3).

One stdio MCP server module per kind (github, gmail, …). Each is spawned by a
backend as `python -m server.mcp_servers.connectors.<kind>` with the shared
callback env plus OCTOPUS_INSTALLATION_ID, and fetches its access token from
the host's internal /token route at call time. Shared HTTP/token/truncation
helpers live in `_shared`.
"""
