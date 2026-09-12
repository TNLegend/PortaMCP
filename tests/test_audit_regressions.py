"""Isolated regression coverage for the final cross-platform audit."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient
from test_release import EXPECTED_TOOLS, _runtime_settings

from porta_mcp.oauth_server import build_oauth_app
from porta_mcp.self_hosted_oauth import OAuthStateStore, _pkce_s256
from porta_mcp.server import build_server


@pytest.fixture
def flow(tmp_path):
    settings = _runtime_settings(tmp_path, public_base_url="https://mcp.example.test")
    Path(settings.oauth_owner_password_file).write_text("test-owner", encoding="utf-8")
    with TestClient(build_oauth_app(settings), base_url=settings.public_base_url) as http:
        registration = http.post("/register", json={
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": "none",
        })
        assert registration.status_code == 201
        registered = registration.json()
        yield http, settings, registered


def authorize(flow, **overrides):
    http, settings, registered = flow
    params = {
        "client_id": registered["client_id"], "response_type": "code",
        "redirect_uri": registered["redirect_uris"][0], "state": "test-state",
        "code_challenge": _pkce_s256("A" * 43), "code_challenge_method": "S256",
        "resource": settings.public_base_url + "/mcp",
    }
    params.update(overrides)
    return http.get("/authorize", params=params, follow_redirects=False)


def code_for(flow):
    response = authorize(flow)
    assert response.status_code == 200
    pending = re.search(r'name="pending_id" value="([^"]+)"', response.text).group(1)
    response = flow[0].post("/authorize/decision", data={
        "pending_id": pending, "password": "test-owner", "decision": "allow",
    }, follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert "iss" not in query
    assert query["state"] == ["test-state"]
    return query["code"][0]


def exchange(flow, code, **overrides):
    form = {
        "grant_type": "authorization_code", "client_id": flow[2]["client_id"],
        "code": code, "redirect_uri": flow[2]["redirect_uris"][0],
        "code_verifier": "A" * 43, "resource": flow[1].public_base_url + "/mcp",
    }
    form.update(overrides)
    return flow[0].post("/token", data=form)


def rpc(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(messages) == 1
    return messages[0]


def test_oauth_mcp_full_lifecycle(flow):
    http, settings, registered = flow
    assert http.get("/.well-known/oauth-protected-resource").json()["resource"] == settings.public_base_url + "/mcp"
    assert http.get("/.well-known/oauth-authorization-server").json()["authorization_response_iss_parameter_supported"] is False
    assert http.post("/mcp", json={}).status_code == 401
    code = code_for(flow)
    response = exchange(flow, code)
    assert response.status_code == 200
    tokens = response.json()
    assert exchange(flow, code).json()["error"] == "invalid_grant"
    headers = {"Authorization": "Bearer " + tokens["access_token"], "Accept": "application/json, text/event-stream"}
    initialized = http.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "audit-client", "version": "1"},
        },
    })
    result = rpc(initialized)["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert isinstance(result["capabilities"]["tools"], dict)
    assert result["serverInfo"]["name"] and result["serverInfo"]["version"]
    headers.update({"Mcp-Session-Id": initialized.headers["mcp-session-id"], "Mcp-Protocol-Version": result["protocolVersion"]})
    assert http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    listed = rpc(http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))["result"]["tools"]
    assert len(listed) == len(EXPECTED_TOOLS)
    assert {tool["name"] for tool in listed} == EXPECTED_TOOLS
    for tool in listed:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
        assert set(tool["inputSchema"].get("required", [])) <= set(tool["inputSchema"].get("properties", {}))
    called = rpc(http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "security_emergency_stop_status", "arguments": {}}}))
    assert not called["result"].get("isError", False)
    assert http.post("/mcp", headers={**headers, "Mcp-Session-Id": "unknown"}, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"}).status_code == 404
    assert http.delete("/mcp", headers=headers).status_code == 200
    assert http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"}).status_code == 404
    refreshed = http.post("/token", data={"grant_type": "refresh_token", "client_id": registered["client_id"], "refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
    logs = Path(settings.audit_log_path).parent
    persisted = "\n".join(p.read_text(encoding="utf-8") for p in logs.glob("*.log"))
    for value in (tokens["access_token"], tokens["refresh_token"], code, "test-owner", "test-state", "A" * 43):
        assert value not in persisted


@pytest.mark.parametrize("override,error", [
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"scope": "admin"}, "invalid_scope"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge": ""}, "invalid_request"),
    ({"resource": "https://other.example/mcp"}, "invalid_target"),
])
def test_error_callbacks_preserve_state_without_iss(flow, override, error):
    response = authorize(flow, **override)
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["error"] == [error]
    assert query["state"] == ["test-state"]
    assert "iss" not in query


@pytest.mark.parametrize("verifier", ["B" * 43, "\u00e9" * 43, "", "A" * 42, "A" * 129])
def test_invalid_verifier_is_controlled(flow, verifier):
    assert exchange(flow, code_for(flow), code_verifier=verifier).json()["error"] == "invalid_grant"


def test_non_ascii_owner_password_is_controlled(flow):
    response = authorize(flow)
    pending = re.search(r'name="pending_id" value="([^"]+)"', response.text).group(1)
    response = flow[0].post("/authorize/decision", data={"pending_id": pending, "password": "\u00e9", "decision": "allow"})
    assert response.status_code == 401


def test_non_ascii_client_secret_is_controlled(tmp_path):
    store = OAuthStateStore(str(tmp_path / "state.json"), issuer="https://test.example", resource_url="https://test.example/mcp")
    registered = store.register_client({"redirect_uris": ["https://client.example/cb"], "token_endpoint_auth_method": "client_secret_post"})
    assert store.authenticate_client({"client_id": registered["client_id"], "client_secret": "\u00e9"}, None) is None


def test_recursive_fs_respects_nested_denied_roots(tmp_path):
    source = tmp_path / "source"
    denied = source / "private"
    denied.mkdir(parents=True)
    (denied / "hidden.txt").write_text("private-needle")
    settings = _runtime_settings(tmp_path / "runtime", allowed_roots=[str(tmp_path)], denied_roots=[str(denied)], allow_fs_write=True)
    server, _ = build_server(settings)
    tools = server._tool_manager._tools
    found = tools["fs_search"].fn(path=str(source), query="private-needle")
    assert found["ok"] and not found["data"]["matches"]
    copied = tools["fs_copy"].fn(src=str(source), dst=str(tmp_path / "copy"))
    assert not copied["ok"]
    assert not (tmp_path / "copy").exists()


def test_fs_copy_file_to_directory_checks_effective_target(tmp_path):
    source = tmp_path / "hidden.txt"
    source.write_text("new")
    destination = tmp_path / "target"
    destination.mkdir()
    protected = destination / source.name
    protected.write_text("original")
    settings = _runtime_settings(tmp_path / "runtime", allowed_roots=[str(tmp_path)], denied_roots=[str(protected)], allow_fs_write=True)
    server, _ = build_server(settings)
    result = server._tool_manager._tools["fs_copy"].fn(src=str(source), dst=str(destination), overwrite=True)
    assert not result["ok"]
    assert protected.read_text() == "original"


@pytest.mark.parametrize("endpoint", ["/authorize/decision", "/token", "/revoke"])
def test_oauth_form_body_limit(flow, endpoint):
    from porta_mcp.self_hosted_oauth import MAX_OAUTH_FORM_BYTES
    assert flow[0].post(endpoint, content=b"x" * (MAX_OAUTH_FORM_BYTES + 1)).status_code == 413


def test_invalid_bearer_body_limit(flow):
    from porta_mcp.self_hosted_oauth import MAX_OAUTH_FORM_BYTES
    assert flow[0].post("/mcp", headers={"Authorization": "Bearer invalid"}, content=b"x" * (MAX_OAUTH_FORM_BYTES + 1)).status_code == 413


@pytest.mark.parametrize("uri", [
    "https://client.example/cb#", "https://client.example:invalid/cb",
    "https://client.example:70000/cb", "https://client.example\\@evil.example/cb",
    "https://client.example/\ncb", "https://client.example /cb", "https://user:pass@client.example/cb",
])
def test_registration_rejects_malformed_redirect(flow, uri):
    assert flow[0].post("/register", json={"redirect_uris": [uri]}).status_code == 400


def test_authorization_redirect_exact_match_and_denial(flow):
    bad = authorize(flow, redirect_uri=flow[2]["redirect_uris"][0] + "/extra")
    assert bad.status_code == 400 and "location" not in bad.headers
    response = authorize(flow)
    pending = re.search(r'name="pending_id" value="([^"]+)"', response.text).group(1)
    denied = flow[0].post("/authorize/decision", data={"pending_id": pending, "decision": "deny"}, follow_redirects=False)
    query = parse_qs(urlparse(denied.headers["location"]).query)
    assert query == {"state": ["test-state"], "error": ["access_denied"]}


def test_expired_code_and_pending_are_rejected(flow, monkeypatch):
    import porta_mcp.self_hosted_oauth as oauth
    code = code_for(flow)
    response = authorize(flow)
    pending = re.search(r'name="pending_id" value="([^"]+)"', response.text).group(1)
    future = oauth._now() + 601
    monkeypatch.setattr(oauth, "_now", lambda: future)
    assert exchange(flow, code).json()["error"] == "invalid_grant"
    assert flow[0].post("/authorize/decision", data={"pending_id": pending, "password": "test-owner", "decision": "allow"}).status_code == 400


@pytest.mark.parametrize("restart", [False, True])
def test_revoked_refresh_cannot_be_replayed(tmp_path, restart):
    store = OAuthStateStore(str(tmp_path / "state.json"), issuer="https://test.example", resource_url="https://test.example/mcp")
    client = store.register_client({"redirect_uris": ["https://client.example/cb"]})
    pair = store._mint_pair_locked(client_id=client["client_id"], scope="pc:control offline_access", resource=store.resource_url, access_ttl=3600, refresh_ttl=3600)
    kwargs = {"client": client, "requested_scope": None, "access_ttl": 3600, "refresh_ttl": 3600}
    rotated = store.exchange_refresh(refresh_token=pair["refresh_token"], **kwargs)
    if restart:
        store = OAuthStateStore(str(store.path), issuer=store.issuer, resource_url=store.resource_url)
    store.revoke(pair["refresh_token"])
    assert store.exchange_refresh(refresh_token=pair["refresh_token"], **kwargs) is None
    assert store.exchange_refresh(refresh_token=rotated["refresh_token"], **kwargs) is None
    assert store.verify_access_token(rotated["access_token"]) is None


def test_log_privacy(tmp_path):
    import logging
    import sys

    from porta_mcp.audit import AuditLogger
    from porta_mcp.policy import Policy
    from porta_mcp.transport_logging import PrivateFormatter
    settings = _runtime_settings(tmp_path, allow_shell=True)
    audit = AuditLogger(settings.audit_log_path)
    Policy(settings, audit).assert_shell("echo raw-command-credential")
    audit.write("navigate", target="https://user:url-password@client.example/cb?code=private-code&state=private-state", detail={"refresh_token": "private-refresh"})
    content = Path(settings.audit_log_path).read_text()
    for secret in ("raw-command-credential", "url-password", "private-code", "private-state", "private-refresh"):
        assert secret not in content
    try:
        raise ValueError("unlabelled-secret")
    except ValueError:
        record = logging.LogRecord("audit-test", logging.ERROR, __file__, 1, "failed access_token=private-access Bearer private-bearer", (), sys.exc_info())
    formatted = PrivateFormatter().format(record)
    assert "ValueError" in formatted and "test_log_privacy" in formatted
    for secret in ("private-access", "private-bearer", "unlabelled-secret"):
        assert secret not in formatted


@pytest.mark.parametrize("operation", ["fs_delete", "fs_move"])
def test_recursive_mutations_reject_denied_children(tmp_path, operation):
    source = tmp_path / "source"
    source.mkdir()
    denied = source / "private"
    denied.write_text("preserve")
    settings = _runtime_settings(tmp_path / "runtime", allowed_roots=[str(tmp_path)], denied_roots=[str(denied)], allow_fs_write=True, allow_destructive_fs=True)
    server, _ = build_server(settings)
    kwargs = {"path": str(source), "recursive": True} if operation == "fs_delete" else {"src": str(source), "dst": str(tmp_path / "dest")}
    result = server._tool_manager._tools[operation].fn(**kwargs)
    assert not result["ok"]
    assert denied.read_text() == "preserve"


def test_reset_oauth_backend_refuses_live_server(tmp_path, monkeypatch):
    import porta_mcp.control_center_backend as backend
    path = tmp_path / "state.json"
    path.write_text("preserve")
    monkeypatch.setattr(backend, "OAUTH_STATE_PATH", path)
    monkeypatch.setattr(backend, "server_processes", lambda: [{"pid": 123}])
    with pytest.raises(RuntimeError, match="Stop"):
        backend.reset_oauth_state()
    assert path.read_text() == "preserve"


def test_process_relative_executable_cannot_escape_root(tmp_path, monkeypatch):
    import porta_mcp.tools.process as process
    settings = _runtime_settings(tmp_path / "runtime", allowed_roots=[str(tmp_path)], denied_roots=[], allow_process_start=True)
    server, _ = build_server(settings)
    def forbidden(*args, **kwargs):
        pytest.fail("process creation must not be reached")
    monkeypatch.setattr(process.subprocess, "Popen", forbidden)
    result = server._tool_manager._tools["process_start"].fn(executable="../outside/program", cwd=str(tmp_path))
    assert not result["ok"]


def test_pending_authorizations_are_bounded(flow, monkeypatch):
    import porta_mcp.self_hosted_oauth as oauth
    monkeypatch.setattr(oauth, "MAX_PENDING_AUTHORIZATIONS", 1)
    assert authorize(flow).status_code == 200
    assert authorize(flow).status_code == 429
    future = oauth._now() + 601
    monkeypatch.setattr(oauth, "_now", lambda: future)
    assert authorize(flow).status_code == 200


def test_unknown_client_auth_method_fails_closed(flow):
    response = flow[0].post("/register", json={"redirect_uris": ["https://client.example/cb"], "token_endpoint_auth_method": "private_key_jwt"})
    assert response.status_code == 400


def test_pkce_syntax_is_enforced():
    assert _pkce_s256("A" * 42) is None
    assert _pkce_s256("A" * 129) is None
    assert _pkce_s256("!" * 43) is None
    assert _pkce_s256("A" * 128)


def test_error_result_redacts_credentials():
    from porta_mcp.result import fail
    response = fail("test", ValueError("access_token=private-value at https://example.test/?code=private-code"))
    assert "private-value" not in response["error"]
    assert "private-code" not in response["error"]


def test_formatter_does_not_reuse_unsafe_cached_exception():
    import logging
    import sys

    from porta_mcp.transport_logging import PrivateFormatter
    try:
        raise ValueError("opaque-secret-value")
    except ValueError:
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Operation failed", (), sys.exc_info())
    logging.Formatter().format(record)
    assert "opaque-secret-value" not in PrivateFormatter().format(record)


def test_mcp_dependency_constraints_match():
    import tomllib
    root = Path(__file__).resolve().parents[1]
    dependencies = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    requirement = next(line for line in (root / "requirements.txt").read_text().splitlines() if line.startswith("mcp"))
    assert requirement in dependencies
    assert requirement == "mcp>=2.1.1,<2.2"


def test_bearer_non_ascii_header_is_rejected(tmp_path):
    from porta_mcp.http_server import build_http_app
    settings = _runtime_settings(tmp_path, bearer_token="test-bearer")
    with TestClient(build_http_app(settings), base_url="http://127.0.0.1") as client:
        response = client.post("/mcp", headers=[(b"authorization", b"Bearer \xff")], json={})
    assert response.status_code == 401


def test_chrome_bridge_non_ascii_token_is_rejected():
    import asyncio

    from porta_mcp.chrome_bridge import ChromeBridgeHub

    class Socket:
        client = type("Peer", (), {"host": "127.0.0.1"})()
        query_params = {"token": "\u00e9"}
        close_code = None

        async def close(self, code):
            self.close_code = code

    socket = Socket()
    asyncio.run(ChromeBridgeHub().handle(socket, "test-bridge-token"))
    assert socket.close_code == 1008
