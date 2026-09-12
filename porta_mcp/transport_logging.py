from __future__ import annotations

import copy
import logging
import secrets
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .log_privacy import redact_text


class PrivateFormatter(logging.Formatter):
    def formatException(self, exc_info) -> str:
        # Preserve error type and stack locations, never arbitrary exception text.
        return "\n".join(
            [exc_info[0].__name__]
            + [f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}" for frame in traceback.extract_tb(exc_info[2])]
        )

    def format(self, record: logging.LogRecord) -> str:
        private_record = copy.copy(record)
        private_record.exc_text = None
        return redact_text(super().format(private_record))


def transport_log_path(audit_log_path: str) -> Path:
    return Path(audit_log_path).with_name("transport.log")


def _build_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"portamcp.transport.{path}")
    if not logger.handlers:
        handler = RotatingFileHandler(
            path,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(PrivateFormatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def server_log_path(audit_log_path: str) -> Path:
    return Path(audit_log_path).with_name("server.log")


def configure_server_logging(audit_log_path: str) -> None:
    """Persist SDK/Uvicorn errors without changing console logging."""
    path = server_log_path(audit_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(PrivateFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    setattr(handler, "_portamcp_runtime_handler", True)

    for name in ("uvicorn.error", "mcp", "porta_mcp"):
        logger = logging.getLogger(name)
        if any(getattr(existing, "_portamcp_runtime_handler", False) for existing in logger.handlers):
            continue
        logger.addHandler(handler)


class TransportLogMiddleware:
    """Persist compact HTTP/WebSocket transport diagnostics without secrets."""

    def __init__(self, app: Any, log_path: str) -> None:
        self.app = app
        self.logger = _build_logger(Path(log_path))

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = str(scope.get("type") or "")
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "WS")
        path = str(scope.get("path") or "")
        request_id = secrets.token_hex(4)
        started = time.monotonic()
        status: int | None = None
        disconnected = False

        if path != "/healthz":
            self.logger.info(
                "request_start id=%s method=%s path=%s",
                request_id,
                method,
                path,
            )

        async def traced_send(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                try:
                    status = int(message.get("status"))
                except (TypeError, ValueError):
                    status = None
            await send(message)

        async def traced_receive() -> dict[str, Any]:
            nonlocal disconnected
            message = await receive()
            if message.get("type") in {"http.disconnect", "websocket.disconnect"}:
                disconnected = True
            return message

        try:
            await self.app(scope, traced_receive, traced_send)
        except BaseException as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self.logger.error(
                "transport_exception id=%s method=%s path=%s status=%s elapsed_ms=%s disconnected=%s exception_type=%s frames=%s",
                request_id,
                method,
                path,
                status if status is not None else "-",
                elapsed_ms,
                disconnected,
                type(exc).__name__,
                [(Path(frame.filename).name, frame.lineno, frame.name) for frame in traceback.extract_tb(exc.__traceback__)],
            )
            raise
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if path != "/healthz" or (status is not None and status >= 400):
                level = logging.WARNING if status is not None and status >= 400 else logging.INFO
                self.logger.log(
                    level,
                    "request_end id=%s method=%s path=%s status=%s elapsed_ms=%s disconnected=%s",
                    request_id,
                    method,
                    path,
                    status if status is not None else "-",
                    elapsed_ms,
                    disconnected,
                )


def install_transport_logging(app: Any, audit_log_path: str) -> Any:
    return TransportLogMiddleware(
        app,
        log_path=str(transport_log_path(audit_log_path)),
    )
