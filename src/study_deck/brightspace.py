"""Brightspace access for the Study Deck web app.

Live mode uses a Purdue-registered OAuth 2.0 app (authorization code + PKCE):
each player signs in on Purdue's own page, and their tokens live in server
memory only. Requests go through the same read-only, rate-limited, allowlisted
client as the MCP server, with course-file reading switched on.

Demo mode serves built-in sample courses so the game can be tried without any
Purdue access. It never falls back to passwords or page scraping.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import secrets
import time
from dataclasses import dataclass, field
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
    id: int
    title: str
    file_count: int


@dataclass(frozen=True)
class MaterialPiece:
    text: str
    module_id: int | None
    module_title: str
    title: str


class Source(Protocol):
    kind: str

    async def courses(self) -> list[Course]: ...

    async def modules(self, org_id: int) -> list[Module]: ...

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

    def __init__(self, settings: StudyDeckSettings, transport: TokenTransport, audit: AuditLog) -> None:
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
        toc = await self._toc_for(org_id)
        return [
            Module(int(m["ModuleId"]), m.get("Title") or "Module", sum(1 for t in _walk_topics(m) if _usable_file(t)))
            for m in toc.get("Modules") or []
            if m.get("ModuleId") is not None and not m.get("IsHidden")
        ]

    async def material(self, org_id: int, module_ids: list[int] | None) -> list[MaterialPiece]:
        toc = await self._toc_for(org_id)
        versions = await self.client.versions()
        wanted = set(module_ids or [])
        pieces: list[MaterialPiece] = []
        budget = self.settings.max_course_files
        for module in toc.get("Modules") or []:
            if module.get("IsHidden") or (wanted and int(module.get("ModuleId", -1)) not in wanted):
                continue
            for topic in _walk_topics(module):
                if budget <= 0:
                    return pieces
                if not _usable_file(topic):
                    continue
                budget -= 1
                path = f"/d2l/api/le/{versions['le']}/{org_id}/content/topics/{int(topic['TopicId'])}/file"
                try:
                    data, ctype = await self.client.get_course_file(path)
                    name = posixpath.basename(urlsplit(str(topic.get("Url") or "")).path) or str(topic.get("Title"))
                    text = extract_text(data, name, ctype)
                except (UnsupportedFile, BrightspaceHTTPError, PolicyViolation):
                    continue
                if text:
                    pieces.append(MaterialPiece(text, int(module["ModuleId"]), module.get("Title") or "Module",
                                                topic.get("Title") or "Topic"))
        return pieces

    async def close(self) -> None:
        await self.client.close()


# ---------------------------------------------------------------------- demo source


@dataclass
class _DemoModule:
    id: int
    title: str
    topics: list[tuple[str, str]] = field(default_factory=list)


_DEMO: dict[int, tuple[Course, list[_DemoModule]]] = {
    900101: (
        Course(900101, "BIOL 1100 · Cell Biology (sample)", "BIOL1100-SAMPLE"),
        [
            _DemoModule(1, "Week 1 · Cells and organelles", [
                ("Lecture 1 slides", "The cell is the basic unit of life. Prokaryotic cells lack a nucleus, while eukaryotic cells keep their DNA inside a membrane-bound nucleus. The plasma membrane is a phospholipid bilayer that controls what enters and leaves the cell. Ribosomes are the site of protein synthesis and are found in both prokaryotic and eukaryotic cells.\n\nThe mitochondrion produces most of the cell's ATP through cellular respiration. The endoplasmic reticulum comes in two forms: rough endoplasmic reticulum is studded with ribosomes and folds proteins, while smooth endoplasmic reticulum makes lipids and detoxifies chemicals. The Golgi apparatus modifies, sorts and packages proteins for transport. Lysosomes contain digestive enzymes that break down worn-out organelles."),
                ("Quiz 1 answer key", "This assessment file is excluded automatically."),
            ]),
            _DemoModule(2, "Week 2 · Photosynthesis", [
                ("Lecture 2 notes", "Photosynthesis converts light energy into chemical energy stored in glucose. It takes place in the chloroplast, an organelle found in plant cells and algae. The light-dependent reactions occur in the thylakoid membranes and produce ATP and NADPH while splitting water and releasing oxygen.\n\nThe Calvin cycle takes place in the stroma and uses ATP and NADPH to fix carbon dioxide into sugar. The enzyme rubisco catalyzes the first step of carbon fixation. Chlorophyll is the green pigment that absorbs mostly red and blue light and reflects green light."),
            ]),
            _DemoModule(3, "Week 3 · Cellular respiration", [
                ("Lecture 3 slides", "Cellular respiration breaks down glucose to release energy as ATP. Glycolysis happens in the cytoplasm and splits one glucose molecule into two molecules of pyruvate. The citric acid cycle, also called the Krebs cycle, runs in the mitochondrial matrix and releases carbon dioxide.\n\nThe electron transport chain sits in the inner mitochondrial membrane and produces most of the ATP through oxidative phosphorylation. Oxygen is the final electron acceptor and combines with hydrogen to form water. Without oxygen, cells rely on fermentation, which regenerates NAD+ but yields far less ATP."),
            ]),
        ],
    ),
    900202: (
        Course(900202, "CS 1800 · Problem Solving and Programming (sample)", "CS1800-SAMPLE"),
        [
            _DemoModule(11, "Unit 1 · Algorithms and complexity", [
                ("Unit 1 reading", "An algorithm is a finite sequence of well-defined steps that solves a problem. Big-O notation describes how an algorithm's running time grows as the input size grows. Linear search checks each element in turn and runs in linear time, while binary search repeatedly halves a sorted list and runs in logarithmic time.\n\nSelection sort and insertion sort run in quadratic time in the worst case. Merge sort divides the list in half, sorts each half recursively and merges the results, giving a running time of n log n. A stable sorting algorithm keeps equal elements in their original relative order."),
            ]),
            _DemoModule(12, "Unit 2 · Recursion", [
                ("Unit 2 slides", "Recursion is a technique where a method solves a problem by calling itself on a smaller instance of the same problem. Every recursive method needs a base case that stops the recursion; without one, the calls continue until the program runs out of stack space and throws a stack overflow error.\n\nEach recursive call gets its own frame on the call stack with its own copies of local variables. Tail recursion is when the recursive call is the last operation in the method. The factorial function and the Fibonacci sequence are classic examples of recursion, and memoization stores earlier results so repeated subproblems are not recomputed."),
            ]),
            _DemoModule(13, "Unit 3 · Data structures", [
                ("Unit 3 notes", "An array stores elements in contiguous memory and allows constant-time access by index. A linked list stores elements in nodes that each point to the next node, which makes insertion at the front fast but access by index slow. A stack is a last-in, first-out structure with push and pop operations.\n\nA queue is a first-in, first-out structure where elements are added at the rear and removed from the front. A hash table maps keys to values using a hash function and offers average constant-time lookup. A binary search tree keeps smaller keys in the left subtree and larger keys in the right subtree."),
            ]),
        ],
    ),
}


class DemoSource:
    """Sample courses for demo mode. Every demo player is 'enrolled' in both."""

    kind = "demo"

    async def courses(self) -> list[Course]:
        return [course for course, _ in _DEMO.values()]

    async def modules(self, org_id: int) -> list[Module]:
        _, modules = _DEMO.get(org_id, (None, []))
        return [Module(m.id, m.title, sum(1 for t, _ in m.topics if not ASSESSMENT_TITLE.search(t)))
                for m in modules]

    async def material(self, org_id: int, module_ids: list[int] | None) -> list[MaterialPiece]:
        _, modules = _DEMO.get(org_id, (None, []))
        wanted = set(module_ids or [])
        return [
            MaterialPiece(text, m.id, m.title, title)
            for m in modules
            if not wanted or m.id in wanted
            for title, text in m.topics
            if not ASSESSMENT_TITLE.search(title)
        ]

    async def close(self) -> None:
        pass
