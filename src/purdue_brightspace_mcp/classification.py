"""Purdue data classification tiers.

Purdue classifies university data as Public, Sensitive or Restricted
(Acceptable Use of IT Resources and Information Assets, VII.A.4, and the
Secure Purdue Data Classification and Handling Procedures). The Artificial
Intelligence Use policy (VII.A.5) forbids entering Sensitive or Restricted
data into an AI tool unless that tool is authorized for that tier.

Every tool in this server declares the highest tier of data it can return,
and the policy engine compares it against the tier the user says their AI
client is authorized for.
"""

from __future__ import annotations

from enum import IntEnum


class DataClass(IntEnum):
    PUBLIC = 0
    SENSITIVE = 1
    RESTRICTED = 2

    @property
    def label(self) -> str:
        return self.name.capitalize()

    @classmethod
    def parse(cls, value: str) -> "DataClass":
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            allowed = ", ".join(m.name.lower() for m in cls)
            raise ValueError(f"Unknown data tier {value!r}; expected one of: {allowed}") from exc


# Why each kind of Brightspace data gets the tier it does. Shown to the user by
# the `compliance_status` tool so the reasoning is never hidden.
CLASSIFICATION_RATIONALE: dict[str, tuple[DataClass, str]] = {
    "course_enrollments": (
        DataClass.SENSITIVE,
        "A student's own enrollments form part of their education record (FERPA, "
        "VIII.A.4); Purdue's handling examples treat student-linked course data as Sensitive.",
    ),
    "due_dates_and_assignment_titles": (
        DataClass.SENSITIVE,
        "Course data visible only to enrolled users, tied to the student's enrollment.",
    ),
    "instructor_content": (
        DataClass.SENSITIVE,
        "Announcements, assignment instructions and course outlines are non-public "
        "instructional works (Intellectual Property, I.A.1), so they are also gated by a "
        "per-course instructor-permission attestation.",
    ),
    "submission_status": (
        DataClass.SENSITIVE,
        "Whether and when the student submitted is part of their education record.",
    ),
    "grades_scores_feedback": (
        DataClass.RESTRICTED,
        "Grades are Restricted under Purdue's data handling examples and FERPA.",
    ),
    "other_students": (
        DataClass.RESTRICTED,
        "Classlists, classmates' posts and group rosters are other students' education "
        "records. This server never retrieves them.",
    ),
}
