"""Command-line entry point for holonbridge-mcp.

    python -m holonbridge_mcp                     # stdio, for Claude Desktop
    python -m holonbridge_mcp --transport sse     # remote, for claude.ai

``python -m holonbridge_mcp.server`` still runs stdio directly, so existing
client configurations keep working unchanged.

stdio is a child process the client launches, so nothing else can reach it.
The remote transports are reachable by anyone who can reach the URL and carry
the bridge's own bearer token, so they require an inbound token of their own —
see :mod:`holonbridge_mcp.remote`.
"""

from __future__ import annotations

import argparse
import os

from .server import mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="holonbridge-mcp")
    parser.add_argument(
        "--transport",
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        choices=("stdio", "sse", "http"),
        help="stdio for a locally launched client; sse or http to serve over a URL",
    )
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "3032")))
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    from .remote import serve

    serve(mcp, transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
