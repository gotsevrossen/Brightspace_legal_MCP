"""Brightspace access for the Study Deck web app.

Live mode uses a Purdue-registered OAuth 2.0 app (authorization code + PKCE):
each player signs in on Purdue's own page, and their tokens live in server
memory only. Requests go through the same read-only, rate-limited, allowlisted
client as the MCP server, with course-file reading switched on.

Personal sign-in mode (opt-in) is for one person running the app on their own
computer without a Purdue OAuth client: Study Deck runs in a browser window the
app opens, they sign in themselves in a new tab of that window, and the app uses
that in-memory session for the same read-only routes.

Without either, players use their own uploaded files and notes. It never falls
back to passwords or page scraping.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import posixpath
import re
import secrets
import sys
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from purdue_brightspace_mcp.client import AuthRequired, BrightspaceClient, BrightspaceHTTPError
from purdue_brightspace_mcp.config import OAUTH_AUTHORIZE_URL, OAUTH_TOKEN_URL
from purdue_brightspace_mcp.policy import AuditLog, PolicyViolation

from .config import StudyDeckSettings
from .extract import ASSESSMENT_TITLE, UnsupportedFile, extract_text

COURSE_OFFERING = 3


@dataclass(frozen=True)
class Course:
    org_id: int
    name: str
    code: str | None = None


@dataclass(frozen=True)
class Module:
    """A content folder. Folders inside folders are listed too, depth first."""

    id: int
    title: str
    file_count: int  # readable files in this folder and every folder inside it
    depth: int = 0
    parent: int | None = None
    path: tuple[str, ...] = ()  # folder titles from the top level down to this one

    @property
    def label(self) -> str:
        return " › ".join(self.path or (self.title,))


@dataclass(frozen=True)
class FileRef:
    """A readable course file, located by the folders it sits in (titles only, no content)."""

    topic_id: int
    title: str
    path: tuple[int, ...]  # folder ids from the top level down
    path_titles: tuple[str, ...]
    name: str = ""  # file name, for picking the text extractor

    @property
    def label(self) -> str:
        return " › ".join((*self.path_titles, self.title))


@dataclass(frozen=True)
class MaterialPiece:
    text: str
    module_id: int | None
    module_title: str
    title: str
    module_path: tuple[int, ...] = ()


class Source(Protocol):
    kind: str

    async def courses(self) -> list[Course]: ...

    async def modules(self, org_id: int) -> list[Module]: ...

    async def files(self, org_id: int, module_ids: list[int] | None) -> list[FileRef]: ...

    async def read(self, org_id: int, files: list[FileRef]) -> list[MaterialPiece]: ...

    async def material(self, org_id: int, module_ids: list[int] | None) -> list[MaterialPiece]: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- OAuth


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float


class WebOAuth:
    def __init__(self, settings: StudyDeckSettings, http: httpx.AsyncClient | None = None) -> None:
        if not settings.oauth_client_id:
            raise AuthRequired("Brightspace sign-in needs a Purdue-issued OAuth client ID.")
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=30.0)

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_url}/auth/brightspace/callback"

    def authorize_url(self, state: str, challenge: str) -> str:
        return OAUTH_AUTHORIZE_URL + "?" + urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.oauth_client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.settings.oauth_scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )

    async def _token(self, form: dict[str, str]) -> Tokens:
        form = {**form, "client_id": self.settings.oauth_client_id or ""}
        if self.settings.oauth_client_secret:
            form["client_secret"] = self.settings.oauth_client_secret
        resp = await self.http.post(OAUTH_TOKEN_URL, data=form)
        if resp.status_code != 200:
            raise AuthRequired(f"Brightspace sign-in failed (HTTP {resp.status_code}).")
        data = resp.json()
        return Tokens(data["access_token"], data.get("refresh_token"),
                      time.time() + int(data.get("expires_in", 3600)) - 60)

    async def exchange(self, code: str, verifier: str) -> Tokens:
        return await self._token({"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": self.redirect_uri, "code_verifier": verifier})

    async def refresh(self, tokens: Tokens) -> Tokens:
        if not tokens.refresh_token:
            raise AuthRequired("Your Brightspace session expired. Connect Brightspace again.")
        return await self._token({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})


class TokenTransport:
    """Bearer-token transport for one signed-in player. Tokens stay in memory."""

    def __init__(self, oauth: WebOAuth, tokens: Tokens, http: httpx.AsyncClient | None = None) -> None:
        self.oauth = oauth
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(timeout=60.0, follow_redirects=False)

    async def _auth(self) -> dict[str, str]:
        if time.time() >= self.tokens.expires_at:
            self.tokens = await self.oauth.refresh(self.tokens)
        return {"Authorization": f"Bearer {self.tokens.access_token}"}

    def _check(self, resp: httpx.Response, url: str) -> None:
        if resp.status_code in (401, 403):
            raise AuthRequired(f"Brightspace refused the request (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            raise BrightspaceHTTPError(resp.status_code, urlsplit(url).path)

    async def get_json(self, url: str) -> Any:
        resp = await self.http.get(url, headers=await self._auth())
        self._check(resp, url)
        return resp.json()

    async def get_bytes(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        async with self.http.stream("GET", url, headers=await self._auth()) as resp:
            self._check(resp, url)
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise UnsupportedFile("A course file is larger than the 15 MB limit and was skipped.")
                chunks.append(chunk)
            return b"".join(chunks), resp.headers.get("content-type", "")

    async def close(self) -> None:
        await self.http.aclose()


# ------------------------------------------------------------ personal sign-in


class BrowserTransport:
    """Personal sign-in mode: the user's own Brightspace session, for the person
    running Study Deck on their own computer (opt-in, see STUDY_DECK.md). Other
    players sign in through GuestTransport, in private windows of the same browser.

    Study Deck runs in a browser window this app opens (the default browser,
    Chrome or Edge). Connect
    Brightspace opens Purdue's sign-in page as a new tab in that same window; the
    user signs in and approves two-factor themselves, so no password ever reaches
    this app, and the tab closes itself once they're in. The window's profile is
    not persistent: cookies stay in memory and are gone when the window closes or
    the server stops. Disconnecting clears the Brightspace cookies but keeps the
    window, since the game runs in it. After sign-in only the same allowlisted,
    read-only, rate-limited API routes as OAuth mode are called.
    """

    poll_seconds = 1.0  # how often a sign-in tab is checked

    @staticmethod
    def channels() -> tuple[str | None, ...]:
        """Browsers to try: the computer's default browser first, then the other one
        already installed, then Playwright's own Chromium (nothing downloaded)."""
        if sys.platform != "win32":
            return ("chrome", "msedge", None)
        try:
            import winreg  # noqa: PLC0415

            key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                prog_id = str(winreg.QueryValueEx(k, "ProgId")[0])
        except OSError:
            prog_id = ""
        if prog_id.startswith("ChromeHTML"):
            return ("chrome", "msedge", None)
        return ("msedge", "chrome", None)

    def __init__(self, base_url: str, app_url: str = "http://127.0.0.1:8000") -> None:
        self.base_url = base_url
        self.app_url = app_url.rstrip("/")
        host = urlsplit(base_url).hostname or ""
        self._brightspace_cookies = re.compile(r"(^|\.)" + re.escape(host.lstrip(".")) + r"$")
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.signed_in = False

    @property
    def is_open(self) -> bool:
        return self._context is not None and self._browser is not None and self._browser.is_connected()

    async def open(self) -> None:
        """Open the Study Deck window, or do nothing if it's already open."""
        if self.is_open:
            return
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise AuthRequired(
                "Personal sign-in needs Playwright: pip install -e \".[web,browser]\""
            ) from exc
        await self.shutdown()
        self._pw = await async_playwright().start()
        last: Exception | None = None
        for channel in self.channels():
            try:
                self._browser = await self._pw.chromium.launch(headless=False, channel=channel)
                break
            except Exception as exc:  # that browser isn't installed; try the next one
                last = exc
        if self._browser is None:
            await self.shutdown()
            raise AuthRequired(
                "No browser found for sign-in. Install Microsoft Edge or Google Chrome, or run "
                "`playwright install chromium`."
            ) from last
        self._context = await self._browser.new_context(no_viewport=True)

    def app_pages(self) -> list[Any]:
        """Tabs in the window that show Study Deck."""
        if not self.is_open:
            return []
        return [p for p in self._context.pages if p.url.startswith(self.app_url + "/")]

    async def app_cookie(self, name: str) -> str | None:
        if not self.is_open:
            return None
        for cookie in await self._context.cookies(self.app_url):
            if cookie.get("name") == name:
                return cookie.get("value")
        return None

    async def show_app(self, url: str | None = None) -> None:
        """Bring Study Deck to the front of the window; with url (a page on this
        site), load it there."""
        await self.open()
        pages = self.app_pages()
        if url is None and pages:
            await pages[0].bring_to_front()
            return
        if pages:
            page = pages[0]
        else:
            blank = [p for p in self._context.pages if p.url in ("", "about:blank")]
            page = blank[0] if blank else await self._context.new_page()
        await page.goto(url or self.app_url + "/")
        await page.bring_to_front()

    async def sign_in(self, timeout: float = 300.0) -> None:
        """Open Purdue's sign-in as a new tab in the Study Deck window and wait."""
        await self.open()
        await self._sign_in_tab(f"{self.base_url}/d2l/home", timeout)
        for page in self.app_pages()[:1]:
            await page.bring_to_front()

    async def _sign_in_tab(self, start_url: str, timeout: float) -> None:
        """Open a tab at start_url, wait until it's signed in to Brightspace, close it."""
        from purdue_brightspace_mcp.auth.browser import is_signed_in_url  # noqa: PLC0415

        self.signed_in = False
        tab = await self._context.new_page()
        await tab.goto(start_url)
        await tab.bring_to_front()
        # The address alone isn't proof: on the way to Purdue's login page the tab
        # passes through /d2l/home before Brightspace redirects it. So each time the
        # tab lands on a new Brightspace page, ask Brightspace whether it's signed in.
        # Asked again when the address changes, or every few seconds while it stays put.
        deadline = time.monotonic() + timeout
        checked, checked_at = "", 0.0
        try:
            while True:
                if tab.is_closed():
                    raise AuthRequired("The sign-in tab was closed before you finished.")
                if time.monotonic() > deadline:
                    raise AuthRequired("Sign-in was not completed in time.")
                url = tab.url
                if is_signed_in_url(url) and (url != checked or time.monotonic() - checked_at > 5):
                    checked, checked_at = url, time.monotonic()
                    if await self._session_ok():
                        break
                await asyncio.sleep(self.poll_seconds)
        except BaseException:
            if not tab.is_closed():
                try:
                    await tab.close()
                except Exception:
                    pass
            raise
        self.signed_in = True
        try:
            await tab.close()
        except Exception:
            pass

    async def _session_ok(self) -> bool:
        """Brightspace confirms this window is signed in (only the status is read)."""
        try:
            resp = await self._context.request.get(f"{self.base_url}/d2l/api/lp/1.0/users/whoami",
                                                   max_redirects=0, fail_on_status_code=False)
            return resp.status == 200
        except Exception:
            return False

    async def _get(self, url: str, method: str = "GET") -> Any:
        if not self.signed_in:
            raise AuthRequired("Not signed in. Connect Brightspace again.")
        if not self.is_open:
            raise AuthRequired("The Study Deck window was closed, which ended the Brightspace session. "
                               "Connect Brightspace again.")
        try:
            resp = await self._context.request.fetch(url, method=method, max_redirects=0,
                                                     fail_on_status_code=False, timeout=120_000)
        except Exception:
            # A timeout or network error. Playwright's message lists the request's
            # headers, session cookies included, so it's dropped here (from None),
            # never logged or shown.
            raise BrightspaceHTTPError(0, urlsplit(url).path) from None
        if resp.status in (301, 302, 303, 307, 308, 401, 403):
            raise AuthRequired(f"Brightspace refused the request (HTTP {resp.status}). Connect Brightspace again.")
        if resp.status >= 400:
            raise BrightspaceHTTPError(resp.status, urlsplit(url).path)
        return resp

    async def get_json(self, url: str) -> Any:
        resp = await self._get(url)
        try:
            return json.loads(await resp.text())
        except json.JSONDecodeError as exc:
            raise BrightspaceHTTPError(resp.status, urlsplit(url).path) from exc

    async def get_bytes(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        # The browser downloads a whole response before it can be checked, so ask for
        # the size first and skip big files (lecture videos, narrated slides) unread.
        too_big = UnsupportedFile("A course file is larger than the 15 MB limit and was skipped.")
        try:
            head = await self._get(url, method="HEAD")
            length = head.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise too_big
        except (BrightspaceHTTPError, AuthRequired):
            pass  # HEAD isn't answered for this file; the download below still checks
        resp = await self._get(url)
        length = resp.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise too_big
        data = await resp.body()
        if len(data) > max_bytes:
            raise UnsupportedFile("A course file is larger than the 15 MB limit and was skipped.")
        return data, resp.headers.get("content-type", "")

    async def close(self) -> None:
        """Disconnect: forget the Brightspace session but keep the window, since the game runs in it."""
        self.signed_in = False
        if self.is_open:
            try:
                await self._context.clear_cookies(domain=self._brightspace_cookies)
            except Exception:
                pass

    async def shutdown(self) -> None:
        """Close the window (server stopping)."""
        self.signed_in = False
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass
        self._pw = self._browser = self._context = None


class GuestTransport(BrowserTransport):
    """Personal sign-in for another player in the game (someone on a phone).

    Purdue only lets people sign in on its own page in a real browser, and the
    browser Study Deck controls is on the host's computer, so the player signs in
    there: in a private window of that browser with its own cookies, so their
    session is never mixed with the host's or anyone else's. The window starts on
    a Study Deck page naming the player, so the right person signs in to it.
    Like the host's session it lives in memory only and ends when they
    disconnect or the server stops.
    """

    def __init__(self, window: BrowserTransport) -> None:
        super().__init__(window.base_url, window.app_url)
        self.window = window

    @property
    def is_open(self) -> bool:
        return self._context is not None and self.window.is_open

    async def open(self) -> None:
        if self.is_open:
            return
        await self.window.open()
        self._context = await self.window._browser.new_context(no_viewport=True)

    def app_pages(self) -> list[Any]:
        return []

    async def sign_in(self, timeout: float = 300.0, intro_url: str | None = None) -> None:
        await self.open()
        try:
            await self._sign_in_tab(intro_url or f"{self.base_url}/d2l/home", timeout)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Disconnect: close this player's private window and forget its cookies."""
        self.signed_in = False
        context, self._context = self._context, None
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    async def shutdown(self) -> None:
        await self.close()


# ---------------------------------------------------------------------- live source


def _walk_topics(module: dict[str, Any]):
    for topic in module.get("Topics") or []:
        yield topic
    for sub in module.get("Modules") or []:
        yield from _walk_topics(sub)


def _usable_file(topic: dict[str, Any]) -> bool:
    if topic.get("IsHidden") or topic.get("IsBroken"):
        return False
    if str(topic.get("TypeIdentifier", "")).lower() != "file":
        return False
    return not ASSESSMENT_TITLE.search(str(topic.get("Title", "")))


class LiveSource:
    kind = "live"

    def __init__(self, settings: StudyDeckSettings, transport: TokenTransport | BrowserTransport,
                 audit: AuditLog) -> None:
        self.settings = settings
        self.client = BrightspaceClient(settings.brightspace, transport, audit, allow_course_files=True)
        self._toc: dict[int, dict[str, Any]] = {}

    async def courses(self) -> list[Course]:
        versions = await self.client.versions()
        items = await self.client.get_paged(
            f"/d2l/api/lp/{versions['lp']}/enrollments/myenrollments/", {"orgUnitTypeId": COURSE_OFFERING}
        )
        out = []
        for item in items:
            org, access = item.get("OrgUnit") or {}, item.get("Access") or {}
            if org.get("Id") is not None and access.get("IsActive") and access.get("CanAccess"):
                out.append(Course(int(org["Id"]), org.get("Name") or "Course", org.get("Code")))
        return out

    async def _toc_for(self, org_id: int) -> dict[str, Any]:
        if org_id not in self._toc:
            versions = await self.client.versions()
            self._toc[org_id] = await self.client.get(f"/d2l/api/le/{versions['le']}/{org_id}/content/toc") or {}
        return self._toc[org_id]

    async def modules(self, org_id: int) -> list[Module]:
        """Every folder that holds readable files (directly or in folders inside it), depth first."""
        toc = await self._toc_for(org_id)
        out: list[Module] = []

        def walk(mods: list[dict[str, Any]], depth: int, parent: int | None, path: tuple[str, ...]) -> None:
            for m in mods:
                if m.get("ModuleId") is None or m.get("IsHidden"):
                    continue
                title = m.get("Title") or "Module"
                count = sum(1 for t in _walk_topics(m) if _usable_file(t))
                if not count:  # nothing readable in it or in any folder inside it
                    continue
                out.append(Module(int(m["ModuleId"]), title, count, depth, parent, (*path, title)))
                walk(m.get("Modules") or [], depth + 1, int(m["ModuleId"]), (*path, title))

        walk(toc.get("Modules") or [], 0, None, ())
        return out

    async def files(self, org_id: int, module_ids: list[int] | None) -> list[FileRef]:
        """Readable files in the chosen folders (any depth), or the whole course."""
        toc = await self._toc_for(org_id)
        wanted = set(module_ids or [])
        out: list[FileRef] = []

        def walk(mods: list[dict[str, Any]], path: tuple[int, ...], titles: tuple[str, ...]) -> None:
            for m in mods:
                if m.get("ModuleId") is None or m.get("IsHidden"):
                    continue
                here, here_titles = (*path, int(m["ModuleId"])), (*titles, m.get("Title") or "Module")
                inside = not wanted or any(i in wanted for i in here)
                for topic in m.get("Topics") or []:
                    if inside and _usable_file(topic) and topic.get("TopicId") is not None:
                        name = posixpath.basename(urlsplit(str(topic.get("Url") or "")).path)
                        out.append(FileRef(int(topic["TopicId"]), topic.get("Title") or "Topic",
                                           here, here_titles, name or str(topic.get("Title") or "")))
                walk(m.get("Modules") or [], here, here_titles)

        walk(toc.get("Modules") or [], (), ())
        return out

    async def read(self, org_id: int, files: list[FileRef]) -> list[MaterialPiece]:
        versions = await self.client.versions()
        pieces: list[MaterialPiece] = []
        for f in files[: self.settings.max_course_files]:
            path = f"/d2l/api/le/{versions['le']}/{org_id}/content/topics/{f.topic_id}/file"
            try:
                data, ctype = await self.client.get_course_file(path)
                text = extract_text(data, f.name or f.title, ctype)
            except (UnsupportedFile, BrightspaceHTTPError, PolicyViolation):
                continue
            if text:
                pieces.append(MaterialPiece(text, f.path[0] if f.path else None,
                                            f.path_titles[0] if f.path_titles else "Module", f.title, f.path))
        return pieces

    async def material(self, org_id: int, module_ids: list[int] | None) -> list[MaterialPiece]:
        return await self.read(org_id, await self.files(org_id, module_ids))

    async def close(self) -> None:
        await self.client.close()
