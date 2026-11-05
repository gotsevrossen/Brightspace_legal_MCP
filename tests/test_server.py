from __future__ import annotations

from purdue_brightspace_mcp.server import build_server

EXPECTED = {
    "compliance_status",
    "sign_in",
    "sign_out",
    "list_courses",
    "upcoming_due_dates",
    "list_assignments",
    "assignment_submission_status",
    "get_my_grades",
    "get_announcements",
    "get_course_outline",
}


async def test_tools_registered_and_data_tools_read_only(make_tools):
    server = build_server(tools=make_tools())
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == EXPECTED
    for name in EXPECTED - {"sign_in", "sign_out"}:
        assert tools[name].annotations.read_only_hint is True, name


async def test_compliance_status_makes_no_requests(make_tools, transport):
    result = await make_tools().compliance_status()
    assert result["purdue_data_classification"] == "Public"
    assert result["tools_available_at_this_tier"]["get_my_grades"] is False
    assert transport.calls == []
