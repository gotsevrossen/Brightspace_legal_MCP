"""Runtime configuration, read only from environment variables.

There are deliberately no settings for a Purdue username or password. Purdue's
User Credentials standard (S-16) says passwords are for the authorized user
only, and the Authentication, Authorization and Access Controls standard
(S-13) says credentials must not be coded into programs unless encrypted and
no other reasonable option exists. The user types credentials into Purdue's
own login page and nowhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .classification import DataClass

ENV_PREFIX = "PURDUE_MCP_"

# The only Brightspace tenant this server will ever talk to.
DEFAULT_BASE_URL = "https://purdue.brightspace.com"
ALLOWED_HOSTS = frozenset({"purdue.brightspace.com"})

# D2L's OAuth 2.0 endpoints (docs.valence.desire2learn.com/basic/oauth2.html).
OAUTH_AUTHORIZE_URL = "https://auth.brightspace.com/oauth2/auth"
OAUTH_TOKEN_URL = "https://auth.brightspace.com/core/connect/token"

# Least-privilege, read-only scopes covering exactly the routes this server
# calls (from D2L's OAuth 2 scopes table). No write or "manage" scope, and no
# enrollment:orgunit:read, which would expose classlists.
DEFAULT_OAUTH_SCOPES = (
    "calendar:my_events:read",
    "content:toc:read",
    "dropbox:folders:read",
    "enrollment:own_enrollment:read",
    "grades:own_grades:read",
    "news:newsitems:read",
)

AUTH_MODES = ("oauth", "browser")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return default if value is None else int(value)


def _parse_course_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            raise ValueError(
                f"{ENV_PREFIX}AI_PERMITTED_COURSES does not accept '*'. AI permission is set "
                "per course by each instructor's syllabus, so list each course ID you have "
                "checked."
            )
        ids.add(int(part))
    return frozenset(ids)


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "purdue-brightspace-mcp"


@dataclass(frozen=True)
class Settings:
    # The highest Purdue data tier the AI client is authorized for, according
    # to Purdue's current authorized AI tools list. Defaults to Sensitive, which
    # assumes an AI client approved for Sensitive data and used through Purdue
    # SSO. Grades and feedback (Restricted) still need an explicit opt-in. Set
    # it to "public" when using a personal AI account.
    ai_data_tier: DataClass = DataClass.SENSITIVE
    # Courses whose syllabus the user has checked and which permit AI tools to
    # process course materials.
    ai_permitted_courses: frozenset[int] = field(default_factory=frozenset)

    auth_mode: str = "oauth"
    base_url: str = DEFAULT_BASE_URL

    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_redirect_port: int = 8765
    oauth_scopes: tuple[str, ...] = DEFAULT_OAUTH_SCOPES

    # Browser mode must be acknowledged explicitly; see POLICY_COMPLIANCE.md.
    browser_mode_acknowledged: bool = False

    # Load limits (VII.A.4: do not degrade IT resources for others).
    min_seconds_between_requests: float = 1.0
    max_requests_per_hour: int = 300
    cache_ttl_seconds: int = 300

    max_text_chars: int = 4000
    audit_log_enabled: bool = True
    state_dir: Path = field(default_factory=default_state_dir)

    @classmethod
    def from_env(cls) -> "Settings":
        auth_mode = (_env("AUTH_MODE", "oauth") or "oauth").lower()
        if auth_mode not in AUTH_MODES:
            raise ValueError(f"{ENV_PREFIX}AUTH_MODE must be one of {AUTH_MODES}, got {auth_mode!r}")
        scopes_raw = _env("OAUTH_SCOPES")
        scopes = tuple(scopes_raw.split()) if scopes_raw else DEFAULT_OAUTH_SCOPES
        state_dir = _env("STATE_DIR")
        return cls(
            ai_data_tier=DataClass.parse(_env("AI_DATA_TIER", "sensitive") or "sensitive"),
            ai_permitted_courses=_parse_course_ids(_env("AI_PERMITTED_COURSES")),
            auth_mode=auth_mode,
            oauth_client_id=_env("OAUTH_CLIENT_ID"),
            oauth_client_secret=_env("OAUTH_CLIENT_SECRET"),
            oauth_redirect_port=_env_int("OAUTH_REDIRECT_PORT", 8765),
            oauth_scopes=scopes,
            browser_mode_acknowledged=_env_bool("BROWSER_MODE_ACKNOWLEDGED", False),
            min_seconds_between_requests=max(1.0, float(_env("MIN_SECONDS_BETWEEN_REQUESTS", "1.0") or 1.0)),
            max_requests_per_hour=min(600, _env_int("MAX_REQUESTS_PER_HOUR", 300)),
            cache_ttl_seconds=_env_int("CACHE_TTL_SECONDS", 300),
            max_text_chars=_env_int("MAX_TEXT_CHARS", 4000),
            audit_log_enabled=_env_bool("AUDIT_LOG", True),
            state_dir=Path(state_dir).expanduser() if state_dir else default_state_dir(),
        )
