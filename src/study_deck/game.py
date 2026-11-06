"""Game sessions, the sharing rule, stratified deck building and answer checking.

Everything lives in server memory and is purged when the host ends the
session or it sits idle past the time limit. Nothing is written to disk.

Sharing rule (Purdue data classification, I.A.1, FERPA): Brightspace material
from a course is used only when EVERY player in the session has verified,
through their own Brightspace sign-in, that they are enrolled in that course.
Uploaded material is shared on the uploader's own confirmation.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .extract import chunk_text, relevance
from .genai import BLANK, GenerationError, Generator

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MATERIAL_CHARS_PER_CALL = 6000
BATCH_SIZE = 8
TOPIC_CHUNKS = 12


@dataclass
class Stratum:
    id: str
    course_label: str
    org_id: int | None  # None for a course typed in by hand (uploads only)
    kind: str  # "course" | "module" | "topic"
    module_ids: list[int] = field(default_factory=list)
    module_labels: list[str] = field(default_factory=list)
    topic: str | None = None

    @property
    def label(self) -> str:
        short = self.course_label.split("·")[0].strip()
        if self.kind == "module":
            return f"{short} · {', '.join(self.module_labels)}"
        if self.kind == "topic":
            return f"{short} · {self.topic}"
        return f"{short} · whole course"


@dataclass
class Chunk:
    text: str
    source: str  # "brightspace" | "upload" | "notes"
    contributor: str
    org_id: int | None = None
    module_id: int | None = None
    stratum_id: str | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass
class Question:
    id: str
    stratum_id: str
    label: str
    type: str
    prompt: str
    explanation: str
    options: list[str] = field(default_factory=list)
    answer_index: int | None = None
    answer: str | None = None
    accept: list[str] = field(default_factory=list)

    @property
    def expected(self) -> str:
        if self.type == "mcq" and self.answer_index is not None:
            return self.options[self.answer_index]
        return self.answer or ""

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "label": self.label, "type": self.type, "prompt": self.prompt}
        if self.type == "mcq":
            out["options"] = self.options
        return out


@dataclass
class Player:
    id: str
    name: str
    is_host: bool = False
    verified_org_ids: frozenset[int] = frozenset()
    share_brightspace: bool = True
    score: int = 0
    answered: int = 0
    uploads: int = 0


@dataclass
class GameSession:
    code: str
    host_id: str
    strata: list[Stratum]
    types: list[str]
    deck_size: int
    generator: Generator
    players: dict[str, Player] = field(default_factory=dict)
    chunks: list[Chunk] = field(default_factory=list)
    deck: list[Question] = field(default_factory=list)
    cursor: int = 0
    current: Question | None = None
    card_number: int = 0
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: str = "lobby"  # lobby | building | ready | ended
    progress: str = ""
    notices: list[str] = field(default_factory=list)
    refilling: bool = False
    used_prompts: list[str] = field(default_factory=list)
    stratum_cursor: dict[str, int] = field(default_factory=dict)
    brightspace_loaded: set[int] = field(default_factory=set)
    rng: random.Random = field(default_factory=random.Random)
    created: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    tasks: set[asyncio.Task] = field(default_factory=set)

    def touch(self) -> None:
        self.last_active = time.time()

    # ------------------------------------------------------------- sharing rule

    def course_orgs(self) -> dict[int, str]:
        return {s.org_id: s.course_label for s in self.strata if s.org_id is not None}

    def waiting_on(self, org_id: int) -> list[str]:
        """Players who have not verified enrollment in this course."""
        return [p.name for p in self.players.values() if org_id not in p.verified_org_ids]

    def shareable_orgs(self) -> set[int]:
        return {org for org in self.course_orgs() if not self.waiting_on(org)}

    def held_back(self) -> list[dict[str, Any]]:
        return [
            {"course": label, "waiting_on": self.waiting_on(org)}
            for org, label in self.course_orgs().items()
            if self.waiting_on(org)
        ]

    def deck_has_brightspace(self) -> set[int]:
        return {c.org_id for c in self.chunks if c.source == "brightspace" and c.org_id is not None}

    # ----------------------------------------------------------------- material

    def add_chunks(self, chunks: list[Chunk]) -> int:
        known = {c.digest for c in self.chunks}
        added = 0
        for chunk in chunks:
            if chunk.digest not in known:
                self.chunks.append(chunk)
                known.add(chunk.digest)
                added += 1
        return added

    def material_for(self, stratum: Stratum) -> list[Chunk]:
        shareable = self.shareable_orgs()
        brightspace = [
            c for c in self.chunks
            if c.source == "brightspace" and c.org_id == stratum.org_id and c.org_id in shareable
        ]
        if stratum.kind == "module":
            brightspace = [c for c in brightspace if c.module_id in stratum.module_ids]
        uploads = [c for c in self.chunks if c.source != "brightspace" and c.stratum_id == stratum.id]
        pool = brightspace + uploads
        if stratum.kind == "topic" and stratum.topic:
            scored = sorted(((relevance(c.text, stratum.topic), i, c) for i, c in enumerate(pool)), reverse=True)
            pool = [c for score, _, c in scored if score > 0][:TOPIC_CHUNKS] or uploads
        return pool

    def source_counts(self, stratum: Stratum) -> dict[str, int]:
        counts = {"brightspace": 0, "upload": 0, "notes": 0}
        for c in self.material_for(stratum):
            counts[c.source] += 1
        return counts

    def next_window(self, stratum: Stratum) -> str:
        """The next slice of a stratum's material, so top-ups draw on new passages."""
        pool = self.material_for(stratum)
        if not pool:
            return ""
        start = self.stratum_cursor.get(stratum.id, 0) % len(pool)
        parts, size, i = [], 0, start
        while size < MATERIAL_CHARS_PER_CALL:
            text = pool[i % len(pool)].text
            parts.append(text)
            size += len(text)
            i += 1
            if i - start >= len(pool):
                break
        self.stratum_cursor[stratum.id] = i % len(pool)
        return "\n\n---\n\n".join(parts)[: MATERIAL_CHARS_PER_CALL + 1500]

    # --------------------------------------------------------------- generation

    def remaining(self) -> int:
        return len(self.deck) - self.cursor

    async def generate(self, total: int) -> int:
        """Generate `total` cards split evenly across strata that have material, interleaved."""
        active = [s for s in self.strata if self.material_for(s)]
        if not active:
            raise GenerationError("No study material yet. Add notes or files, or connect Brightspace.")
        share, extra = divmod(total, len(active))
        per_stratum: dict[str, list[Question]] = {}
        for n, stratum in enumerate(active):
            want = share + (1 if n < extra else 0)
            got: list[Question] = []
            attempts = 0
            while len(got) < want and attempts < 4:
                attempts += 1
                self.progress = f"Writing cards for {stratum.label}…"
                material = self.next_window(stratum)
                batch = await self.generator.generate(
                    material, stratum.label, stratum.topic, self.types,
                    min(BATCH_SIZE, want - len(got)), self.used_prompts,
                )
                for q in batch:
                    q = shuffle_options(q, self.rng)
                    question = Question(
                        id=secrets.token_hex(6), stratum_id=stratum.id, label=stratum.label, type=q["type"],
                        prompt=q["prompt"], explanation=q.get("explanation", ""), options=q.get("options", []),
                        answer_index=q.get("answer_index"), answer=q.get("answer"), accept=q.get("accept", []),
                    )
                    got.append(question)
                    self.used_prompts.append(q["prompt"])
            per_stratum[stratum.id] = got[:want]
        # Stratify: deal one card from each section in turn.
        added = 0
        queues = [per_stratum[s.id] for s in active]
        while any(queues):
            for queue in queues:
                if queue:
                    self.deck.append(queue.pop(0))
                    added += 1
        return added

    def refill_threshold(self) -> int:
        return max(2, self.deck_size // 4)

    def wants_refill(self) -> bool:
        return self.status == "ready" and not self.refilling and self.remaining() <= self.refill_threshold()

    async def refill(self) -> None:
        if self.refilling:
            return
        self.refilling = True
        try:
            added = await self.generate(max(len(self.strata) * 2, self.deck_size // 2))
            if not added:
                self.notices.append("Couldn't write new cards from the remaining material.")
        except GenerationError as exc:
            self.notices.append(f"Top-up failed: {exc}")
        finally:
            self.refilling = False
            self.progress = ""

    # ----------------------------------------------------------------- gameplay

    def draw(self) -> Question | None:
        if self.cursor >= len(self.deck):
            return None
        self.current = self.deck[self.cursor]
        self.cursor += 1
        self.card_number += 1
        self.answers = {}
        return self.current

    def submit(self, player_id: str, question_id: str, given: Any) -> dict[str, Any]:
        q = self.current
        if q is None or q.id != question_id:
            raise ValueError("That card is no longer in play.")
        if player_id in self.answers:
            raise ValueError("You already answered this card.")
        correct = check_answer(q, given)
        player = self.players[player_id]
        player.answered += 1
        player.score += int(correct)
        shown = q.options[int(given)] if q.type == "mcq" and str(given).isdigit() and int(given) < len(q.options) else str(given)
        self.answers[player_id] = {"correct": correct, "given": shown[:80]}
        return {"correct": correct, "expected": q.expected, "explanation": q.explanation}


# -------------------------------------------------------------------- answers

_ARTICLES = re.compile(r"^(a|an|the)\s+")


def normalize_answer(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^\w\s.\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return _ARTICLES.sub("", text)


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def check_answer(q: Question, given: Any) -> bool:
    if q.type == "mcq":
        try:
            return int(given) == q.answer_index
        except (TypeError, ValueError):
            return False
    guess = normalize_answer(given)
    if not guess:
        return False
    for target in [q.answer or "", *q.accept]:
        want = normalize_answer(target)
        if not want:
            continue
        if guess == want:
            return True
        a, b = _number(guess), _number(want)
        if a is not None and b is not None:
            if abs(a - b) <= 1e-9 * max(1.0, abs(b)):
                return True
            continue
        if len(want) >= 5 and difflib.SequenceMatcher(None, guess, want).ratio() >= 0.85:
            return True
    return False


# ---------------------------------------------------------------------- store


class SessionStore:
    def __init__(self, ttl_seconds: int, max_sessions: int) -> None:
        self.ttl = ttl_seconds
        self.max_sessions = max_sessions
        self.sessions: dict[str, GameSession] = {}

    def new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if code not in self.sessions:
                return code

    def add(self, session: GameSession) -> None:
        self.purge()
        if len(self.sessions) >= self.max_sessions:
            raise RuntimeError("Too many active games right now. Try again later.")
        self.sessions[session.code] = session

    def get(self, code: str) -> GameSession | None:
        self.purge()
        session = self.sessions.get(code.upper().strip())
        if session:
            session.touch()
        return session

    def end(self, code: str) -> None:
        session = self.sessions.pop(code, None)
        if session:
            for task in session.tasks:
                task.cancel()
            session.chunks.clear()
            session.deck.clear()
            session.status = "ended"

    def purge(self, now: float | None = None) -> None:
        now = now or time.time()
        for code in [c for c, s in self.sessions.items() if now - s.last_active > self.ttl]:
            self.end(code)


def spawn(session: GameSession, coro_factory: Callable[[], Any]) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro_factory())
    session.tasks.add(task)
    task.add_done_callback(session.tasks.discard)
    return task


def make_chunks(text: str, **kw: Any) -> list[Chunk]:
    return [Chunk(text=part, **kw) for part in chunk_text(text)]


def shuffle_options(raw: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    if raw.get("type") != "mcq":
        return raw
    correct = raw["options"][raw["answer_index"]]
    options = raw["options"][:]
    rng.shuffle(options)
    return {**raw, "options": options, "answer_index": options.index(correct)}


__all__ = ["BLANK", "GameSession", "SessionStore", "Stratum", "Chunk", "Player", "Question", "check_answer"]
