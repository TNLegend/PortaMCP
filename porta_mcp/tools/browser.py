from __future__ import annotations

import uuid
from functools import wraps
from typing import Any

from ..context import AppContext
from ..platform_support import is_linux, system_chromium_executable
from ..result import fail, ok

try:
    from mcp.server.fastmcp import Image as MCPImage
except Exception:
    try:
        from mcp.server.mcpserver import Image as MCPImage
    except Exception:
        MCPImage = None


def _get_page(app: AppContext, page_id: str):
    if page_id not in app.state.pages:
        raise KeyError(f"Unknown page_id: {page_id}")
    return app.state.pages[page_id]


def register(mcp: Any, app: AppContext) -> None:
    def serialized(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            async with app.state.browser_lock:
                return await fn(*args, **kwargs)
        return wrapper

    @mcp.tool()
    @serialized
    async def browser_start(headless: bool | None = None) -> dict:
        """Start a Playwright Chromium instance. Browser-control action, disabled by default."""
        playwright = None
        browser = None
        try:
            app.policy.assert_feature("browser")
            if app.state.browser is not None:
                return ok("browser_start", {"already_running": True, "pages": list(app.state.pages)})
            if app.state.playwright is not None:
                try:
                    await app.state.playwright.stop()
                except Exception:
                    pass
                app.state.playwright = None
                app.state.browser_context = None
                app.state.pages.clear()
            from playwright.async_api import async_playwright
            playwright = await async_playwright().start()
            actual_headless = app.settings.playwright_headless if headless is None else headless
            launch_kwargs: dict[str, Any] = {"headless": actual_headless}
            browser_backend = "playwright"
            if is_linux():
                system_browser = system_chromium_executable()
                if system_browser is not None:
                    launch_kwargs["executable_path"] = str(system_browser)
                    browser_backend = "system-chromium"
            try:
                browser = await playwright.chromium.launch(**launch_kwargs)
            except Exception:
                if "executable_path" not in launch_kwargs:
                    raise
                browser = await playwright.chromium.launch(headless=actual_headless)
                browser_backend = "playwright-fallback"
            context = await browser.new_context()
            page = await context.new_page()
            pid = str(uuid.uuid4())
            app.state.playwright = playwright
            app.state.browser = browser
            app.state.browser_context = context
            app.state.pages.clear()
            app.state.pages[pid] = page
            app.audit.write(
                "browser_start",
                detail={"page_id": pid, "backend": browser_backend},
            )
            return ok(
                "browser_start",
                {"page_id": pid, "backend": browser_backend},
            )
        except Exception as e:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass
            app.state.browser = None
            app.state.browser_context = None
            app.state.playwright = None
            app.state.pages.clear()
            return fail("browser_start", e)

    @mcp.tool()
    @serialized
    async def browser_connect_cdp(cdp_url: str = "http://127.0.0.1:9222") -> dict:
        """Connect to an existing Chromium/Chrome CDP endpoint. Useful for controlling a browser launched with remote debugging."""
        started_playwright = False
        browser = None
        playwright = app.state.playwright
        try:
            app.policy.assert_feature("browser")
            if playwright is None:
                from playwright.async_api import async_playwright
                playwright = await async_playwright().start()
                started_playwright = True
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            pages: dict[str, Any] = {}
            for page in context.pages:
                pages[str(uuid.uuid4())] = page
            if not pages:
                page = await context.new_page()
                pages[str(uuid.uuid4())] = page
            app.state.playwright = playwright
            app.state.browser = browser
            app.state.browser_context = context
            app.state.pages.clear()
            app.state.pages.update(pages)
            app.audit.write("browser_connect_cdp", target=cdp_url)
            return ok("browser_connect_cdp", {"pages": [{"page_id": k, "url": p.url} for k, p in app.state.pages.items()]})
        except Exception as e:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            if started_playwright and playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass
                if app.state.playwright is playwright:
                    app.state.playwright = None
            return fail("browser_connect_cdp", e)

    @mcp.tool()
    @serialized
    async def browser_pages() -> dict:
        """List tracked browser pages/tabs. Read-only observation."""
        try:
            rows = []
            for pid, p in list(app.state.pages.items()):
                if p.is_closed():
                    app.state.pages.pop(pid, None)
                    continue
                rows.append({"page_id": pid, "url": p.url, "title": await p.title()})
            return ok("browser_pages", {"pages": rows})
        except Exception as e:
            return fail("browser_pages", e)

    @mcp.tool()
    @serialized
    async def browser_new_page(url: str = "about:blank") -> dict:
        """Open a new page and optionally navigate. Browser-control action."""
        try:
            app.policy.assert_feature("browser")
            if app.state.browser_context is None:
                raise RuntimeError("Start/connect browser first")
            p = await app.state.browser_context.new_page()
            pid = str(uuid.uuid4())
            app.state.pages[pid] = p
            if url != "about:blank":
                await p.goto(url, wait_until="domcontentloaded", timeout=30000)
            return ok("browser_new_page", {"page_id": pid, "url": p.url, "title": await p.title()})
        except Exception as e:
            return fail("browser_new_page", e)

    @mcp.tool()
    @serialized
    async def browser_navigate(page_id: str, url: str, wait_until: str = "domcontentloaded") -> dict:
        """Navigate a tracked browser page. Browser-control action."""
        try:
            app.policy.assert_feature("browser")
            p = _get_page(app, page_id)
            resp = await p.goto(url, wait_until=wait_until, timeout=30000)
            return ok("browser_navigate", {"page_id": page_id, "url": p.url, "title": await p.title(), "status": resp.status if resp else None})
        except Exception as e:
            return fail("browser_navigate", e)

    @mcp.tool()
    @serialized
    async def browser_snapshot(page_id: str, max_elements: int = 300, include_body_text: bool = True) -> dict:
        """Return compact structured page state: URL/title, interactive elements and optional visible body text. Read-only."""
        try:
            p = _get_page(app, page_id)
            max_elements = max(1, min(max_elements, 1000))
            data = await p.evaluate("""(limit) => {
              const sel = 'a,button,input,textarea,select,[role],[contenteditable="true"]';
              return Array.from(document.querySelectorAll(sel)).slice(0, limit).map((e,i)=>{
                const r=e.getBoundingClientRect();
                return {i,tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),name:e.getAttribute('aria-label')||e.getAttribute('name')||e.innerText||e.value||'',type:e.getAttribute('type'),id:e.id||'',placeholder:e.getAttribute('placeholder')||'',visible:!!(r.width&&r.height),rect:[r.x,r.y,r.width,r.height]};
              });
            }""", max_elements)
            body = ""
            if include_body_text:
                body = (await p.locator("body").inner_text(timeout=5000))[:100000]
            return ok("browser_snapshot", {"page_id": page_id, "url": p.url, "title": await p.title(), "elements": data, "body_text": body})
        except Exception as e:
            return fail("browser_snapshot", e)

    @mcp.tool()
    @serialized
    async def browser_click(page_id: str, selector: str = "", text: str = "", role: str = "", name: str = "") -> dict:
        """Click a browser element by CSS selector, exact-ish text, or ARIA role/name. Browser-control action."""
        try:
            app.policy.assert_feature("browser")
            p = _get_page(app, page_id)
            if selector:
                loc = p.locator(selector).first
            elif role:
                loc = p.get_by_role(role, name=name or None).first
            elif text:
                loc = p.get_by_text(text, exact=False).first
            else:
                raise ValueError("Provide selector, text, or role")
            await loc.click(timeout=10000)
            return ok("browser_click", {"page_id": page_id, "url": p.url})
        except Exception as e:
            return fail("browser_click", e)

    @mcp.tool()
    @serialized
    async def browser_fill(page_id: str, value: str, selector: str = "", label: str = "", placeholder: str = "") -> dict:
        """Fill a browser form control by CSS selector, label, or placeholder. Browser-control action."""
        try:
            app.policy.assert_feature("browser")
            p = _get_page(app, page_id)
            if selector:
                loc = p.locator(selector).first
            elif label:
                loc = p.get_by_label(label).first
            elif placeholder:
                loc = p.get_by_placeholder(placeholder).first
            else:
                raise ValueError("Provide selector, label, or placeholder")
            await loc.fill(value, timeout=10000)
            app.audit.write("browser_fill", target=p.url, detail={"chars": len(value)})
            return ok("browser_fill", {"page_id": page_id})
        except Exception as e:
            return fail("browser_fill", e)

    @mcp.tool()
    @serialized
    async def browser_press(page_id: str, key: str, selector: str = "body") -> dict:
        """Press a keyboard key against a page element. Browser-control action."""
        try:
            app.policy.assert_feature("browser")
            p = _get_page(app, page_id)
            await p.locator(selector).press(key)
            return ok("browser_press", {"page_id": page_id, "key": key})
        except Exception as e:
            return fail("browser_press", e)

    @mcp.tool()
    @serialized
    async def browser_evaluate(page_id: str, javascript: str) -> dict:
        """Evaluate JavaScript in a page and return a JSON-serializable result. Powerful browser action, disabled by default with browser control."""
        try:
            app.policy.assert_feature("browser")
            if len(javascript) > 50000:
                raise ValueError("javascript too long")
            p = _get_page(app, page_id)
            data = await p.evaluate(javascript)
            app.audit.write("browser_evaluate", target=p.url, detail={"chars": len(javascript)})
            return ok("browser_evaluate", {"result": data})
        except Exception as e:
            return fail("browser_evaluate", e)

    @mcp.tool(structured_output=False)
    @serialized
    async def browser_screenshot(page_id: str, full_page: bool = False):
        """Capture a browser page as MCP image content. Read-only visual observation."""
        try:
            p = _get_page(app, page_id)
            raw = await p.screenshot(full_page=full_page, type="png")
            if MCPImage is not None:
                return MCPImage(data=raw, format="png")
            import base64
            return {"mime_type": "image/png", "base64": base64.b64encode(raw).decode("ascii")}
        except Exception as e:
            return str(fail("browser_screenshot", e))

    @mcp.tool()
    @serialized
    async def browser_close_page(page_id: str) -> dict:
        """Close a browser page. Browser-control action."""
        try:
            app.policy.assert_feature("browser")
            p = _get_page(app, page_id)
            await p.close()
            app.state.pages.pop(page_id, None)
            return ok("browser_close_page", {"page_id": page_id})
        except Exception as e:
            return fail("browser_close_page", e)

    @mcp.tool()
    @serialized
    async def browser_stop() -> dict:
        """Close the managed browser and Playwright runtime. Browser-control action."""
        try:
            app.policy.assert_feature("browser")
        except Exception as e:
            return fail("browser_stop", e)

        browser = app.state.browser
        playwright = app.state.playwright
        close_error: Exception | None = None
        try:
            if browser is not None:
                try:
                    await browser.close()
                except Exception as exc:
                    close_error = exc
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception as exc:
                    if close_error is None:
                        close_error = exc
        finally:
            app.state.browser = None
            app.state.playwright = None
            app.state.browser_context = None
            app.state.pages.clear()
        if close_error is not None:
            return fail("browser_stop", close_error)
        return ok("browser_stop", {})
