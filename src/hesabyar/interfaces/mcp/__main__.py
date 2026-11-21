"""Run the MCP server as a module: ``python -m hesabyar.interfaces.mcp``."""

from __future__ import annotations

from hesabyar.interfaces.mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
