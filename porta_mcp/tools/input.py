from __future__ import annotations

import base64
import platform
import selectors
import subprocess
import sys
import threading
import time
from typing import Any

from ..context import AppContext
from ..result import fail, ok


def _windows_text_key_specs(text: str) -> list[list[tuple[int, int, int]]]:
    """Convert text into per-character Win32 keyboard specs.

    Printable Unicode is emitted with KEYEVENTF_UNICODE. Newline sequences are
    represented as VK_RETURN because applications such as Windows Notepad ignore
    a raw U+000A KEYEVENTF_UNICODE event. CRLF is treated as one newline.
    """
    keyeventf_unicode = 0x0004
    vk_return = 0x0D
    groups: list[list[tuple[int, int, int]]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in {"\r", "\n"}:
            if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
            groups.append([(vk_return, 0, 0)])
        else:
            encoded = char.encode("utf-16-le")
            groups.append(
                [
                    (0, int.from_bytes(encoded[offset : offset + 2], "little"), keyeventf_unicode)
                    for offset in range(0, len(encoded), 2)
                ]
            )
        index += 1
    return groups


def _windows_text_effective_interval(text: str, interval: float) -> float:
    has_non_ascii = any(ord(char) > 0x7F for char in text)
    return max(interval, 0.02) if has_non_ascii else interval


def _type_text_windows(text: str, interval: float) -> None:
    """Type literal Unicode text on Windows without keyboard-layout remapping."""
    import ctypes
    from ctypes import wintypes

    effective_interval = _windows_text_effective_interval(text, interval)

    input_keyboard = 1
    keyeventf_keyup = 0x0002

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class KeyboardInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [
            ("mi", MouseInput),
            ("ki", KeyboardInput),
            ("hi", HardwareInput),
        ]

    class Input(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [
            ("type", wintypes.DWORD),
            ("value", InputUnion),
        ]

    user32 = ctypes.windll.user32
    send_input = user32.SendInput
    send_input.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    send_input.restype = wintypes.UINT

    for group in _windows_text_key_specs(text):
        # Modern Windows RichEdit/Notepad can ignore VK_RETURN when it is sent
        # through our Unicode SendInput batch, while the native keybd_event path
        # used by PyAutoGUI's proven input_press backend is accepted reliably.
        if len(group) == 1 and group[0] == (0x0D, 0, 0):
            user32.keybd_event(0x0D, 0, 0, 0)
            user32.keybd_event(0x0D, 0, keyeventf_keyup, 0)
            if effective_interval > 0:
                time.sleep(effective_interval)
            continue

        events: list[Input] = []
        for vk, scan, flags in group:
            events.append(
                Input(
                    type=input_keyboard,
                    ki=KeyboardInput(
                        wVk=vk,
                        wScan=scan,
                        dwFlags=flags,
                        time=0,
                        dwExtraInfo=None,
                    ),
                )
            )
            events.append(
                Input(
                    type=input_keyboard,
                    ki=KeyboardInput(
                        wVk=vk,
                        wScan=scan,
                        dwFlags=flags | keyeventf_keyup,
                        time=0,
                        dwExtraInfo=None,
                    ),
                )
            )

        batch = (Input * len(events))(*events)
        inserted = int(send_input(len(events), batch, ctypes.sizeof(Input)))
        if inserted != len(events):
            raise RuntimeError(
                f"Windows SendInput inserted {inserted} of {len(events)} keyboard events"
            )
        if effective_interval > 0:
            time.sleep(effective_interval)


def _scroll_windows(clicks: int, x: int | None = None, y: int | None = None) -> None:
    """Send a native vertical mouse-wheel event on Windows via ``SendInput``.

    Both PyAutoGUI and the legacy ``mouse_event`` API can report success while
    producing no wheel event on some Windows desktop configurations. ``SendInput``
    is the same reliable Win32 injection path used by PortaMCP's literal keyboard
    backend and lets us verify that Windows actually accepted the event.
    """
    import ctypes
    from ctypes import wintypes

    input_mouse = 0
    mouseeventf_wheel = 0x0800

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput)]

    class Input(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("type", wintypes.DWORD), ("value", InputUnion)]

    user32 = ctypes.windll.user32
    if x is not None or y is not None:
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise RuntimeError("GetCursorPos failed before Windows scroll")
        target_x = int(x if x is not None else point.x)
        target_y = int(y if y is not None else point.y)
        if not user32.SetCursorPos(target_x, target_y):
            raise RuntimeError("SetCursorPos failed before Windows scroll")

    wheel_delta = int(clicks) * 120
    event = Input(
        type=input_mouse,
        mi=MouseInput(
            dx=0,
            dy=0,
            mouseData=ctypes.c_uint32(wheel_delta).value,
            dwFlags=mouseeventf_wheel,
            time=0,
            dwExtraInfo=0,
        ),
    )
    send_input = user32.SendInput
    send_input.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    send_input.restype = wintypes.UINT
    inserted = int(send_input(1, ctypes.byref(event), ctypes.sizeof(Input)))
    if inserted != 1:
        raise RuntimeError(f"Windows SendInput inserted {inserted} of 1 wheel events")


