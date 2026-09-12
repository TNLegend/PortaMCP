from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from .chrome_bridge import (
    ensure_bridge_token,
    install_bridge_route,
    write_extension_config,
)
from .config import Settings
from .health import install_health_route
from .runtime_server import run_uvicorn
from .self_hosted_oauth import (
    OAuthGateMiddleware,
    RootMcpAliasMiddleware,
    SelfHostedOAuth,
)
from .server import build_server
from .transport_logging import configure_server_logging, install_transport_logging

LOGGER = logging.getLogger("porta_mcp.startup")


def _effective_oauth_allowed_hosts(settings: Settings) -> list[str]:
    result: list[str] = []
    for value in [
        *settings.allowed_hosts,
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]:
        if value and value not in result:
            result.append(value)

    public_host = urlparse(settings.public_base_url).hostname
    if public_host:
        for value in (public_host, f"{public_host}:*"):
            if value not in result:
                result.append(value)
    return result


def build_oauth_app(settings: Settings):
    if not settings.public_base_url:
        raise RuntimeError(
            "OAuth requires an HTTPS public endpoint. Configure one in PortaMCP Control Center under Connection & Auth."
        )
    settings.ensure_runtime_dirs()
    oauth = SelfHostedOAuth(
        public_base_url=settings.public_base_url,
        state_path=settings.oauth_state_path,
        owner_password_file=settings.oauth_owner_password_file,
        access_ttl=settings.oauth_access_token_ttl_seconds,
        refresh_ttl=settings.oauth_refresh_token_ttl_seconds,
    )

    mcp, ctx = build_server(settings)
    bridge_token = ensure_bridge_token(settings.chrome_bridge_token_file)
    write_extension_config(settings.chrome_extension_dir, port=settings.port, token=bridge_token)
    effective_allowed_hosts = _effective_oauth_allowed_hosts(settings)
    public_host = urlparse(settings.public_base_url).hostname or ""
    LOGGER.info(
        "oauth_runtime_config pid=%s host=%s port=%s public_host=%s allowed_hosts=%s module=%s",
        os.getpid(),
        settings.host,
        settings.port,
        public_host,
        effective_allowed_hosts,
        str(Path(__file__).resolve()),
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=effective_allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
    app = mcp.streamable_http_app(
        host=settings.host,
        stateless_http=False,
        json_response=False,
        transport_security=security,
    )
    install_health_route(app, mcp)
    install_bridge_route(app, ctx.state.chrome_bridge, bridge_token)

    # OAuth endpoints share the MCP app/lifespan, which keeps the MCP HTTP session
    # manager alive correctly. The OAuth gate protects /mcp but leaves discovery,
    # registration, login, and token endpoints reachable as required by OAuth.
    routes = [
        Route("/", oauth.root_handler, methods=["GET"]),
        Route("/robots.txt", oauth.robots_handler, methods=["GET"]),
        Route("/favicon.ico", oauth.favicon_handler, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth.metadata_handler, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/mcp", oauth.metadata_handler, methods=["GET"]),
        Route("/mcp/.well-known/oauth-authorization-server", oauth.metadata_handler, methods=["GET"]),
        Route("/.well-known/openid-configuration", oauth.metadata_handler, methods=["GET"]),
        Route("/.well-known/openid-configuration/mcp", oauth.metadata_handler, methods=["GET"]),
        Route("/mcp/.well-known/openid-configuration", oauth.metadata_handler, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", oauth.protected_resource_handler, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", oauth.protected_resource_handler, methods=["GET"]),
        Route("/register", oauth.register_handler, methods=["POST"]),
        Route("/authorize", oauth.authorize_handler, methods=["GET"]),
        Route("/authorize/decision", oauth.decision_handler, methods=["POST"]),
        Route("/token", oauth.token_handler, methods=["POST"]),
        Route("/revoke", oauth.revoke_handler, methods=["POST"]),
        Route("/oauth/health", oauth.health_handler, methods=["GET"]),
    ]
    # Put exact OAuth routes before the existing /mcp route.
    app.router.routes[0:0] = routes
    app.add_middleware(OAuthGateMiddleware, oauth=oauth)
    app.add_middleware(RootMcpAliasMiddleware)
    app = install_transport_logging(app, settings.audit_log_path)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-hosted OAuth + Streamable HTTP PortaMCP")
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
    run_uvicorn(build_oauth_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
