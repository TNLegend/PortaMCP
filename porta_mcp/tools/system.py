from __future__ import annotations

import os
import platform
import socket
from typing import Any

from ..context import AppContext
from ..result import fail, ok


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def system_info() -> dict:
        """Return compact OS/runtime/device information. Read-only."""
        try:
            import psutil
            vm = psutil.virtual_memory()
            return ok("system_info", {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                "memory_total": vm.total,
                "memory_available": vm.available,
                "pid": os.getpid(),
            })
        except Exception as e:
            return fail("system_info", e)

    @mcp.tool()
    def server_capabilities() -> dict:
        """Return the server's configured roots, limits and enabled control features. Read-only."""
        return ok("server_capabilities", app.settings.public_summary())

    @mcp.tool()
    def security_emergency_stop_status() -> dict:
        """Check whether the local emergency-stop file is active. Read-only."""
        return ok("security_emergency_stop_status", {"active": os.path.exists(app.settings.emergency_stop_file), "path": app.settings.emergency_stop_file})
