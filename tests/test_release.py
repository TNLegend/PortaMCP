from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import stat
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import porta_mcp.control_center_backend as backend_module
import porta_mcp.oauth_server as oauth_server_module
import porta_mcp.self_hosted_oauth as oauth_module
import porta_mcp.tools.browser as browser_module
import porta_mcp.tools.filesystem as filesystem_module
import porta_mcp.tools.input as input_module
import porta_mcp.tools.linux_ui as linux_ui_module
import porta_mcp.tools.screen as screen_module
import porta_mcp.tools.shell as shell_module
import porta_mcp.tools.windows_ui as windows_ui_module
from porta_mcp.audit import AuditLogger
from porta_mcp.chrome_bridge import ChromeBridgeHub
from porta_mcp.config import Settings, default_config, default_denied_roots
from porta_mcp.control_center_backend import (
    CONFIG_PATH,
    PROJECT_ROOT,
    _path_is_within_project,
    apply_preset,
    build_server_command,
    configure_public_url,
    reset_denied_roots,
)
from porta_mcp.http_server import build_http_app
from porta_mcp.local_http_server import _loopback_allowed_hosts, build_local_http_app
from porta_mcp.oauth_server import build_oauth_app
from porta_mcp.platform_support import (
    os_family,
    platform_capabilities,
    project_venv_dir,
    venv_python,
)
from porta_mcp.policy import Policy, PolicyError
from porta_mcp.self_hosted_oauth import (
    OAuthStateStore,
    _is_safe_redirect_uri,
    _pkce_s256,
)
from porta_mcp.server import _assert_safe_direct_http_host, build_server

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "system_info", "server_capabilities", "security_emergency_stop_status",
    "fs_list", "fs_stat", "fs_read_text", "fs_read_bytes", "fs_write_text", "fs_patch_text",
    "fs_search", "fs_hash", "fs_copy", "fs_exists", "fs_mkdir", "fs_move", "fs_delete",
    "shell_run", "shell_session_create", "shell_session_run", "shell_session_close",
    "process_list", "process_start", "process_kill",
    "git_status", "git_diff", "git_log", "git_add", "git_commit",
    "screen_monitors", "screen_capture", "screen_capture_region", "screen_capture_to_file",
    "input_mouse_position", "input_mouse_click", "input_mouse_move", "input_mouse_drag", "input_scroll",
    "input_type_text", "input_press", "input_hotkey",
    "windows_active", "windows_list", "windows_focus", "windows_ui_tree", "windows_click_control", "windows_set_text",
    "linux_active", "linux_list", "linux_focus", "linux_ui_tree", "linux_click_control", "linux_set_text",
    "browser_start", "browser_connect_cdp", "browser_pages", "browser_new_page", "browser_navigate",
    "browser_snapshot", "browser_click", "browser_fill", "browser_press", "browser_evaluate", "browser_screenshot",
    "browser_close_page", "browser_stop",
    "chrome_live_status", "chrome_live_tabs", "chrome_live_snapshot", "chrome_live_activate", "chrome_live_navigate",
    "chrome_live_click", "chrome_live_fill", "chrome_live_evaluate", "chrome_live_screenshot", "chrome_live_detach",
}


def _runtime_settings(tmp_path: Path, **overrides) -> Settings:
    data = default_config()
    data.update(
        {
            "audit_log_path": str(tmp_path / "audit.jsonl"),
            "emergency_stop_file": str(tmp_path / "EMERGENCY_STOP"),
            "browser_profile_dir": str(tmp_path / "browser-profile"),
            "chrome_bridge_token_file": str(tmp_path / "chrome_bridge_token.txt"),
            "chrome_extension_dir": str(tmp_path / "chrome_extension"),
            "oauth_state_path": str(tmp_path / "oauth_state.json"),
            "oauth_owner_password_file": str(tmp_path / "oauth_owner_password.txt"),
        }
    )
    data.update(overrides)
    return Settings(**data)


def _safe_required_value(name: str, schema: dict) -> object:
    if name in {"path", "src", "dst"}:
        return str(ROOT / ".portamcp" / "release-smoke-target")
    if name == "repo":
        return str(ROOT)
    if name == "command":
        return "Write-Output release-smoke"
    if name == "session_id":
        return "missing-session"
    if name == "executable":
        return "cmd.exe"
    if name == "page_id":
        return "missing-page"
    if name == "url":
        return "about:blank"
    if name == "cdp_url":
        return "http://127.0.0.1:1"
    if name == "javascript":
        return "1"
    if name == "text":
        return "release-smoke"
    if name == "key":
        return "esc"
    if name == "keys":
        return ["ctrl"]
    if name == "paths":
        return ["README.md"]
    if name == "pid":
        return 2_147_000_000
    if name == "handle":
        return 0
    if name == "tab_id":
        return -1
    if name in {"width", "height"}:
        return 1
    if name in {"x", "y", "x1", "y1", "x2", "y2"}:
        return 0
    if name == "clicks":
        return 1
    kind = schema.get("type")
    if kind == "integer":
        return 1
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    return "release-smoke"


def test_fresh_defaults_are_fail_closed_and_portable() -> None:
    cfg = default_config()
    assert cfg["allowed_roots"] == []
    assert cfg["public_base_url"] == ""
    assert cfg["bearer_token"] == ""
    for key in (
        "allow_fs_write", "allow_shell", "allow_process_start", "allow_process_kill",
        "allow_destructive_fs", "allow_input_control", "allow_ui_automation",
        "allow_browser_control", "allow_admin_commands",
    ):
        assert cfg[key] is False
    denied = cfg["denied_roots"]
    assert denied == default_denied_roots()
    if os.name == "nt":
        assert all(not Path(x).is_absolute() for x in denied)
        assert any("%USERPROFILE%" in x for x in denied)
        assert any("Chrome" in x for x in denied)
        assert any("Edge" in x for x in denied)


@pytest.mark.parametrize("preset", ["full", "balanced", "readonly"])
def test_presets_change_capabilities_but_never_scopes(preset: str) -> None:
    original = default_config()
    original["allowed_roots"] = [r"X:\explicit-scope"]
    original["denied_roots"] = [r"%USERPROFILE%\.ssh", r"%USERPROFILE%\.private"]
    updated = apply_preset(original, preset)
    assert updated["allowed_roots"] == original["allowed_roots"]
    assert updated["denied_roots"] == original["denied_roots"]


def test_reset_denied_roots_returns_fresh_portable_defaults() -> None:
    first = reset_denied_roots()
    second = reset_denied_roots()
    assert first == default_denied_roots()
    assert second == default_denied_roots()
    assert first is not second


def test_tunnel_configuration_is_https_only_and_scope_neutral() -> None:
    cfg = default_config()
    cfg["allowed_roots"] = [r"X:\explicit-scope"]
    with pytest.raises(ValueError):
        configure_public_url(cfg, "http://example.test")
    with pytest.raises(ValueError):
        configure_public_url(cfg, "https://example.test/mcp")
    updated = configure_public_url(cfg, "https://example.test")
    assert updated["public_base_url"] == "https://example.test"
    assert updated["allowed_roots"] == cfg["allowed_roots"]
    assert "example.test" in updated["allowed_hosts"]
    assert "127.0.0.1:*" in updated["allowed_hosts"]


def test_local_no_auth_is_loopback_only(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path, host="0.0.0.0")
    with pytest.raises(RuntimeError, match="loopback"):
        build_local_http_app(settings)


def test_bearer_mode_requires_and_enforces_token(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="bearer_token"):
        build_http_app(_runtime_settings(tmp_path, bearer_token=""))

    settings = _runtime_settings(tmp_path, bearer_token="release-test-token")
    app = build_http_app(settings)
    client = TestClient(app)
    with TestClient(app) as health_client:
        health = health_client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"ok": True, "service": "PortaMCP"}
    response = client.post("/mcp")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_oauth_requires_https_public_base(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="HTTPS public endpoint"):
        build_oauth_app(_runtime_settings(tmp_path, public_base_url=""))

    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "mcp.example.test", "mcp.example.test:*"],
    )
    app = build_oauth_app(settings)
    with TestClient(app, base_url="https://mcp.example.test") as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"ok": True, "service": "PortaMCP"}
        response = client.get("/.well-known/oauth-authorization-server")
        protected = client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == "https://mcp.example.test"
    assert body["authorization_response_iss_parameter_supported"] is False
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "refresh_token" in body["grant_types_supported"]
    assert "offline_access" in body["scopes_supported"]

    assert protected.status_code == 200
    resource = protected.json()
    assert resource["resource"] == "https://mcp.example.test/mcp"
    assert resource["authorization_servers"] == ["https://mcp.example.test"]


def test_oauth_mcp_transport_stays_sessionful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=["mcp.example.test", "mcp.example.test:*"],
    )
    captured: dict[str, Any] = {}
    real_build_server = oauth_server_module.build_server

    def build_server_spy(current_settings: Settings):
        mcp, ctx = real_build_server(current_settings)
        real_streamable_http_app = mcp.streamable_http_app

        def streamable_http_app_spy(*args, **kwargs):
            captured.update(kwargs)
            return real_streamable_http_app(*args, **kwargs)

        monkeypatch.setattr(mcp, "streamable_http_app", streamable_http_app_spy)
        return mcp, ctx

    monkeypatch.setattr(oauth_server_module, "build_server", build_server_spy)
    oauth_server_module.build_oauth_app(settings)

    assert captured["stateless_http"] is False
    assert captured["json_response"] is False


