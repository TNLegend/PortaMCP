from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from ..context import AppContext
from ..result import fail, ok

_TOP_LEVEL_ROLES = {"frame", "dialog", "window", "alert"}
_ACTION_PRIORITY = ("click", "press", "activate", "invoke", "open", "toggle")
_DEBIAN_DIST_PACKAGES = Path("/usr/lib/python3/dist-packages")


def _linux_only() -> None:
    if platform.system() != "Linux":
        raise RuntimeError("Linux UI tools require Linux")


def _atspi():
    """Load AT-SPI through PyGObject without making it a hard cross-platform import.

    Ubuntu/Debian commonly provide PyGObject as the system ``python3-gi`` package,
    which is intentionally outside a normal virtualenv. When the venv cannot see
    it directly, add only the standard Debian dist-packages directory and retry.
    Other distributions can provide ``gi`` directly in the active environment.
    """
    _linux_only()
    try:
        import gi  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        candidate = str(_DEBIAN_DIST_PACKAGES)
        if _DEBIAN_DIST_PACKAGES.is_dir() and candidate not in sys.path:
            sys.path.append(candidate)
        try:
            import gi  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Linux structured UI automation requires AT-SPI/PyGObject. "
                "Install the distribution packages that provide python3-gi and the AT-SPI typelib."
            ) from exc
    try:
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore[import-not-found]
    except Exception as exc:
        raise RuntimeError(
            "Linux structured UI automation requires the AT-SPI 2.0 introspection typelib"
        ) from exc
    return Atspi


def _desktop():
    Atspi = _atspi()
    desktop = Atspi.get_desktop(0)
    if desktop is None:
        raise RuntimeError("AT-SPI desktop is unavailable in the current Linux session")
    return Atspi, desktop


def _children(node: Any) -> Iterable[Any]:
    try:
        count = max(0, int(node.get_child_count()))
    except Exception:
        return []
    rows: list[Any] = []
    for index in range(min(count, 10_000)):
        try:
            child = node.get_child_at_index(index)
        except Exception:
            continue
        if child is not None:
            rows.append(child)
    return rows


def _role_name(node: Any) -> str:
    try:
        return str(node.get_role_name() or "")
    except Exception:
        return ""


def _name(node: Any) -> str:
    try:
        return str(node.get_name() or "")
    except Exception:
        return ""


def _accessible_id(node: Any) -> str:
    try:
        return str(node.get_accessible_id() or "")
    except Exception:
        return ""


def _process_id(node: Any) -> int:
    try:
        return int(node.get_process_id())
    except Exception:
        return 0


def _path(node: Any) -> str:
    try:
        return str(getattr(node, "path", "") or "")
    except Exception:
        return ""


def _node_id(node: Any) -> str:
    pid = _process_id(node)
    path = _path(node)
    if not pid or not path:
        return ""
    return f"{pid}|{path}"


def _interfaces(node: Any) -> set[str]:
    try:
        return {str(value) for value in node.get_interfaces()}
    except Exception:
        return set()


def _has_state(node: Any, Atspi: Any, state_name: str) -> bool:
    try:
        state = getattr(Atspi.StateType, state_name)
        return bool(node.get_state_set().contains(state))
    except Exception:
        return False


def _rect(node: Any, Atspi: Any) -> list[int] | None:
    try:
        rect = node.get_extents(Atspi.CoordType.SCREEN)
        return [int(rect.x), int(rect.y), int(rect.x + rect.width), int(rect.y + rect.height)]
    except Exception:
        return None


def _actions(node: Any) -> list[str]:
    try:
        count = max(0, min(int(node.get_n_actions()), 32))
    except Exception:
        return []
    result: list[str] = []
    for index in range(count):
        try:
            result.append(str(node.get_action_name(index) or ""))
        except Exception:
            result.append("")
    return result


