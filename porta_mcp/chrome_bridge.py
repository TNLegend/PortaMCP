from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ChromeBridgeHub:
    """One authenticated localhost WebSocket connection from the Chrome extension."""

    def __init__(self) -> None:
        self.websocket: WebSocket | None = None
        self.client_info: dict[str, Any] = {}
        self.connected_at: float | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.websocket is not None

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connected_at": self.connected_at,
            "client": self.client_info,
        }

    async def request(self, op: str, args: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        ws = self.websocket
        if ws is None:
            raise RuntimeError(
                "Current-Chrome bridge is not connected. Load/enable the unpacked PortaMCP Chrome extension first."
            )
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        try:
            async with self._send_lock:
                await ws.send_json({"id": request_id, "op": op, "args": args or {}})
            message = await asyncio.wait_for(future, timeout=timeout)
            if not message.get("ok", False):
                raise RuntimeError(str(message.get("error") or f"Chrome bridge operation failed: {op}"))
            return message.get("data")
        finally:
            self._pending.pop(request_id, None)

    def _fail_pending(self, message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(message))

    def _disconnect(self, websocket: WebSocket) -> None:
        if self.websocket is not websocket:
            return
        self.websocket = None
        self.connected_at = None
        self.client_info = {}
        self._fail_pending("Chrome bridge disconnected")

    async def handle(self, websocket: WebSocket, token: str) -> None:
        client = websocket.client
        host = client.host if client else ""
        supplied = websocket.query_params.get("token", "")
        if host not in {"127.0.0.1", "::1"} or not supplied or not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            await websocket.close(code=1008)
            return

        await websocket.accept()
        old = self.websocket
        if old is not None and old is not websocket:
            # Requests already sent on the old socket cannot receive a reply on
            # the replacement connection. Fail them immediately instead of
            # leaving callers blocked until their operation timeout expires.
            self._fail_pending("Chrome bridge reconnected")
            try:
                await old.close(code=1012)
            except Exception:
                pass
        self.websocket = websocket
        self.connected_at = time.time()
        self.client_info = {}

        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "hello":
                    self.client_info = dict(message.get("data") or {})
                    continue
                if message.get("type") == "heartbeat":
                    await websocket.send_json({"type": "heartbeat_ack"})
                    continue
                request_id = str(message.get("id") or "")
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
        except WebSocketDisconnect:
            pass
        except Exception:
            # A malformed extension message or socket failure must not escape
            # the WebSocket route and destabilize unrelated MCP requests.
            logger.exception("Chrome bridge connection failed")
        finally:
            self._disconnect(websocket)


def ensure_bridge_token(path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if len(token) < 32:
        token = secrets.token_urlsafe(48)
        p.write_text(token, encoding="utf-8")
    return token


def write_extension_config(extension_dir: str, *, port: int, token: str) -> Path:
    root = Path(extension_dir)
    root.mkdir(parents=True, exist_ok=True)
    config = root / "bridge_config.js"
    config.write_text(
        "globalThis.PORTAMCP_BRIDGE = "
        + json.dumps({"host": "127.0.0.1", "port": int(port), "token": token}, ensure_ascii=True)
        + ";\n",
        encoding="utf-8",
    )
    return config


def install_bridge_route(app: Any, hub: ChromeBridgeHub, token: str) -> None:
    async def endpoint(websocket: WebSocket) -> None:
        await hub.handle(websocket, token)

    app.router.routes.insert(0, WebSocketRoute("/chrome-bridge", endpoint))