def test_oauth_callback_responses_omit_issuer_parameter(tmp_path: Path) -> None:
    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "mcp.example.test",
            "mcp.example.test:*",
        ],
    )
    app = build_oauth_app(settings)
    redirect_uri = "https://" + "chat" + "gpt.com/connector_platform_oauth_redirect"
    metadata = {
        "client_name": "Issuer probe",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "offline_access pc:control",
        "token_endpoint_auth_method": "none",
    }
    with TestClient(app, base_url="https://mcp.example.test") as client:
        registered = client.post("/register", json=metadata)
        assert registered.status_code == 201
        client_id = registered.json()["client_id"]

        unsupported = client.get(
            "/authorize",
            params={
                "response_type": "token",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "issuer-test",
            },
            follow_redirects=False,
        )
        assert unsupported.status_code == 302
        location = unsupported.headers["location"]
        assert "error=unsupported_response_type" in location
        assert "state=issuer-test" in location
        assert "iss=" not in location

        unknown = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": "missing-client",
                "redirect_uri": redirect_uri,
            },
            follow_redirects=False,
        )
        assert unknown.status_code == 400
        assert unknown.json()["error"] == "unauthorized_client"


def test_oauth_stale_bearer_is_fail_closed_without_http_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "mcp.example.test",
            "mcp.example.test:*",
        ],
    )
    app = build_oauth_app(settings)
    request_body = {
        "jsonrpc": "2.0",
        "id": 77,
        "method": "tools/list",
        "params": {},
    }
    with TestClient(app, base_url="https://mcp.example.test") as client:
        landing = client.get("/")
        assert landing.status_code == 200
        assert landing.json()["mcp_endpoint"] == "/mcp"

        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Disallow: /" in robots.text
        favicon = client.get("/favicon.ico")
        assert favicon.status_code == 204

        bootstrap = client.post("/mcp", json=request_body)
        assert bootstrap.status_code == 401
        assert "resource_metadata" in bootstrap.headers.get("www-authenticate", "")

        root_bootstrap = client.post("/", json=request_body)
        assert root_bootstrap.status_code == 401
        assert "resource_metadata" in root_bootstrap.headers.get("www-authenticate", "")

        monkeypatch.setattr(
            oauth_module.SelfHostedOAuth,
            "token_was_just_issued",
            lambda self: True,
        )
        post_token_missing = client.post("/mcp", json=request_body)
        assert post_token_missing.status_code == 200
        assert (
            post_token_missing.headers.get("x-portamcp-auth")
            == "post-token-missing-bearer"
        )
        assert post_token_missing.json()["error"]["code"] == -32001

        stale = client.post(
            "/mcp",
            json=request_body,
            headers={"Authorization": "Bearer definitely-stale-token"},
        )
        assert stale.status_code == 200
        assert stale.headers.get("x-portamcp-auth") == "stale-bearer"
        payload = stale.json()
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == 77
        assert payload["error"]["code"] == -32001
        assert payload["error"]["data"]["reauthenticate"] is True

        root_stale = client.post(
            "/",
            json=request_body,
            headers={"Authorization": "Bearer definitely-stale-token"},
        )
        assert root_stale.status_code == 200
        assert root_stale.headers.get("x-portamcp-auth") == "stale-bearer"
        assert root_stale.json()["error"]["code"] == -32001


def test_oauth_public_host_is_derived_from_public_base_url(tmp_path: Path) -> None:
    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
    )
    app = build_oauth_app(settings)
    with TestClient(app, base_url="https://mcp.example.test") as client:
        response = client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        assert response.json()["issuer"] == "https://mcp.example.test"


def test_server_command_always_passes_absolute_config_path() -> None:
    config = default_config()
    config["public_base_url"] = "https://mcp.example.test"
    for profile in ("local", "bearer", "oauth"):
        cmd, env = build_server_command(profile, config)
        assert "--config" in cmd
        idx = cmd.index("--config")
        assert Path(cmd[idx + 1]).resolve() == CONFIG_PATH.resolve()
        assert env["PORTAMCP_CONFIG"] == str(CONFIG_PATH)


def test_tailscale_public_recovery_uses_reset_reopen_status_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Completed:
        def __init__(self, argv: list[str]) -> None:
            self.returncode = 0
            self.stderr = ""
            self.stdout = (
                "https://mcp.example.test (Funnel on)\n"
                "|-- / proxy http://127.0.0.1:8765\n"
                if argv[-2:] == ["funnel", "status"]
                else ""
            )

    def fake_run(argv, **kwargs):
        args = [str(x) for x in argv]
        calls.append(args)
        return Completed(args)

    monkeypatch.setattr(
        backend_module,
        "tailscale_executable",
        lambda: Path("tailscale.exe"),
    )
    monkeypatch.setattr(
        backend_module,
        "is_tailscale_public_config",
        lambda config: True,
    )
    monkeypatch.setattr(backend_module.subprocess, "run", fake_run)

    result = backend_module.recover_tailscale_funnel(
        {
            "port": 8765,
            "public_base_url": "https://mcp.example.test",
        }
    )

    assert result["ok"] is True
    assert calls == [
        ["tailscale.exe", "funnel", "reset"],
        ["tailscale.exe", "funnel", "--bg", "8765"],
        ["tailscale.exe", "funnel", "status"],
    ]


def test_tailscale_public_recovery_detects_linux_operator_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        returncode = 1
        stdout = ""
        stderr = (
            "sending serve config: Access denied: serve config denied\n"
            "Use 'sudo tailscale funnel reset'.\n"
            "To not require root, use 'sudo tailscale set --operator=$USER' once."
        )

    monkeypatch.setattr(backend_module.sys, "platform", "linux")
    monkeypatch.setattr(
        backend_module,
        "tailscale_executable",
        lambda: Path("/usr/bin/tailscale"),
    )
    monkeypatch.setattr(
        backend_module,
        "is_tailscale_public_config",
        lambda config: True,
    )
    monkeypatch.setattr(
        backend_module.subprocess,
        "run",
        lambda *args, **kwargs: Completed(),
    )

    with pytest.raises(backend_module.TailscaleOperatorRequired):
        backend_module.recover_tailscale_funnel(
            {
                "port": 8765,
                "public_base_url": "https://mcp.example.test",
            }
        )


def test_enable_tailscale_operator_uses_pkexec_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append([str(x) for x in argv])
        return Completed()

    monkeypatch.setattr(backend_module.sys, "platform", "linux")
    monkeypatch.setattr(backend_module.getpass, "getuser", lambda: "devasc")
    monkeypatch.setattr(
        backend_module,
        "tailscale_executable",
        lambda: Path("/usr/bin/tailscale"),
    )
    monkeypatch.setattr(
        backend_module.shutil,
        "which",
        lambda name: "/usr/bin/pkexec" if name == "pkexec" else None,
    )
    monkeypatch.setattr(backend_module.subprocess, "run", fake_run)

    result = backend_module.enable_tailscale_operator()

    assert result == {"ok": True, "operator": "devasc"}
    assert len(calls) == 1
    assert calls[0][0] == "/usr/bin/pkexec"
    assert calls[0][1].replace("\\", "/").endswith("/usr/bin/tailscale")
    assert calls[0][2:] == ["set", "--operator=devasc"]


def test_linux_shell_admin_and_destructive_commands_are_guarded(tmp_path: Path) -> None:
    settings = _runtime_settings(
        tmp_path,
        allow_shell=True,
        allow_destructive_fs=False,
        allow_admin_commands=False,
    )
    audit = AuditLogger(settings.audit_log_path, enabled=False)
    policy = Policy(settings, audit)

    for command in (
        "sudo apt update",
        "pkexec tailscale set --operator=user",
        "systemctl restart ssh",
        "apt install curl",
        "useradd testuser",
    ):
        with pytest.raises(PolicyError, match="Administrative shell command"):
            policy.assert_shell(command)

    for command in (
        "rm -rf ./data",
        "unlink ./secret.txt",
        "mkfs.ext4 /dev/sdb1",
        "dd if=/dev/zero of=/dev/sdb",
        "reboot",
        "find . -delete",
    ):
        with pytest.raises(PolicyError, match="Potentially destructive shell command"):
            policy.assert_shell(command)

    policy.assert_shell("printf 'safe' && ls -la")


def test_preset_disables_platform_unsupported_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend_module,
        "platform_capabilities",
        lambda: {
            "input": True,
            "windows_ui": False,
            "linux_ui": False,
            "ui_automation": False,
            "managed_browser": True,
        },
    )
    config = default_config()
    result = backend_module.apply_preset(config, "full")
    assert result["allow_input_control"] is True
    assert result["allow_ui_automation"] is False
    assert result["allow_browser_control"] is True


def test_emergency_stop_blocks_policy_in_isolation(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path)
    settings.allowed_roots = [str(tmp_path)]
    settings.allow_fs_write = True
    audit = AuditLogger(settings.audit_log_path, enabled=False)
    policy = Policy(settings, audit)
    Path(settings.emergency_stop_file).touch()
    with pytest.raises(PolicyError, match="Emergency stop"):
        policy.assert_path(str(tmp_path / "file.txt"), write=True)


def test_exact_75_tool_surface_and_default_smoke(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path)
    server, _ = build_server(settings)
    tools = server._tool_manager._tools
    assert len(tools) == 75
    assert set(tools) == EXPECTED_TOOLS

    failures: list[str] = []
    for name, tool in sorted(tools.items()):
        schema = tool.parameters
        kwargs: dict[str, object] = {}
        for required in schema.get("required", []):
            kwargs[required] = _safe_required_value(required, schema.get("properties", {}).get(required, {}))
        try:
            result = tool.fn(**kwargs)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
        except Exception as exc:  # direct smoke must never escape a tool wrapper
            failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
            continue
        if result is None:
            failures.append(f"{name}: returned None")
            continue
        if isinstance(result, dict):
            for key in ("ok", "action", "data", "error", "warnings"):
                if key not in result:
                    failures.append(f"{name}: missing result key {key}")
                    break
    assert not failures, "\n".join(failures)