def _window_rows() -> list[tuple[Any, Any, dict[str, Any]]]:
    Atspi, desktop = _desktop()
    rows: list[tuple[Any, Any, dict[str, Any]]] = []
    for app_node in _children(desktop):
        application = _name(app_node)
        for node in _children(app_node):
            role = _role_name(node).lower()
            if role not in _TOP_LEVEL_ROLES:
                continue
            # GNOME/X11 exposes a separate mutter decoration frame for each real
            # application window. It has the same title but only title-bar
            # controls, so exposing it would duplicate/ambiguous the real app.
            if "/mutter_x11_frames/" in _path(node):
                continue
            node_id = _node_id(node)
            if not node_id:
                continue
            rows.append(
                (
                    Atspi,
                    node,
                    {
                        "window_id": node_id,
                        "pid": _process_id(node),
                        "application": application,
                        "title": _name(node),
                        "role": role,
                        "rect": _rect(node, Atspi),
                        "focused": _has_state(node, Atspi, "FOCUSED"),
                        "visible": _has_state(node, Atspi, "VISIBLE"),
                        "showing": _has_state(node, Atspi, "SHOWING"),
                    },
                )
            )
    return rows


def _resolve_window(window_id: str) -> tuple[Any, Any, dict[str, Any]]:
    if not window_id or "|" not in window_id:
        raise ValueError("window_id must come from linux_list or linux_active")
    for Atspi, node, row in _window_rows():
        if row["window_id"] == window_id:
            return Atspi, node, row
    raise RuntimeError("Linux AT-SPI window is no longer available; call linux_list again")


def _iter_tree(root: Any, max_nodes: int = 5000) -> Iterable[tuple[Any, int]]:
    stack: list[tuple[Any, int]] = [(root, 0)]
    emitted = 0
    while stack and emitted < max_nodes:
        node, depth = stack.pop()
        emitted += 1
        yield node, depth
        children = list(_children(node))
        for child in reversed(children):
            stack.append((child, depth + 1))


def _node_matches(
    node: Any,
    *,
    node_id: str,
    name: str,
    accessible_id: str,
    role: str,
) -> bool:
    if node_id and _node_id(node) != node_id:
        return False
    if name and _name(node) != name:
        return False
    if accessible_id and _accessible_id(node) != accessible_id:
        return False
    if role and _role_name(node).lower() != role.lower():
        return False
    return True


def _find_control(
    root: Any,
    *,
    node_id: str = "",
    name: str = "",
    accessible_id: str = "",
    role: str = "",
) -> Any:
    if not any((node_id, name, accessible_id, role)):
        raise ValueError("Provide node_id, name, accessible_id, or role")
    matches: list[Any] = []
    for node, _ in _iter_tree(root, max_nodes=5000):
        if _node_matches(
            node,
            node_id=node_id,
            name=name,
            accessible_id=accessible_id,
            role=role,
        ):
            matches.append(node)
            if len(matches) > 1:
                break
    if not matches:
        raise RuntimeError("No matching AT-SPI control found")
    if len(matches) > 1:
        raise RuntimeError("AT-SPI selector is ambiguous; provide node_id or more selector fields")
    return matches[0]


def _invoke_control(node: Any) -> tuple[str, int]:
    names = _actions(node)
    if not names:
        raise RuntimeError("Matching AT-SPI control exposes no semantic action")
    lowered = [name.strip().lower() for name in names]
    index = -1
    for preferred in _ACTION_PRIORITY:
        if preferred in lowered:
            index = lowered.index(preferred)
            break
    if index < 0 and len(names) == 1:
        index = 0
    if index < 0:
        raise RuntimeError("AT-SPI control has multiple actions but none is an unambiguous click/activate action")
    result = node.do_action(index)
    if result is False:
        raise RuntimeError("AT-SPI action reported failure")
    return names[index], index


