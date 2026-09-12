from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .platform_support import platform_capabilities


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path)))


def _unique_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    for raw in paths:
        if not raw:
            continue
        normalized = _expand(raw)
        if normalized not in result:
            result.append(normalized)
    return result


def default_denied_roots() -> list[str]:
    """Return portable sensitive-path templates resolved when Settings loads."""
    if os.name == "nt":
        return [
            r"%USERPROFILE%\.ssh",
            r"%USERPROFILE%\.gnupg",
            r"%USERPROFILE%\.aws",
            r"%USERPROFILE%\.azure",
            r"%USERPROFILE%\.kube",
            r"%WINDIR%\System32\config",
            r"%APPDATA%\Microsoft\Credentials",
            r"%APPDATA%\Microsoft\Protect",
            r"%LOCALAPPDATA%\Microsoft\Credentials",
            r"%LOCALAPPDATA%\Microsoft\Vault",
            r"%LOCALAPPDATA%\Google\Chrome\User Data",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
        ]
    return [
        "~/.ssh",
        "~/.gnupg",
        "~/.aws",
        "~/.azure",
        "~/.kube",
        "~/.docker",
        "~/.config/gcloud",
        "~/.config/gh",
        "~/.config/google-chrome",
        "~/.config/chromium",
        "~/.config/microsoft-edge",
        "~/.mozilla/firefox",
        "~/.local/share/keyrings",
        "/etc/ssh",
        "/etc/shadow",
        "/etc/gshadow",
    ]


@dataclass(slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    transport: str = "streamable-http"
    bearer_token: str = ""
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*"])
    allowed_origins: list[str] = field(default_factory=list)

    # Filesystem access starts with no granted scope. Users explicitly add roots
    # from the Control Center. Denied roots are generated from this machine.
    allowed_roots: list[str] = field(default_factory=list)
    denied_roots: list[str] = field(default_factory=default_denied_roots)

    max_read_bytes: int = 5_000_000
    max_write_bytes: int = 5_000_000
    max_command_output_bytes: int = 1_000_000
    command_timeout_seconds: int = 120

    # Safe first-run capability baseline. Selecting a preset in the UI changes
    # capabilities, but never grants filesystem roots automatically.
    allow_fs_write: bool = False
    allow_shell: bool = False
    allow_process_start: bool = False
    allow_process_kill: bool = False
    allow_destructive_fs: bool = False
    allow_input_control: bool = False
    allow_ui_automation: bool = False
    allow_browser_control: bool = False
    allow_admin_commands: bool = False

    enable_audit_log: bool = True
    audit_log_path: str = ".portamcp/audit.jsonl"
    emergency_stop_file: str = ".portamcp/EMERGENCY_STOP"
    browser_profile_dir: str = ".portamcp/browser-profile"
    chrome_bridge_token_file: str = ".portamcp/chrome_bridge_token.txt"
    chrome_extension_dir: str = "chrome_extension"
    playwright_headless: bool = False

    public_base_url: str = ""
    oauth_state_path: str = ".portamcp/oauth_state.json"
    oauth_owner_password_file: str = ".portamcp/oauth_owner_password.txt"
    oauth_access_token_ttl_seconds: int = 3600
    oauth_refresh_token_ttl_seconds: int = 2_592_000

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        data: dict[str, Any] = {}
        config_path = path or os.getenv("PORTAMCP_CONFIG")
        if config_path:
            with open(config_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)

        env_map = {
            "PORTAMCP_HOST": ("host", str),
            "PORTAMCP_PORT": ("port", int),
            "PORTAMCP_TRANSPORT": ("transport", str),
            "PORTAMCP_BEARER_TOKEN": ("bearer_token", str),
        }
        for env, (key, conv) in env_map.items():
            if env in os.environ:
                data[key] = conv(os.environ[env])

        settings = cls(**data)
        settings.allowed_roots = _unique_paths(settings.allowed_roots)
        settings.denied_roots = _unique_paths(settings.denied_roots)
        settings.audit_log_path = _expand(settings.audit_log_path)
        settings.emergency_stop_file = _expand(settings.emergency_stop_file)
        settings.browser_profile_dir = _expand(settings.browser_profile_dir)
        settings.chrome_bridge_token_file = _expand(settings.chrome_bridge_token_file)
        settings.chrome_extension_dir = _expand(settings.chrome_extension_dir)
        settings.oauth_state_path = _expand(settings.oauth_state_path)
        settings.oauth_owner_password_file = _expand(settings.oauth_owner_password_file)
        settings.public_base_url = settings.public_base_url.strip().rstrip("/")
        return settings

    def ensure_runtime_dirs(self) -> None:
        Path(self.audit_log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.emergency_stop_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.browser_profile_dir).mkdir(parents=True, exist_ok=True)
        Path(self.chrome_bridge_token_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.chrome_extension_dir).mkdir(parents=True, exist_ok=True)
        Path(self.oauth_state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.oauth_owner_password_file).parent.mkdir(parents=True, exist_ok=True)

    def public_summary(self) -> dict[str, Any]:
        return {
            "platform_support": platform_capabilities(),
            "host": self.host,
            "port": self.port,
            "transport": self.transport,
            "allowed_roots": self.allowed_roots,
            "denied_roots": self.denied_roots,
            "max_read_bytes": self.max_read_bytes,
            "max_write_bytes": self.max_write_bytes,
            "command_timeout_seconds": self.command_timeout_seconds,
            "public_base_url": self.public_base_url,
            "chrome_extension_dir": self.chrome_extension_dir,
            "features": {
                "fs_write": self.allow_fs_write,
                "shell": self.allow_shell,
                "process_start": self.allow_process_start,
                "process_kill": self.allow_process_kill,
                "destructive_fs": self.allow_destructive_fs,
                "input_control": self.allow_input_control,
                "ui_automation": self.allow_ui_automation,
                "browser_control": self.allow_browser_control,
                "admin_commands": self.allow_admin_commands,
            },
        }


def default_config() -> dict[str, Any]:
    return asdict(Settings())


def generate_token() -> str:
    return secrets.token_urlsafe(48)
