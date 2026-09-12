from __future__ import annotations

import argparse
import ipaddress
import os
from importlib.metadata import version
from pathlib import Path

try:
    from mcp.server import MCPServer
except Exception:
    from mcp.server.fastmcp import FastMCP as MCPServer

from .chrome_bridge import ChromeBridgeHub
from .config import Settings
from .context import build_context
from .tools import (
    browser,
    current_chrome,
    filesystem,
    git,
    linux_ui,
    process,
    screen,
    shell,
    system,
    windows_ui,
)
from .tools import input as input_tools


def build_server(settings: Settings | None = None, **server_kwargs):
    settings = settings or Settings.load()
    ctx = build_context(settings)
    ctx.state.chrome_bridge = ChromeBridgeHub()
    server_kwargs.setdefault("version", version("portamcp"))
    mcp = MCPServer(
        "PortaMCP",
        instructions=(
            "A client-neutral local computer-control MCP harness. Prefer structured filesystem/shell/browser/UI tools over raw mouse clicks. "
            "Respect policy failures and never try to bypass disabled capabilities. Keep tool outputs compact."
        ),
        **server_kwargs,
    )
    for module in (system, filesystem, shell, process, git, screen, input_tools, windows_ui, linux_ui, browser, current_chrome):
        module.register(mcp, ctx)
    return mcp, ctx


def _assert_safe_direct_http_host(settings: Settings) -> None:
    """Keep the generic unauthenticated HTTP entrypoint loopback-only."""
    host = str(settings.host or "").strip().lower()
    if host == "localhost":
        return
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        if ipaddress.ip_address(candidate).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError(
        "The generic 'portamcp' streamable-http entrypoint is unauthenticated and may only bind to loopback. "
        "Use 'portamcp-http' for Bearer authentication or 'portamcp-oauth' for OAuth when exposing PortaMCP remotely."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="PortaMCP client-neutral PC control server")
    parser.add_argument("--config", default=os.getenv("PORTAMCP_CONFIG", "config.json"))
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg_path = args.config if Path(args.config).exists() else None
    settings = Settings.load(cfg_path)
    if args.transport:
        settings.transport = args.transport
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    if settings.transport == "streamable-http":
        _assert_safe_direct_http_host(settings)
        # For authenticated HTTP use `python -m porta_mcp.http_server` or OAuth.
        mcp, _ = build_server(settings)
        mcp.run(transport="streamable-http", host=settings.host, port=settings.port)
    else:
        mcp, _ = build_server(settings)
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
