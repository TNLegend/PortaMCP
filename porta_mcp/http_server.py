from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

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


class BearerAuthASGI:
    """Small V1 bearer-token gate for /mcp. Replace with MCP OAuth 2.1 for multi-user deployments."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            expected = f"Bearer {self.token}"
            if not self.token or not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
                response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app(settings: Settings):
    if not settings.bearer_token:
        raise RuntimeError("Remote HTTP mode requires bearer_token. Set PORTAMCP_BEARER_TOKEN or configure Bearer mode in PortaMCP Control Center.")
    mcp, ctx = build_server(settings)
    bridge_token = ensure_bridge_token(settings.chrome_bridge_token_file)
    write_extension_config(settings.chrome_extension_dir, port=settings.port, token=bridge_token)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.allowed_hosts,
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
    app = BearerAuthASGI(app, settings.bearer_token)
    app = CORSMiddleware(
        app,
        allow_origins=settings.allowed_origins or [],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "Mcp-Method", "Mcp-Name", "Mcp-Protocol-Version", "Mcp-Session-Id"],
        expose_headers=["Mcp-Session-Id"],
    )
    app = install_transport_logging(app, settings.audit_log_path)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Authenticated Streamable-HTTP PortaMCP")
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
    configure_server_logging(settings.audit_log_path)
    run_uvicorn(build_http_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
