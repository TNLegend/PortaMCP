from __future__ import annotations

import os
import threading
from typing import Any

import uvicorn

_EVENT_PREFIX = "Local\\PortaMCPShutdown-"
_WAIT_OBJECT_0 = 0x00000000
_INFINITE = 0xFFFFFFFF
_EVENT_MODIFY_STATE = 0x0002


def _event_name(pid: int) -> str:
    return f"{_EVENT_PREFIX}{int(pid)}"


def request_windows_graceful_shutdown(pid: int) -> bool:
    """Signal one running PortaMCP Windows server through a local named event."""
    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _event_name(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def _run_windows_uvicorn(app: Any, *, host: str, port: int, log_level: str) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateEventW(None, False, False, _event_name(os.getpid()))
    if not handle:
        # Fail safely back to ordinary Uvicorn behavior. The Control Center will
        # retain its existing terminate/kill fallback if no named event exists.
        uvicorn.run(app, host=host, port=port, log_level=log_level)
        return

    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    watcher_done = threading.Event()

    def watch_shutdown_event() -> None:
        try:
            result = kernel32.WaitForSingleObject(handle, _INFINITE)
            if result == _WAIT_OBJECT_0:
                server.should_exit = True
        finally:
            watcher_done.set()

    watcher = threading.Thread(
        target=watch_shutdown_event,
        name="PortaMCP-WindowsShutdownWatcher",
        daemon=True,
    )
    watcher.start()
    try:
        server.run()
    finally:
        # Release the watcher even when Uvicorn stops for another reason.
        kernel32.SetEvent(handle)
        watcher_done.wait(timeout=1.0)
        kernel32.CloseHandle(handle)


def run_uvicorn(app: Any, *, host: str, port: int, log_level: str = "info") -> None:
    """Run Uvicorn with cooperative hidden-process shutdown support on Windows."""
    if os.name == "nt":
        _run_windows_uvicorn(app, host=host, port=port, log_level=log_level)
        return
    uvicorn.run(app, host=host, port=port, log_level=log_level)
