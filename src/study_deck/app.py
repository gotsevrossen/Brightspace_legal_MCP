"""Study Deck web app: Starlette backend serving a single-page front end."""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import os
from base64 import b64decode
import hashlib
import hmac
import logging
import re
import secrets
import socket
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from purdue_brightspace_mcp.client import AuthRequired, BrightspaceHTTPError
from purdue_brightspace_mcp.policy import AuditLog, PolicyViolation

from .brightspace import (BrowserTransport, Course, GuestTransport, LiveSource, Source, TokenTransport, WebOAuth,
                          pkce_pair)
from .boilerexams import Boilerexams, spaced as spaced_code
from .config import StudyDeckSettings
from .picker import pick_files
from .extract import UnsupportedFile, extract_text
from .game import GameSession, Player, SessionStore, Stratum, make_chunks, spawn
from . import genai as genai_mod
from .genai import QUESTION_TYPES, GenAIStudio, GenerationError, Generator

log = logging.getLogger("study_deck")
STATIC = Path(__file__).parent / "static"
NAME_MAX = 24
KEYRING_SERVICE = "study-deck"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def lan_address() -> str | None:
    """This computer's address on the local network, e.g. 192.168.1.23.

    A UDP "connect" sends no packets; it only asks the OS which network adapter
    it would use for outside traffic, which is the Wi-Fi or Ethernet one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if not ip or ip.startswith("127.") or ip == "0.0.0.0" else ip


def _keyring():
    """The OS keychain (macOS Keychain, Windows Credential Manager, Secret Service), or None."""
    try:
        import keyring  # noqa: PLC0415
        from keyring.backends.fail import Keyring as NoKeyring  # noqa: PLC0415

        return None if isinstance(keyring.get_keyring(), NoKeyring) else keyring
    except Exception:
        return None


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class User:
    id: str
    name: str = ""
    source: Source | None = None
    courses: dict[int, Course] = field(default_factory=dict)
    pending: tuple[str, str] | None = None  # OAuth state, PKCE verifier
    signing_in: bool = False  # personal sign-in tab is open
    signin_error: str | None = None
    next_url: str = "/#connected"
    genai_key: str | None = None  # entered on the Settings page; memory only
    genai_model: str | None = None
    last_active: float = field(default_factory=time.time)
    hits: list[float] = field(default_factory=list)

    @property
    def verified(self) -> frozenset[int]:
        return frozenset(self.courses)


class StudyDeck:
    """Holds all in-memory state. Nothing here is persisted."""

    def __init__(self, settings: StudyDeckSettings) -> None:
        self.settings = settings
        self.users: dict[str, User] = {}
        self.store = SessionStore(settings.session_ttl_minutes * 60, settings.max_sessions)
        self.audit = AuditLog(settings.brightspace.state_dir / "study-deck-audit.jsonl",
                              settings.brightspace.audit_log_enabled)
        self.oauth = WebOAuth(settings) if settings.oauth_client_id else None
        # Tests plug a fake Brightspace in here; the app itself never sets it.
        self.source_factory: Callable[[], Source] | None = None
        self._tasks: set[asyncio.Task] = set()
        # Personal sign-in: the one browser window Study Deck runs in on this computer.
        self.window: BrowserTransport | None = None
        # One-time links that open Study Deck in that window as the same player.
        self.handoffs: dict[str, tuple[str, float]] = {}
        # Pages that open another player's private sign-in window: token -> (name, expiry).
        self.intros: dict[str, tuple[str, float]] = {}
        # Past exam questions used as a style guide for the AI (tests swap in a fake).
        self.boilerexams = Boilerexams() if settings.boilerexams else None

    async def load_styles(self, session: GameSession) -> None:
        """Before writing cards, find each section's course on Boilerexams: read a few of
        its past exam questions for the AI to model its questions on, and let the game
        deal some real ones as cards. If Boilerexams can't be used, nothing is said."""
        bx = self.boilerexams
        if not session.use_boilerexams or bx is None:
            return
        session.exam_material.clear()
        codes: dict[str, str] = {}
        for s in session.strata:
            try:
                code = await bx.match(s.course_label)
                if code is None:
                    continue
                codes[s.id] = code
                exam_based = not session.material_for(s)
                text, count = await bx.style_text(code, _focus(s), related_only=exam_based)
                session.style_info[s.id] = (code, count)
                if count:
                    session.styles[s.id] = text
                    if exam_based:
                        session.exam_material[s.id] = text
                elif exam_based:
                    session.notices.append(f"No usable Boilerexams questions matched {s.label}. "
                                           "Try the whole course, a different topic, or add your own material.")
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                log.info("Boilerexams unavailable for %s", s.label)
                if not session.material_for(s):
                    session.notices.append(f"Couldn't load Boilerexams questions for {s.label}. Try again later.")
        if not codes:
            return

        async def past_exams(stratum: Stratum, want: int, skip: set[str]) -> list[dict[str, Any]]:
            code = codes.get(stratum.id)
            if code is None:
                return []
            out = []
            for ex in await bx.cards(code, _focus(stratum), want, skip):
                exam = await bx.exam_name(code, ex.exam_id)
                course = spaced_code(code)
                out.append({
                    "qid": ex.qid, "type": "mcq", "prompt": ex.card_prompt, "options": ex.card_options,
                    "answer_index": ex.answer, "keep_order": ex.keep_order,
                    "source": f"Past exam · {exam}" if exam else "Past exam",
                    "explanation": f"From the {exam + ' ' if exam else ''}{course} exam, via Boilerexams.",
                })
            return out

        session.past_exams = past_exams

    def app_window(self) -> BrowserTransport:
        if self.window is None:
            self.window = BrowserTransport(self.settings.brightspace.base_url, self.settings.public_url)
        return self.window

    async def open_window(self) -> None:
        """Open Study Deck in its own browser window (personal sign-in mode, at startup)."""
        try:
            await self.app_window().show_app()
        except Exception as exc:
            log.warning("Couldn't open the Study Deck window (%s). Open %s in a browser instead.",
                        exc, self.settings.public_url)

    async def close_window(self) -> None:
        if self.window is not None:
            await self.window.shutdown()
            self.window = None

    def uid_from_cookie(self, value: str | None) -> str | None:
        """Which player a Study Deck session cookie belongs to."""
        if not value:
            return None
        import itsdangerous  # noqa: PLC0415

        try:
            data = itsdangerous.TimestampSigner(str(self.settings.session_secret)).unsign(
                value.encode(), max_age=self.settings.session_ttl_minutes * 60)
            return json.loads(b64decode(data)).get("uid")
        except Exception:
            return None

    async def _show_in_window(self, user: User, window: BrowserTransport) -> None:
        """Make sure the window shows Study Deck as this player, so the sign-in tab
        opens next to the game and closing it lands back on the game."""
        if window.app_pages() and self.uid_from_cookie(await window.app_cookie("study_deck")) == user.id:
            return
        now = time.time()
        self.handoffs = {t: v for t, v in self.handoffs.items() if v[1] > now}
        token = secrets.token_urlsafe(24)
        self.handoffs[token] = (user.id, now + 120)
        await window.show_app(f"{window.app_url}/auth/window?t={token}")

    async def personal_sign_in(self, user: User) -> None:
        """Personal sign-in: open Purdue's page as a new tab in the Study Deck window and wait."""
        transport = self.app_window()
        try:
            await transport.open()
            await self._show_in_window(user, transport)
            await transport.sign_in()
            source = LiveSource(self.settings, transport, self.audit)
            source.kind = "personal"
            user.source = source
            await self.load_courses(user)
            self.audit.write("study_deck_personal_sign_in", courses=len(user.courses))
        except (AuthRequired, BrightspaceHTTPError, PolicyViolation) as exc:
            user.source, user.courses = None, {}
            user.signin_error = str(exc)
        except Exception:
            log.exception("personal sign-in failed")
            user.source, user.courses = None, {}
            user.signin_error = "Brightspace sign-in failed. Try again."
        finally:
            user.signing_in = False

    async def guest_sign_in(self, user: User) -> None:
        """Personal sign-in for a player on another device: a private window of the
        Study Deck browser opens on this computer, starting on a page that names them."""
        transport = GuestTransport(self.app_window())
        now = time.time()
        self.intros = {t: v for t, v in self.intros.items() if v[1] > now}
        token = secrets.token_urlsafe(24)
        self.intros[token] = (user.name, now + 600)
        try:
            await transport.sign_in(intro_url=f"{self.settings.public_url}/signin-for/{token}")
            source = LiveSource(self.settings, transport, self.audit)
            source.kind = "personal"
            user.source = source
            await self.load_courses(user)
            self.audit.write("study_deck_guest_sign_in", courses=len(user.courses))
        except (AuthRequired, BrightspaceHTTPError, PolicyViolation) as exc:
            await transport.close()
            user.source, user.courses = None, {}
            user.signin_error = str(exc)
        except Exception:
            log.exception("guest sign-in failed")
            await transport.close()
            user.source, user.courses = None, {}
            user.signin_error = "Brightspace sign-in failed. Try again."
        finally:
            self.intros.pop(token, None)
            user.signing_in = False

    def in_a_game(self, user: User) -> bool:
        return any(user.id in s.players for s in self.store.sessions.values())

    # ---------------------------------------------------------------- helpers

    def course_key(self, org_id: int) -> str:
        """Opaque course reference for the browser, so raw Brightspace IDs never leave the server."""
        digest = hmac.new(self.settings.session_secret.encode(), str(org_id).encode(), hashlib.sha256)
        return "c" + digest.hexdigest()[:14]

    def org_for_key(self, user: User, key: str) -> int:
        for org in user.courses:
            if hmac.compare_digest(self.course_key(org), key):
                return org
        raise ApiError("That course isn't one of your Brightspace enrollments.", 403)

    def user_for(self, request: Request) -> User:
        uid = request.session.get("uid")
        if not uid or uid not in self.users:
            uid = secrets.token_urlsafe(16)
            request.session["uid"] = uid
            self.users[uid] = User(uid)
        user = self.users[uid]
        now = time.time()
        user.last_active = now
        user.hits = [t for t in user.hits if now - t < 60]
        user.hits.append(now)
        if len(user.hits) > 150:
            raise ApiError("Too many requests. Slow down a little.", 429)
        self._purge_users(now)
        return user

    def _purge_users(self, now: float) -> None:
        ttl = self.settings.session_ttl_minutes * 60
        for uid in [u for u, user in self.users.items() if now - user.last_active > ttl]:
            gone = self.users.pop(uid, None)
            # A player who left also leaves their private sign-in window.
            if gone is not None and gone.source is not None and not gone.signing_in:
                task = asyncio.ensure_future(gone.source.close())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def session_for(self, code: str, user: User, *, host: bool = False) -> GameSession:
        session = self.store.get(code)
        if session is None:
            raise ApiError("That game doesn't exist or has ended.", 404)
        if user.id in session.kicked:
            raise ApiError("The host removed you from this game.", 403)
        if user.id not in session.players:
            raise ApiError("Join this game first.", 403)
        if host and session.host_id != user.id:
            raise ApiError("Only the host can do that.", 403)
        # Enrollment can change when any player connects or disconnects Brightspace.
        for pid, player in session.players.items():
            known = self.users.get(pid)
            player.verified_org_ids = known.verified if known else frozenset()
        return session

    @property
    def brightspace_available(self) -> bool:
        return self.settings.brightspace_enabled or self.source_factory is not None

    def can_connect(self, request: Request) -> bool:
        """Anyone can connect. With personal sign-in, players on other devices sign in on
        the computer running Study Deck, once they've joined a game there."""
        return self.brightspace_available

    def join_base(self, request: Request) -> str:
        """The address other players open or scan to reach this game.

        On a hosted server that's its public URL. When Study Deck runs on this
        computer and you're looking at it as localhost, it's this computer's
        Wi-Fi address instead, since "localhost" on a phone means the phone."""
        public = urlsplit(self.settings.public_url)
        if (public.hostname or "") not in LOCAL_HOSTS:
            return self.settings.public_url
        url = request.url
        if (url.hostname or "") in LOCAL_HOSTS and self.settings.host not in LOCAL_HOSTS:
            ip = lan_address()
            if ip:
                return f"http://{ip}:{url.port or self.settings.port}"
        return f"{url.scheme}://{url.netloc}"

    # ------------------------------------------------------- GenAI Studio key

    def is_local(self, request: Request) -> bool:
        """True when this server only serves this computer: the OS keychain is then the user's own."""
        public = urlsplit(self.settings.public_url).hostname or ""
        client = request.client.host if request.client else ""
        return public in LOCAL_HOSTS and client in LOCAL_HOSTS

    def saved_genai(self) -> tuple[str | None, str | None]:
        kr = _keyring()
        if kr is None:
            return None, None
        try:
            return kr.get_password(KEYRING_SERVICE, "genai_api_key"), kr.get_password(KEYRING_SERVICE, "genai_model")
        except Exception:
            return None, None

    def effective_genai(self, user: User, request: Request) -> tuple[str | None, str, str | None]:
        """(key, model, source) used when this user hosts a game. Source: yours | saved | server."""
        model = user.genai_model or self.settings.genai_model
        if user.genai_key:
            return user.genai_key, model, "yours"
        if self.is_local(request):
            saved_key, saved_model = self.saved_genai()
            if saved_key:
                return saved_key, user.genai_model or saved_model or self.settings.genai_model, "saved"
        if self.settings.genai_api_key:
            return self.settings.genai_api_key, model, "server"
        return None, model, None

    def make_generator(self, form_key: str | None, user: User, request: Request) -> Generator:
        key, model, _ = self.effective_genai(user, request)
        key = (form_key or "").strip() or key
        if key:
            return GenAIStudio(self.settings.genai_url, model, key)
        raise ApiError("Add your Purdue GenAI Studio API key in Settings to write questions.")

    async def load_courses(self, user: User) -> None:
        assert user.source is not None
        user.courses = {c.org_id: c for c in await user.source.courses()}

    # ------------------------------------------------------------------ views

    def view(self, session: GameSession, user: User) -> dict[str, Any]:
        me = session.players[user.id]
        card = session.current.public() if session.current else None
        if card:
            card["number"] = session.card_number
        # Show the card's result on every screen; turns belong to the physical game.
        answering = outcome = None
        if session.current is not None and session.outcome is not None:
            outcome = {**session.outcome, "expected": session.current.expected,
                       "explanation": session.current.explanation}
        orgs = session.course_orgs()
        return {
            "code": session.code,
            "role": "host" if session.host_id == user.id else "player",
            "status": session.status,
            "progress": session.progress,
            "notices": session.notices[-5:],
            "generator": session.generator.name,
            "types": session.types,
            "deck_size": session.deck_size,
            "strata": [
                {"id": s.id, "label": s.label, "kind": s.kind, "course": s.course_label,
                 "counts": session.source_counts(s),
                 "boilerexams": _boilerexams_view(session, s)}
                for s in session.strata
            ],
            "players": [
                {
                    "key": p.key, "name": p.name, "host": p.is_host, "me": p.id == user.id, "score": p.score,
                    "answered": p.answered, "uploads": p.uploads, "share_brightspace": p.share_brightspace,
                    "missing": [label for org, label in orgs.items() if org not in p.verified_org_ids],
                }
                for p in session.players.values()
            ],
            "held_back": session.held_back(),
            "picked": list(session.picked.values()),
            "has_brightspace": bool(orgs),
            "deck": {"total": len(session.deck), "remaining": session.remaining(), "refilling": session.refilling},
            "card": card,
            "answering": answering,
            "outcome": outcome,
            "me": {"name": me.name, "score": me.score, "share_brightspace": me.share_brightspace,
                   "uploads": me.uploads, "max_uploads": self.settings.max_files_per_player},
        }