_X11_TEXT_CLIPBOARD_TARGETS = frozenset(
    {
        "TARGETS",
        "TIMESTAMP",
        "MULTIPLE",
        "SAVE_TARGETS",
        "UTF8_STRING",
        "STRING",
        "TEXT",
        "COMPOUND_TEXT",
        "text/plain",
        "text/plain;charset=utf-8",
    }
)
_X11_INPUT_LOCK = threading.Lock()
_x11_restore_owner_process: subprocess.Popen[str] | None = None
_X11_CLIPBOARD_OWNER_CODE = r"""
import base64
import os
import sys
import time
import tkinter as tk
from Xlib import display

payload = sys.stdin.readline().strip()
text = base64.b64decode(payload.encode("ascii")).decode("utf-8")
root = tk.Tk()
root.withdraw()
root.clipboard_clear()
root.clipboard_append(text)
root.update()
xdisplay = display.Display()
clipboard_atom = xdisplay.intern_atom("CLIPBOARD")
owner = xdisplay.get_selection_owner(clipboard_atom)
owner_id = getattr(owner, "id", owner)
print("READY", flush=True)
try:
    while os.getppid() != 1:
        root.update()
        current = xdisplay.get_selection_owner(clipboard_atom)
        if getattr(current, "id", current) != owner_id:
            break
        time.sleep(0.05)
finally:
    try:
        root.destroy()
    except Exception:
        pass
    xdisplay.close()
"""


def _x11_clipboard_snapshot(root: Any) -> tuple[bool, str]:
    """Return a safely restorable text clipboard snapshot.

    Refuse to overwrite clipboard contents that advertise non-text targets because
    restoring only their text representation would silently destroy richer data.
    """
    import tkinter as tk

    try:
        raw_targets = root.selection_get(selection="CLIPBOARD", type="TARGETS")
    except tk.TclError:
        return False, ""

    targets = set(root.tk.splitlist(raw_targets))
    unsafe_targets = sorted(
        target
        for target in targets
        if target not in _X11_TEXT_CLIPBOARD_TARGETS
        and target.lower() != "text/plain;charset=utf-8"
    )
    if unsafe_targets:
        raise RuntimeError(
            "Linux literal typing cannot safely preserve the current non-text "
            f"clipboard targets: {', '.join(unsafe_targets)}"
        )
    if not targets:
        return False, ""

    try:
        value = root.selection_get(selection="CLIPBOARD", type="UTF8_STRING")
    except tk.TclError as exc:
        raise RuntimeError(
            "Linux literal typing cannot safely read the current text clipboard."
        ) from exc
    return True, str(value)


def _x11_reap_clipboard_owner(process: subprocess.Popen[str]) -> None:
    """Reap a finished X11 clipboard owner and clear the tracked handle."""
    global _x11_restore_owner_process

    try:
        process.wait()
    finally:
        if _x11_restore_owner_process is process:
            _x11_restore_owner_process = None


