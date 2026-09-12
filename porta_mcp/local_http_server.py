from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware

from .chrome_bridge import (
    ensure_bridge_token,
    install_bridge_route,
    write_extension_config,
)
from .config import Settings
from .health import install_health_route
from .runtime_server import run_uvicorn
from .server import build_server
from .transport_logging import configure_server_logging, install_transport_logging


def _is_loopback_host(host: str) -> bool:
    value = host.strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _loopback_allowed_hosts() -> list[str]:
    return ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def build_local_http_app(settings: Settings):
    """Build an unauthenticated MCP endpoint that is intentionally loopback-only."""
    if not _is_loopback_host(settings.host):
        raise RuntimeError(
            "No-auth HTTP mode is restricted to loopback hosts (127.0.0.1, ::1, or localhost). "
            "Use Bearer or OAuth for remote access."
        )

    settings.ensure_runtime_dirs()
    mcp, ctx = build_server(settings)
    bridge_token = ensure_bridge_token(settings.chrome_bridge_token_file)
    write_extension_config(settings.chrome_extension_dir, port=settings.port, token=bridge_token)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_loopback_allowed_hosts(),
        allowed_origins=settings.allowed_origins,
    )
    app = mcp.streamable_http_app(
        host=settings.host,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    install_health_route(app, mcp)
    install_bridge_route(app, ctx.state.chrome_bridge, bridge_token)
    app = CORSMiddleware(
        app,
        allow_origins=settings.allowed_origins or [],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "Mcp-Method",
            "Mcp-Name",
            "Mcp-Protocol-Version",
            "Mcp-Session-Id",
        ],
        expose_headers=["Mcp-Session-Id"],
    )
    app = install_transport_logging(app, settings.audit_log_path)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-only unauthenticated Streamable HTTP MCP")
    parser.add_argument("--config", default=os.getenv("PORTAMCP_CONFIG", "config.json"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg_path = args.config if Path(args.config).exists() else None
    settings = Settings.load(cfg_path)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if not _is_loopback_host(settings.host):
        raise RuntimeError("No-auth HTTP mode can only bind to a loopback address.")

    configure_server_logging(settings.audit_log_path)
    run_uvicorn(build_local_http_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
