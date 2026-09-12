from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .log_privacy import redact_text


@dataclass(slots=True)
class ToolResult:
    ok: bool
    action: str
    data: Any = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ok(action: str, data: Any = None, warnings: list[str] | None = None) -> dict[str, Any]:
    return ToolResult(True, action, data=data, warnings=warnings or []).to_dict()


def fail(action: str, error: Exception | str) -> dict[str, Any]:
    return ToolResult(False, action, error=redact_text(str(error))).to_dict()
