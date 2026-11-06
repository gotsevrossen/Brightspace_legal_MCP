"""Study Deck web app: Starlette backend serving a single-page front end."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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

from .brightspace import Course, DemoSource, LiveSource, Source, TokenTransport, WebOAuth, pkce_pair
from .config import StudyDeckSettings
from .extract import UnsupportedFile, extract_text
from .game import GameSession, Player, SessionStore, Stratum, make_chunks, spawn
from . import genai as genai_mod
from .genai import QUESTION_TYPES, GenAIStudio, GenerationError, Generator, OfflineGenerator

log = logging.getLogger("study_deck")
STATIC = Path(__file__).parent / "static"
NAME_MAX = 24
KEYRING_SERVICE = "study-deck"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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
        self.oauth = WebOAuth(settings) if settings.oauth_client_id and not settings.demo_mode else None

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
            self.users.pop(uid, None)

    def session_for(self, code: str, user: User, *, host: bool = False) -> GameSession:
        session = self.store.get(code)
        if session is None:
            raise ApiError("That game doesn't exist or has ended.", 404)
        if user.id not in session.players:
            raise ApiError("Join this game first.", 403)
        if host and session.host_id != user.id:
            raise ApiError("Only the host can do that.", 403)
        # Enrollment can change when any player connects or disconnects Brightspace.
        for pid, player in session.players.items():
            known = self.users.get(pid)
            player.verified_org_ids = known.verified if known else frozenset()
        return session

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
        if self.settings.demo_mode:
            return OfflineGenerator()
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
        mine = session.answers.get(user.id)
        my_answer = None
        if mine and session.current:
            my_answer = {**mine, "expected": session.current.expected, "explanation": session.current.explanation}
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
                 "counts": session.source_counts(s)}
                for s in session.strata
            ],
            "players": [
                {
                    "name": p.name, "host": p.is_host, "me": p.id == user.id, "score": p.score,
                    "answered": p.answered, "uploads": p.uploads, "share_brightspace": p.share_brightspace,
                    "missing": [label for org, label in orgs.items() if org not in p.verified_org_ids],
                    "answered_current": p.id in session.answers,
                    "correct_current": session.answers.get(p.id, {}).get("correct"),
                }
                for p in session.players.values()
            ],
            "held_back": session.held_back(),
            "has_brightspace": bool(orgs),
            "deck": {"total": len(session.deck), "remaining": session.remaining(), "refilling": session.refilling},
            "card": card,
            "my_answer": my_answer,
            "me": {"name": me.name, "score": me.score, "share_brightspace": me.share_brightspace},
        }


def _clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    if not name:
        raise ApiError("Enter a first name or nickname.")
    return name


def create_app(settings: StudyDeckSettings | None = None) -> Starlette:
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
            "demo_mode": settings.demo_mode,
            "brightspace": {"connected": user.source is not None,
                            "mode": user.source.kind if user.source else None,
                            "courses": len(user.courses)},
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
            "offline_demo": settings.demo_mode and not key,
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
        if settings.demo_mode:
            user.source = DemoSource()
            await deck.load_courses(user)
            return RedirectResponse(user.next_url, 303)
        if deck.oauth is None:
            raise ApiError("Brightspace sign-in isn't configured on this server.", 503)
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        user.pending = (state, verifier)
        deck.audit.write("study_deck_sign_in_start")
        return RedirectResponse(deck.oauth.authorize_url(state, challenge), 303)

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
        return JSONResponse({"modules": [{"id": str(m.id), "title": m.title, "files": m.file_count} for m in mods]})

    # ---------------------------------------------------------------- sessions

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
                    raise ApiError("Name the course for material you'll upload yourself.")
                if kind == "module":
                    raise ApiError("Modules come from Brightspace; use a topic for a course you typed in.")
            stratum = Stratum(id=f"s{n + 1}", course_label=label, org_id=org, kind=kind)
            if kind == "module":
                assert org is not None and user.source is not None
                if org not in module_cache:
                    module_cache[org] = {m.id: m.title for m in await user.source.modules(org)}
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
            size = int(data.get("deck_size") or 20)
        except (TypeError, ValueError) as exc:
            raise ApiError("Deck size must be a number.") from exc
        size = max(max(6, len(strata)), min(size, settings.max_deck_size))
        session = GameSession(code=deck.store.new_code(), host_id=user.id, strata=strata, types=types,
                              deck_size=size, generator=deck.make_generator(data.get("genai_key"), user, request))
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
            added, skipped = 0, []
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
        return JSONResponse({"added": added, "skipped": skipped, "state": deck.view(session, user)})

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
                    whole = any(s.kind != "module" for s in strata)
                    module_ids = None if whole else sorted({m for s in strata for m in s.module_ids})
                    session.progress = f"Reading {session.course_orgs()[org]} from Brightspace…"
                    pieces = await source.material(org, module_ids)
                    for piece in pieces:
                        session.add_chunks(make_chunks(piece.text, source="brightspace", contributor=suppliers[0].id,
                                                       org_id=org, module_id=piece.module_id))
                    session.brightspace_loaded.add(org)
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
        if session.draw() is None:
            session.current = None
            if not session.refilling:
                spawn(session, session.refill)
        if session.wants_refill():
            spawn(session, session.refill)
        return JSONResponse(deck.view(session, user))

    async def answer(request: Request) -> Response:
        user = deck.user_for(request)
        session = deck.session_for(request.path_params["code"], user)
        data = await body(request)
        try:
            session.submit(user.id, str(data.get("question_id")), data.get("answer"))
        except ValueError as exc:
            raise ApiError(str(exc), 409) from exc
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
            session.players.pop(user.id, None)
            # Their uploads leave with them.
            session.chunks = [c for c in session.chunks if c.contributor != user.id or c.source == "brightspace"]
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
        Route("/api/me", api(me), methods=["GET", "POST"]),
        Route("/api/settings/genai", api(genai_save), methods=["POST"]),
        Route("/api/settings/genai/models", api(genai_models), methods=["POST"]),
        Route("/api/settings/genai/forget", api(genai_forget), methods=["POST"]),
        Route("/api/brightspace/disconnect", api(disconnect), methods=["POST"]),
        Route("/api/courses", api(courses)),
        Route("/api/courses/{key}/modules", api(modules)),
        Route("/api/sessions", api(create_session), methods=["POST"]),
        Route("/api/sessions/{code}", api(state)),
        Route("/api/sessions/{code}/join", api(join), methods=["POST"]),
        Route("/api/sessions/{code}/share", api(share), methods=["POST"]),
        Route("/api/sessions/{code}/upload", api(upload), methods=["POST"]),
        Route("/api/sessions/{code}/build", api(build), methods=["POST"]),
        Route("/api/sessions/{code}/draw", api(draw), methods=["POST"]),
        Route("/api/sessions/{code}/answer", api(answer), methods=["POST"]),
        Route("/api/sessions/{code}/end", api(end), methods=["POST"]),
        Route("/api/sessions/{code}/leave", api(leave), methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    from starlette.middleware.base import BaseHTTPMiddleware  # noqa: PLC0415

    app = Starlette(
        routes=routes,
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
    if settings.demo_mode:
        log.warning("Demo mode: sample courses only%s.",
                    "" if settings.genai_api_key else ", offline question generator")
    host = "127.0.0.1"
    uvicorn.run(create_app(settings), host=host, port=int(__import__("os").environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