def _find_focused_window() -> tuple[Any, Any, dict[str, Any]] | None:
    best: tuple[int, Any, Any, dict[str, Any]] | None = None
    for Atspi, node, row in _window_rows():
        score = 0
        if _has_state(node, Atspi, "ACTIVE"):
            score += 100
        if _has_state(node, Atspi, "FOCUSED"):
            score += 50
        for descendant, _ in _iter_tree(node, max_nodes=3000):
            if descendant is node:
                continue
            if _has_state(descendant, Atspi, "FOCUSED"):
                score += 25
                break
        if score and (best is None or score > best[0]):
            best = (score, Atspi, node, row)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _x11_window_rows() -> list[dict[str, Any]]:
    """Enumerate EWMH X11 client windows as a top-level fallback.

    Some X11 applications (notably Tk/CustomTkinter) render normal desktop
    windows but do not publish an AT-SPI accessibility tree. Keep AT-SPI as the
    structured backend, while making those windows discoverable/focusable.
    """
    if not os.environ.get("DISPLAY"):
        return []
    try:
        from Xlib import X, display  # type: ignore[import-not-found]
    except Exception:
        return []

    connection = None
    try:
        connection = display.Display()
        root = connection.screen().root
        client_list_atom = connection.intern_atom("_NET_CLIENT_LIST")
        active_atom = connection.intern_atom("_NET_ACTIVE_WINDOW")
        pid_atom = connection.intern_atom("_NET_WM_PID")
        name_atom = connection.intern_atom("_NET_WM_NAME")
        utf8_atom = connection.intern_atom("UTF8_STRING")
        clients = root.get_full_property(client_list_atom, X.AnyPropertyType)
        if clients is None:
            return []
        active_prop = root.get_full_property(active_atom, X.AnyPropertyType)
        active_id = int(active_prop.value[0]) if active_prop is not None and len(active_prop.value) else 0
        rows: list[dict[str, Any]] = []
        for raw_window_id in clients.value:
            try:
                xid = int(raw_window_id)
                window = connection.create_resource_object("window", xid)
                pid_prop = window.get_full_property(pid_atom, X.AnyPropertyType)
                pid = int(pid_prop.value[0]) if pid_prop is not None and len(pid_prop.value) else 0
                title_prop = window.get_full_property(name_atom, utf8_atom)
                if title_prop is not None:
                    value = title_prop.value
                    title = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
                else:
                    title = str(window.get_wm_name() or "")
                wm_class = window.get_wm_class() or ()
                application = str(wm_class[-1] if wm_class else "")
                rect: list[int] | None = None
                try:
                    geometry = window.get_geometry()
                    translated = root.translate_coords(window, 0, 0)
                    x = int(translated.x)
                    y = int(translated.y)
                    rect = [x, y, x + int(geometry.width), y + int(geometry.height)]
                except Exception:
                    pass
                try:
                    showing = window.get_attributes().map_state == X.IsViewable
                except Exception:
                    showing = True
                rows.append(
                    {
                        "window_id": f"x11:{xid}",
                        "pid": pid,
                        "application": application,
                        "title": title,
                        "role": "window",
                        "rect": rect,
                        "focused": xid == active_id,
                        "visible": showing,
                        "showing": showing,
                        "backend": "x11",
                    }
                )
            except Exception:
                continue
        return rows
    except Exception:
        return []
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _rects_close(left: list[int] | None, right: list[int] | None, tolerance: int = 80) -> bool:
    if left is None or right is None or len(left) != 4 or len(right) != 4:
        return True
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(left, right))


def _same_window_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_pid = int(left.get("pid") or 0)
    right_pid = int(right.get("pid") or 0)
    if left_pid and right_pid and left_pid != right_pid:
        return False
    left_title = str(left.get("title") or "")
    right_title = str(right.get("title") or "")
    if left_title and right_title and left_title != right_title:
        return False
    if not ((left_pid and right_pid) or (left_title and right_title)):
        return False
    return _rects_close(left.get("rect"), right.get("rect"))


