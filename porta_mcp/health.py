from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import Route


def _manager_health(mcp: Any) -> bool:
    lowlevel = getattr(mcp, "_lowlevel_server", None)
    manager = getattr(lowlevel, "_session_manager", None)
    if manager is None:
        return False
    task_group = getattr(manager, "_task_group", None)
    if task_group is None:
        return False
    cancel_scope = getattr(task_group, "cancel_scope", None)
    return not bool(getattr(cancel_scope, "cancel_called", False))


def install_health_route(app: Any, mcp: Any) -> None:
    async def healthz(_request: Any) -> JSONResponse:
        healthy = _manager_health(mcp)
        return JSONResponse(
            {"ok": healthy, "service": "PortaMCP"},
            status_code=200 if healthy else 503,
            headers={"Cache-Control": "no-store"},
        )

    app.router.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
