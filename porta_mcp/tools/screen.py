from __future__ import annotations

import io
import platform
from pathlib import Path
from typing import Any

from ..context import AppContext
from ..result import fail, ok

try:
    from mcp.server.fastmcp import Image as MCPImage
except Exception:  # newest SDK compatibility path
    try:
        from mcp.server.mcpserver import Image as MCPImage
    except Exception:
        MCPImage = None


def _rgb_is_all_black(rgb: bytes) -> bool:
    return bool(rgb) and not any(rgb)


def _pillow_capture_png(spec: dict[str, int]) -> bytes:
    from PIL import ImageGrab

    left = int(spec["left"])
    top = int(spec["top"])
    width = int(spec["width"])
    height = int(spec["height"])
    image = ImageGrab.grab(
        bbox=(left, top, left + width, top + height),
    ).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _capture_png(sct: Any, spec: Any) -> tuple[bytes, str]:
    import mss.tools

    shot = sct.grab(spec)
    if platform.system() == "Linux" and _rgb_is_all_black(shot.rgb):
        try:
            return _pillow_capture_png(dict(spec)), "pillow-imagegrab"
        except Exception as exc:
            raise RuntimeError(
                "MSS returned an all-black Linux frame and Pillow ImageGrab fallback failed"
            ) from exc
    return mss.tools.to_png(shot.rgb, shot.size), "mss"


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def screen_monitors() -> dict:
        """List monitor geometry. Read-only."""
        try:
            import mss
            with mss.MSS() as sct:
                rows = [{"index": i, **dict(mon)} for i, mon in enumerate(sct.monitors)]
            return ok("screen_monitors", {"monitors": rows})
        except Exception as e:
            return fail("screen_monitors", e)

    @mcp.tool(structured_output=False)
    def screen_capture(monitor: int = 1):
        """Capture a monitor screenshot and return it as MCP image content. Read-only visual observation."""
        try:
            import mss
            with mss.MSS() as sct:
                if monitor < 0 or monitor >= len(sct.monitors):
                    raise ValueError(f"monitor must be 0..{len(sct.monitors)-1}")
                raw, backend = _capture_png(sct, sct.monitors[monitor])
            app.audit.write(
                "screen_capture",
                target=str(monitor),
                detail={"bytes": len(raw), "backend": backend},
            )
            if MCPImage is not None:
                return MCPImage(data=raw, format="png")
            # Fallback for SDKs without helper import path.
            import base64
            return {"mime_type": "image/png", "base64": base64.b64encode(raw).decode("ascii")}
        except Exception as e:
            return str(fail("screen_capture", e))

    @mcp.tool(structured_output=False)
    def screen_capture_region(x: int, y: int, width: int, height: int):
        """Capture a rectangular desktop region as MCP image content. Read-only visual observation."""
        try:
            import mss
            if width <= 0 or height <= 0 or width * height > 20_000_000:
                raise ValueError("Invalid or excessively large capture region")
            spec = {"left": x, "top": y, "width": width, "height": height}
            with mss.MSS() as sct:
                raw, backend = _capture_png(sct, spec)
            app.audit.write(
                "screen_capture_region",
                target=f"{x},{y},{width},{height}",
                detail={"backend": backend},
            )
            if MCPImage is not None:
                return MCPImage(data=raw, format="png")
            import base64
            return {"mime_type": "image/png", "base64": base64.b64encode(raw).decode("ascii")}
        except Exception as e:
            return str(fail("screen_capture_region", e))

    @mcp.tool()
    def screen_capture_to_file(path: str, monitor: int = 1) -> dict:
        """Capture a monitor to a PNG inside an allowed root. Write action."""
        try:
            import mss
            p = Path(app.policy.assert_path(path, write=True))
            if p.suffix.lower() != ".png":
                raise ValueError("path must end in .png")
            p.parent.mkdir(parents=True, exist_ok=True)
            with mss.MSS() as sct:
                if monitor < 0 or monitor >= len(sct.monitors):
                    raise ValueError(f"monitor must be 0..{len(sct.monitors)-1}")
                raw, backend = _capture_png(sct, sct.monitors[monitor])
            p.write_bytes(raw)
            app.audit.write(
                "screen_capture_to_file",
                target=str(p),
                detail={"backend": backend},
            )
            return ok(
                "screen_capture_to_file",
                {"path": str(p), "size": p.stat().st_size, "backend": backend},
            )
        except Exception as e:
            return fail("screen_capture_to_file", e)