def _x11_start_clipboard_owner(text: str) -> None:
    """Keep restored clipboard text owned until another application replaces it."""
    global _x11_restore_owner_process

    previous = _x11_restore_owner_process
    if previous is not None:
        try:
            previous.wait(timeout=0.20)
        except subprocess.TimeoutExpired:
            previous.terminate()
            try:
                previous.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                previous.kill()
        _x11_restore_owner_process = None

    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    process = subprocess.Popen(
        [sys.executable, "-c", _X11_CLIPBOARD_OWNER_CODE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if process.stdin is None or process.stdout is None:
        process.terminate()
        raise RuntimeError("Unable to start the X11 clipboard restore helper.")

    process.stdin.write(payload + "\n")
    process.stdin.flush()
    process.stdin.close()

    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=2.0):
            process.terminate()
            raise RuntimeError("X11 clipboard restore helper did not become ready.")
        ready = process.stdout.readline().strip()
    finally:
        selector.close()
        process.stdout.close()

    if ready != "READY":
        process.terminate()
        raise RuntimeError("X11 clipboard restore helper failed during startup.")
    _x11_restore_owner_process = process
    threading.Thread(
        target=_x11_reap_clipboard_owner,
        args=(process,),
        daemon=True,
        name="portamcp-x11-clipboard-reaper",
    ).start()


def _x11_send_ctrl_v(root: Any, interval: float) -> None:
    """Paste into the current X11 input focus without keyboard-layout mapping."""
    from Xlib import XK, X, display, protocol

    xdisplay = display.Display()
    try:
        focus = xdisplay.get_input_focus().focus
        if not hasattr(focus, "send_event"):
            raise RuntimeError("No focused X11 window is available for literal typing.")

        screen_root = xdisplay.screen().root
        ctrl_key = xdisplay.keysym_to_keycode(XK.string_to_keysym("Control_L"))
        v_key = xdisplay.keysym_to_keycode(XK.string_to_keysym("v"))
        if not ctrl_key or not v_key:
            raise RuntimeError("Unable to resolve X11 Control/V keycodes for paste.")

        event_delay = max(0.005, min(float(interval), 0.05))

        def emit(event_cls: Any, keycode: int, state: int, event_mask: int) -> None:
            event = event_cls(
                time=X.CurrentTime,
                root=screen_root,
                window=focus,
                same_screen=1,
                child=X.NONE,
                root_x=0,
                root_y=0,
                event_x=0,
                event_y=0,
                state=state,
                detail=keycode,
            )
            focus.send_event(event, propagate=True, event_mask=event_mask)
            xdisplay.sync()
            root.update()
            time.sleep(event_delay)

        emit(protocol.event.KeyPress, ctrl_key, 0, X.KeyPressMask)
        emit(protocol.event.KeyPress, v_key, X.ControlMask, X.KeyPressMask)
        emit(protocol.event.KeyRelease, v_key, X.ControlMask, X.KeyReleaseMask)
        emit(protocol.event.KeyRelease, ctrl_key, X.ControlMask, X.KeyReleaseMask)

        deadline = time.monotonic() + 0.30
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.005)
    finally:
        xdisplay.close()


def _type_text_linux_x11(text: str, interval: float) -> None:
    """Type literal Unicode text on X11 without keyboard-layout remapping.

    The focused application receives one Ctrl+V XSendEvent while a temporary Tk
    clipboard owner serves the exact Unicode text. The previous plain-text
    clipboard value is then restored by a small local owner process that exits
    automatically when another application replaces the clipboard.
    """
    import os
    import tkinter as tk

    if not os.environ.get("DISPLAY"):
        raise RuntimeError("Linux X11 literal typing requires DISPLAY to be set.")

    with _X11_INPUT_LOCK:
        root = tk.Tk()
        root.withdraw()
        action_error: Exception | None = None
        snapshot_ready = False
        had_previous = False
        previous_text = ""
        try:
            root.update()
            had_previous, previous_text = _x11_clipboard_snapshot(root)
            snapshot_ready = True
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            _x11_send_ctrl_v(root, interval)
        except Exception as exc:
            action_error = exc
        finally:
            try:
                root.destroy()
            except Exception:
                pass

        if not snapshot_ready:
            if action_error is not None:
                raise action_error
            return

        restore_error: Exception | None = None
        try:
            expected = previous_text if had_previous else ""
            _x11_start_clipboard_owner(expected)
            time.sleep(0.05)
            verify = tk.Tk()
            verify.withdraw()
            try:
                verify.update()
                restored = verify.selection_get(
                    selection="CLIPBOARD",
                    type="UTF8_STRING",
                )
            finally:
                verify.destroy()
            if str(restored) != expected:
                raise RuntimeError("clipboard verification mismatch")
        except Exception as exc:
            restore_error = exc

        if restore_error is not None:
            if action_error is not None:
                raise RuntimeError(
                    "Linux literal typing failed and the previous clipboard could not "
                    f"be restored safely: {restore_error}"
                ) from action_error
            raise RuntimeError(
                "Linux literal text was injected, but PortaMCP could not restore the "
                f"previous clipboard safely: {restore_error}"
            ) from restore_error
        if action_error is not None:
            raise action_error


