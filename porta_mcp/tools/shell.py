from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

import psutil

from ..context import AppContext
from ..result import fail, ok

INTERACTIVE_SHELL_BUDGET_SECONDS = 90


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    cap = max(0, max_bytes)
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text, False
    return raw[:cap].decode("utf-8", errors="ignore"), True


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate a command and all descendants so timeouts cannot leak workers."""
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            parent.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        _, alive = psutil.wait_procs([*children, parent], timeout=2.0)
        for item in alive:
            try:
                item.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        try:
            proc.terminate()
        except Exception:
            pass


def _run(app: AppContext, command: str, cwd: str, timeout: int) -> dict:
    app.policy.assert_shell(command)
    cwd = app.policy.assert_path(cwd)
    requested_timeout = max(1, int(timeout))
    effective_timeout = min(
        requested_timeout,
        int(app.settings.command_timeout_seconds),
        INTERACTIVE_SHELL_BUDGET_SECONDS,
    )
    if platform.system() == "Windows":
        argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        argv = ["/bin/sh", "-lc", command]
        creationflags = 0

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        creationflags=creationflags,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "process tree did not exit cleanly after timeout"

    cap = app.settings.max_command_output_bytes
    out, out_truncated = _truncate_utf8(stdout or "", cap)
    err, err_truncated = _truncate_utf8(stderr or "", cap)
    warnings = []
    if requested_timeout > effective_timeout:
        warnings.append(
            f"interactive timeout capped at {effective_timeout}s; use process_start for longer jobs"
        )
    if timed_out:
        warnings.append(
            f"command timed out after {effective_timeout}s; process tree terminated"
        )
    if out_truncated:
        warnings.append("stdout truncated")
    if err_truncated:
        warnings.append("stderr truncated")

    return ok(
        "shell_run",
        {
            "returncode": proc.returncode if proc.returncode is not None else -1,
            "stdout": out,
            "stderr": err,
            "cwd": cwd,
            "timed_out": timed_out,
            "effective_timeout_seconds": effective_timeout,
        },
        warnings,
    )


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def shell_run(command: str, cwd: str = ".", timeout_seconds: int = 120) -> dict:
        """Run PowerShell on Windows (or /bin/sh elsewhere) in an allowed working directory. Action tool."""
        try:
            return _run(app, command, cwd, timeout_seconds)
        except Exception as e:
            return fail("shell_run", e)

    @mcp.tool()
    def shell_session_create(cwd: str = ".") -> dict:
        """Create a lightweight shell session that remembers its working directory between calls."""
        try:
            cwd2 = app.policy.assert_path(cwd)
            s = app.state.new_shell_session(cwd2)
            return ok("shell_session_create", {"session_id": s.id, "cwd": s.cwd})
        except Exception as e:
            return fail("shell_session_create", e)

    @mcp.tool()
    def shell_session_run(session_id: str, command: str, timeout_seconds: int = 120) -> dict:
        """Run a command in a remembered shell-session cwd. A standalone 'cd PATH' updates the session cwd."""
        try:
            s = app.state.get_shell_session(session_id)
            with s.lock:
                stripped = command.strip()
                if stripped.lower().startswith("cd ") and "&&" not in stripped and ";" not in stripped:
                    target = stripped[3:].strip()
                    if len(target) >= 2 and target[0] == target[-1] and target[0] in {'"', "'"}:
                        target = target[1:-1]
                    new_cwd = target if os.path.isabs(target) else os.path.join(s.cwd, target)
                    resolved = Path(app.policy.assert_path(new_cwd))
                    if not resolved.is_dir():
                        raise NotADirectoryError(str(resolved))
                    s.cwd = str(resolved)
                    return ok("shell_session_run", {"session_id": s.id, "cwd": s.cwd, "returncode": 0, "stdout": "", "stderr": ""})
                result = _run(app, command, s.cwd, timeout_seconds)
                result["action"] = "shell_session_run"
                if result.get("data"):
                    result["data"]["session_id"] = s.id
                return result
        except Exception as e:
            return fail("shell_session_run", e)

    @mcp.tool()
    def shell_session_close(session_id: str) -> dict:
        """Forget a lightweight shell session."""
        try:
            app.state.close_shell_session(session_id)
            return ok("shell_session_close", {"session_id": session_id})
        except Exception as e:
            return fail("shell_session_close", e)
