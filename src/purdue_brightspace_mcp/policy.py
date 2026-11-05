"""Policy engine: decides whether a tool call may run, and records that it did.

The checks map to Purdue policy as follows (details in POLICY_COMPLIANCE.md):

* Data tier gate: AI Use (VII.A.5) and Acceptable Use (VII.A.4). Sensitive or
  Restricted data is released only if the user has declared that their AI
  client is authorized for that tier.
* Course permission gate: instructors decide whether AI may be used in their
  course (Provost / Teaching@Purdue AI guidance), and course materials are the
  instructor's works (I.A.1). Instructor-authored text is released only for
  courses the user has listed after checking the syllabus.
* Audit log: records metadata (never content) so the user can show what was
  accessed.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .classification import DataClass
from .config import ENV_PREFIX, Settings


class PolicyViolation(Exception):
    """Raised when a request would break a policy rule. The message is shown to the AI client."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class AuditLog:
    def __init__(self, path: Path, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled

    def write(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


class PolicyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.audit = AuditLog(settings.state_dir / "audit.jsonl", settings.audit_log_enabled)

    def check_tier(self, tool: str, data_class: DataClass) -> Decision:
        approved = self.settings.ai_data_tier
        if data_class <= approved:
            return Decision(True, f"{data_class.label} data is within the declared {approved.label} tier.")
        return Decision(
            False,
            f"Refused: '{tool}' returns {data_class.label} data, but this server is configured "
            f"for {approved.label} data only ({ENV_PREFIX}AI_DATA_TIER={approved.name.lower()}). "
            "Purdue's AI Use policy (VII.A.5) does not allow Sensitive or Restricted data in an AI "
            "tool unless that tool is authorized for it. Raise the tier only if your AI client "
            "is on Purdue's authorized AI tools list for that tier and you are signed in through "
            "Purdue SSO.",
        )

    def course_content_permitted(self, course_id: int) -> bool:
        return course_id in self.settings.ai_permitted_courses

    def require(self, tool: str, data_class: DataClass, **context: Any) -> None:
        decision = self.check_tier(tool, data_class)
        self.audit.write(
            "tool_call",
            tool=tool,
            data_class=data_class.label,
            allowed=decision.allowed,
            **context,
        )
        if not decision.allowed:
            raise PolicyViolation(decision.reason)

    def course_permission_note(self, course_id: int) -> str:
        return (
            f"Instructor-authored text for course {course_id} was withheld. Add {course_id} to "
            f"{ENV_PREFIX}AI_PERMITTED_COURSES only after you have checked that the course "
            "syllabus allows AI tools to be used with course materials."
        )
