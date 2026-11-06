"""Study Deck settings, read from STUDY_DECK_* environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from purdue_brightspace_mcp.config import DEFAULT_OAUTH_SCOPES, Settings

PREFIX = "STUDY_DECK_"

# Purdue GenAI Studio (RCAC): Purdue-hosted models, authorized for Sensitive
# data excluding FERPA. Course materials are Sensitive, not FERPA records.
DEFAULT_GENAI_URL = "https://genai.rcac.purdue.edu/api/chat/completions"

# The MCP server's read-only scopes, plus reading course content files.
STUDY_SCOPES = tuple(sorted(set(DEFAULT_OAUTH_SCOPES) | {"content:file:read"}))


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(PREFIX + name)
    return default if value is None or not value.strip() else value.strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def check_genai_url(url: str) -> str:
    """Only Purdue-hosted AI endpoints: the AI Use policy (VII.A.5) allows course
    materials only in tools authorized for Sensitive data."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == "purdue.edu" or host.endswith(".purdue.edu")):
        raise ValueError(f"{PREFIX}GENAI_URL must be an https URL on a purdue.edu host, got {url!r}")
    return url


@dataclass(frozen=True)
class StudyDeckSettings:
    public_url: str = "http://127.0.0.1:8000"
    session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    demo_mode: bool = True

    genai_url: str = DEFAULT_GENAI_URL
    genai_model: str = "llama3.1:latest"
    genai_api_key: str | None = None

    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_scopes: tuple[str, ...] = STUDY_SCOPES

    session_ttl_minutes: int = 240
    max_players: int = 8
    max_sessions: int = 200
    max_deck_size: int = 60
    max_upload_bytes: int = 15 * 1024 * 1024
    max_files_per_player: int = 20
    max_course_files: int = 30

    brightspace: Settings = field(default_factory=Settings)

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls) -> "StudyDeckSettings":
        client_id = _env("OAUTH_CLIENT_ID")
        scopes = _env("OAUTH_SCOPES")
        return cls(
            public_url=(_env("PUBLIC_URL", "http://127.0.0.1:8000") or "").rstrip("/"),
            session_secret=_env("SESSION_SECRET") or secrets.token_urlsafe(32),
            # Without a Purdue-issued Brightspace OAuth client the app runs on
            # sample courses only. It never falls back to scraping or passwords.
            demo_mode=_bool("DEMO", client_id is None),
            genai_url=check_genai_url(_env("GENAI_URL", DEFAULT_GENAI_URL) or DEFAULT_GENAI_URL),
            genai_model=_env("GENAI_MODEL", "llama3.1:latest") or "llama3.1:latest",
            genai_api_key=_env("GENAI_API_KEY"),
            oauth_client_id=client_id,
            oauth_client_secret=_env("OAUTH_CLIENT_SECRET"),
            oauth_scopes=tuple(scopes.split()) if scopes else STUDY_SCOPES,
            session_ttl_minutes=int(_env("SESSION_TTL_MINUTES", "240") or 240),
            max_players=int(_env("MAX_PLAYERS", "8") or 8),
            brightspace=Settings(),
        )
