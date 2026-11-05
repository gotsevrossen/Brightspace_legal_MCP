"""Authentication transports. Neither one ever handles a Purdue password."""

from __future__ import annotations

from typing import Any, Protocol

from ..config import Settings


class SignInTransport(Protocol):
    def is_signed_in(self) -> bool: ...

    async def sign_in(self, timeout: float = 300.0) -> str: ...

    async def sign_out(self) -> None: ...

    async def get_json(self, url: str) -> Any: ...

    async def close(self) -> None: ...


def make_transport(settings: Settings) -> SignInTransport:
    if settings.auth_mode == "browser":
        from .browser import BrowserSessionTransport  # noqa: PLC0415

        return BrowserSessionTransport(settings)
    from .oauth import OAuthTransport  # noqa: PLC0415

    return OAuthTransport(settings)
