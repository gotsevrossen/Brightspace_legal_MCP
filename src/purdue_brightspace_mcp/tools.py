"""Tool implementations, kept separate from MCP registration so they can be tested."""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from .auth import SignInTransport, make_transport
from .classification import CLASSIFICATION_RATIONALE, DataClass
from .client import ALLOWED_ROUTES, FORBIDDEN_ROUTE_REASONS, AuthRequired, BrightspaceClient, BrightspaceHTTPError
from .config import ENV_PREFIX, Settings, normalize_course_ref
from .policy import PolicyEngine, PolicyViolation
from .sanitize import clean_text, labelled

COURSE_OFFERING_TYPE_ID = 3

POLICY_REFERENCES = [
    "Acceptable Use of IT Resources and Information Assets (VII.A.4)",
    "Artificial Intelligence Use (VII.A.5) and Purdue's authorized AI tools list",
    "Authentication, Authorization and Access Controls standard (S-13)",
    "User Credentials standard (S-16)",
    "Secure Purdue Data Classification and Handling Procedures (Public / Sensitive / Restricted)",
    "Access to Student Education Records (VIII.A.4) / FERPA",
    "Intellectual Property (I.A.1)",
    "Student Regulations: academic integrity; instructor AI rules in each syllabus",
    "New Brightspace tool integration review (Purdue Brightspace Change Management Board)",
]


def _date(value: str | None) -> str | None:
    return value or None


