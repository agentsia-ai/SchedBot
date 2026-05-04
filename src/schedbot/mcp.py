"""SchedBot MCP server entry point for Claude Desktop.

Usage:
    python -m schedbot.mcp

Add to Claude Desktop config:
    "schedbot": {
      "command": "python",
      "args": ["-m", "schedbot.mcp"]
    }
"""

import asyncio
import logging
import sys

from schedbot.mcp_server.server import main as mcp_main

if __name__ == "__main__":
    # Default logging to stderr to preserve stdout purity for JSON-RPC.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(mcp_main())