def _type_literal_text(text: str, interval: float) -> str:
    system = platform.system()
    if system == "Windows":
        _type_text_windows(text, interval)
        return "windows-unicode"
    if system == "Linux":
        _type_text_linux_x11(text, interval)
        return "linux-x11-paste"
    _pg().write(text, interval=interval)
    return "pyautogui"


def _pg():
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def input_mouse_position() -> dict:
        """Read current mouse coordinates. Read-only."""
        try:
            p = _pg().position()
            return ok("input_mouse_position", {"x": p.x, "y": p.y})
        except Exception as e:
            return fail("input_mouse_position", e)

    @mcp.tool()
    def input_mouse_click(x: int, y: int, button: str = "left", clicks: int = 1, interval: float = 0.1) -> dict:
        """Move and click the mouse. Desktop-control action, disabled by default."""
        try:
            app.policy.assert_feature("input")
            actual_clicks = max(1, min(clicks, 5))
            actual_interval = max(0.0, min(interval, 2.0))
            _pg().click(x=x, y=y, button=button, clicks=actual_clicks, interval=actual_interval)
            app.audit.write("input_mouse_click", target=f"{x},{y}", detail={"button": button, "clicks": actual_clicks})
            return ok("input_mouse_click", {"x": x, "y": y, "clicks": actual_clicks})
        except Exception as e:
            return fail("input_mouse_click", e)

    @mcp.tool()
    def input_mouse_move(x: int, y: int, duration: float = 0.2) -> dict:
        """Move the mouse pointer. Desktop-control action, disabled by default."""
        try:
            app.policy.assert_feature("input")
            _pg().moveTo(x, y, duration=max(0.0, min(duration, 5.0)))
            return ok("input_mouse_move", {"x": x, "y": y})
        except Exception as e:
            return fail("input_mouse_move", e)

    @mcp.tool()
    def input_mouse_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5, button: str = "left") -> dict:
        """Drag between two screen points. Desktop-control action, disabled by default."""
        try:
            app.policy.assert_feature("input")
            pg = _pg()
            pg.moveTo(x1, y1)
            pg.dragTo(x2, y2, duration=max(0.0, min(duration, 10.0)), button=button)
            return ok("input_mouse_drag", {"from": [x1,y1], "to": [x2,y2]})
        except Exception as e:
            return fail("input_mouse_drag", e)

    @mcp.tool()
    def input_scroll(clicks: int, x: int | None = None, y: int | None = None) -> dict:
        """Scroll at the current pointer or optional coordinates. Desktop-control action."""
        try:
            app.policy.assert_feature("input")
            actual_clicks = max(-100, min(clicks, 100))
            if platform.system() == "Windows":
                _scroll_windows(actual_clicks, x=x, y=y)
            else:
                _pg().scroll(clicks=actual_clicks, x=x, y=y)
            return ok("input_scroll", {"clicks": actual_clicks})
        except Exception as e:
            return fail("input_scroll", e)

    @mcp.tool()
    def input_type_text(text: str, interval: float = 0.01) -> dict:
        """Type literal text into the focused application. Desktop-control action."""
        try:
            app.policy.assert_feature("input")
            if len(text) > 20000:
                raise ValueError("text too long")
            actual_interval = max(0.0, min(interval, 1.0))
            backend = _type_literal_text(text, actual_interval)
            app.audit.write(
                "input_type_text",
                detail={"chars": len(text), "backend": backend},
            )
            return ok(
                "input_type_text",
                {"chars": len(text), "backend": backend},
            )
        except Exception as e:
            return fail("input_type_text", e)

    @mcp.tool()
    def input_press(key: str, presses: int = 1, interval: float = 0.05) -> dict:
        """Press a keyboard key. Desktop-control action."""
        try:
            app.policy.assert_feature("input")
            actual_presses = max(1, min(presses, 20))
            actual_interval = max(0.0, min(interval, 1.0))
            _pg().press(key, presses=actual_presses, interval=actual_interval)
            return ok("input_press", {"key": key, "presses": actual_presses})
        except Exception as e:
            return fail("input_press", e)

    @mcp.tool()
    def input_hotkey(keys: list[str]) -> dict:
        """Press a key combination such as ['ctrl','shift','s']. Desktop-control action."""
        try:
            app.policy.assert_feature("input")
            if not 1 <= len(keys) <= 6:
                raise ValueError("keys must contain 1..6 entries")
            _pg().hotkey(*keys)
            return ok("input_hotkey", {"keys": keys})
        except Exception as e:
            return fail("input_hotkey", e)
