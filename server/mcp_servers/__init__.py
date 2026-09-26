"""The MCP tool namespaces we hand the CLI.

Each namespace lives in its own module and is served by the FastAPI app itself,
mounted at `/mcp/<key>` over streamable-HTTP; the harness points the CLI at
those URLs in the config it renders per turn (`server/mcp_http.py`,
polish-2026-09.md §4 B1). They were stdio subprocesses until then — seven per
session — which is why the bodies talk to the rest of Octopus over loopback HTTP
rather than by calling a manager directly.
"""
