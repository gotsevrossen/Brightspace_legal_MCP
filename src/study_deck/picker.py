"""Choosing which Brightspace files a deck reads.

Some instructors put everything in one folder with folders inside it, so the
top-level folder alone says little. When a section is a typed topic, or the
chosen folders hold more files than one deck reads, the AI sees an outline of
folder and file TITLES (never file contents, names of people, or grades) and
picks which folders to open, at any depth, and which single files to read.
Without an AI that can pick (offline demo generator) or when its reply is
unusable, titles are matched against the topics instead.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .brightspace import FileRef
from .extract import relevance

log = logging.getLogger(__name__)

OUTLINE_MAX_FILES = 400
OUTLINE_MAX_CHARS = 24_000

PICK_PROMPT = """You help a university student choose which course files to study from.
You see only the folder and file titles of their Brightspace course, as an indented outline
with ids. Choose the files that best cover what the student is studying.
- Open folders as deep as you need: pick a whole folder (every file in it, at any depth) by
  its folder id, or single files by their file id.
- Make sure every section the student listed gets some files when the outline has any.
- Skip syllabi, schedules, policies, rubrics, and anything about grades or logistics.
- Never choose more files than the limit.
Reply with JSON only, no prose: {"folders":[folder ids],"files":[file ids]}"""


@dataclass
class Pick:
    files: list[FileRef]
    how: str  # "all" | "ai" | "titles"
    folders: list[str] = field(default_factory=list)  # folder labels the AI opened whole


def outline(files: list[FileRef]) -> str:
    """Indented folder/file outline with ids, titles only."""
    lines: list[str] = []
    shown: set[tuple[int, ...]] = set()
    for f in files[:OUTLINE_MAX_FILES]:
        for depth in range(len(f.path)):
            key = f.path[: depth + 1]
            if key not in shown:
                shown.add(key)
                lines.append(f"{'  ' * depth}[folder {f.path[depth]}] {f.path_titles[depth]}")
        lines.append(f"{'  ' * len(f.path)}[file {f.topic_id}] {f.title}")
    text = "\n".join(lines)
    if len(files) > OUTLINE_MAX_FILES or len(text) > OUTLINE_MAX_CHARS:
        text = text[:OUTLINE_MAX_CHARS] + "\n(outline cut short)"
    return text


def parse_pick(text: str) -> dict[str, list[int]]:
    """Pull {"folders": [...], "files": [...]} out of a model reply."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    data, _ = json.JSONDecoder().raw_decode(candidate[start:])
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")

    def ids(key: str) -> list[int]:
        out = []
        for v in data.get(key) or []:
            v = str(v).strip().lower().removeprefix("folder").removeprefix("file").strip()
            if v.isdigit():
                out.append(int(v))
        return out

    return {"folders": ids("folders"), "files": ids("files")}


def resolve(scope: list[FileRef], chosen: dict[str, list[int]], limit: int) -> tuple[list[FileRef], list[str]]:
    """Turn chosen folder and file ids into files from `scope`, in the order chosen, up to `limit`.
    Ids that aren't in scope are ignored."""
    by_id = {f.topic_id: f for f in scope}
    out: list[FileRef] = []
    seen: set[int] = set()
    folders: list[str] = []

    def add(f: FileRef) -> None:
        if f.topic_id not in seen and len(out) < limit:
            seen.add(f.topic_id)
            out.append(f)

    for fid in chosen.get("folders", []):
        inside = [f for f in scope if fid in f.path]
        if inside:
            i = inside[0].path.index(fid)
            folders.append(" › ".join(inside[0].path_titles[: i + 1]))
        for f in inside:
            add(f)
    for tid in chosen.get("files", []):
        if tid in by_id:
            add(by_id[tid])
    return out, folders


def by_titles(scope: list[FileRef], sections: list[str], limit: int) -> list[FileRef]:
    """No AI: match file and folder titles to each topic, taking turns so every topic gets files."""
    ranked = []
    for topic in sections:
        scored = sorted(((relevance(f.label, topic), n, f) for n, f in enumerate(scope)), key=lambda x: (-x[0], x[1]))
        ranked.append([f for score, _, f in scored if score > 0])
    out: list[FileRef] = []
    seen: set[int] = set()
    while len(out) < limit and any(ranked):
        for queue in ranked:
            while queue and queue[0].topic_id in seen:
                queue.pop(0)
            if queue and len(out) < limit:
                f = queue.pop(0)
                seen.add(f.topic_id)
                out.append(f)
    return out


def spread(scope: list[FileRef], limit: int) -> list[FileRef]:
    """No AI and no topics: take turns across folders so the whole selection is covered."""
    groups: dict[tuple[int, ...], list[FileRef]] = {}
    for f in scope:
        groups.setdefault(f.path, []).append(f)
    queues = list(groups.values())
    out: list[FileRef] = []
    while len(out) < limit and any(queues):
        for q in queues:
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out


async def pick_files(generator: Any, course: str, scope: list[FileRef], sections: list[str],
                     topics: list[str], limit: int) -> Pick:
    """Choose up to `limit` files from `scope`.

    `sections` describes what the deck covers (folder labels or topics), and `topics`
    are typed topics that should narrow the choice even when everything would fit.
    """
    if not scope:
        return Pick([], "all")
    if len(scope) <= limit and not topics:
        return Pick(scope, "all")
    choose = getattr(generator, "choose_files", None)
    if choose is not None:
        try:
            reply = await choose(course, outline(scope), sections or ["the whole course"], limit)
            files, folders = resolve(scope, parse_pick(reply), limit)
            if files:
                return Pick(files, "ai", folders)
        except Exception as exc:  # fall back to titles; never fail the build over the picker
            log.warning("AI file picking failed (%s); matching titles instead", type(exc).__name__)
    if topics:
        files = by_titles(scope, topics, limit)
        if files:
            return Pick(files, "titles")
    return Pick(spread(scope, limit), "titles" if topics else "all")