def _boilerexams_view(session: GameSession, s: Stratum) -> dict[str, Any] | None:
    """How a section used Boilerexams, or None when it didn't (nothing is shown then)."""
    code, examples = session.style_info.get(s.id, ("", 0))
    cards = session.past_cards.get(s.id, 0)
    if not code or not (examples or cards):
        return None
    view = {"course": code, "examples": examples, "cards": cards}
    if session.exam_material.get(s.id) and not session.material_for(s):
        view["material"] = True
    return view


def _focus(stratum: Stratum) -> list[str]:
    """What a deck section is about, for finding matching past exam topics."""
    return stratum.module_labels or ([stratum.topic] if stratum.topic else [])


def _clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    if not name:
        raise ApiError("Enter a first name or nickname.")
    return name


def create_app(settings: StudyDeckSettings | None = None, *, open_window: bool = False) -> Starlette:
    settings = settings or StudyDeckSettings.from_env()
    deck = StudyDeck(settings)

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception as exc:
            raise ApiError("Expected a JSON body.") from exc
        if not isinstance(data, dict):
            raise ApiError("Expected a JSON object.")
        return data

    # ------------------------------------------------------------------ pages

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html")

    async def me(request: Request) -> Response:
        user = deck.user_for(request)
        if request.method == "POST":
            user.name = _clean_name((await body(request)).get("name"))
        return JSONResponse({
            "name": user.name,
            "personal_mode": settings.browser_mode,
            "max_deck_size": settings.max_deck_size,
            "join_base": deck.join_base(request),
            "on_network": settings.host not in LOCAL_HOSTS,
            "brightspace": {"available": deck.brightspace_available,
                            "can_connect": deck.can_connect(request),
                            "signin_on_host": settings.browser_mode and not deck.is_local(request),
                            "connected": user.source is not None,
                            "mode": user.source.kind if user.source else None,
                            "courses": len(user.courses),
                            "signing_in": user.signing_in,
                            "error": user.signin_error},
            "genai": genai_status(user, request),
        })

    # ---------------------------------------------------------------- settings

    def genai_status(user: User, request: Request) -> dict[str, Any]:
        key, model, source = deck.effective_genai(user, request)
        return {
            "configured": bool(key),
            "source": source,
            "key_hint": f"…{key[-4:]}" if key and len(key) > 8 else None,
            "model": model,
            "can_remember": deck.is_local(request) and _keyring() is not None,
            "remembered": source == "saved" or (deck.is_local(request) and bool(deck.saved_genai()[0])),
        }

    async def genai_models(request: Request) -> Response:
        user = deck.user_for(request)
        key = str((await body(request)).get("key") or "").strip() or deck.effective_genai(user, request)[0]
        if not key:
            raise ApiError("Paste your GenAI Studio API key first.")
        try:
            models = await genai_mod.list_models(settings.genai_url, key)
        except GenerationError as exc:
            raise ApiError(str(exc)) from exc
        return JSONResponse({"models": models})

    async def genai_save(request: Request) -> Response:
        user = deck.user_for(request)
        data = await body(request)
        key = str(data.get("key") or "").strip()
        current = deck.effective_genai(user, request)[0]
        check_key = key or current
        if not check_key:
            raise ApiError("Paste your GenAI Studio API key first.")
        if len(check_key) > 400:
            raise ApiError("That doesn't look like a GenAI Studio API key.")
        try:
            models = await genai_mod.list_models(settings.genai_url, check_key)
        except GenerationError as exc:
            raise ApiError(str(exc)) from exc
        model = str(data.get("model") or "").strip() or (models[0] if models else settings.genai_model)
        if model not in models:
            raise ApiError("Pick a model from the list.")
        if key:
            user.genai_key = key
        user.genai_model = model
        if data.get("remember"):
            kr = _keyring() if deck.is_local(request) else None
            if kr is None:
                raise ApiError("Remembering the key only works when the app runs on your own computer.")
            kr.set_password(KEYRING_SERVICE, "genai_api_key", check_key)
            kr.set_password(KEYRING_SERVICE, "genai_model", model)
        deck.audit.write("study_deck_genai_key_saved", remembered=bool(data.get("remember")))
        return JSONResponse({"genai": genai_status(user, request), "models": models})

    async def genai_forget(request: Request) -> Response:
        user = deck.user_for(request)
        user.genai_key = user.genai_model = None
        if deck.is_local(request):
            kr = _keyring()
            for name in ("genai_api_key", "genai_model"):
                try:
                    if kr is not None:
                        kr.delete_password(KEYRING_SERVICE, name)
                except Exception:
                    pass
        return JSONResponse({"genai": genai_status(user, request)})

    # ------------------------------------------------------------- Brightspace

    async def connect_start(request: Request) -> Response:
        user = deck.user_for(request)
        nxt = request.query_params.get("next", "")
        # Only ever return to a game page on this site (no open redirects).
        user.next_url = nxt if re.fullmatch(r"/#/game/[A-Z0-9]{6}", nxt) else "/#connected"
        if deck.source_factory is not None:
            user.source = deck.source_factory()
            await deck.load_courses(user)
            return RedirectResponse(user.next_url, 303)
        if not settings.brightspace_enabled:
            return RedirectResponse("/#signin-unavailable", 303)
        if settings.browser_mode:
            # Personal sign-in uses the browser on this computer. Players on other devices
            # get a private window there, but only once they're in a game, so nobody else
            # on the network can open windows on the host's screen.
            local = deck.is_local(request)
            if not local and not deck.in_a_game(user):
                return RedirectResponse("/#signin-join-first", 303)
            if not local and not user.signing_in and \
                    sum(u.signing_in for u in deck.users.values()) >= settings.max_players:
                return RedirectResponse("/#signin-busy", 303)
            if not user.signing_in:
                if user.source is not None:
                    await user.source.close()
                user.source, user.courses = None, {}
                user.signing_in, user.signin_error = True, None
                deck.audit.write("study_deck_sign_in_start", mode="personal" if local else "guest")
                task = asyncio.create_task(deck.personal_sign_in(user) if local else deck.guest_sign_in(user))
                deck._tasks.add(task)
                task.add_done_callback(deck._tasks.discard)
            return RedirectResponse(user.next_url if user.next_url.startswith("/#/game/") else "/#/", 303)
        if deck.oauth is None:
            raise ApiError("Brightspace sign-in isn't configured on this server.", 503)
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        user.pending = (state, verifier)
        deck.audit.write("study_deck_sign_in_start")
        return RedirectResponse(deck.oauth.authorize_url(state, challenge), 303)

    async def window_handoff(request: Request) -> Response:
        """One-time link the app opens in its own window, so Study Deck there is the
        same player who clicked Connect Brightspace in another browser."""
        uid, expires = deck.handoffs.pop(request.query_params.get("t", ""), (None, 0.0))
        if not deck.is_local(request) or uid not in deck.users or expires < time.time():
            return RedirectResponse("/#/", 303)
        request.session["uid"] = uid
        user = deck.user_for(request)
        return RedirectResponse(user.next_url if user.next_url.startswith("/#/game/") else "/#/", 303)

    async def signin_intro(request: Request) -> Response:
        """First page of another player's private sign-in window, on the host's screen:
        says whose window it is, so the right person signs in to it."""
        name, expires = deck.intros.get(request.path_params["token"], ("", 0.0))
        if not deck.is_local(request) or expires < time.time():
            return Response("This sign-in link has expired. Ask for a new one from Study Deck.",
                            status_code=404, media_type="text/plain")
        who = html.escape(name or "this player")
        brightspace = html.escape(f"{settings.brightspace.base_url}/d2l/home")
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brightspace sign-in for {who}</title><link rel="stylesheet" href="/static/app.css"></head>
<body><main class="wrap"><section class="card lead">
<p class="eyebrow">Study Deck</p><h1>Sign-in for {who}</h1>
<p>{who} asked to connect Brightspace from their phone. <b>Only {who} should sign in here</b>, with their
own Purdue account and Authenticator. This window closes by itself once they're in, and their sign-in
is kept apart from everyone else's.</p>
<a class="btn dark big wide" href="{brightspace}">Continue to Purdue sign-in</a>
</section></main></body></html>"""
        return Response(page, media_type="text/html")

    async def connect_callback(request: Request) -> Response:
        user = deck.user_for(request)
        if deck.oauth is None or user.pending is None:
            return RedirectResponse("/#signin-failed", 303)
        state, verifier = user.pending
        user.pending = None
        if not hmac.compare_digest(request.query_params.get("state", ""), state) or "code" not in request.query_params:
            return RedirectResponse("/#signin-failed", 303)
        try:
            tokens = await deck.oauth.exchange(request.query_params["code"], verifier)
            user.source = LiveSource(settings, TokenTransport(deck.oauth, tokens), deck.audit)
            await deck.load_courses(user)
        except (AuthRequired, BrightspaceHTTPError, PolicyViolation):
            user.source = None
            return RedirectResponse("/#signin-failed", 303)
        return RedirectResponse(user.next_url, 303)

    async def disconnect(request: Request) -> Response:
        user = deck.user_for(request)
        if user.source is not None:
            await user.source.close()
        user.source, user.courses = None, {}
        return JSONResponse({"ok": True})

    async def courses(request: Request) -> Response:
        user = deck.user_for(request)
        if user.source is None:
            return JSONResponse({"courses": []})
        return JSONResponse({"courses": [
            {"key": deck.course_key(c.org_id), "name": c.name} for c in user.courses.values()
        ]})

    async def modules(request: Request) -> Response:
        user = deck.user_for(request)
        if user.source is None:
            raise ApiError("Connect Brightspace first.", 403)
        org = deck.org_for_key(user, request.path_params["key"])
        mods = await user.source.modules(org)
        return JSONResponse({"modules": [
            {"id": str(m.id), "title": m.title, "label": m.label, "files": m.file_count, "depth": m.depth,
             "parent": str(m.parent) if m.parent is not None else None}
            for m in mods]})

    # ---------------------------------------------------------------- sessions

    async def boilerexams_courses(request: Request) -> Response:
        """Course codes on Boilerexams, so the host page can mark matching courses."""
        deck.user_for(request)
        if deck.boilerexams is None:
            return JSONResponse({"enabled": False, "courses": []})
        try:
            codes = sorted(await deck.boilerexams.courses())
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return JSONResponse({"enabled": True, "courses": [], "unreachable": True})
        return JSONResponse({"enabled": True, "courses": codes})

    async def create_session(request: Request) -> Response:
        user = deck.user_for(request)
        data = await body(request)
        user.name = _clean_name(data.get("name") or user.name)
        raw_strata = data.get("strata") or []
        if not isinstance(raw_strata, list) or not 1 <= len(raw_strata) <= 12:
            raise ApiError("Choose between 1 and 12 things to study.")
        permitted = set(data.get("ai_permission_courses") or [])
        strata: list[Stratum] = []
        module_cache: dict[int, dict[int, str]] = {}
        for n, raw in enumerate(raw_strata):
            kind = raw.get("kind")
            if kind not in ("course", "module", "topic"):
                raise ApiError("Each selection must be a whole course, modules or a topic.")
            key = raw.get("course")
            if key:
                org = deck.org_for_key(user, key)
                if key not in permitted:
                    raise ApiError(f"Confirm that {user.courses[org].name}'s syllabus allows AI study tools.")
                label = user.courses[org].name
            else:
                org = None
                label = " ".join(str(raw.get("manual_course") or "").split())[:60]
                if not label:
                    raise ApiError("Choose a Boilerexams course or name the course for your own material.")
                if kind == "module":
                    raise ApiError("Modules come from Brightspace; use a topic for a course you typed in.")
            stratum = Stratum(id=f"s{n + 1}", course_label=label, org_id=org, kind=kind)
            if kind == "module":
                assert org is not None and user.source is not None
                if org not in module_cache:
                    module_cache[org] = {m.id: m.label for m in await user.source.modules(org)}
                ids = [int(i) for i in raw.get("modules") or [] if str(i).isdigit()]
                ids = [i for i in ids if i in module_cache[org]]
                if not ids:
                    raise ApiError(f"Pick at least one module in {label}.")
                stratum.module_ids, stratum.module_labels = ids, [module_cache[org][i] for i in ids]
            if kind == "topic":
                topic = " ".join(str(raw.get("topic") or "").split())[:80]
                if len(topic) < 2:
                    raise ApiError(f"Type a topic for {label}.")
                stratum.topic = topic
            strata.append(stratum)
        types = [t for t in QUESTION_TYPES if t in (data.get("types") or [])]
        if not types:
            raise ApiError("Choose multiple choice, fill in the blank, or both.")
        try:
            raw_size = data.get("deck_size")
            size = 20 if raw_size is None or raw_size == "" else int(raw_size)
        except (TypeError, ValueError) as exc:
            raise ApiError("Deck size must be a number.") from exc
        smallest = len(strata)  # at least one card per section
        if not smallest <= size <= settings.max_deck_size:
            raise ApiError(f"Choose a deck of {smallest} to {settings.max_deck_size} cards"
                           f"{' (at least one per section)' if smallest > 1 else ''}.")
        session = GameSession(code=deck.store.new_code(), host_id=user.id, strata=strata, types=types,
                              deck_size=size, generator=deck.make_generator(data.get("genai_key"), user, request),
                              use_boilerexams=data.get("boilerexams", True) is not False)
        session.players[user.id] = Player(user.id, user.name, is_host=True, verified_org_ids=user.verified)
        try:
            deck.store.add(session)
        except RuntimeError as exc:
            raise ApiError(str(exc), 503) from exc
        deck.audit.write("study_deck_session_created", strata=len(strata), deck_size=size)
        return JSONResponse({"code": session.code})

    async def join(request: Request) -> Response:
        user = deck.user_for(request)
        data = await body(request)
        session = deck.store.get(str(request.path_params["code"]))
        if session is None:
            raise ApiError("No game with that code. Check it with the host.", 404)
        if user.id in session.kicked:
            raise ApiError("The host removed you from this game.", 403)
        if user.id not in session.players:
            if len(session.players) >= settings.max_players:
                raise ApiError("This game is full.", 403)
            needed = session.deck_has_brightspace() - user.verified
            if needed:
                names = ", ".join(session.course_orgs()[o] for o in needed)
                raise ApiError(
                    f"This deck already uses Brightspace material from {names}. Connect Brightspace to "
                    "confirm you're enrolled before joining.", 403)
            user.name = _clean_name(data.get("name") or user.name)
            session.players[user.id] = Player(user.id, user.name, verified_org_ids=user.verified)
        return JSONResponse({"code": session.code})

    async def state(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user)
        return JSONResponse(deck.view(session, user))

    async def qr(request: Request) -> Response:
        """QR code that opens this game's join page, for players' phone cameras."""
        import segno  # noqa: PLC0415

        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user)
        buf = io.BytesIO()
        segno.make(f"{deck.join_base(request)}/#/join/{session.code}", error="m").save(
            buf, kind="svg", scale=8, border=2, dark="#000000", light="#ffffff", xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml")

    async def share(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user)
        session.players[user.id].share_brightspace = bool((await body(request)).get("share"))
        return JSONResponse(deck.view(session, user))

    async def upload(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user)
        player = session.players[user.id]
        if session.status == "building":
            raise ApiError("The deck is being built. Add material after it's ready.", 409)
        length = int(request.headers.get("content-length") or 0)
        if length > settings.max_upload_bytes * 4:
            raise ApiError("That upload is too large. Send fewer or smaller files.", 413)
        async with request.form(max_files=settings.max_files_per_player, max_part_size=200_000) as form:
            if form.get("attest") != "yes":
                raise ApiError("Confirm you're allowed to share this material with the players in this game.")
            stratum_id = str(form.get("stratum") or "")
            if stratum_id not in {s.id for s in session.strata}:
                raise ApiError("Pick which section this material is for.")
            added, skipped = [], []
            notes = str(form.get("notes") or "").strip()
            if notes:
                from .extract import clean_text  # noqa: PLC0415

                added += session.add_chunks(make_chunks(clean_text(notes)[:60_000], source="notes",
                                                        contributor=user.id, stratum_id=stratum_id))
            # Browsers send an empty file part when nothing was picked.
            files = [f for f in form.getlist("files") if hasattr(f, "read") and f.filename and (f.size or 0) > 0]
            if player.uploads + len(files) > settings.max_files_per_player:
                raise ApiError(f"Each player can add up to {settings.max_files_per_player} files.")
            for f in files:
                data = await f.read()
                if len(data) > settings.max_upload_bytes:
                    skipped.append(f"{f.filename}: larger than 15 MB")
                    continue
                try:
                    text = await run_in_threadpool(extract_text, data, f.filename, f.content_type)
                except UnsupportedFile as exc:
                    skipped.append(str(exc))
                    continue
                player.uploads += 1
                added += session.add_chunks(make_chunks(text, source="upload", contributor=user.id,
                                                        stratum_id=stratum_id))
        if not added and not skipped:
            raise ApiError("Add a file or paste some notes.")
        # During play, write cards from it now rather than when its turn comes round.
        writing = session.status == "ready" and bool(added)
        if writing:
            async def write_new() -> None:
                try:
                    await session.write_from_new(stratum_id, added)
                except GenerationError as exc:
                    session.notices.append(f"Couldn't write cards from the new material: {exc}")
                except Exception:  # keep the game alive; never leak internals to players
                    log.exception("writing cards from new material failed")
                    session.notices.append("Couldn't write cards from the new material.")

            spawn(session, write_new)
        return JSONResponse({"added": len(added), "skipped": skipped, "writing": writing,
                             "state": deck.view(session, user)})

    async def build(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user, host=True)
        if session.status not in ("lobby",):
            raise ApiError("The deck is already built.", 409)
        session.status, session.progress = "building", "Gathering material…"

        async def run() -> None:
            try:
                for org in sorted(session.shareable_orgs() - session.brightspace_loaded):
                    suppliers = sorted(
                        (p for p in session.players.values()
                         if p.share_brightspace and org in p.verified_org_ids
                         and deck.users.get(p.id) and deck.users[p.id].source is not None),
                        key=lambda p: not p.is_host,
                    )
                    if not suppliers:
                        continue
                    source = deck.users[suppliers[0].id].source
                    assert source is not None
                    strata = [s for s in session.strata if s.org_id == org]
                    course = session.course_orgs()[org]
                    whole = any(s.kind != "module" for s in strata)
                    module_ids = None if whole else sorted({m for s in strata for m in s.module_ids})
                    session.progress = f"Looking through {course}'s folders…"
                    scope = await source.files(org, module_ids)
                    # Typed topics narrow the files, unless a section wants the whole course.
                    topics = [] if any(s.kind == "course" for s in strata) else [
                        s.topic for s in strata if s.kind == "topic" and s.topic]
                    sections = [s.topic if s.kind == "topic" else
                                " / ".join(s.module_labels) if s.kind == "module" else "the whole course"
                                for s in strata]
                    if len(scope) > settings.max_course_files or topics:
                        session.progress = f"Choosing which {course} files to read…"
                    pick = await pick_files(session.generator, course, scope, [x for x in sections if x],
                                            topics, settings.max_course_files)
                    session.picked[org] = {"course": course, "how": pick.how, "of": len(scope),
                                           "folders": pick.folders, "files": [f.label for f in pick.files]}
                    deck.audit.write("study_deck_files_picked", how=pick.how, files=len(pick.files), of=len(scope))
                    session.progress = f"Reading {len(pick.files)} file{'' if len(pick.files) == 1 else 's'} from {course}…"
                    pieces = await source.read(org, pick.files)
                    for piece in pieces:
                        session.add_chunks(make_chunks(piece.text, source="brightspace", contributor=suppliers[0].id,
                                                       org_id=org, module_id=piece.module_id,
                                                       module_path=piece.module_path))
                    session.brightspace_loaded.add(org)
                await deck.load_styles(session)
                added = await session.generate(session.deck_size)
                if not added:
                    raise GenerationError("No cards could be written from this material.")
                session.status = "ready"
                deck.audit.write("study_deck_deck_built", cards=added)
            except (GenerationError, AuthRequired, BrightspaceHTTPError, PolicyViolation) as exc:
                session.status = "lobby"
                session.notices.append(str(exc))
            except Exception:  # keep the game alive; never leak internals to players
                log.exception("deck build failed")
                session.status = "lobby"
                session.notices.append("Something went wrong building the deck. Try again.")
            finally:
                session.progress = ""

        spawn(session, run)
        return JSONResponse(deck.view(session, user))

    async def draw(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user, host=True)
        if session.status != "ready":
            raise ApiError("Build the deck first.", 409)
        if session.current is not None and session.outcome is None:
            raise ApiError("Enter the answer for this card before drawing the next one.", 409)
        if session.draw() is None:
            session.current = None
            if not session.refilling:
                spawn(session, session.refill)
        if session.wants_refill():
            spawn(session, session.refill)
        return JSONResponse(deck.view(session, user))

    async def answer(request: Request) -> Response:
        """The host checks an answer; the physical game determines who is answering."""
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user, host=True)
        data = await body(request)
        try:
            session.submit(str(data.get("question_id")), data.get("answer"), skipped=data.get("skip") is True)
        except ValueError as exc:
            raise ApiError(str(exc), 409) from exc
        return JSONResponse(deck.view(session, user))

    async def kick(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user, host=True)
        if session.status == "building":
            raise ApiError("Wait until the deck is built, then remove them.", 409)
        player = session.player_by_key((await body(request)).get("player"))
        if player is None or player.is_host:
            raise ApiError("That player isn't in this game.", 404)
        session.remove_player(player.id, kicked=True)
        deck.audit.write("study_deck_player_removed")
        return JSONResponse(deck.view(session, user))

    async def end(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user, host=True)
        deck.store.end(session.code)
        deck.audit.write("study_deck_session_ended")
        return JSONResponse({"ended": True})

    async def leave(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user)
        if session.host_id == user.id:
            deck.store.end(session.code)
        else:
            session.remove_player(user.id)  # their uploads leave with them
        return JSONResponse({"left": True})

    # ------------------------------------------------------------- middleware

    async def guard(request: Request, call_next):
        try:
            if request.method == "POST" and request.url.path.startswith("/api/"):
                if request.headers.get("x-study-deck") != "1":
                    return JSONResponse({"error": "Missing request header."}, status_code=403)
            response = await call_next(request)
        except ApiError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=exc.status)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def api(handler):
        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except ApiError as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status)
            except (AuthRequired, BrightspaceHTTPError) as exc:
                return JSONResponse({"error": f"Brightspace: {exc}"}, status_code=502)
            except PolicyViolation as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
        return wrapped

    routes = [
        Route("/", index),
        Route("/auth/brightspace/start", api(connect_start)),
        Route("/auth/brightspace/callback", api(connect_callback)),
        Route("/auth/window", api(window_handoff)),
        Route("/signin-for/{token}", api(signin_intro)),
        Route("/api/me", api(me), methods=["GET", "POST"]),
        Route("/api/settings/genai", api(genai_save), methods=["POST"]),
        Route("/api/settings/genai/models", api(genai_models), methods=["POST"]),
        Route("/api/settings/genai/forget", api(genai_forget), methods=["POST"]),
        Route("/api/brightspace/disconnect", api(disconnect), methods=["POST"]),
        Route("/api/courses", api(courses)),
        Route("/api/courses/{key}/modules", api(modules)),
        Route("/api/sessions", api(create_session), methods=["POST"]),
        Route("/api/sessions/{code}", api(state)),
        Route("/api/sessions/{code}/qr.svg", api(qr)),
        Route("/api/sessions/{code}/join", api(join), methods=["POST"]),
        Route("/api/sessions/{code}/share", api(share), methods=["POST"]),
        Route("/api/sessions/{code}/upload", api(upload), methods=["POST"]),
        Route("/api/sessions/{code}/build", api(build), methods=["POST"]),
        Route("/api/sessions/{code}/draw", api(draw), methods=["POST"]),
        Route("/api/sessions/{code}/answer", api(answer), methods=["POST"]),
        Route("/api/sessions/{code}/kick", api(kick), methods=["POST"]),
        Route("/api/boilerexams", api(boilerexams_courses)),
        Route("/api/sessions/{code}/end", api(end), methods=["POST"]),
        Route("/api/sessions/{code}/leave", api(leave), methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    from starlette.middleware.base import BaseHTTPMiddleware  # noqa: PLC0415

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        if open_window and settings.browser_mode:
            task = asyncio.create_task(deck.open_window())
            deck._tasks.add(task)
            task.add_done_callback(deck._tasks.discard)
        elif open_window:
            # Open the site in this computer's default browser once the server is up.
            asyncio.get_running_loop().call_later(1.0, webbrowser.open, settings.public_url)
        yield
        await deck.close_window()
        if deck.boilerexams is not None:
            await deck.boilerexams.close()

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[
            Middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="study_deck",
                       max_age=settings.session_ttl_minutes * 60, same_site="lax",
                       https_only=settings.secure_cookies),
            Middleware(BaseHTTPMiddleware, dispatch=guard),
        ],
    )
    app.state.deck = deck
    return app


def main() -> None:
    import uvicorn  # noqa: PLC0415

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    settings = StudyDeckSettings.from_env()
    if settings.browser_mode:
        log.warning("Personal sign-in mode: Brightspace uses your own session on this computer "
                    "(STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED). Players on other devices sign in here, "
                    "in private windows.")
    elif not settings.oauth_client_id:
        log.info("Brightspace isn't connected on this server: players add their own files and notes.")
    lines = [f"  On this computer:   {settings.public_url}"]
    if settings.host not in LOCAL_HOSTS:
        public = urlsplit(settings.public_url).hostname or ""
        ip = lan_address()
        if public not in LOCAL_HOSTS:
            lines.append(f"  Players join at:    {settings.public_url}")
        elif ip:
            lines.append(f"  Same Wi-Fi players: http://{ip}:{settings.port}")
        else:
            lines.append("  No network connection found, so only this computer can reach it.")
    print("\nStudy Deck is running.\n" + "\n".join(lines) + "\n  Press Ctrl+C to stop.\n", flush=True)
    open_browser = os.environ.get("STUDY_DECK_OPEN_BROWSER", "true").strip().lower() in {"1", "true", "yes", "on"}
    uvicorn.run(create_app(settings, open_window=open_browser or settings.browser_mode),
                host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