def test_launcher_and_first_run_are_relative_and_branded() -> None:
    exe = ROOT / "PortaMCP.exe"
    linux_exe = ROOT / "PortaMCP"
    launcher = (ROOT / "launcher" / "PortaMCPLauncher.cs").read_text(encoding="utf-8")
    linux_launcher = (ROOT / "launcher" / "PortaMCPLauncherLinux.c").read_text(encoding="utf-8")
    linux_builder = (ROOT / "launcher" / "build-launcher-linux.sh").read_text(encoding="utf-8")
    git_mode_prep = (ROOT / "launcher" / "prepare-git-modes.ps1").read_text(encoding="utf-8")
    public_archive_builder = (ROOT / "launcher" / "build-public-archive.py").read_text(encoding="utf-8")
    git_attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    windows_builder = (ROOT / "launcher" / "build-launcher.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "porta_mcp" / "installer.py").read_text(encoding="utf-8")
    bootstrap = (ROOT / "portamcp.pyw").read_text(encoding="utf-8")
    shell_launcher = (ROOT / "portamcp.sh").read_text(encoding="utf-8")

    assert linux_exe.is_file() and linux_exe.stat().st_size > 0
    if os.name == "nt":
        assert exe.is_file() and exe.stat().st_size > 0

    # Windows release launcher owns first-run bootstrap and does not require a
    # preinstalled Python. The release carries a portable CPython runtime rather
    # than installing Python into the user's system. All paths are relative.
    runtime_builder = (ROOT / "launcher" / "build-bootstrap-runtime.ps1").read_text(encoding="utf-8")
    assert "AppDomain.CurrentDomain.BaseDirectory" in launcher
    assert '"runtime\\\\python-bootstrap.zip"' in launcher
    assert '"runtime\\\\python-bootstrap.sha256"' in launcher
    assert "SHA256.Create" in launcher
    assert "ZipFile.OpenRead" in launcher
    assert "unsafe path" in launcher
    assert "PromoteDirectoryWithRetry" in launcher
    assert "Thread.Sleep(200)" in launcher
    assert "repeated attempts" in launcher
    assert "Resolve the issue above and choose Retry." in launcher
    assert "Check your Internet connection and choose Retry." not in launcher
    assert 'Path.Combine(root, ".portamcp", "bootstrap-python")' in launcher
    assert "python.org/ftp" not in launcher
    assert 'new PythonCommand("py.exe"' not in launcher
    assert 'new PythonCommand("python.exe"' not in launcher
    assert "InstallAllUsers" not in launcher
    assert "PrependPath" not in launcher
    assert "porta_mcp.installer" in launcher
    assert 'Path.Combine(root, ".venv", "Scripts", "python.exe")' in launcher
    assert "-m porta_mcp.control_center" in launcher
    assert 'Path.Combine(runtimeDir, "setup.log")' in launcher
    assert "Install Python and launch PortaMCP.exe again" not in launcher
    assert "bootstrap-runtime-stage" in runtime_builder
    assert '[string]$ExpectedVersion = "3.13.15"' in runtime_builder
    assert "import sys, tkinter, venv, ensurepip" in runtime_builder
    assert "python-bootstrap.zip" in runtime_builder
    assert "python-bootstrap.sha256" in runtime_builder
    assert "Get-FileHash $RuntimeArchive -Algorithm SHA256" in windows_builder
    assert "bootstrap runtime checksum does not match" in windows_builder

    # Linux gets its own native C launcher. It can request narrowly scoped
    # package-manager setup through PolicyKit, then creates .venv-linux itself.
    assert 'readlink("/proc/self/exe"' in linux_launcher
    assert '"pkexec"' in linux_launcher
    assert "apt-get" in linux_launcher
    assert "python3-venv" in linux_launcher
    assert "python3-tk" in linux_launcher
    assert "python3-gi" in linux_launcher
    assert "gir1.2-atspi-2.0" in linux_launcher
    assert '"git"' in linux_launcher
    assert '"zenity"' in linux_launcher
    assert ".venv-linux/bin/python" in linux_launcher
    assert "porta_mcp.installer" in linux_launcher
    assert "porta_mcp.control_center" in linux_launcher
    assert "gcc -std=c11" in linux_builder
    assert 'exec "$ROOT/PortaMCP"' in shell_launcher
    assert "git update-index --add --chmod=+x" in git_mode_prep
    assert '"100755"' in git_mode_prep
    for required in ("PortaMCP", "portamcp.sh", "launcher/build-launcher-linux.sh"):
        assert required in git_mode_prep
    assert "*.sh text eol=lf" in git_attributes
    assert "PortaMCP -text" in git_attributes
    assert "PortaMCP.exe -text" in git_attributes
    assert "EXECUTABLE_PATHS" in public_archive_builder
    assert "info.external_attr = mode << 16" in public_archive_builder
    assert '"runtime/python-bootstrap.zip"' in public_archive_builder
    assert '"chrome_extension/bridge_config.js"' not in public_archive_builder

    # Python dependency install is self-healing and remains isolated.
    assert "_ensure_clean_venv" in installer
    assert '"-m", "ensurepip", "--upgrade"' in installer
    assert '"-e", "."' in installer
    assert '"playwright", "install", "chromium"' in installer
    assert "porta_mcp.installer" in bootstrap

    combined = launcher + linux_launcher + linux_builder + installer + bootstrap + shell_launcher
    assert "D:\\" not in combined
    assert "C:\\Users\\" not in combined

def test_linux_git_executable_modes_when_git_metadata_is_available() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("Git metadata is intentionally absent from this source tree")
    paths = ["PortaMCP", "portamcp.sh", "launcher/build-launcher-linux.sh"]
    completed = subprocess.run(
        ["git", "ls-files", "--stage", "--", *paths],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    modes: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        match = re.match(r"^(\d{6})\s+[0-9a-f]+\s+\d+\t(.+)$", line)
        if match:
            modes[match.group(2)] = match.group(1)
    assert modes == {path: "100755" for path in paths}

def test_public_archive_builder_is_sanitized_and_preserves_linux_modes(tmp_path: Path) -> None:
    output = tmp_path / "PortaMCP-public.zip"
    subprocess.run(
        [sys.executable, str(ROOT / "launcher" / "build-public-archive.py"), str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert output.is_file() and output.stat().st_size > 0
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        required = {
            "PortaMCP",
            "PortaMCP.exe",
            "portamcp.sh",
            "launcher/build-launcher-linux.sh",
            "launcher/prepare-git-modes.ps1",
            "runtime/python-bootstrap.zip",
            "runtime/python-bootstrap.sha256",
            "chrome_extension/manifest.json",
            "chrome_extension/service_worker.js",
        }
        assert required <= names
        forbidden = {"config.json", "chrome_extension/bridge_config.js"}
        assert not (forbidden & names)
        assert not any(
            part in {".git", ".venv", ".venv-linux", ".portamcp", "__pycache__", "build", "dist"}
            for name in names
            for part in Path(name).parts
        )
        for name in ("PortaMCP", "portamcp.sh", "launcher/build-launcher-linux.sh"):
            mode = (archive.getinfo(name).external_attr >> 16) & 0o177777
            assert mode == (stat.S_IFREG | 0o755)

def test_chrome_bridge_branding_and_generated_config_is_ignored() -> None:
    manifest_path = ROOT / "chrome_extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "PortaMCP Chrome Bridge"
    assert "PortaMCP" in manifest.get("description", "")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "chrome_extension/bridge_config.js" in ignore


def test_public_text_surface_has_no_stale_client_or_machine_terms() -> None:
    excluded_parts = {".venv", ".venv-linux", ".portamcp", "__pycache__", ".pytest_cache", ".ruff_cache"}
    ignored_names = {"config.json", "bridge_config.js"}
    extensions = {".py", ".pyw", ".md", ".toml", ".txt", ".json", ".js", ".cs", ".c", ".ps1", ".sh"}
    banned = (
        "co" + "dex",
        "chat" + "gpt",
        "open" + "ai",
        "mcp-security-" + "framework",
        "chat" + "gpt_oauth",
        "co" + "dex_mcp",
        "c:" + "\\users\\",
        "\u00e2\u20ac\u00a2",
    )
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or any(part in excluded_parts for part in path.parts)
            or path.name in ignored_names
            or (path.name.startswith("AUDIT_") and path.suffix.lower() == ".md")
        ):
            continue
        if path.suffix.lower() not in extensions and path.name != ".gitignore":
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for term in banned:
            if term in text:
                hits.append(f"{path.relative_to(ROOT)}: {term}")
    assert not hits, "\n".join(hits)


def test_generic_direct_http_refuses_non_loopback(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="unauthenticated"):
        _assert_safe_direct_http_host(_runtime_settings(tmp_path, host="0.0.0.0"))
    for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
        _assert_safe_direct_http_host(_runtime_settings(tmp_path, host=host))


def test_local_no_auth_forces_loopback_allowed_hosts() -> None:
    assert _loopback_allowed_hosts() == ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def test_public_url_replaces_and_clears_stale_remote_hosts() -> None:
    cfg = default_config()
    cfg["public_base_url"] = "https://old.example.test"
    cfg["allowed_hosts"] = [
        "127.0.0.1:*", "localhost:*", "[::1]:*", "old.example.test", "old.example.test:*", "stale.example.test"
    ]
    changed = configure_public_url(cfg, "https://new.example.test")
    assert changed["allowed_hosts"] == [
        "127.0.0.1:*", "localhost:*", "[::1]:*", "new.example.test", "new.example.test:*"
    ]
    assert "old.example.test" not in changed["allowed_hosts"]
    cleared = configure_public_url(changed, "")
    assert cleared["public_base_url"] == ""
    assert cleared["allowed_hosts"] == ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def test_oauth_state_survives_restart_but_not_endpoint_change(tmp_path: Path) -> None:
    state_path = tmp_path / "oauth_state.json"
    issuer = "https://one.example.test"
    resource = issuer + "/mcp"
    store = OAuthStateStore(str(state_path), issuer=issuer, resource_url=resource)
    client = store.register_client({"redirect_uris": ["http://127.0.0.1/callback"], "scope": "pc:control offline_access"})
    with store._lock:
        pair = store._mint_pair_locked(
            client_id=str(client["client_id"]), scope="pc:control offline_access", resource=resource,
            access_ttl=3600, refresh_ttl=2_592_000,
        )

    restarted = OAuthStateStore(str(state_path), issuer=issuer, resource_url=resource)
    loaded_client = restarted.get_client(str(client["client_id"]))
    assert loaded_client is not None
    refreshed = restarted.exchange_refresh(
        client=loaded_client,
        refresh_token=str(pair["refresh_token"]),
        requested_scope=None,
        access_ttl=3600,
        refresh_ttl=2_592_000,
    )
    assert refreshed is not None and refreshed.get("access_token")
    # Refresh must be safe under concurrent/retried client refreshes: the same
    # still-valid refresh token can be exchanged again instead of racing into
    # an invalid_grant response.
    refreshed_again = restarted.exchange_refresh(
        client=loaded_client,
        refresh_token=str(pair["refresh_token"]),
        requested_scope=None,
        access_ttl=3600,
        refresh_ttl=2_592_000,
    )
    assert refreshed_again is not None and refreshed_again.get("access_token")
    assert refreshed_again == refreshed
    assert refreshed_again.get("refresh_token") != pair["refresh_token"]
    assert len(restarted.refresh_tokens) == 1

    changed = OAuthStateStore(
        str(state_path), issuer="https://two.example.test", resource_url="https://two.example.test/mcp"
    )
    assert changed.clients == {}
    assert changed.access_tokens == {}
    assert changed.refresh_tokens == {}
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["issuer"] == "https://two.example.test"


def test_oauth_refresh_is_concurrency_safe(tmp_path: Path) -> None:
    issuer = "https://refresh.example.test"
    resource = issuer + "/mcp"
    store = OAuthStateStore(str(tmp_path / "oauth_state.json"), issuer=issuer, resource_url=resource)
    client = store.register_client({
        "redirect_uris": ["http://127.0.0.1/callback"],
        "scope": "pc:control offline_access",
    })
    with store._lock:
        pair = store._mint_pair_locked(
            client_id=str(client["client_id"]),
            scope="pc:control offline_access",
            resource=resource,
            access_ttl=3600,
            refresh_ttl=2_592_000,
        )

    token = str(pair["refresh_token"])

    def do_refresh(_: int) -> dict[str, Any] | None:
        return store.exchange_refresh(
            client=client,
            refresh_token=token,
            requested_scope=None,
            access_ttl=3600,
            refresh_ttl=2_592_000,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do_refresh, range(16)))

    assert all(result is not None for result in results)
    assert all(result and result.get("access_token") for result in results)
    first = results[0]
    assert first is not None
    assert all(result == first for result in results)
    assert first.get("refresh_token") != token
    assert len(store.refresh_tokens) == 1


def test_oauth_refresh_replay_survives_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "oauth_state.json"
    issuer = "https://restart-refresh.example.test"
    resource = issuer + "/mcp"
    store = OAuthStateStore(str(state_path), issuer=issuer, resource_url=resource)
    client = store.register_client({
        "redirect_uris": ["http://127.0.0.1/callback"],
        "scope": "pc:control offline_access",
    })
    with store._lock:
        pair = store._mint_pair_locked(
            client_id=str(client["client_id"]),
            scope="pc:control offline_access",
            resource=resource,
            access_ttl=3600,
            refresh_ttl=2_592_000,
        )

    old_refresh = str(pair["refresh_token"])
    first = store.exchange_refresh(
        client=client,
        refresh_token=old_refresh,
        requested_scope=None,
        access_ttl=3600,
        refresh_ttl=2_592_000,
    )
    assert first is not None

    restarted = OAuthStateStore(str(state_path), issuer=issuer, resource_url=resource)
    loaded_client = restarted.get_client(str(client["client_id"]))
    assert loaded_client is not None
    recovered = restarted.exchange_refresh(
        client=loaded_client,
        refresh_token=old_refresh,
        requested_scope=None,
        access_ttl=3600,
        refresh_ttl=2_592_000,
    )
    assert recovered is not None
    assert recovered.get("access_token")
    assert recovered.get("refresh_token")
    assert recovered.get("refresh_token") != old_refresh


def test_oauth_discovery_aliases_and_revoke_are_reconnect_safe(tmp_path: Path) -> None:
    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "mcp.example.test",
            "mcp.example.test:*",
        ],
    )
    app = build_oauth_app(settings)
    with TestClient(app, base_url="https://mcp.example.test") as client:
        for path in (
            "/.well-known/openid-configuration",
            "/.well-known/openid-configuration/mcp",
            "/mcp/.well-known/openid-configuration",
            "/mcp/.well-known/oauth-authorization-server",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json()["issuer"] == "https://mcp.example.test"

        # Revocation must be idempotent and must not turn reconnect cleanup
        # into a transport-fatal 401.
        response = client.post(
            "/revoke",
            data={"token": "already-gone-or-unknown-token"},
        )
        assert response.status_code == 200


def test_oauth_repeated_public_registration_reuses_client(tmp_path: Path) -> None:
    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "mcp.example.test",
            "mcp.example.test:*",
        ],
    )
    app = build_oauth_app(settings)
    metadata = {
        "client_name": "Chat" + "GPT",
        "redirect_uris": ["https://" + "chat" + "gpt.com/connector/oauth/test"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "offline_access pc:control",
        "token_endpoint_auth_method": "none",
    }
    with TestClient(app, base_url="https://mcp.example.test") as client:
        first = client.post("/register", json=metadata)
        second = client.post("/register", json=metadata)
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["client_id"] == second.json()["client_id"]


def test_oauth_bad_password_keeps_pending_authorization(tmp_path: Path) -> None:
    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "mcp.example.test",
            "mcp.example.test:*",
        ],
    )
    Path(settings.oauth_owner_password_file).write_text("correct-password", encoding="utf-8")
    app = build_oauth_app(settings)
    metadata = {
        "client_name": "Chat" + "GPT",
        "redirect_uris": ["https://" + "chat" + "gpt.com/connector/oauth/test"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "offline_access pc:control",
        "token_endpoint_auth_method": "none",
    }
    verifier = "B" * 43
    challenge = oauth_module._pkce_s256(verifier)
    assert challenge is not None

    with TestClient(app, base_url="https://mcp.example.test") as client:
        registered = client.post("/register", json=metadata).json()
        auth = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registered["client_id"],
                "redirect_uri": metadata["redirect_uris"][0],
                "scope": metadata["scope"],
                "state": "success-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "https://mcp.example.test/mcp",
            },
        )
        assert auth.status_code == 200
        match = re.search(r'name="pending_id" value="([^"]+)"', auth.text)
        assert match is not None
        pending_id = match.group(1)

        bad = client.post(
            "/authorize/decision",
            data={
                "pending_id": pending_id,
                "decision": "allow",
                "password": "wrong-password",
            },
        )
        assert bad.status_code == 401

        good = client.post(
            "/authorize/decision",
            data={
                "pending_id": pending_id,
                "decision": "allow",
                "password": "correct-password",
            },
            follow_redirects=False,
        )
        assert good.status_code == 302
        location = good.headers["location"]
        assert "iss=" not in location
        assert "state=success-state" in location
        code_match = re.search(r"[?&]code=([^&]+)", location)
        assert code_match is not None
        code = code_match.group(1)

        wrong_resource = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registered["client_id"],
                "code": code,
                "redirect_uri": metadata["redirect_uris"][0],
                "code_verifier": verifier,
                "resource": "https://mcp.example.test/not-mcp",
            },
        )
        assert wrong_resource.status_code == 400
        assert wrong_resource.json()["error"] == "invalid_target"

        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registered["client_id"],
                "code": code,
                "redirect_uri": metadata["redirect_uris"][0],
                "code_verifier": verifier,
                "resource": "https://mcp.example.test/mcp",
            },
        )
        assert token.status_code == 200
        token_body = token.json()
        assert token_body["token_type"] == "Bearer"
        assert token_body["access_token"]
        assert token_body["refresh_token"]


