from __future__ import annotations

import base64
from typing import Any

from ..context import AppContext
from ..result import fail, ok

try:
    from mcp.server.fastmcp import Image as MCPImage
except Exception:
    try:
        from mcp.server.mcpserver import Image as MCPImage
    except Exception:
        MCPImage = None


def _hub(app: AppContext):
    hub = app.state.chrome_bridge
    if hub is None:
        raise RuntimeError("Current-Chrome bridge is unavailable in this server transport.")
    return hub


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    async def chrome_live_status() -> dict:
        """Return current-Chrome extension bridge status. Does not attach to any tab."""
        try:
            hub = _hub(app)
            data = hub.status()
            if hub.connected:
                data["extension"] = await hub.request("status", timeout=5)
            return ok("chrome_live_status", data)
        except Exception as e:
            return fail("chrome_live_status", e)

    @mcp.tool()
    async def chrome_live_tabs() -> dict:
        """List tabs from the user's normal running Chrome profile through the local extension bridge."""
        try:
            app.policy.assert_feature("browser")
            tabs = await _hub(app).request("list_tabs", timeout=10)
            return ok("chrome_live_tabs", {"tabs": tabs})
        except Exception as e:
            return fail("chrome_live_tabs", e)

    @mcp.tool()
    async def chrome_live_snapshot(tab_id: int, max_elements: int = 300, include_body_text: bool = True) -> dict:
        """Inspect one normal-Chrome tab using CDP through the extension bridge."""
        try:
            app.policy.assert_feature("browser")
            data = await _hub(app).request(
                "snapshot",
                {
                    "tab_id": tab_id,
                    "max_elements": max(1, min(max_elements, 1000)),
                    "include_body_text": include_body_text,
                },
            )
            return ok("chrome_live_snapshot", {"tab_id": tab_id, **(data or {})})
        except Exception as e:
            return fail("chrome_live_snapshot", e)

    @mcp.tool()
    async def chrome_live_activate(tab_id: int) -> dict:
        """Activate/focus one tab in the user's normal Chrome."""
        try:
            app.policy.assert_feature("browser")
            data = await _hub(app).request("activate", {"tab_id": tab_id})
            app.audit.write("chrome_live_activate", target=str(tab_id))
            return ok("chrome_live_activate", data)
        except Exception as e:
            return fail("chrome_live_activate", e)

    @mcp.tool()
    async def chrome_live_navigate(tab_id: int, url: str) -> dict:
        """Navigate one normal-Chrome tab to a URL."""
        try:
            app.policy.assert_feature("browser")
            data = await _hub(app).request("navigate", {"tab_id": tab_id, "url": url})
            app.audit.write("chrome_live_navigate", target=str(tab_id), detail={"url": url[:2000]})
            return ok("chrome_live_navigate", data)
        except Exception as e:
            return fail("chrome_live_navigate", e)

    @mcp.tool()
    async def chrome_live_click(tab_id: int, selector: str = "", text: str = "", role: str = "", name: str = "") -> dict:
        """Click a DOM element in a normal-Chrome tab by CSS selector, text, or explicit ARIA role/name."""
        try:
            app.policy.assert_feature("browser")
            if not (selector or text or role):
                raise ValueError("Provide selector, text, or role")
            data = await _hub(app).request(
                "click",
                {"tab_id": tab_id, "selector": selector, "text": text, "role": role, "name": name},
            )
            app.audit.write("chrome_live_click", target=str(tab_id), detail={"selector": selector, "text": text, "role": role, "name": name})
            return ok("chrome_live_click", data)
        except Exception as e:
            return fail("chrome_live_click", e)

    @mcp.tool()
    async def chrome_live_fill(
        tab_id: int,
        value: str,
        selector: str = "",
        label: str = "",
        placeholder: str = "",
    ) -> dict:
        """Fill a form control in a normal-Chrome tab by CSS selector, label, or placeholder."""
        try:
            app.policy.assert_feature("browser")
            if not (selector or label or placeholder):
                raise ValueError("Provide selector, label, or placeholder")
            if len(value) > 20000:
                raise ValueError("value too long")
            data = await _hub(app).request(
                "fill",
                {
                    "tab_id": tab_id,
                    "value": value,
                    "selector": selector,
                    "label": label,
                    "placeholder": placeholder,
                },
            )
            app.audit.write("chrome_live_fill", target=str(tab_id), detail={"chars": len(value)})
            return ok("chrome_live_fill", data)
        except Exception as e:
            return fail("chrome_live_fill", e)

    @mcp.tool()
    async def chrome_live_evaluate(tab_id: int, javascript: str) -> dict:
        """Evaluate JavaScript in a normal-Chrome tab through the extension-backed CDP session."""
        try:
            app.policy.assert_feature("browser")
            if len(javascript) > 50000:
                raise ValueError("javascript too long")
            data = await _hub(app).request("evaluate", {"tab_id": tab_id, "javascript": javascript})
            app.audit.write("chrome_live_evaluate", target=str(tab_id), detail={"chars": len(javascript)})
            return ok("chrome_live_evaluate", {"result": data})
        except Exception as e:
            return fail("chrome_live_evaluate", e)

    @mcp.tool(structured_output=False)
    async def chrome_live_screenshot(tab_id: int, full_page: bool = False):
        """Capture a normal-Chrome tab through the extension-backed CDP session."""
        try:
            app.policy.assert_feature("browser")
            data = await _hub(app).request("screenshot", {"tab_id": tab_id, "full_page": full_page}, timeout=30)
            raw = base64.b64decode((data or {}).get("base64", ""), validate=True)
            if not raw:
                raise RuntimeError("Chrome bridge returned an empty screenshot")
            if MCPImage is not None:
                return MCPImage(data=raw, format="png")
            return {"mime_type": "image/png", "base64": base64.b64encode(raw).decode("ascii")}
        except Exception as e:
            return str(fail("chrome_live_screenshot", e))

    @mcp.tool()
    async def chrome_live_detach(tab_id: int) -> dict:
        """Detach the extension-backed debugger session from one normal-Chrome tab."""
        try:
            app.policy.assert_feature("browser")
            data = await _hub(app).request("detach", {"tab_id": tab_id})
            app.audit.write("chrome_live_detach", target=str(tab_id))
            return ok("chrome_live_detach", data)
        except Exception as e:
            return fail("chrome_live_detach", e)
