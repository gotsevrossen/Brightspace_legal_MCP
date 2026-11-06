from __future__ import annotations

import json

import pytest

from purdue_brightspace_mcp.classification import DataClass
from purdue_brightspace_mcp.client import check_route
from purdue_brightspace_mcp.config import Settings
from purdue_brightspace_mcp.policy import PolicyViolation
from purdue_brightspace_mcp.sanitize import clean_text, html_to_text


def _clean_env(monkeypatch, tmp_path):
    for key in list(__import__("os").environ):
        if key.startswith("PURDUE_MCP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PURDUE_MCP_STATE_DIR", str(tmp_path))


async def test_default_tier_is_sensitive_but_grades_stay_restricted(tmp_path, transport, monkeypatch):
    _clean_env(monkeypatch, tmp_path)
    from purdue_brightspace_mcp.tools import BrightspaceTools

    tools = BrightspaceTools(Settings.from_env(), transport_factory=lambda s: transport)
    assert tools.settings.ai_data_tier is DataClass.SENSITIVE
    assert Settings().ai_data_tier is DataClass.SENSITIVE
    assert (await tools.list_courses())["purdue_data_classification"] == "Sensitive"
    assert (await tools.get_announcements(101))["purdue_data_classification"] == "Sensitive"
    assert (await tools.get_my_grades(101))["status"] == "refused_by_policy"
    assert not any("grades" in path for path, _ in transport.calls)
    status = await tools.assignment_submission_status(101, 9)
    assert "feedback" not in status["entries"][0]


async def test_public_tier_refuses_everything(tmp_path, transport, monkeypatch):
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PURDUE_MCP_AI_DATA_TIER", "public")
    from purdue_brightspace_mcp.tools import BrightspaceTools

    tools = BrightspaceTools(Settings.from_env(), transport_factory=lambda s: transport)
    for result in [await tools.list_courses(), await tools.get_my_grades(101), await tools.get_announcements(101)]:
        assert result["status"] == "refused_by_policy"
    assert transport.calls == []  # nothing was fetched


async def test_grades_need_restricted_tier(make_tools, transport):
    result = await make_tools(tier=DataClass.SENSITIVE).get_my_grades(101)
    assert result["status"] == "refused_by_policy"
    assert "VII.A.5" in result["message"]
    assert not any("grades" in path for path, _ in transport.calls)


async def test_grades_allowed_at_restricted(make_tools):
    result = await make_tools(tier=DataClass.RESTRICTED).get_my_grades(101)
    assert result["purdue_data_classification"] == "Restricted"
    assert result["grades"][0]["displayed_grade"] == "95 %"
    assert result["final_grade"] is None  # 404 -> not released


async def test_instructor_text_requires_course_permission(make_tools):
    tools = make_tools()
    permitted = await tools.get_announcements(101)
    assert "body" in permitted["announcements"][0]
    assert "policy_notes" not in permitted

    withheld = await tools.get_announcements(202)
    assert "body" not in withheld["announcements"][0]
    assert "AI_PERMITTED_COURSES" in withheld["policy_notes"][0]


async def test_identifiers_are_redacted(make_tools):
    body = (await make_tools().get_announcements(101))["announcements"][0]["body"]
    assert "@" not in body and "0012345678" not in body
    assert "[email removed]" in body and "[ID removed]" in body


async def test_assignments_drop_class_wide_and_staff_fields(make_tools):
    result = await make_tools().list_assignments(101)
    item = result["assignments"][0]
    assert item["instructions"] == "Write a parser"
    assert "TotalUsers" not in json.dumps(result) and "prof@purdue.edu" not in json.dumps(result)


async def test_course_must_be_own_enrollment(make_tools, transport):
    result = await make_tools().list_assignments(999)
    assert result["status"] == "refused_by_policy"
    assert not any("/999/" in path for path, _ in transport.calls)