def test_oauth_post_token_grace_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = 1000.0
    monkeypatch.setattr(oauth_module.time, "monotonic", lambda: base)
    oauth = oauth_module.SelfHostedOAuth(
        public_base_url="https://mcp.example.test",
        state_path=str(tmp_path / "oauth_state.json"),
        owner_password_file=str(tmp_path / "oauth_owner_password.txt"),
    )
    assert oauth.token_was_just_issued() is False
    oauth.note_token_issued()
    assert oauth.token_was_just_issued() is True

    monkeypatch.setattr(
        oauth_module.time,
        "monotonic",
        lambda: base + oauth_module.POST_TOKEN_AUTH_GRACE_SECONDS + 0.1,
    )
    assert oauth.token_was_just_issued() is False


def test_oauth_access_token_allows_small_clock_skew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = 1_800_000_000
    monkeypatch.setattr(oauth_module, "_now", lambda: base)
    issuer = "https://skew.example.test"
    resource = issuer + "/mcp"
    store = OAuthStateStore(str(tmp_path / "oauth_state.json"), issuer=issuer, resource_url=resource)
    client = store.register_client({
        "redirect_uris": ["http://127.0.0.1/callback"],
        "scope": "pc:control offline_access",
    })
    with store._lock:
        pair = store._mint_pair_locked(
            client_id=str(client["client_id"]),
            scope="pc:control offline_access",
            resource=resource,
            access_ttl=3600,
            refresh_ttl=2_592_000,
        )

    monkeypatch.setattr(oauth_module, "_now", lambda: base + 3630)
    assert store.verify_access_token(str(pair["access_token"])) is not None

    monkeypatch.setattr(
        oauth_module,
        "_now",
        lambda: base + 3600 + oauth_module.ACCESS_TOKEN_CLOCK_SKEW_SECONDS + 1,
    )
    assert store.verify_access_token(str(pair["access_token"])) is None


