"""Read-only Brightspace API client with hard limits.

* GET only. There is no code path that sends POST, PUT or DELETE, so this
  server cannot submit work, post, or change anything in Brightspace.
* Only https://purdue.brightspace.com, and only the API routes in
  ALLOWED_ROUTES. Routes for quizzes, exams, classlists, discussions, groups,
  email and file downloads are refused before any request is made.
* Rate limited and cached so it never puts more load on Brightspace than a
  person clicking around would (VII.A.4).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from .config import ALLOWED_HOSTS, Settings
from .policy import AuditLog, PolicyViolation

_V = r"\d+\.\d+"
_ID = r"\d+"

ALLOWED_ROUTES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"^/d2l/api/versions/$",
        rf"^/d2l/api/lp/{_V}/enrollments/myenrollments/$",
        rf"^/d2l/api/le/{_V}/calendar/events/myEvents/$",
        rf"^/d2l/api/le/{_V}/{_ID}/news/$",
        rf"^/d2l/api/le/{_V}/{_ID}/dropbox/folders/$",
        rf"^/d2l/api/le/{_V}/{_ID}/dropbox/folders/{_ID}/submissions/mysubmissions/$",
        rf"^/d2l/api/le/{_V}/{_ID}/content/toc$",
        rf"^/d2l/api/le/{_V}/{_ID}/grades/values/myGradeValues/$",
        rf"^/d2l/api/le/{_V}/{_ID}/grades/final/values/myGradeValue$",
    )
)

# Checked first so the refusal names the reason. Anything not in ALLOWED_ROUTES
# is refused anyway.
FORBIDDEN_ROUTE_REASONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), reason)
    for p, reason in (
        (r"/quizzes?/|/quizzing/|/surveys?/|/selfassessments?/",
         "Quiz, exam and survey content is never accessed (academic integrity, Student Regulations)."),
        (r"/classlist|/enrollments/orgUnits/|/groupcategories|/groups/",
         "Classlists and group rosters contain other students' education records (FERPA, VIII.A.4)."),
        (r"/discussions/",
         "Discussion posts are authored by other students (FERPA, VIII.A.4)."),
        (r"/email|/pager|/messages/",
         "Messages are private communications and are out of scope."),
        (r"/file$|/files/|/attachments/|/download",
         "Course files are not downloaded or redistributed (Intellectual Property, I.A.1)."),
    )
)


class AuthRequired(Exception):
    """No valid Brightspace session; the user needs to sign in."""


class BrightspaceHTTPError(Exception):
    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"Brightspace returned HTTP {status} for {path}")
        self.status = status


class Transport(Protocol):
    async def get_json(self, url: str) -> Any: ...

    async def close(self) -> None: ...


def check_route(path: str) -> None:
    for pattern, reason in FORBIDDEN_ROUTE_REASONS:
        if pattern.search(path):
            raise PolicyViolation(f"Blocked request: {reason}")
    if not any(p.match(path) for p in ALLOWED_ROUTES):
        raise PolicyViolation("Blocked request: the route is not on this server's read-only allowlist.")


class RateLimiter:
    def __init__(self, min_interval: float, per_hour: int, clock=time.monotonic) -> None:
        self.min_interval = min_interval
        self.per_hour = per_hour
        self._clock = clock
        self._last = float("-inf")
        self._window: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            self._window = [t for t in self._window if now - t < 3600]
            if len(self._window) >= self.per_hour:
                raise PolicyViolation(
                    f"Hourly request limit ({self.per_hour}) reached. This limit keeps load on "
                    "Purdue's Brightspace at human browsing levels (VII.A.4). Try again later."
                )
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
                now = self._clock()
            self._last = now
            self._window.append(now)


class BrightspaceClient:
    def __init__(self, settings: Settings, transport: Transport, audit: AuditLog) -> None:
        parts = urlsplit(settings.base_url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"Base URL must be https on one of {sorted(ALLOWED_HOSTS)}")
        self.base_url = f"https://{parts.hostname}"
        self.settings = settings
        self.transport = transport
        self.audit = audit
        self.limiter = RateLimiter(settings.min_seconds_between_requests, settings.max_requests_per_hour)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._versions: dict[str, str] | None = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        check_route(path)
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self.settings.cache_ttl_seconds:
            return cached[1]
        await self.limiter.acquire()
        self.audit.write("api_get", path=path)
        data = await self.transport.get_json(url)
        self._cache[url] = (time.monotonic(), data)
        return data

    async def versions(self) -> dict[str, str]:
        """Latest LP/LE API versions, discovered from Brightspace once per session."""
        if self._versions is None:
            found = {"lp": "1.46", "le": "1.74"}
            try:
                for product in await self.get("/d2l/api/versions/"):
                    code = str(product.get("ProductCode", "")).lower()
                    if code in found and product.get("LatestVersion"):
                        found[code] = product["LatestVersion"]
            except (BrightspaceHTTPError, AuthRequired, TypeError, AttributeError):
                pass
            self._versions = found
        return self._versions

    async def get_paged(self, path: str, params: dict[str, Any] | None = None, max_pages: int = 5) -> list[Any]:
        """Follow Brightspace bookmark paging, capped to keep load small."""
        items: list[Any] = []
        params = dict(params or {})
        for _ in range(max_pages):
            page = await self.get(path, params)
            items.extend(page.get("Items", []))
            paging = page.get("PagingInfo") or {}
            if not paging.get("HasMoreItems"):
                break
            params["bookmark"] = paging.get("Bookmark")
        return items

    async def close(self) -> None:
        await self.transport.close()
