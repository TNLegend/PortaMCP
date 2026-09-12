from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


def os_family() -> str:
    if os.name == "nt":
        return "windows"
    if sys_platform := platform.system().lower():
        if sys_platform == "linux":
            return "linux"
        if sys_platform == "darwin":
            return "macos"
    return "other"


def is_windows() -> bool:
    return os_family() == "windows"


def is_linux() -> bool:
    return os_family() == "linux"


def project_venv_dir(project_root: str | os.PathLike[str]) -> Path:
    root = Path(project_root)
    return root / (".venv" if is_windows() else ".venv-linux")


def venv_python(venv_dir: str | os.PathLike[str]) -> Path:
    root = Path(venv_dir)
    if is_windows():
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def venv_gui_python(venv_dir: str | os.PathLike[str]) -> Path:
    root = Path(venv_dir)
    if is_windows():
        candidate = root / "Scripts" / "pythonw.exe"
        return candidate if candidate.exists() else venv_python(root)
    return venv_python(root)


def creation_flags_no_window() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if is_windows() else 0


def desktop_session_available() -> bool:
    if is_windows():
        return True
    if is_linux():
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def _python_module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@lru_cache(maxsize=1)
def linux_atspi_available() -> bool:
    """Return whether this Linux desktop appears to have usable AT-SPI bindings.

    Debian/Ubuntu normally install PyGObject outside virtual environments under
    ``/usr/lib/python3/dist-packages``. The Linux UI backend can deliberately
    import from that one system directory, so capability detection mirrors that
    behavior without importing GI on every Control Center refresh.
    """
    if not is_linux() or not desktop_session_available():
        return False
    has_gi = _python_module_available("gi") or (Path("/usr/lib/python3/dist-packages") / "gi").is_dir()
    if not has_gi:
        return False
    typelib_candidates = [
        Path("/usr/lib/girepository-1.0/Atspi-2.0.typelib"),
        Path("/usr/local/lib/girepository-1.0/Atspi-2.0.typelib"),
    ]
    if any(path.is_file() for path in typelib_candidates):
        return True
    for base in (Path("/usr/lib"), Path("/usr/local/lib")):
        try:
            if any(base.glob("*/girepository-1.0/Atspi-2.0.typelib")):
                return True
        except OSError:
            continue
    return False


def platform_capabilities() -> dict[str, Any]:
    desktop = desktop_session_available()
    return {
        "os": os_family(),
        "desktop_session": desktop,
        "filesystem": True,
        "shell": True,
        "process": True,
        "git": shutil.which("git") is not None,
        "screen": is_windows() or (is_linux() and desktop),
        "input": is_windows()
        or (
            is_linux()
            and desktop
            and bool(os.environ.get("DISPLAY"))
            and _python_module_available("Xlib")
            and _python_module_available("tkinter")
        ),
        "windows_ui": is_windows(),
        "linux_ui": linux_atspi_available(),
        "ui_automation": is_windows() or linux_atspi_available(),
        "managed_browser": True,
        "chrome_bridge": is_windows() or is_linux(),
    }


def system_chromium_executable() -> Path | None:
    """Return a locally installed Chromium-family browser suitable for Playwright."""
    if is_linux():
        names = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        )
    elif is_windows():
        names = ("chrome.exe", "msedge.exe", "chromium.exe")
    else:
        names = ("google-chrome", "chromium", "chromium-browser")

    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)

    if is_windows():
        roots = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rels = (
            Path("Google/Chrome/Application/chrome.exe"),
            Path("Microsoft/Edge/Application/msedge.exe"),
        )
        for root in roots:
            if not root:
                continue
            for rel in rels:
                candidate = Path(root) / rel
                if candidate.is_file():
                    return candidate
    return None


def tailscale_executable() -> Path | None:
    names = ["tailscale.exe", "tailscale"] if is_windows() else ["tailscale", "tailscale.exe"]
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    if is_windows():
        program_files = os.environ.get("ProgramFiles", "")
        if program_files:
            candidate = Path(program_files) / "Tailscale" / "tailscale.exe"
            if candidate.exists():
                return candidate
    return None


def open_local_path(path: str | os.PathLike[str]) -> None:
    target = str(Path(path))
    if is_windows():
        os.startfile(target)  # type: ignore[attr-defined]
        return
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if opener:
        args = [opener, target] if Path(opener).name != "gio" else [opener, "open", target]
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    raise RuntimeError("No desktop path opener is available. Open this path manually: " + target)