def test_oauth_access_token_is_bound_to_mcp_resource(tmp_path: Path) -> None:
    issuer = "https://resource.example.test"
    resource = issuer + "/mcp"
    store = OAuthStateStore(
        str(tmp_path / "oauth_state.json"),
        issuer=issuer,
        resource_url=resource,
    )
    client = store.register_client(
        {
            "redirect_uris": ["http://127.0.0.1/callback"],
            "scope": "pc:control offline_access",
        }
    )
    with store._lock:
        wrong = store._mint_pair_locked(
            client_id=str(client["client_id"]),
            scope="pc:control offline_access",
            resource=issuer + "/different-resource",
            access_ttl=3600,
            refresh_ttl=2_592_000,
        )
        correct = store._mint_pair_locked(
            client_id=str(client["client_id"]),
            scope="pc:control offline_access",
            resource=resource,
            access_ttl=3600,
            refresh_ttl=2_592_000,
        )

    assert store.verify_access_token(str(wrong["access_token"])) is None
    verified = store.verify_access_token(str(correct["access_token"]))
    assert verified is not None
    assert verified["resource"] == resource


def test_transport_logging_records_status_without_credentials(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path, host="127.0.0.1")
    app = build_local_http_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/definitely-missing", headers={"Authorization": "Bearer never-log-me"})
        assert response.status_code == 404

    log_path = tmp_path / "transport.log"
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "path=/definitely-missing" in text
    assert "status=404" in text
    assert "never-log-me" not in text

    start = re.search(
        r"request_start id=([0-9a-f]{8}) method=GET path=/definitely-missing",
        text,
    )
    assert start is not None
    request_id = start.group(1)
    assert (
        f"request_end id={request_id} method=GET path=/definitely-missing "
        "status=404"
    ) in text


def test_oauth_redirect_and_pkce_validation_fail_closed() -> None:
    assert _is_safe_redirect_uri("https://client.example/callback")
    assert _is_safe_redirect_uri("http://127.0.0.1:8765/callback")
    assert not _is_safe_redirect_uri("https://client.example/callback#fragment")
    assert not _is_safe_redirect_uri("https://user:pass@client.example/callback")
    assert _pkce_s256("a" * 43)
    assert _pkce_s256("verifier-" + chr(0xE9)) is None


def test_dynamic_registration_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _runtime_settings(
        tmp_path,
        public_base_url="https://mcp.example.test",
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "mcp.example.test", "mcp.example.test:*"],
    )
    app = build_oauth_app(settings)
    client = TestClient(app, base_url="https://mcp.example.test")
    response = client.post("/register", content=b"x" * (oauth_module.MAX_REGISTRATION_BYTES + 1))
    assert response.status_code == 413

    monkeypatch.setattr(oauth_module, "MAX_DYNAMIC_CLIENTS", 1)
    metadata = {"redirect_uris": ["http://127.0.0.1/callback"], "scope": "pc:control offline_access"}
    assert client.post("/register", json=metadata).status_code == 201
    assert client.post("/register", json=metadata).status_code == 429


def test_chrome_extension_config_import_is_guarded() -> None:
    worker = (ROOT / "chrome_extension" / "service_worker.js").read_text(encoding="utf-8")
    assert 'try {\n  importScripts("bridge_config.js");' in worker
    assert "globalThis.PORTAMCP_BRIDGE || null" in worker


def test_chrome_old_disconnect_does_not_break_new_connection() -> None:
    loop = asyncio.new_event_loop()
    try:
        hub = ChromeBridgeHub()
        old = object()
        new = object()
        future = loop.create_future()
        hub.websocket = new  # type: ignore[assignment]
        hub._pending["request"] = future
        hub._disconnect(old)  # type: ignore[arg-type]
        assert hub.websocket is new
        assert not future.done()
        hub._disconnect(new)  # type: ignore[arg-type]
        assert future.done()
        with pytest.raises(RuntimeError, match="disconnected"):
            future.result()
    finally:
        loop.close()


def test_chrome_reconnect_fails_requests_bound_to_old_socket() -> None:
    class Client:
        host = "127.0.0.1"

    class OldSocket:
        def __init__(self) -> None:
            self.closed = False

        async def close(self, code: int) -> None:
            self.closed = True

    class NewSocket:
        client = Client()
        query_params = {"token": "bridge-secret"}

        async def accept(self) -> None:
            return None

        async def receive_json(self):
            raise WebSocketDisconnect()

    async def scenario() -> None:
        hub = ChromeBridgeHub()
        old = OldSocket()
        new = NewSocket()
        hub.websocket = old  # type: ignore[assignment]
        future = asyncio.get_running_loop().create_future()
        hub._pending["old-request"] = future

        await hub.handle(new, "bridge-secret")  # type: ignore[arg-type]

        assert old.closed is True
        assert future.done()
        with pytest.raises(RuntimeError, match="reconnected"):
            future.result()
        assert hub.websocket is None

    asyncio.run(scenario())


def test_shell_run_caps_interactive_timeout_and_terminates_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(
        tmp_path,
        allowed_roots=[str(tmp_path)],
        allow_shell=True,
        command_timeout_seconds=300,
    )
    server, _ = build_server(settings)

    class FakeProc:
        pid = 43210

        def __init__(self) -> None:
            self.returncode = None
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise shell_module.subprocess.TimeoutExpired("fake", timeout)
            return ("partial-output", "")

    fake = FakeProc()
    terminated: list[int] = []

    monkeypatch.setattr(shell_module, "INTERACTIVE_SHELL_BUDGET_SECONDS", 1)
    monkeypatch.setattr(shell_module.subprocess, "Popen", lambda *a, **k: fake)

    def terminate(proc) -> None:
        terminated.append(proc.pid)
        proc.returncode = -9

    monkeypatch.setattr(shell_module, "_terminate_process_tree", terminate)

    result = server._tool_manager._tools["shell_run"].fn(
        "Write-Output never-finishes",
        str(tmp_path),
        300,
    )

    assert result["ok"] is True
    assert result["data"]["timed_out"] is True
    assert result["data"]["effective_timeout_seconds"] == 1
    assert terminated == [43210]
    assert any("process_start" in warning for warning in result["warnings"])


def test_shell_session_run_is_serialized_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(
        tmp_path,
        allowed_roots=[str(tmp_path)],
        allow_shell=True,
    )
    server, _ = build_server(settings)
    create = server._tool_manager._tools["shell_session_create"].fn
    run = server._tool_manager._tools["shell_session_run"].fn
    created = create(str(tmp_path))
    session_id = str(created["data"]["session_id"])

    active = 0
    max_active = 0

    def fake_run(app, command: str, cwd: str, timeout: int) -> dict:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.04)
        active -= 1
        return {"ok": True, "action": "shell_run", "data": {"returncode": 0, "stdout": command, "stderr": "", "cwd": cwd}, "error": None, "warnings": []}

    monkeypatch.setattr(shell_module, "_run", fake_run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: run(session_id, f"cmd-{i}", 30), range(8)))

    assert all(result["ok"] is True for result in results)
    assert max_active == 1


def test_managed_browser_tools_are_serialized(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path, allow_browser_control=True)
    server, ctx = build_server(settings)
    active = 0
    max_active = 0

    class FakePage:
        url = "about:blank"

        def is_closed(self) -> bool:
            return False

        async def title(self) -> str:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.03)
            active -= 1
            return "fake"

    ctx.state.pages["page"] = FakePage()
    pages = server._tool_manager._tools["browser_pages"].fn

    async def scenario() -> list[dict]:
        return await asyncio.gather(pages(), pages(), pages())

    results = asyncio.run(scenario())
    assert all(result["ok"] is True for result in results)
    assert max_active == 1


def test_linux_browser_start_falls_back_from_system_to_playwright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class FakePage:
        pass

    class FakeContext:
        async def new_page(self):
            return FakePage()

    class FakeBrowser:
        async def new_context(self):
            return FakeContext()

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch(self, **kwargs):
            calls.append(dict(kwargs))
            if "executable_path" in kwargs:
                raise RuntimeError("system browser launch failed")
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

        async def stop(self) -> None:
            return None

    class FakeStarter:
        async def start(self):
            return FakePlaywright()

    monkeypatch.setattr(browser_module, "is_linux", lambda: True)
    monkeypatch.setattr(
        browser_module,
        "system_chromium_executable",
        lambda: Path("/usr/bin/chromium"),
    )
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: FakeStarter(),
    )

    settings = _runtime_settings(tmp_path, allow_browser_control=True)
    server, _ = build_server(settings)
    result = asyncio.run(server._tool_manager._tools["browser_start"].fn())

    assert result["ok"] is True
    assert result["data"]["backend"] == "playwright-fallback"
    assert len(calls) == 2
    assert "executable_path" in calls[0]
    assert "executable_path" not in calls[1]