class BrightspaceTools:
    def __init__(
        self,
        settings: Settings,
        transport_factory: Callable[[Settings], SignInTransport] = make_transport,
    ) -> None:
        self.settings = settings
        self.policy = PolicyEngine(settings)
        self._transport_factory = transport_factory
        self._transport: SignInTransport | None = None
        self._client: BrightspaceClient | None = None
        self._courses: dict[int, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ plumbing

    def _get_client(self) -> BrightspaceClient:
        if self._client is None:
            self._transport = self._transport_factory(self.settings)
            self._client = BrightspaceClient(self.settings, self._transport, self.policy.audit)
        return self._client

    async def _run(self, func, *args, **kwargs) -> dict[str, Any]:
        """Turn policy refusals and auth problems into readable results instead of crashes."""
        try:
            return await func(*args, **kwargs)
        except PolicyViolation as exc:
            return {"status": "refused_by_policy", "message": str(exc)}
        except AuthRequired as exc:
            return {"status": "sign_in_required", "message": str(exc)}
        except BrightspaceHTTPError as exc:
            return {"status": "brightspace_error", "message": f"Brightspace returned HTTP {exc.status}."}

    async def _courses_by_id(self) -> dict[int, dict[str, Any]]:
        """Enrollments keyed by Brightspace org unit ID.

        The IDs stay inside the server: they are used to build API routes but are
        never included in tool output. The AI refers to courses by name or code.
        """
        if self._courses is None:
            client = self._get_client()
            versions = await client.versions()
            items = await client.get_paged(
                f"/d2l/api/lp/{versions['lp']}/enrollments/myenrollments/",
                {"orgUnitTypeId": COURSE_OFFERING_TYPE_ID},
            )
            courses: dict[int, dict[str, Any]] = {}
            for item in items:
                org = item.get("OrgUnit") or {}
                access = item.get("Access") or {}
                if org.get("Id") is None:
                    continue
                course = {
                    "name": org.get("Name"),
                    "code": org.get("Code"),
                    "active": bool(access.get("IsActive")),
                    "can_access": bool(access.get("CanAccess")),
                    "start_date": _date(access.get("StartDate")),
                    "end_date": _date(access.get("EndDate")),
                }
                course["ai_course_materials_permitted"] = self.policy.course_content_permitted(course)
                courses[int(org["Id"])] = course
            self._courses = courses
        return self._courses

    async def _resolve_course(self, course: str) -> tuple[int, dict[str, Any]]:
        """Find one of the user's own enrollments by code or name, e.g. 'CS 18000'."""
        courses = await self._courses_by_id()
        return _resolve(course, courses, "course", lambda c: [c.get("code") or "", c.get("name") or ""],
                        lambda c: c.get("name") or c.get("code") or "?",
                        prefer=lambda c: c["active"] and c["can_access"],
                        missing="is not one of your enrollments. This server only reads data for "
                                "courses you are enrolled in.")

    async def _folders(self, course_id: int) -> list[dict[str, Any]]:
        client = self._get_client()
        versions = await client.versions()
        folders = await client.get(f"/d2l/api/le/{versions['le']}/{course_id}/dropbox/folders/")
        return [f for f in (folders or []) if not f.get("IsHidden") and f.get("Id") is not None]

    # --------------------------------------------------------------------- tools

    async def compliance_status(self) -> dict[str, Any]:
        s = self.settings
        tier = s.ai_data_tier
        return labelled(
            DataClass.PUBLIC,
            {
                "declared_ai_data_tier": tier.label,
                "auth_mode": s.auth_mode,
                "signed_in": bool(self._transport and self._transport.is_signed_in()),
                "ai_permitted_courses": sorted(s.ai_permitted_courses),
                "tools_available_at_this_tier": {
                    "list_courses": tier >= DataClass.SENSITIVE,
                    "upcoming_due_dates": tier >= DataClass.SENSITIVE,
                    "list_assignments": tier >= DataClass.SENSITIVE,
                    "assignment_submission_status": tier >= DataClass.SENSITIVE,
                    "get_announcements": tier >= DataClass.SENSITIVE,
                    "get_course_outline": tier >= DataClass.SENSITIVE,
                    "get_my_grades": tier >= DataClass.RESTRICTED,
                },
                "classification_rationale": {
                    k: {"tier": v[0].label, "why": v[1]} for k, v in CLASSIFICATION_RATIONALE.items()
                },
                "never_accessed": sorted({reason for _, reason in FORBIDDEN_ROUTE_REASONS}),
                "allowed_api_routes": [p.pattern for p in ALLOWED_ROUTES],
                "limits": {
                    "http_methods": ["GET"],
                    "min_seconds_between_requests": s.min_seconds_between_requests,
                    "max_requests_per_hour": s.max_requests_per_hour,
                    "cache_ttl_seconds": s.cache_ttl_seconds,
                    "data_written_to_disk": "none (the audit log holds metadata only)",
                },
                "audit_log": str(self.policy.audit.path) if s.audit_log_enabled else "disabled",
                "policies": POLICY_REFERENCES,
                "tier_guidance": (
                    "The default tier is Sensitive. That is appropriate only if the AI client "
                    "connected to this server is on Purdue's current authorized AI tools list for "
                    "Sensitive data and you use it through Purdue SSO. A personal consumer AI "
                    f"account counts as Public-only: set {ENV_PREFIX}AI_DATA_TIER=public in that "
                    "case. Grades and feedback need AI_DATA_TIER=restricted."
                ),
            },
        )

    async def sign_in(self) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            self._get_client()
            assert self._transport is not None
            self.policy.audit.write("sign_in", auth_mode=self.settings.auth_mode)
            message = await self._transport.sign_in()
            self._courses = None
            return {"status": "signed_in", "message": message}

        return await self._run(go)

    async def sign_out(self) -> dict[str, Any]:
        if self._transport is not None:
            await self._transport.sign_out()
        self._courses = None
        if self._client is not None:
            self._client._cache.clear()
        self.policy.audit.write("sign_out")
        return {"status": "signed_out"}

    async def list_courses(self, active_only: bool = True) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            self.policy.require("list_courses", DataClass.SENSITIVE)
            courses = list((await self._courses_by_id()).values())
            if active_only:
                courses = [c for c in courses if c["active"] and c["can_access"]]
            return labelled(DataClass.SENSITIVE, {"courses": courses})

        return await self._run(go)

    async def upcoming_due_dates(self, days: int = 14) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            self.policy.require("upcoming_due_dates", DataClass.SENSITIVE, days=days)
            window = max(1, min(int(days), 60))
            courses = {cid: c for cid, c in (await self._courses_by_id()).items() if c["active"] and c["can_access"]}
            if not courses:
                return labelled(DataClass.SENSITIVE, {"events": []})
            client = self._get_client()
            versions = await client.versions()
            now = dt.datetime.now(dt.timezone.utc)
            fmt = "%Y-%m-%dT%H:%M:%S.000Z"
            page = await client.get(
                f"/d2l/api/le/{versions['le']}/calendar/events/myEvents/",
                {
                    "orgUnitIdsCSV": ",".join(str(cid) for cid in courses),
                    "startDateTime": now.strftime(fmt),
                    "endDateTime": (now + dt.timedelta(days=window)).strftime(fmt),
                },
            )
            raw = page.get("Objects", []) if isinstance(page, dict) else (page or [])
            events = []
            for ev in raw:
                org_id = ev.get("OrgUnitId")
                course = courses.get(int(org_id)) if org_id is not None else None
                entry = {
                    "title": ev.get("Title"),
                    "course": (course or {}).get("name") or ev.get("OrgUnitName") or ev.get("OrgUnitCode"),
                    "start": ev.get("StartDateTime"),
                    "end": ev.get("EndDateTime"),
                    "all_day": ev.get("IsAllDayEvent"),
                }
                if course is not None and course["ai_course_materials_permitted"]:
                    entry["description"] = clean_text({"Text": ev.get("Description")}, 500)
                events.append(entry)
            events.sort(key=lambda e: e.get("end") or e.get("start") or "")
            return labelled(DataClass.SENSITIVE, {"window_days": window, "events": events})

        return await self._run(go)

    async def list_assignments(self, course: str) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            self.policy.require("list_assignments", DataClass.SENSITIVE, course=course)
            course_id, info = await self._resolve_course(course)
            permitted = info["ai_course_materials_permitted"]
            out = []
            for f in await self._folders(course_id):
                avail = f.get("Availability") or {}
                item = {
                    "name": f.get("Name"),
                    "due_date": f.get("DueDate"),
                    "available_from": avail.get("StartDate"),
                    "available_until": avail.get("EndDate"),
                    "points_possible": (f.get("Assessment") or {}).get("ScoreDenominator"),
                }
                if permitted:
                    item["instructions"] = clean_text(f.get("CustomInstructions"), self.settings.max_text_chars)
                out.append(item)
            notes = None if permitted else [self.policy.course_permission_note(info["name"])]
            return labelled(DataClass.SENSITIVE, {"course": info["name"], "assignments": out}, notes)

        return await self._run(go)

    async def assignment_submission_status(self, course: str, assignment: str) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            self.policy.require(
                "assignment_submission_status", DataClass.SENSITIVE, course=course, assignment=assignment
            )
            course_id, info = await self._resolve_course(course)
            folders = {int(f["Id"]): f for f in await self._folders(course_id)}
            folder_id, folder = _resolve(assignment, folders, "assignment", lambda f: [f.get("Name") or ""],
                                         lambda f: f.get("Name") or "?",
                                         missing=f"was not found in {info['name']}.")
            client = self._get_client()
            versions = await client.versions()
            entries = await client.get(
                f"/d2l/api/le/{versions['le']}/{course_id}/dropbox/folders/{folder_id}/submissions/mysubmissions/"
            )
            include_feedback = self.settings.ai_data_tier >= DataClass.RESTRICTED
            results = []
            for entry in entries or []:
                subs = entry.get("Submissions") or []
                item: dict[str, Any] = {
                    "status": entry.get("Status"),
                    "submission_count": len(subs),
                    "submitted_at": [s.get("SubmissionDate") for s in subs],
                    "files": [f.get("FileName") for s in subs for f in (s.get("Files") or [])],
                }
                fb = entry.get("Feedback") or {}
                if include_feedback and fb:
                    item["feedback"] = {
                        "score": fb.get("Score"),
                        "is_graded": fb.get("IsGraded"),
                        "text": clean_text(fb.get("Feedback"), self.settings.max_text_chars),
                    }
                results.append(item)
            notes = None
            data_class = DataClass.RESTRICTED if include_feedback else DataClass.SENSITIVE
            if not include_feedback:
                notes = ["Scores and feedback are Restricted data and were withheld at the current tier."]
            return labelled(
                data_class, {"course": info["name"], "assignment": folder.get("Name"), "entries": results}, notes
            )

        return await self._run(go)

    async def get_my_grades(self, course: str) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            self.policy.require("get_my_grades", DataClass.RESTRICTED, course=course)
            course_id, info = await self._resolve_course(course)
            client = self._get_client()
            versions = await client.versions()
            values = await client.get(f"/d2l/api/le/{versions['le']}/{course_id}/grades/values/myGradeValues/")
            grades = [
                {
                    "item": g.get("GradeObjectName"),
                    "type": g.get("GradeObjectTypeName"),
                    "displayed_grade": g.get("DisplayedGrade"),
                    "points": g.get("PointsNumerator"),
                    "points_possible": g.get("PointsDenominator"),
                    "weighted": g.get("WeightedNumerator"),
                    "weighted_possible": g.get("WeightedDenominator"),
                    "comments": clean_text(g.get("Comments"), 1000),
                }
                for g in (values or [])
            ]
            final = None
            try:
                fv = await client.get(f"/d2l/api/le/{versions['le']}/{course_id}/grades/final/values/myGradeValue")
                if fv:
                    final = {"displayed_grade": fv.get("DisplayedGrade"), "points": fv.get("PointsNumerator"),
                             "points_possible": fv.get("PointsDenominator")}
            except BrightspaceHTTPError:
                final = None  # not released to students
            return labelled(DataClass.RESTRICTED, {"course": info["name"], "grades": grades, "final_grade": final})

        return await self._run(go)

    async def get_announcements(self, course: str, limit: int = 10) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            self.policy.require("get_announcements", DataClass.SENSITIVE, course=course)
            course_id, info = await self._resolve_course(course)
            client = self._get_client()
            versions = await client.versions()
            items = await client.get(f"/d2l/api/le/{versions['le']}/{course_id}/news/")
            permitted = info["ai_course_materials_permitted"]
            items = sorted(items or [], key=lambda n: n.get("StartDate") or n.get("CreatedDate") or "", reverse=True)
            out = []
            for n in items[: max(1, min(int(limit), 25))]:
                if n.get("IsHidden") or n.get("IsPublished") is False:
                    continue
                entry = {"title": n.get("Title"), "date": n.get("StartDate") or n.get("CreatedDate")}
                if permitted:
                    entry["body"] = clean_text(n.get("Body"), self.settings.max_text_chars)
                out.append(entry)
            notes = None if permitted else [self.policy.course_permission_note(info["name"])]
            return labelled(DataClass.SENSITIVE, {"course": info["name"], "announcements": out}, notes)

        return await self._run(go)

    async def get_course_outline(self, course: str) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            self.policy.require("get_course_outline", DataClass.SENSITIVE, course=course)
            course_id, info = await self._resolve_course(course)
            client = self._get_client()
            versions = await client.versions()
            toc = await client.get(f"/d2l/api/le/{versions['le']}/{course_id}/content/toc")

            def walk(modules: list[dict[str, Any]], depth: int = 0) -> list[dict[str, Any]]:
                out = []
                for m in modules or []:
                    if depth > 6:
                        break
                    out.append(
                        {
                            "module": m.get("Title"),
                            "topics": [
                                {"title": t.get("Title"), "due": t.get("DueDateTime") or t.get("EndDateTime")}
                                for t in (m.get("Topics") or [])
                                if not t.get("IsHidden")
                            ],
                            "submodules": walk(m.get("Modules") or [], depth + 1),
                        }
                    )
                return out

            return labelled(
                DataClass.SENSITIVE,
                {
                    "course": info["name"],
                    "outline": walk((toc or {}).get("Modules") or []),
                    "note": "Titles only. File contents are never downloaded.",
                },
            )

        return await self._run(go)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


def _norm(value: str) -> str:
    return normalize_course_ref(value or "")


def _resolve(query, items, kind, keys, label, prefer=None, missing="was not found."):
    """Match a user-supplied name against items without exposing internal IDs.

    Exact (normalized) matches win, then substring matches. If several remain,
    `prefer` (e.g. "is active") breaks the tie; otherwise the user is asked to
    be more specific, with the candidate names listed.
    """
    q = _norm(str(query))
    if not q:
        raise PolicyViolation(f"Give a {kind} name.")
    exact = [(k, v) for k, v in items.items() if any(_norm(x) == q for x in keys(v))]
    matches = exact or [(k, v) for k, v in items.items() if any(q in _norm(x) for x in keys(v))]
    if len(matches) > 1 and prefer is not None:
        preferred = [m for m in matches if prefer(m[1])]
        matches = preferred or matches
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PolicyViolation(f"{kind.capitalize()} {query!r} {missing}")
    names = sorted({label(v) for _, v in matches})
    raise PolicyViolation(f"{kind.capitalize()} {query!r} matches more than one: {names}. Be more specific.")
