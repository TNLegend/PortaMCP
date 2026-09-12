from __future__ import annotations

import platform
from typing import Any

from ..context import AppContext
from ..result import fail, ok


def _windows_only() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("Windows UI tools require Windows")


def _desktop():
    _windows_only()
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _find_text_control(root: Any, title: str = "", automation_id: str = "") -> tuple[Any, str]:
    """Find one visible text-entry control without repeated UIA polling.

    Classic Win32 applications usually expose editable text as ``Edit`` while
    modern Windows apps such as Windows 11 Notepad expose the active editor as
    ``Document``. Search each class once so large tabbed applications cannot
    turn a 5-second wait into repeated expensive tree walks.
    """
    for control_type in ("Edit", "Document"):
        for ctrl in root.descendants(control_type=control_type):
            try:
                info = ctrl.element_info
                if title and ctrl.window_text() != title:
                    continue
                if automation_id and getattr(info, "automation_id", "") != automation_id:
                    continue
                if not ctrl.is_visible() or not ctrl.is_enabled():
                    continue
                return ctrl, control_type
            except Exception:
                continue
    selector = []
    if title:
        selector.append(f"title={title!r}")
    if automation_id:
        selector.append(f"automation_id={automation_id!r}")
    detail = ", ".join(selector) if selector else "default selector"
    raise RuntimeError(f"No visible editable UIA Edit/Document control found ({detail})")


def _set_text_control(ctrl: Any, control_type: str, text: str) -> str:
    if control_type == "Edit":
        ctrl.set_edit_text(text)
        return "uia-edit"

    # UIA ValuePattern.SetValue on modern RichEditD2DPT/Document controls can
    # silently replace non-ASCII characters with '?'. Select the document via
    # TextPattern (no synthetic Ctrl+A), then reuse PortaMCP's layout-independent
    # Win32 Unicode SendInput backend for literal text instead.
    ctrl.set_focus()
    ctrl.iface_text.DocumentRange.Select()
    from .input import _type_text_windows

    _type_text_windows(text, interval=0.0)
    return "windows-unicode-document"


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def windows_active() -> dict:
        """Return the foreground window handle/title/rectangle on Windows. Read-only."""
        try:
            _windows_only()
            import win32gui
            h = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(h)
            rect = list(win32gui.GetWindowRect(h))
            return ok("windows_active", {"handle": int(h), "title": title, "rect": rect})
        except Exception as e:
            return fail("windows_active", e)

    @mcp.tool()
    def windows_list() -> dict:
        """Enumerate top-level Windows UI Automation windows. Read-only."""
        try:
            rows = []
            for w in _desktop().windows():
                try:
                    r = w.rectangle()
                    rows.append({"handle": int(w.handle), "title": w.window_text(), "class_name": w.class_name(), "rect": [r.left,r.top,r.right,r.bottom], "visible": w.is_visible(), "enabled": w.is_enabled()})
                except Exception:
                    continue
            return ok("windows_list", {"windows": rows[:500]})
        except Exception as e:
            return fail("windows_list", e)

    @mcp.tool()
    def windows_focus(handle: int) -> dict:
        """Focus a top-level window by native handle. UI-control action, disabled by default."""
        try:
            app.policy.assert_feature("ui")
            w = _desktop().window(handle=handle)
            w.set_focus()
            return ok("windows_focus", {"handle": handle, "title": w.window_text()})
        except Exception as e:
            return fail("windows_focus", e)

    @mcp.tool()
    def windows_ui_tree(handle: int, max_depth: int = 4, max_nodes: int = 500) -> dict:
        """Inspect UI Automation controls in a window. Read-only structured alternative to screenshots."""
        try:
            _windows_only()
            root = _desktop().window(handle=handle)
            max_depth = max(1, min(max_depth, 8))
            max_nodes = max(1, min(max_nodes, 3000))
            rows = []
            def walk(node, depth: int):
                if len(rows) >= max_nodes or depth > max_depth:
                    return
                try:
                    info = node.element_info
                    r = node.rectangle()
                    rows.append({"runtime_id": str(getattr(info, "runtime_id", "")), "depth": depth, "name": node.window_text(), "control_type": getattr(info, "control_type", None), "automation_id": getattr(info, "automation_id", None), "class_name": getattr(info, "class_name", None), "rect": [r.left,r.top,r.right,r.bottom], "enabled": node.is_enabled()})
                    for child in node.children():
                        walk(child, depth + 1)
                except Exception:
                    return
            walk(root, 0)
            return ok("windows_ui_tree", {"nodes": rows, "truncated": len(rows) >= max_nodes})
        except Exception as e:
            return fail("windows_ui_tree", e)

    @mcp.tool()
    def windows_click_control(handle: int, title: str = "", automation_id: str = "", control_type: str = "") -> dict:
        """Click a UI Automation control using selector fields instead of coordinates. UI-control action."""
        try:
            app.policy.assert_feature("ui")
            root = _desktop().window(handle=handle)
            kwargs = {}
            if title:
                kwargs["title"] = title
            if automation_id:
                kwargs["auto_id"] = automation_id
            if control_type:
                kwargs["control_type"] = control_type
            if not kwargs:
                raise ValueError("Provide title, automation_id, or control_type")
            ctrl = root.child_window(**kwargs)
            ctrl.wait("exists enabled visible", timeout=5)
            ctrl.click_input()
            app.audit.write("windows_click_control", target=str(handle), detail=kwargs)
            return ok("windows_click_control", {"selector": kwargs})
        except Exception as e:
            return fail("windows_click_control", e)

    @mcp.tool()
    def windows_set_text(handle: int, text: str, title: str = "", automation_id: str = "") -> dict:
        """Set text on a visible UIA Edit/Document control. UI-control action."""
        try:
            app.policy.assert_feature("ui")
            if len(text) > 20_000:
                raise ValueError("text exceeds 20,000 character limit")
            root = _desktop().window(handle=handle)
            ctrl, control_type = _find_text_control(root, title=title, automation_id=automation_id)
            backend = _set_text_control(ctrl, control_type, text)
            selector = {
                "control_type": control_type,
                "title": title,
                "automation_id": automation_id,
            }
            app.audit.write(
                "windows_set_text",
                target=str(handle),
                detail={"selector": selector, "chars": len(text), "backend": backend},
            )
            return ok(
                "windows_set_text",
                {"chars": len(text), "control_type": control_type, "backend": backend},
            )
        except Exception as e:
            return fail("windows_set_text", e)
