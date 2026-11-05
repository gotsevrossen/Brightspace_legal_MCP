"""Turn raw Brightspace JSON into minimal, labelled output.

Only fields that the tool needs are copied out (data minimization). Identifiers
of other people (CreatedBy user IDs, instructor notification emails, class-wide
submission counts) are never copied.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

from .classification import DataClass

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Purdue University IDs (PUIDs) are 10 digits, usually starting with 00.
_PUID_RE = re.compile(r"\b00\d{8}\b")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def redact_identifiers(text: str) -> str:
    """Remove email addresses and PUID-like numbers from free text."""
    text = _EMAIL_RE.sub("[email removed]", text)
    return _PUID_RE.sub("[ID removed]", text)


def clean_text(rich: dict[str, Any] | None, limit: int) -> str:
    """Brightspace RichText ({'Text':..., 'Html':...}) -> short, redacted plain text."""
    if not rich:
        return ""
    text = rich.get("Text") or html_to_text(rich.get("Html"))
    text = redact_identifiers(text.strip())
    if len(text) > limit:
        text = text[:limit].rstrip() + " [truncated]"
    return text


def labelled(data_class: DataClass, payload: dict[str, Any], notes: list[str] | None = None) -> dict[str, Any]:
    """Wrap a result with its Purdue classification label.

    Purdue's authorized AI tools list says outputs of AI tools used with
    Sensitive or Restricted data must carry an appropriate sensitivity label.
    """
    result: dict[str, Any] = {
        "purdue_data_classification": data_class.label,
        "handling": _HANDLING[data_class],
        **payload,
    }
    if notes:
        result["policy_notes"] = notes
    return result


_HANDLING = {
    DataClass.PUBLIC: "No special handling required.",
    DataClass.SENSITIVE: (
        "Purdue Sensitive data. Keep it in Purdue-authorized tools and storage; do not post "
        "or share it."
    ),
    DataClass.RESTRICTED: (
        "Purdue Restricted data (education record). Use only in an AI tool authorized for "
        "Restricted data; do not share, post or store it outside Purdue-approved locations."
    ),
}