async def test_submission_feedback_only_at_restricted(make_tools):
    sensitive = await make_tools(tier=DataClass.SENSITIVE).assignment_submission_status(101, 9)
    assert sensitive["purdue_data_classification"] == "Sensitive"
    assert "feedback" not in sensitive["entries"][0]

    restricted = await make_tools(tier=DataClass.RESTRICTED).assignment_submission_status(101, 9)
    assert restricted["purdue_data_classification"] == "Restricted"
    assert restricted["entries"][0]["feedback"]["score"] == 88


async def test_due_dates_gate_descriptions_per_course(make_tools, transport):
    result = await make_tools().upcoming_due_dates(7)
    by_course = {e["course_id"]: e for e in result["events"]}
    assert "description" in by_course[101] and "@" not in by_course[101]["description"]
    assert "description" not in by_course[202]
    _, query = next(c for c in transport.calls if "calendar" in c[0])
    assert query["orgUnitIdsCSV"] == ["101,202"]


@pytest.mark.parametrize(
    "path",
    [
        "/d2l/api/le/1.80/101/quizzes/",
        "/d2l/api/le/1.80/101/classlist/",
        "/d2l/api/le/1.80/101/discussions/forums/",
        "/d2l/api/le/1.80/101/content/topics/5/file",
        "/d2l/api/le/1.80/101/dropbox/folders/9/submissions/",
        "/d2l/api/lp/1.50/users/whoami",
        "/d2l/api/le/1.80/101/dropbox/folders/9/attachments/3",
    ],
)
def test_routes_outside_allowlist_are_blocked(path):
    with pytest.raises(PolicyViolation):
        check_route(path)


def test_wildcard_course_permission_rejected(monkeypatch):
    monkeypatch.setenv("PURDUE_MCP_AI_PERMITTED_COURSES", "*")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_rate_limits_cannot_be_loosened(monkeypatch):
    monkeypatch.setenv("PURDUE_MCP_MIN_SECONDS_BETWEEN_REQUESTS", "0")
    monkeypatch.setenv("PURDUE_MCP_MAX_REQUESTS_PER_HOUR", "100000")
    s = Settings.from_env()
    assert s.min_seconds_between_requests == 1.0 and s.max_requests_per_hour == 600


def test_no_credential_settings_exist():
    names = {f.lower() for f in Settings.__dataclass_fields__}
    assert not any("password" in n or "username" in n for n in names)


async def test_audit_log_has_metadata_not_content(make_tools, tmp_path):
    await make_tools().get_announcements(101)
    log = (tmp_path / "audit.jsonl").read_text()
    assert "get_announcements" in log and "Project 2 posted" not in log
    assert oct((tmp_path / "audit.jsonl").stat().st_mode & 0o777) == "0o600"


async def test_rate_limiter_hourly_cap(make_tools):
    tools = make_tools(max_requests_per_hour=2)
    # versions + enrollments use the 2 allowed requests; the next distinct call is refused.
    result = await tools.get_announcements(101)
    assert result["status"] == "refused_by_policy" and "Hourly request limit" in result["message"]


def test_html_to_text():
    assert html_to_text("<p>a</p><script>x()</script><p>b &amp; c</p>") == "a\nb & c"
    assert clean_text({"Text": "x" * 50}, 10).endswith("[truncated]")


def test_base_url_locked_to_purdue(tmp_path, transport):
    from purdue_brightspace_mcp.client import BrightspaceClient
    from purdue_brightspace_mcp.policy import AuditLog

    with pytest.raises(ValueError):
        BrightspaceClient(Settings(base_url="https://evil.example.com", state_dir=tmp_path), transport,
                          AuditLog(tmp_path / "a", False))


def test_browser_mode_requires_acknowledgement(tmp_path):
    from purdue_brightspace_mcp.auth.browser import BrowserSessionTransport
    from purdue_brightspace_mcp.client import AuthRequired

    with pytest.raises(AuthRequired, match="ACKNOWLEDGED"):
        BrowserSessionTransport(Settings(auth_mode="browser", state_dir=tmp_path))
    assert not BrowserSessionTransport(
        Settings(auth_mode="browser", browser_mode_acknowledged=True, state_dir=tmp_path)
    ).is_signed_in()
