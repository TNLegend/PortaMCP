from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from .platform_support import (
    creation_flags_no_window,
    open_local_path,
    platform_capabilities,
    project_venv_dir,
    system_chromium_executable,
    tailscale_executable,
    venv_python,
)
from .runtime_server import request_windows_graceful_shutdown

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PROJECT_ROOT / ".portamcp"
CONFIG_PATH = PROJECT_ROOT / "config.json"
TOKEN_PATH = RUNTIME_DIR / "token.txt"
OAUTH_PASSWORD_PATH = RUNTIME_DIR / "oauth_owner_password.txt"
OAUTH_STATE_PATH = RUNTIME_DIR / "oauth_state.json"
EMERGENCY_PATH = RUNTIME_DIR / "EMERGENCY_STOP"
UI_STATE_PATH = RUNTIME_DIR / "portamcp_ui.json"
AUDIT_PATH = RUNTIME_DIR / "audit.jsonl"
TRANSPORT_LOG_PATH = RUNTIME_DIR / "transport.log"
SERVER_LOG_PATH = RUNTIME_DIR / "server.log"
VENV_PYTHON = venv_python(project_venv_dir(PROJECT_ROOT))
CHROME_EXTENSION_DIR = PROJECT_ROOT / "chrome_extension"

PROFILE_MODULES = {
    "local": "porta_mcp.local_http_server",
    "bearer": "porta_mcp.http_server",
    "oauth": "porta_mcp.oauth_server",
}

PROFILE_LABELS = {
    "local": "Local / No Auth",
    "bearer": "Bearer Token",
    "oauth": "OAuth",
}

PRESET_LABELS = {
    "full": "Full Control",
    "balanced": "Balanced",
    "readonly": "Read Only",
    "custom": "Custom",
}

FEATURE_KEYS = (
    "allow_fs_write",
    "allow_shell",
    "allow_process_start",
    "allow_process_kill",
    "allow_destructive_fs",
    "allow_input_control",
    "allow_ui_automation",
    "allow_browser_control",
    "allow_admin_commands",
)

FEATURE_PLATFORM_CAPABILITIES = {
    "allow_input_control": "input",
    "allow_ui_automation": "ui_automation",
    "allow_browser_control": "managed_browser",
}


def feature_supported(key: str) -> bool:
    capability = FEATURE_PLATFORM_CAPABILITIES.get(key)
    if capability is None:
        return True
    return bool(platform_capabilities().get(capability, False))


def effective_feature_enabled(config: dict[str, Any], key: str) -> bool:
    return bool(config.get(key, False)) and feature_supported(key)


SERVER_MARKERS = (
    "porta_mcp.local_http_server",
    "porta_mcp.http_server",
    "porta_mcp.oauth_server",
)