def test_browser_failed_start_cleans_partial_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeChromium:
        async def launch(self, **kwargs):
            raise RuntimeError("launch failed")

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FakeStarter:
        def __init__(self, playwright) -> None:
            self.playwright = playwright

        async def start(self):
            return self.playwright

    fake = FakePlaywright()
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: FakeStarter(fake))
    settings = _runtime_settings(tmp_path, allow_browser_control=True)
    server, ctx = build_server(settings)
    result = asyncio.run(server._tool_manager._tools["browser_start"].fn())
    assert result["ok"] is False
    assert fake.stopped is True
    assert ctx.state.playwright is None
    assert ctx.state.browser is None
    assert ctx.state.browser_context is None
    assert ctx.state.pages == {}


def test_browser_failed_cdp_connect_cleans_new_playwright(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeChromium:
        async def connect_over_cdp(self, url: str):
            raise RuntimeError("cdp failed")

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FakeStarter:
        def __init__(self, playwright) -> None:
            self.playwright = playwright

        async def start(self):
            return self.playwright

    fake = FakePlaywright()
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: FakeStarter(fake))
    settings = _runtime_settings(tmp_path, allow_browser_control=True)
    server, ctx = build_server(settings)
    result = asyncio.run(server._tool_manager._tools["browser_connect_cdp"].fn("http://127.0.0.1:1"))
    assert result["ok"] is False
    assert fake.stopped is True
    assert ctx.state.playwright is None
    assert ctx.state.browser is None


def test_browser_stop_clears_state_even_when_close_fails(tmp_path: Path) -> None:
    class FakeBrowser:
        async def close(self) -> None:
            raise RuntimeError("close failed")

    class FakePlaywright:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    settings = _runtime_settings(tmp_path, allow_browser_control=True)
    server, ctx = build_server(settings)
    fake_playwright = FakePlaywright()
    ctx.state.browser = FakeBrowser()
    ctx.state.playwright = fake_playwright
    ctx.state.browser_context = object()
    ctx.state.pages["page"] = object()
    result = asyncio.run(server._tool_manager._tools["browser_stop"].fn())
    assert result["ok"] is False
    assert fake_playwright.stopped is True
    assert ctx.state.browser is None
    assert ctx.state.playwright is None
    assert ctx.state.browser_context is None
    assert ctx.state.pages == {}


def test_recursive_filesystem_tools_stop_at_traversal_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    settings = _runtime_settings(tmp_path, allowed_roots=[str(tmp_path)])
    server, _ = build_server(settings)

    monkeypatch.setattr(filesystem_module, "FILESYSTEM_TRAVERSAL_BUDGET_SECONDS", 30)

    values = iter([0.0, 31.0])
    monkeypatch.setattr(
        filesystem_module.time,
        "monotonic",
        lambda: next(values, 31.0),
    )
    listed = server._tool_manager._tools["fs_list"].fn(
        str(tmp_path),
        True,
        "*",
        1000,
    )
    assert listed["ok"] is True
    assert listed["data"]["timed_out"] is True
    assert listed["data"]["truncated"] is True

    values = iter([0.0, 31.0])
    monkeypatch.setattr(
        filesystem_module.time,
        "monotonic",
        lambda: next(values, 31.0),
    )
    searched = server._tool_manager._tools["fs_search"].fn(
        str(tmp_path),
        "needle",
        "*",
        False,
        200,
    )
    assert searched["ok"] is True
    assert searched["data"]["timed_out"] is True
    assert searched["data"]["truncated"] is True