def _combined_window_rows() -> list[dict[str, Any]]:
    try:
        atspi_rows = [row for _, _, row in _window_rows()]
    except Exception:
        atspi_rows = []
    rows = list(atspi_rows)
    for x11_row in _x11_window_rows():
        if any(_same_window_identity(x11_row, atspi_row) for atspi_row in atspi_rows):
            continue
        rows.append(x11_row)
    return rows


def _resolve_x11_window(window_id: str) -> dict[str, Any]:
    if not window_id.startswith("x11:"):
        raise ValueError("window_id is not an X11 fallback window")
    for row in _x11_window_rows():
        if row.get("window_id") == window_id:
            return row
    raise RuntimeError("Linux X11 window is no longer available; call linux_list again")


def _active_window_row() -> dict[str, Any] | None:
    x11_active = next((row for row in _x11_window_rows() if row.get("focused")), None)
    if x11_active is not None:
        try:
            for _, _, atspi_row in _window_rows():
                if _same_window_identity(x11_active, atspi_row):
                    return atspi_row
        except Exception:
            pass
        return x11_active
    match = _find_focused_window()
    return match[2] if match is not None else None


def _x11_synthetic_tree(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": str(row.get("window_id") or ""),
        "depth": 0,
        "name": str(row.get("title") or ""),
        "role": "window",
        "accessible_id": "",
        "rect": row.get("rect"),
        "focused": bool(row.get("focused")),
        "focusable": True,
        "enabled": True,
        "visible": bool(row.get("visible", True)),
        "showing": bool(row.get("showing", True)),
        "editable": False,
        "interfaces": ["X11"],
        "actions": [],
    }


