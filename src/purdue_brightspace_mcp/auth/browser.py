"""Personal browser-session mode (opt-in, see POLICY_COMPLIANCE.md).

For students who cannot get a Purdue-issued OAuth client ID. Unlike the
original scraper this project replaces, this mode:

* never reads, stores or types the user's Purdue password. A normal browser
  window opens on Purdue's own sign-in page, and the user signs in and
  approves Duo themselves (S-13, S-16).
* keeps the session in memory only: the browser context is not persistent,
  so no cookies or tokens are written to disk. Closing the server ends it.
* does not scrape pages or take screenshots. After sign-in it calls the same
  documented, read-only Brightspace REST routes as OAuth mode, through the
  same allowlist and rate limiter.

It still uses the user's interactive session for automated requests, which
Purdue has not explicitly authorized, so it must be switched on with
PURDUE_MCP_BROWSER_MODE_ACKNOWLEDGED=true.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlsplit

from ..client import AuthRequired, BrightspaceHTTPError
from ..config import ALLOWED_HOSTS, Settings

log = logging.getLogger(__name__)


class BrowserSessionTransport:
    def __init__(self, settings: Settings) -> None:
        if not settings.browser_mode_acknowledged:
            raise AuthRequired(
                "Browser mode is off. It uses your own signed-in Brightspace session for "
                "automated read-only requests, which Purdue has not explicitly authorized. Read "
                "POLICY_COMPLIANCE.md and set PURDUE_MCP_BROWSER_MODE_ACKNOWLEDGED=true only if "
                "you accept that, or use OAuth mode with a Purdue-issued client ID."
            )
        self.settings = settings
        self._pw = None
        self._browser = None
        self._context = None

    def is_signed_in(self) -> bool:
        return self._context is not None

    async def sign_in(self, timeout: float = 300.0) -> str:
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise AuthRequired(
                "Browser mode needs Playwright: pip install 'purdue-brightspace-mcp[browser]' "
                "&& playwright install chromium"
            ) from exc

        await self.sign_out()
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=False)
        self._context = await self._browser.new_context()
        page = await self._context.new_page()
        await page.goto(f"{self.settings.base_url}/d2l/home")
        log.info("Sign in to Purdue Brightspace in the browser window that opened.")
        try:
            await page.wait_for_url(
                lambda url: urlsplit(url).hostname in ALLOWED_HOSTS and "/d2l/home" in url,
                timeout=timeout * 1000,
            )
        except Exception as exc:
            await self.sign_out()
            raise AuthRequired("Sign-in was not completed in time.") from exc
        return (
            "Signed in. The browser window can stay open in the background; the session is held "
            "in memory only and ends when the server stops."
        )

    async def sign_out(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._pw = self._browser = self._context = None

    async def get_json(self, url: str) -> Any:
        if self._context is None:
            raise AuthRequired("Not signed in. Call the `sign_in` tool first.")
        resp = await self._context.request.get(url, max_redirects=0, fail_on_status_code=False)
        if resp.status in (301, 302, 303, 307, 308, 401, 403):
            raise AuthRequired(
                f"Brightspace refused the request (HTTP {resp.status}). Your session may have "
                "expired; call `sign_in` again."
            )
        if resp.status >= 400:
            raise BrightspaceHTTPError(resp.status, urlsplit(url).path)
        try:
            return json.loads(await resp.text())
        except json.JSONDecodeError as exc:
            raise BrightspaceHTTPError(resp.status, urlsplit(url).path) from exc

    async def close(self) -> None:
        await self.sign_out()