def test_fs_patch_text_respects_max_read_bytes(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_text("abcdefghij", encoding="utf-8")
    settings = _runtime_settings(
        tmp_path, allowed_roots=[str(tmp_path)], allow_fs_write=True, max_read_bytes=4, max_write_bytes=1024
    )
    server, _ = build_server(settings)
    result = server._tool_manager._tools["fs_patch_text"].fn(str(target), "a", "z")
    assert result["ok"] is False
    assert "max_read_bytes" in json.dumps(result)
    assert target.read_text(encoding="utf-8") == "abcdefghij"


def test_fs_copy_rejects_symlink_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    source.mkdir()
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = source / "escape"
    try:
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
        settings = _runtime_settings(tmp_path, allowed_roots=[str(tmp_path)], allow_fs_write=True)
        server, _ = build_server(settings)
        result = server._tool_manager._tools["fs_copy"].fn(str(source), str(tmp_path / "copy"))
        assert result["ok"] is False
        assert "symlink or junction" in json.dumps(result).lower()
    finally:
        try:
            link.unlink()
        except OSError:
            pass
        try:
            (outside / "secret.txt").unlink()
            outside.rmdir()
        except OSError:
            pass


def test_windows_text_key_specs_use_return_for_newlines() -> None:
    specs = input_module._windows_text_key_specs("A\nB\r\nC✓")

    assert len(specs) == 6
    assert specs[0] == [(0, ord("A"), 0x0004)]
    assert specs[1] == [(0x0D, 0, 0)]
    assert specs[2] == [(0, ord("B"), 0x0004)]
    assert specs[3] == [(0x0D, 0, 0)]
    assert specs[4] == [(0, ord("C"), 0x0004)]
    assert specs[5] == [(0, ord("✓"), 0x0004)]


def test_windows_unicode_text_gets_minimum_delivery_delay() -> None:
    assert input_module._windows_text_effective_interval("ASCII only", 0.0) == 0.0
    assert input_module._windows_text_effective_interval("AéB", 0.0) == 0.02
    assert input_module._windows_text_effective_interval("A✓漢字B", 0.01) == 0.02
    assert input_module._windows_text_effective_interval("AéB", 0.05) == 0.05


def test_windows_newline_backend_uses_native_keybd_event() -> None:
    assert "keybd_event" in set(input_module._type_text_windows.__code__.co_names)


def test_input_type_text_uses_layout_independent_windows_unicode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(tmp_path, allow_input_control=True)
    server, _ = build_server(settings)
    typed: dict[str, object] = {}

    monkeypatch.setattr(input_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        input_module,
        "_type_text_windows",
        lambda text, interval: typed.update(text=text, interval=interval),
    )
    monkeypatch.setattr(
        input_module,
        "_pg",
        lambda: (_ for _ in ()).throw(AssertionError("PyAutoGUI must not type literal text on Windows")),
    )

    literal = "chrome://extensions :/@\\ é 😀"
    result = server._tool_manager._tools["input_type_text"].fn(literal, 0.0)

    assert result["ok"] is True
    assert result["data"]["backend"] == "windows-unicode"
    assert typed == {"text": literal, "interval": 0.0}


def test_input_type_text_uses_layout_independent_linux_x11_paste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(tmp_path, allow_input_control=True)
    server, _ = build_server(settings)
    typed: dict[str, object] = {}

    monkeypatch.setattr(input_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        input_module,
        "_type_text_linux_x11",
        lambda text, interval: typed.update(text=text, interval=interval),
    )
    monkeypatch.setattr(
        input_module,
        "_pg",
        lambda: (_ for _ in ()).throw(AssertionError("PyAutoGUI must not type literal text on Linux")),
    )

    literal = "chrome://extensions 123 :/@\\\\ é"
    result = server._tool_manager._tools["input_type_text"].fn(literal, 0.0)

    assert result["ok"] is True
    assert result["data"]["backend"] == "linux-x11-paste"
    assert typed == {"text": literal, "interval": 0.0}


def test_linux_x11_clipboard_snapshot_rejects_non_text_targets() -> None:
    class FakeTk:
        @staticmethod
        def splitlist(value: str) -> tuple[str, ...]:
            return tuple(value.split("|"))

    class FakeRoot:
        tk = FakeTk()

        @staticmethod
        def selection_get(*, selection: str, type: str) -> str:
            assert selection == "CLIPBOARD"
            if type == "TARGETS":
                return "TARGETS|TIMESTAMP|image/png"
            return ""

    with pytest.raises(RuntimeError, match="non-text clipboard"):
        input_module._x11_clipboard_snapshot(FakeRoot())


def test_linux_x11_clipboard_snapshot_preserves_plain_text() -> None:
    class FakeTk:
        @staticmethod
        def splitlist(value: str) -> tuple[str, ...]:
            return tuple(value.split("|"))

    class FakeRoot:
        tk = FakeTk()

        @staticmethod
        def selection_get(*, selection: str, type: str) -> str:
            assert selection == "CLIPBOARD"
            if type == "TARGETS":
                return "TARGETS|TIMESTAMP|UTF8_STRING|text/plain;charset=UTF-8"
            if type == "UTF8_STRING":
                return "previous ✓"
            raise AssertionError(type)

    assert input_module._x11_clipboard_snapshot(FakeRoot()) == (True, "previous ✓")


def test_linux_x11_clipboard_owner_reaper_clears_finished_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        waited = False

        def wait(self) -> int:
            self.waited = True
            return 0

    process = FakeProcess()
    monkeypatch.setattr(input_module, "_x11_restore_owner_process", process)

    input_module._x11_reap_clipboard_owner(process)  # type: ignore[arg-type]

    assert process.waited is True
    assert input_module._x11_restore_owner_process is None
    assert "deadline =" not in input_module._X11_CLIPBOARD_OWNER_CODE


def test_linux_all_black_mss_frame_uses_pillow_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Shot:
        rgb = bytes(3 * 4)
        size = (2, 2)

    class FakeMSS:
        @staticmethod
        def grab(spec: object) -> Shot:
            return Shot()

    seen: dict[str, object] = {}
    monkeypatch.setattr(screen_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        screen_module,
        "_pillow_capture_png",
        lambda spec: seen.setdefault("spec", spec) and b"pillow-png",
    )

    spec = {"left": 0, "top": 0, "width": 2, "height": 2}
    raw, backend = screen_module._capture_png(FakeMSS(), spec)

    assert raw == b"pillow-png"
    assert backend == "pillow-imagegrab"
    assert seen["spec"] == spec


def test_input_scroll_uses_native_windows_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(tmp_path, allow_input_control=True)
    server, _ = build_server(settings)
    calls: dict[str, object] = {}

    monkeypatch.setattr(input_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        input_module,
        "_scroll_windows",
        lambda clicks, x=None, y=None: calls.update(clicks=clicks, x=x, y=y),
    )
    monkeypatch.setattr(
        input_module,
        "_pg",
        lambda: (_ for _ in ()).throw(AssertionError("PyAutoGUI must not scroll on Windows")),
    )

    result = server._tool_manager._tools["input_scroll"].fn(-5, 10, 20)

    assert result["ok"] is True
    assert result["data"] == {"clicks": -5}
    assert calls == {"clicks": -5, "x": 10, "y": 20}


def test_windows_scroll_backend_uses_sendinput_not_legacy_mouse_event() -> None:
    names = set(input_module._scroll_windows.__code__.co_names)
    assert "SendInput" in names
    assert "mouse_event" not in names


def test_input_scroll_keeps_pyautogui_backend_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(tmp_path, allow_input_control=True)
    server, _ = build_server(settings)
    calls: dict[str, object] = {}

    class FakePG:
        @staticmethod
        def scroll(*, clicks: int, x: int | None = None, y: int | None = None) -> None:
            calls.update(clicks=clicks, x=x, y=y)

    monkeypatch.setattr(input_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(input_module, "_pg", lambda: FakePG())
    monkeypatch.setattr(
        input_module,
        "_scroll_windows",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Windows scroll backend must not run off Windows")
        ),
    )

    result = server._tool_manager._tools["input_scroll"].fn(7, 30, 40)

    assert result["ok"] is True
    assert result["data"] == {"clicks": 7}
    assert calls == {"clicks": 7, "x": 30, "y": 40}


def test_windows_set_text_finds_modern_document_without_polling() -> None:
    class Info:
        automation_id = "doc-id"

    class Control:
        element_info = Info()

        @staticmethod
        def window_text() -> str:
            return "Document body"

        @staticmethod
        def is_visible() -> bool:
            return True

        @staticmethod
        def is_enabled() -> bool:
            return True

    document = Control()

    class Root:
        calls: list[str] = []

        def descendants(self, *, control_type: str) -> list[Control]:
            self.calls.append(control_type)
            return [] if control_type == "Edit" else [document]

    root = Root()
    ctrl, control_type = windows_ui_module._find_text_control(root)

    assert ctrl is document
    assert control_type == "Document"
    assert root.calls == ["Edit", "Document"]


def test_windows_document_text_uses_unicode_keyboard_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class Range:
        @staticmethod
        def Select() -> None:
            calls["selected"] = True

    class TextPattern:
        DocumentRange = Range()

    class Control:
        iface_text = TextPattern()

        @staticmethod
        def set_focus() -> None:
            calls["focused"] = True

    monkeypatch.setattr(
        input_module,
        "_type_text_windows",
        lambda text, interval=0.0: calls.update(typed=(text, interval)),
    )

    backend = windows_ui_module._set_text_control(Control(), "Document", "éèà ✓ 漢字")

    assert backend == "windows-unicode-document"
    assert calls["focused"] is True
    assert calls["selected"] is True
    assert calls["typed"] == ("éèà ✓ 漢字", 0.0)


def test_windows_set_text_has_input_ceiling(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path, allow_ui_automation=True)
    server, _ = build_server(settings)
    result = server._tool_manager._tools["windows_set_text"].fn(0, "x" * 20_001)
    assert result["ok"] is False
    assert "20,000" in json.dumps(result)


def test_linux_ui_selector_is_fail_closed_on_ambiguity() -> None:
    class Node:
        def __init__(self, name: str, role: str, children: list["Node"] | None = None):
            self._name = name
            self._role = role
            self._children = children or []

        def get_name(self) -> str:
            return self._name

        def get_role_name(self) -> str:
            return self._role

        def get_accessible_id(self) -> str:
            return ""

        def get_process_id(self) -> int:
            return 123

        @property
        def path(self) -> str:
            return f"/fake/{id(self)}"

        def get_child_count(self) -> int:
            return len(self._children)

        def get_child_at_index(self, index: int) -> "Node":
            return self._children[index]

    first = Node("Save", "push button")
    second = Node("Save", "push button")
    root = Node("Window", "frame", [first, second])

    with pytest.raises(RuntimeError, match="ambiguous"):
        linux_ui_module._find_control(root, name="Save", role="push button")
    assert linux_ui_module._find_control(root, node_id=linux_ui_module._node_id(first)) is first


def test_linux_ui_action_prefers_semantic_click() -> None:
    class Node:
        performed: list[int] = []

        @staticmethod
        def get_n_actions() -> int:
            return 3

        @staticmethod
        def get_action_name(index: int) -> str:
            return ["show-menu", "click", "activate"][index]

        @classmethod
        def do_action(cls, index: int) -> bool:
            cls.performed.append(index)
            return True

    node = Node()
    action, index = linux_ui_module._invoke_control(node)
    assert action == "click"
    assert index == 1
    assert node.performed == [1]


def test_linux_set_text_has_input_ceiling(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path, allow_ui_automation=True)
    server, _ = build_server(settings)
    result = server._tool_manager._tools["linux_set_text"].fn("missing|/window", "x" * 20_001)
    assert result["ok"] is False
    assert "20,000" in json.dumps(result)


def test_linux_set_text_verifies_through_atspi_text_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(tmp_path, allow_ui_automation=True)
    server, _ = build_server(settings)
    calls: dict[str, object] = {}

    class Control:
        @staticmethod
        def set_text_contents(text: str) -> bool:
            calls["written"] = text
            return True

    ctrl = Control()

    class FakeText:
        @staticmethod
        def get_text(node: object, start: int, end: int) -> str:
            calls["read"] = (node, start, end)
            return str(calls["written"])

    class FakeAtspi:
        Text = FakeText

    monkeypatch.setattr(linux_ui_module, "_resolve_window", lambda window_id: (FakeAtspi, object(), {}))
    monkeypatch.setattr(linux_ui_module, "_find_control", lambda *args, **kwargs: ctrl)
    monkeypatch.setattr(linux_ui_module, "_interfaces", lambda node: {"EditableText", "Text"})
    monkeypatch.setattr(linux_ui_module, "_atspi", lambda: FakeAtspi)

    result = server._tool_manager._tools["linux_set_text"].fn("123|/window", "éèà ✓ 漢字", node_id="123|/node")

    assert result["ok"] is True
    assert result["data"]["verified"] is True
    assert calls["written"] == "éèà ✓ 漢字"
    assert calls["read"] == (ctrl, 0, -1)


def test_linux_focus_falls_back_to_x11_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(tmp_path, allow_ui_automation=True)
    server, _ = build_server(settings)
    row = {"window_id": "123|/window", "pid": 123, "title": "Target"}

    class Window:
        @staticmethod
        def grab_focus() -> bool:
            return False

    monkeypatch.setattr(linux_ui_module, "_resolve_window", lambda window_id: (object(), Window(), row))
    monkeypatch.setattr(linux_ui_module, "_find_focused_window", lambda: None)
    monkeypatch.setattr(linux_ui_module, "_actions", lambda node: [])
    monkeypatch.setattr(linux_ui_module, "_x11_focus_window", lambda value: value is row)

    result = server._tool_manager._tools["linux_focus"].fn(row["window_id"])

    assert result["ok"] is True
    assert result["data"]["window_id"] == row["window_id"]


def test_control_center_release_text_is_ascii_clean_and_current() -> None:
    control = ROOT / "porta_mcp" / "control_center.py"
    bootstrap = ROOT / "portamcp.pyw"
    for path in (control, bootstrap):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8")
        assert all(ord(ch) < 128 for ch in text)
    source = control.read_text(encoding="utf-8")
    assert "PortaMCP/" + "legacy" not in source
    dashboard = source.split("def _page_dashboard", 1)[1].split("def _page_connection", 1)[0]
    for key in (
        "allow_fs_write", "allow_shell", "allow_process_start", "allow_process_kill", "allow_destructive_fs",
        "allow_input_control", "allow_ui_automation", "allow_browser_control", "allow_admin_commands",
    ):
        assert key in dashboard
    assert "wraplength=660" in source
    assert "wraplength=650" in source
    assert "wraplength=430" in source
    assert "widget=toast" in source
    assert "self._schedule_runtime_refresh(500)" in source
    assert "self._schedule_runtime_refresh(700)" in source
    assert "self._schedule_runtime_refresh(400)" in source
    assert "self.after(1500, self._refresh_runtime_state)" not in source
    assert '"Recover public"' in source
    assert "def recover_public_connection" in source
    recovery = source.split("def recover_public_connection", 1)[1].split(
        "def _read_process_output", 1
    )[0]
    assert "backend.recover_tailscale_funnel(cfg)" in recovery
    assert recovery.index("backend.recover_tailscale_funnel(cfg)") < recovery.index(
        "backend.stop_server_processes()"
    )
    reset = source.split("def reset_oauth", 1)[1].split("def detect_tailscale", 1)[0]
    assert reset.index("server_processes") < reset.index("reset_oauth_state")
    assert "self.stop_server(ask=False)" in reset


def test_control_center_navigation_is_cached_and_setup_check_is_async() -> None:
    source = (ROOT / "porta_mcp" / "control_center.py").read_text(encoding="utf-8")
    routing = source.split("def show_page", 1)[1].split("# ---------- reusable UI ----------", 1)[0]
    assert "_page_cache" in routing
    assert "grid_remove()" in routing
    assert "winfo_children()" not in routing
    assert ".destroy()" not in routing

    setup = source.split("def _page_setup", 1)[1].split(
        "def _refresh_dependency_status", 1
    )[0]
    assert "backend.installed()" not in setup
    assert "Checking dependencies..." in setup
    assert "def _refresh_dependency_status" in source
    assert "threading.Thread(target=worker, daemon=True).start()" in source


def test_tail_text_file_reads_only_requested_tail(tmp_path: Path) -> None:
    target = tmp_path / "large.log"
    target.write_text(
        "".join(f"line-{index:05d}\n" for index in range(10_000)),
        encoding="utf-8",
    )
    tail = backend_module._tail_text_file(target, 7)
    assert tail.splitlines() == [
        "line-09993",
        "line-09994",
        "line-09995",
        "line-09996",
        "line-09997",
        "line-09998",
        "line-09999",
    ]


def test_server_process_scope_helper_rejects_other_install(tmp_path: Path) -> None:
    assert _path_is_within_project(PROJECT_ROOT)
    assert _path_is_within_project(PROJECT_ROOT / "porta_mcp")
    assert not _path_is_within_project("")
    assert not _path_is_within_project(tmp_path / "another-portamcp")


def test_dependency_checks_verify_chromium_executable() -> None:
    backend = (ROOT / "porta_mcp" / "control_center_backend.py").read_text(encoding="utf-8")
    bootstrap = (ROOT / "portamcp.pyw").read_text(encoding="utf-8")
    assert "p.chromium.executable_path" in backend
    assert "p.chromium.executable_path" in bootstrap


def test_cross_platform_venv_layout_and_linux_launcher() -> None:
    family = os_family()
    venv = project_venv_dir(ROOT)
    if family == "windows":
        assert venv.name == ".venv"
        assert venv_python(venv).name == "python.exe"
    elif family == "linux":
        assert venv.name == ".venv-linux"
        assert venv_python(venv) == venv / "bin" / "python"
    launcher_path = ROOT / "portamcp.sh"
    launcher = launcher_path.read_text(encoding="utf-8")
    assert b"\r\n" not in launcher_path.read_bytes()
    assert "python3" in launcher
    assert "python3.12" in launcher
    assert "python3.11" in launcher
    assert "Do not run the PortaMCP Control Center with sudo/root." in launcher
    assert "python3-tk" in launcher
    assert "ensurepip" in launcher
    assert "python${PYTHON_MM}-venv" in launcher
    assert "sudo apt install git" in launcher
    assert "xdotool" not in launcher
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "python3-xlib>=0.15; sys_platform == 'linux'" in requirements
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".venv-linux/" in ignore
    assert "chrome_extension_current_chrome/" in ignore
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "Operating System :: POSIX :: Linux" in pyproject
    assert "Operating System :: Microsoft :: Windows" in pyproject
    assert "python3-xlib>=0.15; sys_platform == 'linux'" in pyproject


def test_linux_ui_merges_x11_fallback_without_duplicate(monkeypatch) -> None:
    atspi = {
        "window_id": "42|/org/a11y/atspi/accessible/1",
        "pid": 42,
        "application": "Terminal",
        "title": "Terminal",
        "role": "frame",
        "rect": [10, 10, 410, 310],
        "focused": False,
        "visible": True,
        "showing": True,
    }
    duplicate = {
        "window_id": "x11:100",
        "pid": 42,
        "application": "terminal",
        "title": "Terminal",
        "role": "window",
        "rect": [12, 12, 412, 312],
        "focused": False,
        "visible": True,
        "showing": True,
        "backend": "x11",
    }
    fallback = {
        "window_id": "x11:200",
        "pid": 77,
        "application": "python",
        "title": "PortaMCP Control Center",
        "role": "window",
        "rect": [50, 50, 850, 650],
        "focused": True,
        "visible": True,
        "showing": True,
        "backend": "x11",
    }
    monkeypatch.setattr(linux_ui_module, "_window_rows", lambda: [(object(), object(), atspi)])
    monkeypatch.setattr(linux_ui_module, "_x11_window_rows", lambda: [duplicate, fallback])
    rows = linux_ui_module._combined_window_rows()
    assert [row["window_id"] for row in rows] == [atspi["window_id"], fallback["window_id"]]
    assert linux_ui_module._active_window_row() == fallback
    node = linux_ui_module._x11_synthetic_tree(fallback)
    assert node["node_id"] == fallback["window_id"]
    assert node["name"] == "PortaMCP Control Center"
    assert node["interfaces"] == ["X11"]
    assert node["actions"] == []

def test_readme_cross_platform_claims_are_conservative_and_current() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Client-neutral computer control over the Model Context Protocol for Windows and Linux." in readme
    assert "Ubuntu 24.04 Desktop (X11)" in readme
    assert "Other modern Linux distributions" in readme
    assert "not yet officially certified" in readme
    assert "./PortaMCP" in readme
    assert "portamcp.sh" in readme
    assert "not as root" in readme
    assert ".venv-linux" in readme
    assert "PolicyKit" in readme
    assert "No separate setup script" in readme
    assert "AT-SPI structured UI automation" in readme
    assert "X11 top-level fallback" in readme
    assert "prepare-git-modes.ps1" in readme
    assert "Windows UI Automation is unavailable by design" not in readme
    assert "Windows-focused" not in readme
    assert "supports all Linux distributions" not in readme


def test_public_screenshots_are_current_and_metadata_free() -> None:
    from PIL import Image as PILImage

    sizes: list[tuple[int, int]] = []
    for name in ("overview.png", "connection-auth.png", "security-profiles.png"):
        path = ROOT / "assets" / "screenshots" / name
        assert path.exists()
        with PILImage.open(path) as image:
            image.verify()
        with PILImage.open(path) as image:
            size = image.size
            assert size[0] >= 1600
            assert size[1] >= 900
            assert not image.info
            assert len(image.getexif()) == 0
            sizes.append(size)
    assert len(set(sizes)) == 1


def test_http_entrypoints_use_cooperative_runtime_wrapper() -> None:
    for name in ("local_http_server.py", "http_server.py", "oauth_server.py"):
        source = (ROOT / "porta_mcp" / name).read_text(encoding="utf-8")
        assert "from .runtime_server import run_uvicorn" in source
        assert "run_uvicorn(" in source
        assert "uvicorn.run(" not in source


def test_windows_graceful_shutdown_uses_local_named_event_before_force() -> None:
    runtime = (ROOT / "porta_mcp" / "runtime_server.py").read_text(encoding="utf-8")
    assert 'Local\\\\PortaMCPShutdown-' in runtime
    assert "OpenEventW" in runtime
    assert "SetEvent" in runtime
    assert "server.should_exit = True" in runtime
    assert "Route(" not in runtime

    backend = (ROOT / "porta_mcp" / "control_center_backend.py").read_text(encoding="utf-8")
    stop = backend.split("def stop_server_processes", 1)[1].split("def build_server_command", 1)[0]
    assert "request_windows_graceful_shutdown(proc.pid)" in stop
    assert stop.index("request_windows_graceful_shutdown(proc.pid)") < stop.index("proc.terminate()")
    assert "timeout=5 if graceful_requested else 3" in stop

    control = (ROOT / "porta_mcp" / "control_center.py").read_text(encoding="utf-8")
    stop_ui = control.split("def stop_server", 1)[1].split("def restart_server", 1)[0]
    assert "self.server_process.wait(timeout=1.0)" in stop_ui
    assert stop_ui.index("self.server_process.wait(timeout=1.0)") < stop_ui.index(
        "self.server_process.terminate()"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only named-event round trip")
def test_windows_named_shutdown_event_round_trip() -> None:
    import ctypes
    from ctypes import wintypes

    from porta_mcp.runtime_server import _event_name, request_windows_graceful_shutdown

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
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateEventW(None, False, False, _event_name(os.getpid()))
    assert handle
    try:
        assert request_windows_graceful_shutdown(os.getpid()) is True
        assert kernel32.WaitForSingleObject(handle, 1000) == 0
    finally:
        kernel32.CloseHandle(handle)


def test_platform_capabilities_are_truthful() -> None:
    caps = platform_capabilities()
    assert caps["os"] == os_family()
    assert caps["filesystem"] is True
    assert caps["shell"] is True
    assert caps["process"] is True
    assert caps["managed_browser"] is True
    if os_family() == "windows":
        assert caps["windows_ui"] is True
        assert caps["linux_ui"] is False
        assert caps["ui_automation"] is True
    elif os_family() == "linux":
        assert caps["windows_ui"] is False
        assert caps["ui_automation"] is caps["linux_ui"]


def test_linux_sensitive_defaults_when_running_on_linux() -> None:
    if os_family() != "linux":
        pytest.skip("Linux-only default check")
    denied = default_denied_roots()
    for expected in (
        "~/.ssh", "~/.gnupg", "~/.aws", "~/.kube", "~/.docker",
        "~/.config/gcloud", "~/.config/google-chrome", "~/.mozilla/firefox",
        "/etc/ssh", "/etc/shadow", "/etc/gshadow",
    ):
        assert expected in denied
