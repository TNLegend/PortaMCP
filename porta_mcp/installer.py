from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .platform_support import (
    is_linux,
    project_venv_dir,
    system_chromium_executable,
    venv_python,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = project_venv_dir(PROJECT_ROOT)
VENV_PYTHON = venv_python(VENV_DIR)


def _run(label: str, command: list[str]) -> None:
    print(f"\n== {label} ==", flush=True)
    subprocess.run(command, cwd=str(PROJECT_ROOT), check=True)


def _venv_python_healthy() -> bool:
    if not VENV_PYTHON.is_file():
        return False
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 9)"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _running_inside_target_venv() -> bool:
    try:
        executable = Path(sys.executable).resolve()
        root = VENV_DIR.resolve()
        return executable == VENV_PYTHON.resolve() or root in executable.parents
    except OSError:
        return False


def _ensure_clean_venv() -> None:
    if _venv_python_healthy():
        return
    if VENV_DIR.exists():
        if _running_inside_target_venv():
            raise RuntimeError(
                "The existing PortaMCP virtual environment is damaged and cannot repair itself while it is running. "
                "Close PortaMCP and launch the native PortaMCP launcher again."
            )
        print("\n== Rebuilding incomplete virtual environment ==", flush=True)
        shutil.rmtree(VENV_DIR)
    _run("Creating isolated environment", [sys.executable, "-m", "venv", str(VENV_DIR)])
    if not _venv_python_healthy():
        raise RuntimeError("The PortaMCP virtual environment was created but its Python runtime is not usable.")


def main() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("PortaMCP requires Python 3.11 or newer.")

    _ensure_clean_venv()

    # ensurepip makes interrupted/partial first-run installs self-healing before
    # pip is upgraded from the network.
    _run("Preparing pip", [str(VENV_PYTHON), "-m", "ensurepip", "--upgrade"])
    _run(
        "Updating pip",
        [str(VENV_PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"],
    )
    _run(
        "Installing PortaMCP and Python dependencies",
        [str(VENV_PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-e", "."],
    )

    system_browser = system_chromium_executable() if is_linux() else None
    try:
        _run(
            "Installing managed Chromium",
            [str(VENV_PYTHON), "-m", "playwright", "install", "chromium"],
        )
    except subprocess.CalledProcessError:
        if system_browser is None:
            raise
        print(
            "\nManaged Chromium download was unavailable, but PortaMCP detected a system Chromium-family browser and will use it.",
            flush=True,
        )

    print("\nPortaMCP environment is ready.", flush=True)


if __name__ == "__main__":
    main()
