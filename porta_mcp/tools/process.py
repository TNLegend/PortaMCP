from __future__ import annotations

import os
import subprocess
from typing import Any

from ..context import AppContext
from ..result import fail, ok


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def process_list(name_contains: str = "", limit: int = 300) -> dict:
        """List running processes with pid/name/executable when accessible. Read-only."""
        try:
            import psutil
            rows = []
            for p in psutil.process_iter(["pid", "name", "exe", "username", "status"]):
                info = p.info
                if name_contains and name_contains.lower() not in (info.get("name") or "").lower():
                    continue
                rows.append(info)
                if len(rows) >= max(1, min(limit, 2000)):
                    break
            return ok("process_list", {"processes": rows})
        except Exception as e:
            return fail("process_list", e)

    @mcp.tool()
    def process_start(executable: str, args: list[str] | None = None, cwd: str = ".") -> dict:
        """Launch a process. Executable and cwd must resolve inside allowed roots unless executable is a bare command on PATH."""
        try:
            app.policy.assert_feature("process_start")
            cwd2 = app.policy.assert_path(cwd)
            exe = executable
            if os.path.isabs(exe) or "/" in exe or "\\" in exe:
                exe = app.policy.assert_path(os.path.join(cwd2, exe))
            proc = subprocess.Popen([exe] + (args or []), cwd=cwd2)
            app.audit.write("process_start", detail={"pid": proc.pid, "argument_count": len(args or [])})
            return ok("process_start", {"pid": proc.pid})
        except Exception as e:
            return fail("process_start", e)

    @mcp.tool()
    def process_kill(pid: int, tree: bool = False) -> dict:
        """Terminate a process by PID. Disabled by default because it is destructive."""
        try:
            app.policy.assert_feature("process_kill")
            import psutil
            p = psutil.Process(pid)
            killed = []
            if tree:
                for child in p.children(recursive=True):
                    try:
                        child.terminate()
                        killed.append(child.pid)
                    except psutil.Error:
                        pass
            p.terminate()
            killed.append(pid)
            app.audit.write("process_kill", target=str(pid), detail={"tree": tree})
            return ok("process_kill", {"pids": killed})
        except Exception as e:
            return fail("process_kill", e)
