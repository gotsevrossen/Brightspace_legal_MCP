"""MCP server entry point (stdio)."""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.types import ToolAnnotations

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

from . import __version__
from .config import Settings
from .tools import BrightspaceTools

INSTRUCTIONS = """\
Read-only access to the signed-in student's own Purdue Brightspace data, under Purdue policy limits.

Rules for the assistant:
- Every result carries `purdue_data_classification`. Treat Sensitive and Restricted results as
  confidential: do not repeat them into public places, other tools or shared documents.
- A result with status `refused_by_policy` is final. Explain the reason to the user; do not try
  other tools to get the same data.
- Academic integrity: this server is for organizing coursework (deadlines, announcements, the
  student's own grades). Do not use course materials to complete graded work unless the course
  syllabus allows it. Quiz and exam content is never available.
- Never ask for the user's Purdue password or Duo codes. Sign-in happens only on Purdue's own page.
- Refer to courses and assignments by name or code (for example "CS 18000"). Internal Brightspace
  IDs are never shown.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
LOCAL_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build_server(settings: Settings | None = None, tools: BrightspaceTools | None = None) -> Any:
    settings = settings or Settings.from_env()
    t = tools or BrightspaceTools(settings)
    server = _Server(name="purdue-brightspace", instructions=INSTRUCTIONS, version=__version__)

    @server.tool(annotations=LOCAL_ONLY)
    async def compliance_status() -> dict[str, Any]:
        """Show the Purdue policy settings in force: the declared AI data tier, which tools are
        enabled, courses with AI permission, request limits, and the policies they implement.
        Makes no Brightspace request."""
        return await t.compliance_status()

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
    async def sign_in() -> dict[str, Any]:
        """Open Purdue's sign-in page in the user's browser so they can sign in and approve Duo
        themselves. The server never sees the password."""
        return await t.sign_in()

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    async def sign_out() -> dict[str, Any]:
        """End the Brightspace session and clear cached data and stored tokens."""
        return await t.sign_out()

    @server.tool(annotations=READ_ONLY)
    async def list_courses(active_only: bool = True) -> dict[str, Any]:
        """List the student's own Brightspace course enrollments (name, code, dates).
        Other tools take a course name or code from this list, e.g. "CS 18000".
        Purdue Sensitive data."""
        return await t.list_courses(active_only)

    @server.tool(annotations=READ_ONLY)
    async def upcoming_due_dates(days: int = 14) -> dict[str, Any]:
        """Calendar events and due dates across the student's active courses for the next `days`
        days (max 60). Purdue Sensitive data."""
        return await t.upcoming_due_dates(days)

    @server.tool(annotations=READ_ONLY)
    async def list_assignments(course: str) -> dict[str, Any]:
        """Assignment folders in one of the student's courses: names, due dates, points.
        Instructions are included only for courses with AI permission. Purdue Sensitive data."""
        return await t.list_assignments(course)

    @server.tool(annotations=READ_ONLY)
    async def assignment_submission_status(course: str, assignment: str) -> dict[str, Any]:
        """Whether and when the student submitted one assignment, given the course and assignment
        names. Score and feedback are included only at the Restricted tier."""
        return await t.assignment_submission_status(course, assignment)

    @server.tool(annotations=READ_ONLY)
    async def get_my_grades(course: str) -> dict[str, Any]:
        """The student's own released grades in one course. Purdue Restricted data: available
        only when the AI client is authorized for Restricted data."""
        return await t.get_my_grades(course)

    @server.tool(annotations=READ_ONLY)
    async def get_announcements(course: str, limit: int = 10) -> dict[str, Any]:
        """Recent announcements in one course. Bodies are included only for courses with AI
        permission. Purdue Sensitive data."""
        return await t.get_announcements(course, limit)

    @server.tool(annotations=READ_ONLY)
    async def get_course_outline(course: str) -> dict[str, Any]:
        """Module and topic titles for one course (no file contents). Purdue Sensitive data."""
        return await t.get_course_outline(course)

    return server


def main() -> None:
    # stdout carries the MCP protocol, so all logging goes to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()
