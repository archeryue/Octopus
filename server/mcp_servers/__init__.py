"""MCP servers we register with the `claude` CLI subprocess.

These are stdio-transport servers spawned by claude itself, not by
the FastAPI app. Each lives in its own module and is launched via
the `--mcp-config` flag we inject in `ClaudeCodeBackend.build_args`.
"""
