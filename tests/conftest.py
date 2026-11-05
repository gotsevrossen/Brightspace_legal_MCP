from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from purdue_brightspace_mcp.classification import DataClass
from purdue_brightspace_mcp.config import Settings
from purdue_brightspace_mcp.tools import BrightspaceTools

LE = "1.80"
LP = "1.50"

RESPONSES: dict[str, Any] = {
    "/d2l/api/versions/": [
        {"ProductCode": "lp", "LatestVersion": LP},
        {"ProductCode": "le", "LatestVersion": LE},
    ],
    f"/d2l/api/lp/{LP}/enrollments/myenrollments/": {
        "PagingInfo": {"Bookmark": None, "HasMoreItems": False},
        "Items": [
            {
                "OrgUnit": {"Id": 101, "Name": "CS 18000 Fall 2026", "Code": "CS18000", "Type": {"Id": 3}},
                "Access": {"IsActive": True, "CanAccess": True, "StartDate": "2026-08-24T00:00:00.000Z"},
            },
            {
                "OrgUnit": {"Id": 202, "Name": "MA 26100 Fall 2026", "Code": "MA26100", "Type": {"Id": 3}},
                "Access": {"IsActive": True, "CanAccess": True},
            },
        ],
    },
    f"/d2l/api/le/{LE}/101/news/": [
        {
            "Id": 1,
            "Title": "Project 2 posted",
            "Body": {"Text": "Email ta@purdue.edu, PUID 0012345678 not needed.", "Html": ""},
            "StartDate": "2026-11-01T00:00:00.000Z",
            "CreatedBy": 555,
            "IsPublished": True,
        }
    ],
    f"/d2l/api/le/{LE}/202/news/": [
        {"Id": 2, "Title": "Exam room", "Body": {"Text": "Room WTHR 104"}, "StartDate": "2026-11-02T00:00:00.000Z"}
    ],
    f"/d2l/api/le/{LE}/101/dropbox/folders/": [
        {
            "Id": 9,
            "Name": "Project 2",
            "DueDate": "2026-11-20T04:59:00.000Z",
            "CustomInstructions": {"Html": "<p>Write a <b>parser</b></p>"},
            "Assessment": {"ScoreDenominator": 100},
            "TotalUsers": 300,
            "NotificationEmail": "prof@purdue.edu",
        }
    ],
    f"/d2l/api/le/{LE}/101/grades/values/myGradeValues/": [
        {"GradeObjectName": "Project 1", "DisplayedGrade": "95 %", "PointsNumerator": 95, "PointsDenominator": 100}
    ],
    f"/d2l/api/le/{LE}/101/dropbox/folders/9/submissions/mysubmissions/": [
        {
            "Status": 1,
            "Submissions": [{"SubmissionDate": "2026-11-10T00:00:00.000Z", "Files": [{"FileName": "p2.zip"}]}],
            "Feedback": {"Score": 88, "IsGraded": True, "Feedback": {"Text": "Nice"}},
        }
    ],
    f"/d2l/api/le/{LE}/calendar/events/myEvents/": {
        "Objects": [
            {"Title": "Project 2 due", "OrgUnitId": 101, "OrgUnitName": "CS 18000", "EndDateTime": "2026-11-20T04:59:00Z",
             "Description": "see prof@purdue.edu"},
            {"Title": "Exam 2", "OrgUnitId": 202, "OrgUnitName": "MA 26100", "EndDateTime": "2026-11-12T00:00:00Z",
             "Description": "Bring calculator"},
        ]
    },
}


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, list[str]]]] = []
        self.signed_in = True

    def is_signed_in(self) -> bool:
        return self.signed_in

    async def sign_in(self, timeout: float = 300.0) -> str:
        self.signed_in = True
        return "ok"

    async def sign_out(self) -> None:
        self.signed_in = False

    async def get_json(self, url: str) -> Any:
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.hostname == "purdue.brightspace.com"
        self.calls.append((parts.path, parse_qs(parts.query)))
        if parts.path not in RESPONSES:
            from purdue_brightspace_mcp.client import BrightspaceHTTPError

            raise BrightspaceHTTPError(404, parts.path)
        return RESPONSES[parts.path]

    async def close(self) -> None:
        pass


def make_settings(tmp_path, tier: DataClass = DataClass.SENSITIVE, permitted=(101,), **kw) -> Settings:
    return Settings(
        ai_data_tier=tier,
        ai_permitted_courses=frozenset(permitted),
        min_seconds_between_requests=0.0,
        state_dir=tmp_path,
        **kw,
    )


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def make_tools(tmp_path, transport):
    def _make(**kw) -> BrightspaceTools:
        return BrightspaceTools(make_settings(tmp_path, **kw), transport_factory=lambda s: transport)

    return _make