def _x11_focus_window(row: dict[str, Any]) -> bool:
    """Request focus for one uniquely matched X11 client using EWMH.

    AT-SPI top-level frames commonly cannot grab focus themselves. On an X11
    desktop, match the actual client by PID plus exact title and ask the window
    manager to activate it through ``_NET_ACTIVE_WINDOW``. Refuse ambiguity.
    """
    if not os.environ.get("DISPLAY"):
        return False
    try:
        from Xlib import X, display, protocol  # type: ignore[import-not-found]
    except Exception:
        return False

    connection = None
    try:
        connection = display.Display()
        root = connection.screen().root
        client_list_atom = connection.intern_atom("_NET_CLIENT_LIST")
        active_atom = connection.intern_atom("_NET_ACTIVE_WINDOW")
        pid_atom = connection.intern_atom("_NET_WM_PID")
        name_atom = connection.intern_atom("_NET_WM_NAME")
        utf8_atom = connection.intern_atom("UTF8_STRING")
        prop = root.get_full_property(client_list_atom, X.AnyPropertyType)
        if prop is None:
            return False

        wanted_pid = int(row.get("pid") or 0)
        wanted_title = str(row.get("title") or "")
        wanted_window_id = str(row.get("window_id") or "")
        wanted_xid = 0
        if wanted_window_id.startswith("x11:"):
            try:
                wanted_xid = int(wanted_window_id.split(":", 1)[1])
            except ValueError:
                return False
        matches: list[Any] = []
        for raw_window_id in prop.value:
            try:
                xid = int(raw_window_id)
                window = connection.create_resource_object("window", xid)
                if wanted_xid:
                    if xid != wanted_xid:
                        continue
                    matches.append(window)
                    break
                pid_prop = window.get_full_property(pid_atom, X.AnyPropertyType)
                if wanted_pid:
                    if pid_prop is None or not len(pid_prop.value) or int(pid_prop.value[0]) != wanted_pid:
                        continue
                title_prop = window.get_full_property(name_atom, utf8_atom)
                if title_prop is not None:
                    value = title_prop.value
                    title = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
                else:
                    title = str(window.get_wm_name() or "")
                if wanted_title and title != wanted_title:
                    continue
                matches.append(window)
                if len(matches) > 1:
                    return False
            except Exception:
                continue
        if len(matches) != 1:
            return False

        target = matches[0]
        event = protocol.event.ClientMessage(
            window=target,
            client_type=active_atom,
            data=(32, [2, X.CurrentTime, 0, 0, 0]),
        )
        root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        connection.sync()
        for _ in range(10):
            active = root.get_full_property(active_atom, X.AnyPropertyType)
            if active is not None and len(active.value) and int(active.value[0]) == int(target.id):
                return True
            time.sleep(0.05)
        return False
    except Exception:
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def linux_active() -> dict:
        """Return the active Linux top-level window. AT-SPI is preferred with an X11 fallback."""
        try:
            _linux_only()
            return ok("linux_active", {"window": _active_window_row()})
        except Exception as e:
            return fail("linux_active", e)

    @mcp.tool()
    def linux_list() -> dict:
        """Enumerate Linux top-level windows via AT-SPI plus an X11 fallback when needed. Read-only."""
        try:
            _linux_only()
            return ok("linux_list", {"windows": _combined_window_rows()[:500]})
        except Exception as e:
            return fail("linux_list", e)

    @mcp.tool()
    def linux_focus(window_id: str) -> dict:
        """Focus a Linux top-level window by window_id using AT-SPI or the X11 fallback."""
        try:
            app.policy.assert_feature("ui")
            if window_id.startswith("x11:"):
                row = _resolve_x11_window(window_id)
                if not _x11_focus_window(row):
                    raise RuntimeError("X11 could not focus the requested fallback window")
                app.audit.write("linux_focus", target=window_id, detail={"pid": row["pid"], "backend": "x11"})
                return ok("linux_focus", {"window_id": window_id, "title": row["title"], "backend": "x11"})

            _, node, row = _resolve_window(window_id)
            current = _active_window_row()
            focused = current is not None and current.get("window_id") == window_id
            if not focused:
                focused = bool(node.grab_focus())
            if not focused:
                names = _actions(node)
                lowered = [name.strip().lower() for name in names]
                for candidate in ("activate", "raise"):
                    if candidate in lowered:
                        result = node.do_action(lowered.index(candidate))
                        focused = result is not False
                        break
            if not focused:
                focused = _x11_focus_window(row)
            if not focused:
                raise RuntimeError("AT-SPI/X11 could not focus the requested window")
            app.audit.write("linux_focus", target=window_id, detail={"pid": row["pid"], "backend": "atspi"})
            return ok("linux_focus", {"window_id": window_id, "title": row["title"], "backend": "atspi"})
        except Exception as e:
            return fail("linux_focus", e)

    @mcp.tool()
    def linux_ui_tree(window_id: str, max_depth: int = 4, max_nodes: int = 500) -> dict:
        """Inspect Linux controls with AT-SPI; X11-only windows return top-level metadata."""
        try:
            if window_id.startswith("x11:"):
                row = _resolve_x11_window(window_id)
                return ok(
                    "linux_ui_tree",
                    {"nodes": [_x11_synthetic_tree(row)], "truncated": False, "backend": "x11"},
                    warnings=[
                        "This window is not exposed through AT-SPI; only X11 top-level metadata/focus is available. "
                        "Use screen capture and input tools for non-semantic interaction."
                    ],
                )
            Atspi, root, _ = _resolve_window(window_id)
            max_depth = max(1, min(int(max_depth), 8))
            max_nodes = max(1, min(int(max_nodes), 3000))
            rows: list[dict[str, Any]] = []
            truncated = False
            stack: list[tuple[Any, int]] = [(root, 0)]
            while stack:
                node, depth = stack.pop()
                if depth > max_depth:
                    continue
                if len(rows) >= max_nodes:
                    truncated = True
                    break
                rows.append(
                    {
                        "node_id": _node_id(node),
                        "depth": depth,
                        "name": _name(node),
                        "role": _role_name(node),
                        "accessible_id": _accessible_id(node),
                        "rect": _rect(node, Atspi),
                        "focused": _has_state(node, Atspi, "FOCUSED"),
                        "focusable": _has_state(node, Atspi, "FOCUSABLE"),
                        "enabled": _has_state(node, Atspi, "ENABLED"),
                        "visible": _has_state(node, Atspi, "VISIBLE"),
                        "showing": _has_state(node, Atspi, "SHOWING"),
                        "editable": _has_state(node, Atspi, "EDITABLE"),
                        "interfaces": sorted(_interfaces(node)),
                        "actions": _actions(node),
                    }
                )
                children = list(_children(node))
                for child in reversed(children):
                    stack.append((child, depth + 1))
            return ok("linux_ui_tree", {"nodes": rows, "truncated": truncated, "backend": "atspi"})
        except Exception as e:
            return fail("linux_ui_tree", e)

    @mcp.tool()
    def linux_click_control(
        window_id: str,
        node_id: str = "",
        name: str = "",
        accessible_id: str = "",
        role: str = "",
    ) -> dict:
        """Invoke one Linux AT-SPI control semantically using deterministic selector fields. UI-control action."""
        try:
            app.policy.assert_feature("ui")
            if window_id.startswith("x11:"):
                raise RuntimeError(
                    "This X11 fallback window does not expose an AT-SPI control tree; "
                    "use screen capture and input tools for non-semantic interaction."
                )
            _, root, _ = _resolve_window(window_id)
            ctrl = _find_control(
                root,
                node_id=node_id,
                name=name,
                accessible_id=accessible_id,
                role=role,
            )
            action_name, action_index = _invoke_control(ctrl)
            selector = {
                "node_id": node_id,
                "name": name,
                "accessible_id": accessible_id,
                "role": role,
            }
            app.audit.write("linux_click_control", target=window_id, detail={"selector": selector, "action": action_name})
            return ok(
                "linux_click_control",
                {"selector": selector, "action": action_name, "action_index": action_index},
            )
        except Exception as e:
            return fail("linux_click_control", e)

    @mcp.tool()
    def linux_set_text(
        window_id: str,
        text: str,
        node_id: str = "",
        name: str = "",
        accessible_id: str = "",
        role: str = "",
    ) -> dict:
        """Replace text in one editable Linux AT-SPI control. UI-control action, disabled by default."""
        try:
            app.policy.assert_feature("ui")
            if len(text) > 20_000:
                raise ValueError("text exceeds 20,000 character limit")
            if window_id.startswith("x11:"):
                raise RuntimeError(
                    "This X11 fallback window does not expose an AT-SPI control tree; "
                    "use screen capture and input tools for non-semantic interaction."
                )
            _, root, _ = _resolve_window(window_id)
            ctrl = _find_control(
                root,
                node_id=node_id,
                name=name,
                accessible_id=accessible_id,
                role=role,
            )
            if "EditableText" not in _interfaces(ctrl):
                raise RuntimeError("Matching AT-SPI control does not expose EditableText")
            result = ctrl.set_text_contents(text)
            if result is False:
                raise RuntimeError("AT-SPI EditableText reported failure")
            verified: bool | None = None
            if "Text" in _interfaces(ctrl):
                try:
                    Atspi = _atspi()
                    actual = str(Atspi.Text.get_text(ctrl, 0, -1))
                    verified = actual == text
                except Exception:
                    verified = None
            selector = {
                "node_id": node_id,
                "name": name,
                "accessible_id": accessible_id,
                "role": role,
            }
            app.audit.write(
                "linux_set_text",
                target=window_id,
                detail={"selector": selector, "chars": len(text), "verified": verified},
            )
            return ok("linux_set_text", {"chars": len(text), "verified": verified, "selector": selector})
        except Exception as e:
            return fail("linux_set_text", e)
