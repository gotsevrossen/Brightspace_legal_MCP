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
import logging
import math
from typing import Any, Awaitable, Callable

from .extract import chunk_text, relevance
from .genai import BLANK, GenerationError, Generator, tag_source

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MATERIAL_CHARS_PER_CALL = 6000
BATCH_SIZE = 8
# Write new cards in the background when this many are left in the deck...
REFILL_AT = 5
# ...and this many at a time.
REFILL_CARDS = 10
TOPIC_CHUNKS = 12
# Material added during play gets cards of its own right away, mixed into the cards
# still to come: this many per window of new material, for at most this many
# windows. Anything past that joins the section's normal top-up rotation.
NEW_MATERIAL_CARDS = 5
NEW_MATERIAL_WINDOWS = 2
# Share of a section's cards that are real past exam questions, when there are some that fit.
PAST_EXAM_SHARE = 1 / 3

log = logging.getLogger("study_deck")


@dataclass
class Stratum:
    id: str
    course_label: str
    org_id: int | None  # None for a course using uploads or public past exams
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
    module_path: tuple[int, ...] = ()  # every folder it sits in, top level first

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
    source: str = ""  # set for a real past exam question, e.g. "Past exam · Fall 2018"

    @property
    def expected(self) -> str:
        if self.type == "mcq" and self.answer_index is not None:
            return self.options[self.answer_index]
        return self.answer or ""

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "label": self.label, "type": self.type, "prompt": self.prompt}
        if self.type == "mcq":
            out["options"] = self.options
        if self.source:
            out["source"] = self.source
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
    # Public handle for this player in the page (their id is a session secret).
    key: str = field(default_factory=lambda: secrets.token_hex(5))


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
    # Players decide turns on the physical board; the host checks the card's answer.
    outcome: dict[str, Any] | None = None
    kicked: set[str] = field(default_factory=set)
    status: str = "lobby"  # lobby | building | ready | ended
    progress: str = ""
    notices: list[str] = field(default_factory=list)
    refilling: bool = False
    used_prompts: list[str] = field(default_factory=list)
    stratum_cursor: dict[str, int] = field(default_factory=dict)
    brightspace_loaded: set[int] = field(default_factory=set)
    # Which Brightspace files each course's material came from, and how they were chosen.
    picked: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Boilerexams past exam questions per section, shown to the AI as a style guide
    # (stratum id -> prompt text), and what to tell players (stratum id -> course code, count).
    use_boilerexams: bool = False
    styles: dict[str, str] = field(default_factory=dict)
    # Public exam questions and answers can supply material when a section has no
    # shareable Brightspace text or uploads. Keep them separate from private material.
    exam_material: dict[str, str] = field(default_factory=dict)
    style_info: dict[str, tuple[str, int]] = field(default_factory=dict)
    # Real past exam questions dealt as cards: a source the app plugs in
    # (section, how many, question ids to skip -> cards), which ones were used,
    # and how many each section got.
    past_exams: Callable[[Stratum, int, set[str]], Awaitable[list[dict[str, Any]]]] | None = None
    past_used: set[str] = field(default_factory=set)
    past_cards: dict[str, int] = field(default_factory=dict)
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

    def add_chunks(self, chunks: list[Chunk]) -> list[Chunk]:
        """Add material, skipping passages already in the game. Returns the ones added."""
        known = {c.digest for c in self.chunks}
        added = []
        for chunk in chunks:
            if chunk.digest not in known:
                self.chunks.append(chunk)
                known.add(chunk.digest)
                added.append(chunk)
        return added

    def material_for(self, stratum: Stratum) -> list[Chunk]:
        shareable = self.shareable_orgs()
        brightspace = [
            c for c in self.chunks
            if c.source == "brightspace" and c.org_id == stratum.org_id and c.org_id in shareable
        ]
        if stratum.kind == "module":
            brightspace = [c for c in brightspace
                           if any(m in stratum.module_ids for m in (c.module_path or (c.module_id,)))]
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
            text = self.exam_material.get(stratum.id, "")
            return tag_source("boilerexams", text) if text else ""
        start = self.stratum_cursor.get(stratum.id, 0) % len(pool)
        parts, size, i = [], 0, start
        while size < MATERIAL_CHARS_PER_CALL:
            chunk = pool[i % len(pool)]
            # Labelled so the model can favour the instructor's Brightspace wording.
            text = tag_source(chunk.source, chunk.text)
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
        """Generate `total` cards split evenly across strata that have material, then add
        them to the deck in random order."""
        active = [s for s in self.strata if self.material_for(s) or self.exam_material.get(s.id)]
        if not active:
            raise GenerationError("No study material found. Choose a supported Boilerexams course or matching "
                                  "topic with past exam questions, add notes or files, or connect Brightspace.")
        # When cards don't divide evenly, a different section gets the extra ones each time.
        turn = len(self.deck) % len(active)
        active = active[turn:] + active[:turn]
        share, extra = divmod(total, len(active))
        per_stratum: dict[str, list[Question]] = {}
        for n, stratum in enumerate(active):
            want = share + (1 if n < extra else 0)
            got: list[Question] = []
            attempts = 0
            exam_based = not self.material_for(stratum) and bool(self.exam_material.get(stratum.id))

            async def write(target: int) -> None:
                nonlocal attempts
                while len(got) < target and attempts < 4:
                    attempts += 1
                    self.progress = f"Writing cards for {stratum.label}…"
                    material = self.next_window(stratum)
                    batch = await self.generator.generate(
                        material, stratum.label, stratum.topic, self.types,
                        min(BATCH_SIZE, target - len(got)), self.used_prompts, **self._style(stratum),
                    )
                    for q in batch:
                        got.append(self._question(stratum, q, exam_based=exam_based))
                        self.used_prompts.append(q["prompt"])

            # About a third can be real past exam questions. The AI writes its share first,
            # and past exam cards are added in proportion to what it managed, so they never
            # crowd out cards from the game's own material when that runs low.
            planned = self._past_planned(want)
            if exam_based:
                # Reserve real questions first so the AI avoids copying cards that
                # will appear in this deck, then fill the rest from the exam material.
                got.extend(await self._past_exam_cards(stratum, planned))
                await write(want)
                per_stratum[stratum.id] = got[:want]
                continue
            await write(want - planned)
            if planned:
                written = len(got)
                fair = math.ceil(written * PAST_EXAM_SHARE / (1 - PAST_EXAM_SHARE))
                got.extend(await self._past_exam_cards(stratum, min(planned, fair)))
                await write(want)  # if fewer past exam questions fit than planned
            per_stratum[stratum.id] = got[:want]
        # Every section gets its fair share of each batch (stratified), and the batch is
        # shuffled so the order of sections and questions is random while studying.
        batch = [q for s in active for q in per_stratum[s.id]]
        self.rng.shuffle(batch)
        self.deck.extend(batch)
        return len(batch)

    def _past_planned(self, want: int) -> int:
        """How many of `want` cards for a section could be real past exam questions."""
        if self.past_exams is None or "mcq" not in self.types or want < 2:
            return 0
        return max(1, round(want * PAST_EXAM_SHARE))

    async def _past_exam_cards(self, stratum: Stratum, count: int) -> list[Question]:
        """Up to `count` real past exam questions that fit the section (multiple choice).
        If none can be had, the AI simply writes those cards; nothing is said about it."""
        if self.past_exams is None or count <= 0:
            return []
        try:
            found = await self.past_exams(stratum, count, set(self.past_used))
        except Exception:  # their site being down never stops a game
            log.info("past exam questions unavailable", exc_info=True)
            return []
        out = []
        for raw in found:
            if raw["qid"] in self.past_used:
                continue
            self.past_used.add(raw["qid"])
            q = raw if raw.get("keep_order") else shuffle_options(raw, self.rng)
            out.append(Question(
                id=secrets.token_hex(6), stratum_id=stratum.id, label=stratum.label, type="mcq",
                prompt=q["prompt"], explanation=q.get("explanation", ""), options=q["options"],
                answer_index=q["answer_index"], source=q.get("source", "Past exam"),
            ))
            self.used_prompts.append(q["prompt"])
        self.past_cards[stratum.id] = self.past_cards.get(stratum.id, 0) + len(out)
        return out

    def _style(self, stratum: Stratum) -> dict[str, str]:
        """Past exam examples for this section's AI requests, if it has any."""
        if not self.material_for(stratum) and self.exam_material.get(stratum.id):
            return {}  # these questions are the source material, not a style-only guide
        examples = self.styles.get(stratum.id)
        return {"examples": examples} if examples else {}

    def _question(self, stratum: Stratum, q: dict[str, Any], *, exam_based: bool = False) -> Question:
        q = shuffle_options(q, self.rng)
        return Question(
            id=secrets.token_hex(6), stratum_id=stratum.id, label=stratum.label, type=q["type"],
            prompt=q["prompt"], explanation=q.get("explanation", ""), options=q.get("options", []),
            answer_index=q.get("answer_index"), answer=q.get("answer"), accept=q.get("accept", []),
            source="AI practice · based on Boilerexams" if exam_based else "",
        )

    async def write_from_new(self, stratum_id: str, chunks: list[Chunk]) -> int:
        """Write cards from material added during play and shuffle them into the cards
        still to come. Without this, new material waits its turn in the section's
        rotation and can take several top-ups to reach the deck."""
        stratum = next((s for s in self.strata if s.id == stratum_id), None)
        if stratum is None or not chunks:
            return 0
        windows, part = [], ""
        for c in chunks:
            text = tag_source(c.source, c.text)
            if part and len(part) + len(text) > MATERIAL_CHARS_PER_CALL:
                windows.append(part)
                part = ""
            part = f"{part}\n\n---\n\n{text}" if part else text
        if part:
            windows.append(part)
        new: list[Question] = []
        for material in windows[:NEW_MATERIAL_WINDOWS]:
            batch = await self.generator.generate(
                material[: MATERIAL_CHARS_PER_CALL + 1500], stratum.label, stratum.topic, self.types,
                NEW_MATERIAL_CARDS, self.used_prompts, **self._style(stratum),
            )
            for q in batch[:NEW_MATERIAL_CARDS]:
                new.append(self._question(stratum, q))
                self.used_prompts.append(q["prompt"])
        if self.status != "ready":
            return 0
        # Anywhere among the cards not yet drawn, so they come up this round.
        for q in new:
            self.deck.insert(self.rng.randint(self.cursor, len(self.deck)), q)
        return len(new)

    def refill_threshold(self) -> int:
        # New cards are written when 5 are left (fewer for tiny decks, so they
        # don't top up before they're even played).
        return min(REFILL_AT, self.deck_size - 1)

    def wants_refill(self) -> bool:
        return self.status == "ready" and not self.refilling and self.remaining() <= self.refill_threshold()

    async def refill(self) -> None:
        if self.refilling:
            return
        self.refilling = True
        try:
            added = await self.generate(REFILL_CARDS)
            if not added:
                self.notices.append("Couldn't write new cards from the remaining material.")
        except GenerationError as exc:
            self.notices.append(f"Top-up failed: {exc}")
        finally:
            self.refilling = False
            self.progress = ""

    # ----------------------------------------------------------------- gameplay

    def player_by_key(self, key: Any) -> Player | None:
        return next((p for p in self.players.values() if p.key == key), None)

    def draw(self) -> Question | None:
        if self.cursor >= len(self.deck):
            return None
        self.current = self.deck[self.cursor]
        self.cursor += 1
        self.card_number += 1
        self.outcome = None
        return self.current

    def submit(self, question_id: str, given: Any, skipped: bool = False) -> dict[str, Any]:
        """Check the host-entered answer without assigning a player or awarding points."""
        q = self.current
        if q is None or q.id != question_id:
            raise ValueError("That card is no longer in play.")
        if self.outcome is not None:
            raise ValueError("This card has already been answered.")
        correct = False if skipped else check_answer(q, given)
        if skipped:
            shown = ""
        elif q.type == "mcq" and str(given).isdigit() and int(given) < len(q.options):
            shown = q.options[int(given)]
        else:
            shown = str(given)
        self.outcome = {"correct": correct, "given": shown[:80], "skipped": skipped}
        return {"correct": correct, "expected": q.expected, "explanation": q.explanation}

    def remove_player(self, player_id: str, *, kicked: bool = False) -> None:
        """A player leaves, or the host removes them (kicked: they can't rejoin this
        game). Their own uploads and notes leave with them."""
        if player_id == self.host_id or player_id not in self.players:
            raise ValueError("That player can't be removed.")
        del self.players[player_id]
        if kicked:
            self.kicked.add(player_id)
        self.chunks = [c for c in self.chunks if c.contributor != player_id or c.source == "brightspace"]


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
