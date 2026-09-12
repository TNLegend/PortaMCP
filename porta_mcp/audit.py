from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .log_privacy import audit_value


class AuditLogger:
    def __init__(self, path: str, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._lock = threading.Lock()

    def write(self, action: str, *, target: str | None = None, allowed: bool = True, detail: Any = None) -> None:
        if not self.enabled:
            return
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "target": target,
            "allowed": allowed,
            "detail": detail,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit_value(row), ensure_ascii=False, default=str) + "\n")
