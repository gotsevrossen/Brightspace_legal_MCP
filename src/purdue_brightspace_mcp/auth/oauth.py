"""OAuth 2.0 (authorization code + PKCE) against D2L's auth service.

This is the sanctioned path: Purdue's Brightspace administrators register an
OAuth 2.0 app (Admin Tools > Manage Extensibility > OAuth 2.0) after Purdue's
review process for new Brightspace integrations, and issue a client ID. The
user then consents in their own browser on Purdue's sign-in page. This server
never sees the user's Purdue password or Duo approval.

Refresh tokens are kept in the operating system keyring (encrypted at rest by
the OS), or in memory only if no keyring is available, in line with S-13.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
import webbrowser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..client import AuthRequired, BrightspaceHTTPError
from ..config import OAUTH_AUTHORIZE_URL, OAUTH_TOKEN_URL, Settings

log = logging.getLogger(__name__)

KEYRING_SERVICE = "purdue-brightspace-mcp"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _TokenStore:
    """Refresh token in the OS keyring when available, otherwise memory only."""

    def __init__(self, account: str) -> None:
        self.account = account
        self._memory: str | None = None
        try:
            import keyring  # noqa: PLC0415

            keyring.get_keyring()
            self._keyring = keyring
        except Exception:  # pragma: no cover - depends on the host OS
            self._keyring = None

    def load(self) -> str | None:
        if self._keyring is not None:
            try:
                return self._keyring.get_password(KEYRING_SERVICE, self.account)
            except Exception:
                return self._memory
        return self._memory

    def save(self, refresh_token: str | None) -> None:
        self._memory = refresh_token
        if self._keyring is None:
            return
        try:
            if refresh_token:
                self._keyring.set_password(KEYRING_SERVICE, self.account, refresh_token)
            else:
                self._keyring.delete_password(KEYRING_SERVICE, self.account)
        except Exception:
            log.warning("OS keyring unavailable; the refresh token is kept in memory only.")


class OAuthTransport:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        if not settings.oauth_client_id:
            raise AuthRequired(
                "OAuth mode needs PURDUE_MCP_OAUTH_CLIENT_ID. Purdue's Brightspace administrators "
                "issue it when they approve an OAuth 2.0 app registration. Students cannot "
                "register one themselves. See POLICY_COMPLIANCE.md."
            )
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        self.store = _TokenStore(settings.oauth_client_id)
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.settings.oauth_redirect_port}/callback"

    def is_signed_in(self) -> bool:
        return bool(self._access_token and time.time() < self._expires_at) or bool(self.store.load())

    async def sign_in(self, timeout: float = 300.0) -> str:
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        url = OAUTH_AUTHORIZE_URL + "?" + urlencode(
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
        code = await self._wait_for_code(url, state, timeout)
        await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            }
        )
        return "Signed in to Purdue Brightspace through OAuth 2.0."

    async def sign_out(self) -> None:
        self._access_token = None
        self._expires_at = 0.0
        self.store.save(None)

    async def _wait_for_code(self, auth_url: str, state: str, timeout: float) -> str:
        loop = asyncio.get_running_loop()
        result: asyncio.Future[dict[str, list[str]]] = loop.create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line = await reader.readline()
                while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
                parts = request_line.decode("latin-1").split()
                target = urlsplit(parts[1]) if len(parts) > 1 else None
                if target is None or target.path != "/callback":
                    writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    return
                body = b"Purdue Brightspace sign-in finished. You can close this tab."
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                if not result.done():
                    result.set_result(parse_qs(target.query))
            finally:
                await writer.drain()
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", self.settings.oauth_redirect_port)
        try:
            log.info("Opening the Purdue sign-in page in your browser.")
            if not webbrowser.open(auth_url):
                log.warning("Open this URL to sign in: %s", auth_url)
            query = await asyncio.wait_for(result, timeout)
        except asyncio.TimeoutError as exc:
            raise AuthRequired("Sign-in timed out before Brightspace redirected back.") from exc
        finally:
            server.close()
            await server.wait_closed()

        if query.get("state", [None])[0] != state:
            raise AuthRequired("Sign-in failed: the OAuth state did not match.")
        if "error" in query:
            raise AuthRequired(f"Sign-in was declined or failed: {query['error'][0]}")
        code = query.get("code", [None])[0]
        if not code:
            raise AuthRequired("Sign-in failed: no authorization code was returned.")
        return code

    async def _token_request(self, form: dict[str, str]) -> None:
        form = {**form, "client_id": self.settings.oauth_client_id or ""}
        if self.settings.oauth_client_secret:
            form["client_secret"] = self.settings.oauth_client_secret
        resp = await self.http.post(OAUTH_TOKEN_URL, data=form)
        if resp.status_code != 200:
            raise AuthRequired(f"Token request failed with HTTP {resp.status_code}.")
        tokens = resp.json()
        self._access_token = tokens["access_token"]
        self._expires_at = time.time() + int(tokens.get("expires_in", 3600)) - 60
        if tokens.get("refresh_token"):
            self.store.save(tokens["refresh_token"])

    async def _ensure_token(self) -> str:
        async with self._lock:
            if self._access_token and time.time() < self._expires_at:
                return self._access_token
            refresh = self.store.load()
            if not refresh:
                raise AuthRequired("Not signed in. Call the `sign_in` tool first.")
            try:
                await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
            except AuthRequired:
                self.store.save(None)
                raise AuthRequired("Your Brightspace session expired. Call `sign_in` again.") from None
            assert self._access_token
            return self._access_token

    async def get_json(self, url: str) -> Any:
        token = await self._ensure_token()
        resp = await self.http.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code in (401, 403):
            if resp.status_code == 401:
                self._access_token = None
            raise AuthRequired(
                f"Brightspace refused the request (HTTP {resp.status_code}). Sign in again, or the "
                "OAuth app may lack the scope for this route."
            )
        if resp.status_code >= 400:
            raise BrightspaceHTTPError(resp.status_code, urlsplit(url).path)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise BrightspaceHTTPError(resp.status_code, urlsplit(url).path) from exc

    async def close(self) -> None:
        await self.http.aclose()
