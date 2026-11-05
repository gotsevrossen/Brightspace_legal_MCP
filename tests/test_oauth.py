from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from purdue_brightspace_mcp.auth import oauth as oauth_mod
from purdue_brightspace_mcp.auth.oauth import OAuthTransport
from purdue_brightspace_mcp.client import AuthRequired
from purdue_brightspace_mcp.config import OAUTH_TOKEN_URL, Settings


def test_missing_client_id_explains_how_to_get_one(tmp_path):
    with pytest.raises(AuthRequired, match="Purdue's Brightspace administrators"):
        OAuthTransport(Settings(state_dir=tmp_path))


async def test_pkce_sign_in_and_bearer_requests(tmp_path, monkeypatch, unused_tcp_port):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == OAUTH_TOKEN_URL:
            form = parse_qs(request.content.decode())
            seen["form"] = form
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        state_dir=tmp_path, oauth_client_id="client-123", oauth_redirect_port=unused_tcp_port
    )
    t = OAuthTransport(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(t.store, "_keyring", None)  # keep the test off the real OS keyring

    def fake_browser_open(url: str) -> bool:
        q = parse_qs(urlsplit(url).query)
        seen["authorize"] = q

        async def redirect_back() -> None:
            async with httpx.AsyncClient() as c:
                await c.get(f"{t.redirect_uri}?code=CODE&state={q['state'][0]}")

        asyncio.get_running_loop().create_task(redirect_back())
        return True

    monkeypatch.setattr(oauth_mod.webbrowser, "open", fake_browser_open)
    await t.sign_in(timeout=10)

    auth_q = seen["authorize"]
    assert auth_q["code_challenge_method"] == ["S256"]
    assert "grades:own_grades:read" in auth_q["scope"][0]
    assert not any(s.endswith(":manage") for s in auth_q["scope"][0].split())
    form = seen["form"]
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["CODE"]
    assert "code_verifier" in form and "password" not in form

    assert await t.get_json("https://purdue.brightspace.com/d2l/api/versions/") == {"ok": True}
    assert seen["auth"] == "Bearer AT"
    await t.sign_out()
    assert not t.is_signed_in()


async def test_state_mismatch_rejected(tmp_path, monkeypatch, unused_tcp_port):
    settings = Settings(state_dir=tmp_path, oauth_client_id="c", oauth_redirect_port=unused_tcp_port)
    t = OAuthTransport(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))

    def fake_open(url: str) -> bool:
        async def go() -> None:
            async with httpx.AsyncClient() as c:
                await c.get(f"{t.redirect_uri}?code=X&state=forged")

        asyncio.get_running_loop().create_task(go())
        return True

    monkeypatch.setattr(oauth_mod.webbrowser, "open", fake_open)
    with pytest.raises(AuthRequired, match="state"):
        await t.sign_in(timeout=10)
