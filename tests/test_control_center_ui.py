"""Opt-in real Tk checks: PORTAMCP_UI_TEST=1 python -m pytest -q tests/test_control_center_ui.py.

Requires a desktop (or Xvfb on Linux). All runtime data is temporary.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PORTAMCP_UI_TEST") != "1", reason="requires an explicit desktop UI test session"
)


@pytest.fixture
def app(monkeypatch, tmp_path):
    from porta_mcp import control_center as ui
    from porta_mcp.config import default_config

    backend = ui.backend
    for name in (
        "RUNTIME_DIR", "CONFIG_PATH", "TOKEN_PATH", "OAUTH_PASSWORD_PATH", "OAUTH_STATE_PATH",
        "EMERGENCY_PATH", "UI_STATE_PATH", "AUDIT_PATH", "TRANSPORT_LOG_PATH", "SERVER_LOG_PATH",
    ):
        monkeypatch.setattr(backend, name, tmp_path / name.lower())
    monkeypatch.setattr(backend, "read_config", default_config)
    monkeypatch.setattr(backend, "read_ui_state", lambda: {"profile": "local", "preset": "readonly"})
    monkeypatch.setattr(backend, "server_processes", lambda: [])
    monkeypatch.setattr(backend, "chrome_extension_status", lambda: {"manifest_exists": True, "bridge_config_exists": False})
    monkeypatch.setattr(ui.PortaMCPApp, "_refresh_dependency_status", lambda *a, **k: None)
    show_support = ui.PortaMCPApp._show_support_popup
    monkeypatch.setattr(ui.PortaMCPApp, "_show_support_popup", lambda *a: None)
    window = ui.PortaMCPApp()
    monkeypatch.setattr(ui.PortaMCPApp, "_show_support_popup", show_support)
    errors = []
    window.report_callback_exception = lambda *args: errors.append(args)
    yield window
    window.destroy()
    assert not errors


def _check_pages_resize_and_preserve_unsaved_fields(app):
    for size in ("1380x860", "1160x740"):
        app.geometry(size)
        for page in ("dashboard", "connection", "security", "activity", "setup"):
            app.show_page(page)
            app.update()
            frame = app._page_cache[page]
            assert frame.winfo_width() <= app.page_host.winfo_width()
            assert app.nav_buttons[page].cget("command") is not None
            def check_bounds(widget):
                for child in widget.winfo_children():
                    if child.winfo_ismapped() and child.winfo_manager() in {"pack", "grid"}:
                        assert child.winfo_x() + child.winfo_width() <= widget.winfo_width() + 2, (
                            page, size, type(child).__name__, getattr(child, "_text", ""),
                        )
                        check_bounds(child)
            check_bounds(frame)
    app.show_page("connection")
    app.host_entry.delete(0, "end")
    app.host_entry.insert(0, "demo-unsaved")
    cached = app._page_cache["connection"]
    app.show_page("dashboard")
    app.show_page("connection")
    assert app._page_cache["connection"] is cached
    assert app.host_entry.get() == "demo-unsaved"


def _check_auth_modes_and_runtime_states_render(app, monkeypatch):
    from porta_mcp import control_center as ui

    app.show_page("connection")
    for profile in ("bearer", "oauth", "local"):
        app._profile_selected(ui.backend.PROFILE_LABELS[profile])
        app.update()
        if profile != "local":
            assert app.secret_entry.cget("show") == "*"
            app.secret_toggle_button.invoke()
            assert app.secret_entry.cget("show") == ""
            assert app.secret_toggle_button.cget("text") == "Hide"
            app.secret_toggle_button.invoke()
            assert app.secret_entry.cget("show") == "*"
    app.show_page("dashboard")
    monkeypatch.setattr(ui.backend, "server_processes", lambda: [{"pid": 12345}])
    for healthy, expected in ((True, "Running"), (False, "Degraded")):
        monkeypatch.setattr(ui.backend, "server_health", lambda *a, **k: healthy)
        app._refresh_runtime_state()
        app.update()
        assert app.metric_server.cget("text") == expected
    monkeypatch.setattr(ui.backend, "emergency_active", lambda: True)
    app._refresh_runtime_state()
    assert app.metric_emergency.cget("text") == "DENY ACTIVE"
    assert not Path(ui.backend.EMERGENCY_PATH).exists()


def _check_support_popup_actions_fit_and_can_be_dismissed(app):
    app._show_support_popup()
    app.update()
    popup = app._support_popup
    # CTk temporarily withdraws Windows toplevels while applying their
    # native title-bar style. Wait for that scheduled mapping to finish.
    deadline = time.monotonic() + 3
    while not popup.winfo_viewable() and time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)
    app.update_idletasks()
    for button in (app._support_close_button, app._support_donate_button):
        assert button.winfo_ismapped()
        assert button.winfo_rooty() + button.winfo_height() <= popup.winfo_rooty() + popup.winfo_height()
    app._support_close_button.invoke()
    assert app._support_popup is None


def test_control_center_desktop_smoke(app, monkeypatch):
    # A desktop application owns one Tk interpreter. Exercise its full
    # lifecycle without creating multiple CTk roots in the same process.
    _check_pages_resize_and_preserve_unsaved_fields(app)
    _check_auth_modes_and_runtime_states_render(app, monkeypatch)
    _check_support_popup_actions_fit_and_can_be_dismissed(app)
    from porta_mcp import control_center as ui
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open_new_tab", opened.append)
    app.support_button.invoke()
    assert opened == [ui.SUPPORT_URL]
    assert app.support_button.cget("fg_color") == ui.SUPPORT