def ensure_runtime_layout() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        from .config import default_config
        CONFIG_PATH.write_text(json.dumps(default_config(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ensure_secret(TOKEN_PATH, 48)
    ensure_secret(OAUTH_PASSWORD_PATH, 24)


def ensure_secret(path: Path, nbytes: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_text(encoding="utf-8").strip()
        if current:
            return current
    value = secrets.token_urlsafe(nbytes)
    path.write_text(value, encoding="utf-8")
    return value


def rotate_secret(path: Path, nbytes: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(nbytes)
    path.write_text(value, encoding="utf-8")
    return value


def read_config() -> dict[str, Any]:
    ensure_runtime_layout()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read config.json: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("config.json must contain a JSON object")
    from .config import default_denied_roots
    data.setdefault("allowed_roots", [])
    data.setdefault("denied_roots", default_denied_roots())
    data.setdefault("allow_fs_write", False)
    data.setdefault("allow_process_start", False)
    return data


def write_config(data: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_ui_state() -> dict[str, Any]:
    ensure_runtime_layout()
    cfg = read_config()
    default: dict[str, Any] = {
        "profile": "oauth" if cfg.get("public_base_url") else "local",
        "preset": "readonly",
        "first_run_complete": False,
        "tunnel_provider": "Manual / custom HTTPS",
    }
    if UI_STATE_PATH.exists():
        try:
            saved = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                default.update(saved)
        except Exception:
            pass
    return default


def write_ui_state(state: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    UI_STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def installed() -> bool:
    if not VENV_PYTHON.exists():
        return False
    try:
        if system_chromium_executable() is not None:
            check = "import mcp, customtkinter, playwright, websockets"
        else:
            check = (
                "from pathlib import Path; import mcp, customtkinter, playwright, websockets; "
                "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); "
                "ok=Path(p.chromium.executable_path).is_file(); p.stop(); raise SystemExit(0 if ok else 1)"
            )
        result = subprocess.run(
            [str(VENV_PYTHON), "-c", check],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=creation_flags_no_window(),
        )
        return result.returncode == 0
    except Exception:
        return False


def install_command() -> list[str]:
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    return [str(python), "-u", "-m", "porta_mcp.installer"]


def bearer_token() -> str:
    return ensure_secret(TOKEN_PATH, 48)


def oauth_password() -> str:
    return ensure_secret(OAUTH_PASSWORD_PATH, 24)


def rotate_bearer_token() -> str:
    return rotate_secret(TOKEN_PATH, 48)


def rotate_oauth_password() -> str:
    return rotate_secret(OAUTH_PASSWORD_PATH, 24)


def reset_oauth_state() -> None:
    if server_processes():
        raise RuntimeError("Stop this installation's PortaMCP server before resetting OAuth state.")
    try:
        OAUTH_STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def emergency_active() -> bool:
    return EMERGENCY_PATH.exists()


def set_emergency(active: bool) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if active:
        EMERGENCY_PATH.touch(exist_ok=True)
    else:
        try:
            EMERGENCY_PATH.unlink()
        except FileNotFoundError:
            pass


def available_drives() -> list[str]:
    if os.name != "nt":
        return [str(Path("/").resolve())]
    drives: list[str] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        if root.exists():
            drives.append(str(root))
    return drives


def portable_denied_roots() -> list[str]:
    """Return the portable sensitive-path templates used for a fresh configuration."""
    from .config import default_denied_roots
    return default_denied_roots()


def reset_denied_roots() -> list[str]:
    """Return a fresh copy of the portable machine-derived deny defaults."""
    return list(portable_denied_roots())


def apply_preset(config: dict[str, Any], preset: str) -> dict[str, Any]:
    """Apply capability/limit presets without granting filesystem scope."""
    data = deepcopy(config)
    preserved_allowed = list(data.get("allowed_roots", []))
    preserved_denied = list(data.get("denied_roots", []))

    if preset == "full":
        data.update({
            "max_read_bytes": 10_000_000, "max_write_bytes": 10_000_000,
            "max_command_output_bytes": 3_000_000, "command_timeout_seconds": 300,
            "allow_fs_write": True, "allow_shell": True, "allow_process_start": True,
            "allow_process_kill": True, "allow_destructive_fs": True,
            "allow_input_control": True, "allow_ui_automation": True,
            "allow_browser_control": True, "allow_admin_commands": False,
        })
    elif preset == "balanced":
        data.update({
            "max_read_bytes": 5_000_000, "max_write_bytes": 5_000_000,
            "max_command_output_bytes": 1_500_000, "command_timeout_seconds": 180,
            "allow_fs_write": True, "allow_shell": True, "allow_process_start": False,
            "allow_process_kill": False, "allow_destructive_fs": False,
            "allow_input_control": True, "allow_ui_automation": True,
            "allow_browser_control": True, "allow_admin_commands": False,
        })
    elif preset == "readonly":
        data.update({
            "max_read_bytes": 5_000_000, "max_write_bytes": 1,
            "max_command_output_bytes": 1_000_000, "command_timeout_seconds": 120,
            "allow_fs_write": False, "allow_shell": False, "allow_process_start": False,
            "allow_process_kill": False, "allow_destructive_fs": False,
            "allow_input_control": False, "allow_ui_automation": False,
            "allow_browser_control": False, "allow_admin_commands": False,
        })
    elif preset == "custom":
        data.setdefault("allow_fs_write", False)
        data.setdefault("allow_process_start", False)
    else:
        raise ValueError(f"Unknown security preset: {preset}")

    for key in FEATURE_PLATFORM_CAPABILITIES:
        if not feature_supported(key):
            data[key] = False

    data["allowed_roots"] = preserved_allowed
    data["denied_roots"] = preserved_denied
    data.setdefault("enable_audit_log", True)
    return data


def validate_roots(roots: list[str]) -> list[str]:
    cleaned: list[str] = []
    for root in roots:
        root = root.strip()
        if not root:
            continue
        expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
        if expanded not in cleaned:
            cleaned.append(expanded)
    return cleaned


def configure_public_url(config: dict[str, Any], value: str) -> dict[str, Any]:
    data = deepcopy(config)
    value = value.strip().rstrip("/")
    loopback_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if not value:
        data["public_base_url"] = ""
        data["allowed_hosts"] = loopback_hosts
        return data

    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Tunnel / public endpoint must be a valid https:// URL.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Enter the base tunnel URL only, without /mcp, query parameters, or fragments.")

    data["public_base_url"] = value
    data["allowed_hosts"] = loopback_hosts + [parsed.hostname, f"{parsed.hostname}:*"]
    data.setdefault("oauth_state_path", ".portamcp/oauth_state.json")
    data.setdefault("oauth_owner_password_file", ".portamcp/oauth_owner_password.txt")
    data.setdefault("oauth_access_token_ttl_seconds", 3600)
    data.setdefault("oauth_refresh_token_ttl_seconds", 2_592_000)
    return data


def detect_tailscale_url() -> str:
    exe = tailscale_executable()
    if exe is None:
        return ""
    try:
        result = subprocess.run(
            [str(exe), "status", "--json"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=creation_flags_no_window(),
        )
        if result.returncode != 0:
            return ""
        data = json.loads(result.stdout)
        dns = str(data.get("Self", {}).get("DNSName", "")).strip().rstrip(".")
        return f"https://{dns}" if dns else ""
    except Exception:
        return ""


class TailscaleOperatorRequired(RuntimeError):
    """Raised when Linux tailscaled requires one-time local operator access."""


def _tailscale_error_text(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()


def _is_tailscale_operator_error(message: str) -> bool:
    value = message.lower()
    return (
        sys.platform.startswith("linux")
        and (
            "serve config denied" in value
            or "sending serve config: access denied" in value
            or ("--operator" in value and "access denied" in value)
        )
    )


def is_tailscale_public_config(config: dict[str, Any]) -> bool:
    public = str(config.get("public_base_url") or "").strip().rstrip("/")
    detected = detect_tailscale_url().strip().rstrip("/")
    return bool(public and detected and public.lower() == detected.lower())


def tailscale_operator_setup_command() -> str:
    username = getpass.getuser().strip()
    if not username:
        raise RuntimeError("Could not determine the current Linux username.")
    return f"sudo tailscale set --operator={username}"


def enable_tailscale_operator() -> dict[str, Any]:
    """Grant this Linux desktop user local tailscaled operator access via PolicyKit."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Tailscale operator setup is only required on Linux.")
    exe = tailscale_executable()
    if exe is None:
        raise RuntimeError("Tailscale CLI was not found.")
    username = getpass.getuser().strip()
    if not username:
        raise RuntimeError("Could not determine the current Linux username.")

    pkexec = shutil.which("pkexec")
    if not pkexec:
        raise RuntimeError(
            "Linux Tailscale needs one-time operator permission. Run this in a terminal: "
            + tailscale_operator_setup_command()
        )

    result = subprocess.run(
        [pkexec, str(exe), "set", f"--operator={username}"],
        capture_output=True,
        text=True,
        timeout=90,
        creationflags=creation_flags_no_window(),
    )
    if result.returncode != 0:
        message = _tailscale_error_text(result)
        if result.returncode in {126, 127} and not message:
            message = "System authorization was cancelled or denied."
        raise RuntimeError(
            (message or "Could not grant Tailscale operator permission.")
            + "\n\nYou can run this once in a terminal instead:\n"
            + tailscale_operator_setup_command()
        )
    return {"ok": True, "operator": username}


def recover_tailscale_funnel(config: dict[str, Any]) -> dict[str, Any]:
    """Reset and reopen this machine's Funnel for the configured PortaMCP port."""
    exe = tailscale_executable()
    if exe is None:
        raise RuntimeError("Tailscale CLI was not found.")
    if not is_tailscale_public_config(config):
        raise RuntimeError(
            "The configured public_base_url does not match this machine's Tailscale DNS URL."
        )

    port = int(config.get("port", 8765))
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")

    common = {
        "capture_output": True,
        "text": True,
        "creationflags": creation_flags_no_window(),
    }

    reset = subprocess.run(
        [str(exe), "funnel", "reset"],
        timeout=12,
        **common,
    )
    if reset.returncode != 0:
        message = _tailscale_error_text(reset) or "tailscale funnel reset failed"
        if _is_tailscale_operator_error(message):
            raise TailscaleOperatorRequired(message)
        raise RuntimeError(message)

    reopen = subprocess.run(
        [str(exe), "funnel", "--bg", str(port)],
        timeout=20,
        **common,
    )
    if reopen.returncode != 0:
        message = _tailscale_error_text(reopen) or "tailscale funnel start failed"
        if _is_tailscale_operator_error(message):
            raise TailscaleOperatorRequired(message)
        raise RuntimeError(message)

    status = subprocess.run(
        [str(exe), "funnel", "status"],
        timeout=10,
        **common,
    )
    if status.returncode != 0:
        raise RuntimeError(
            (status.stderr or status.stdout or "tailscale funnel status failed").strip()
        )

    public = str(config.get("public_base_url") or "").strip().rstrip("/")
    if public and public.lower() not in status.stdout.lower():
        raise RuntimeError("Tailscale Funnel restarted but the configured public URL is not active.")

    return {
        "ok": True,
        "port": port,
        "public_base_url": public,
        "status": status.stdout.strip(),
    }


def endpoint_for(profile: str, config: dict[str, Any]) -> str:
    host = str(config.get("host", "127.0.0.1"))
    port = int(config.get("port", 8765))
    public = str(config.get("public_base_url", "")).strip().rstrip("/")
    if profile == "local":
        return f"http://127.0.0.1:{port}/mcp"
    if public:
        return f"{public}/mcp"
    return f"http://{host}:{port}/mcp"


def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def server_health(port: int = 8765, timeout: float = 0.5) -> bool:
    """Return True only when the local PortaMCP transport manager is healthy."""
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/healthz",
            headers={"Cache-Control": "no-cache"},
        )
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
            return int(response.status) == 200
    except Exception:
        return False


def _path_is_within_project(value: str | os.PathLike[str]) -> bool:
    try:
        raw = os.fspath(value)
        if not str(raw).strip():
            return False
        candidate = os.path.normcase(os.path.abspath(raw))
        project = os.path.normcase(os.path.abspath(os.fspath(PROJECT_ROOT)))
        return os.path.commonpath([candidate, project]) == project
    except (OSError, ValueError, TypeError):
        return False


def server_processes() -> list[dict[str, Any]]:
    """Return live PortaMCP runtimes belonging to this installation only."""
    candidates: dict[int, dict[str, Any]] = {}
    for proc in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "create_time"]):
        try:
            name = str(proc.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            cmd = " ".join(proc.info.get("cmdline") or [])
            if not any(marker in cmd for marker in SERVER_MARKERS):
                continue
            try:
                cwd = proc.cwd()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                cwd = ""
            exe = str(proc.info.get("exe") or "")
            if not (_path_is_within_project(cwd) or _path_is_within_project(exe)):
                continue
            candidates[proc.pid] = {
                "pid": proc.pid,
                "ppid": int(proc.info.get("ppid") or 0),
                "name": proc.info.get("name") or "python",
                "exe": exe,
                "cmdline": cmd,
                "cwd": cwd,
                "create_time": proc.info.get("create_time") or 0,
            }
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue

    # A Windows venv launcher can spawn the base interpreter with the same
    # command line. Keep the deepest child and hide its launcher parent.
    parent_pids = {row["ppid"] for row in candidates.values() if row["ppid"] in candidates}
    rows = [row for pid, row in candidates.items() if pid not in parent_pids]
    return sorted(rows, key=lambda row: float(row.get("create_time", 0)))


def stop_server_processes() -> list[int]:
    killed: list[int] = []
    for row in server_processes():
        try:
            proc = psutil.Process(int(row["pid"]))
            children = proc.children(recursive=True)

            # psutil.terminate() maps to TerminateProcess on Windows, which skips
            # Uvicorn/ASGI lifespan shutdown. Prefer a same-session named event
            # handled by runtime_server.py, then fall back to the old forced path
            # if the server is older, unresponsive, or not one of our runtimes.
            graceful_requested = (
                os.name == "nt" and request_windows_graceful_shutdown(proc.pid)
            )
            if not graceful_requested:
                proc.terminate()

            _, alive = psutil.wait_procs(
                [proc],
                timeout=5 if graceful_requested else 3,
            )
            for item in alive:
                item.kill()
            for child in children:
                try:
                    if child.is_running():
                        child.kill()
                except psutil.Error:
                    pass
            killed.append(proc.pid)
        except psutil.Error:
            continue
    return killed


def build_server_command(profile: str, config: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    if profile not in PROFILE_MODULES:
        raise ValueError(f"Unknown auth profile: {profile}")
    if not VENV_PYTHON.exists():
        raise RuntimeError("Virtual environment is not installed yet.")

    port = int(config.get("port", 8765))
    host = str(config.get("host", "127.0.0.1"))
    env = os.environ.copy()
    env["PORTAMCP_CONFIG"] = str(CONFIG_PATH)

    module = PROFILE_MODULES[profile]
    cmd = [
        str(VENV_PYTHON),
        "-u",
        "-m",
        module,
        "--config",
        str(CONFIG_PATH),
    ]
    if profile == "local":
        cmd += ["--host", "127.0.0.1", "--port", str(port)]
    else:
        cmd += ["--host", host, "--port", str(port)]

    if profile == "bearer":
        env["PORTAMCP_BEARER_TOKEN"] = bearer_token()
    elif profile == "oauth" and not str(config.get("public_base_url", "")).strip():
        raise ValueError("OAuth requires an HTTPS public/tunnel base URL.")

    return cmd, env


def _tail_text_file(path: Path, lines: int, block_size: int = 65_536) -> str:
    wanted = max(1, int(lines))
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        chunks: list[bytes] = []
        newline_count = 0
        while position > 0 and newline_count <= wanted:
            size = min(block_size, position)
            position -= size
            handle.seek(position)
            block = handle.read(size)
            chunks.append(block)
            newline_count += block.count(bytes([10]))
        data = b"".join(reversed(chunks))
    text = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(text[-wanted:])


def tail_audit(lines: int = 120) -> str:
    if not AUDIT_PATH.exists():
        return "No audit log yet."
    try:
        return _tail_text_file(AUDIT_PATH, lines)
    except Exception as exc:
        return f"Could not read audit log: {exc}"


def tail_runtime_diagnostics(lines: int = 120) -> str:
    chunks: list[str] = []
    for label, path in (("SERVER", SERVER_LOG_PATH), ("TRANSPORT", TRANSPORT_LOG_PATH)):
        if not path.exists():
            chunks.append(f"[{label}] No log yet.")
            continue
        try:
            chunks.append(f"[{label}]\n" + _tail_text_file(path, lines))
        except Exception as exc:
            chunks.append(f"[{label}] Could not read log: {exc}")
    return "\n\n".join(chunks)


def chrome_extension_status() -> dict[str, Any]:
    manifest = CHROME_EXTENSION_DIR / "manifest.json"
    bridge_config = CHROME_EXTENSION_DIR / "bridge_config.js"
    return {
        "directory_exists": CHROME_EXTENSION_DIR.exists(),
        "manifest_exists": manifest.exists(),
        "bridge_config_exists": bridge_config.exists(),
        "path": str(CHROME_EXTENSION_DIR),
    }


def open_path(path: Path) -> None:
    open_local_path(path)


def open_url(url: str) -> None:
    import webbrowser

    webbrowser.open(url)
