#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Release bundles include a native GTK launcher. Prefer it so first-run setup is
# fully graphical and can request missing Linux prerequisites through PolicyKit.
if [[ -x "$ROOT/PortaMCP" && "${PORTAMCP_SKIP_NATIVE_LAUNCHER:-0}" != "1" ]]; then
  exec "$ROOT/PortaMCP"
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "Do not run the PortaMCP Control Center with sudo/root." >&2
  echo "Run it as your normal desktop user: ./portamcp.sh" >&2
  echo "PortaMCP will request narrowly scoped system authorization when required." >&2
  exit 1
fi

pick_python() {
  local candidate
  if [[ -n "${PYTHON:-}" ]]; then
    candidates=("$PYTHON")
  else
    candidates=(python3.14 python3.13 python3.12 python3.11 python3 python)
  fi
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON_BIN="$(pick_python)"; then
  echo "PortaMCP requires Python 3.11 or newer." >&2
  echo "Install Python 3.11+ without replacing your distribution's system Python, then retry." >&2
  echo "You can also set PYTHON=/path/to/python3.12 explicitly." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -m ensurepip --version >/dev/null 2>&1; then
  PYTHON_MM="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "Python venv/ensurepip support is required for PortaMCP first-run setup." >&2
  echo "Ubuntu/Debian: sudo apt install python${PYTHON_MM}-venv" >&2
  echo "If that versioned package is unavailable, try: sudo apt install python3-venv" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import tkinter' >/dev/null 2>&1; then
  echo "Python Tk support is required for the PortaMCP Control Center." >&2
  echo "Ubuntu/Debian: sudo apt install python3-tk" >&2
  echo "Fedora: sudo dnf install python3-tkinter" >&2
  echo "Arch: sudo pacman -S tk" >&2
  echo "openSUSE: sudo zypper install python3-tk" >&2
  echo "If Python was built with pyenv, install Tk development libraries first and rebuild that Python." >&2
  exit 1
fi

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "No Linux desktop session was detected (DISPLAY/WAYLAND_DISPLAY are empty)." >&2
  echo "The Control Center requires a graphical session. The MCP server can still run headlessly." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "Notice: git is not installed. PortaMCP Git tools will be unavailable." >&2
  echo "Ubuntu/Debian: sudo apt install git" >&2
fi

exec "$PYTHON_BIN" portamcp.pyw
