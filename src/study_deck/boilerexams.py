"""Past exam questions from Boilerexams (boilerexams.com).

Boilerexams publishes Purdue past exams that professors have released, with their
permission. When a game's course is on Boilerexams, Study Deck uses that course's
text-only multiple-choice questions in two ways:

- as cards: some of each matching section's cards are real past exam questions,
  taken only from the Boilerexams topics that fit the section, labeled with the
  exam they came from;
- as a style guide: a few are shown to the AI as examples of how the course's
  exam questions are written, so the cards it writes from the game's own
  material read the same way.

Only public, read-only GETs are made, with nothing about the players sent, a short
pause between question reads, and everything cached in memory for a few hours.
Questions that need a picture (a figure, graph, structure drawing, or options that
are just letters) are left out, since cards are text; so are questions whose math
can't be written out readably.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .extract import relevance

API = "https://api.boilerexams.com/v3"
CACHE_SECONDS = 6 * 3600
QUESTIONS_TO_READ = 14  # style examples: at most twice this many are read per section
EXAMPLES_PER_SECTION = 6

_COURSE_CODE = re.compile(r"\b([A-Z]{2,5})\s?(\d{3,5})\b")
# A question that depends on something drawn: "shown below", "the following compound", "the graph".
_DRAWN = (r"compound|structure|molecule|figure|graph|diagram|table|circuit|image|plot|picture|"
          r"mechanism|spectrum|chain|tree|heap|network|curve|region|map|grid|beam|truss|apparatus")
_NEEDS_PICTURE = re.compile(
    rf"\b(shown|pictured|depicted|illustrated|indicated|drawn|labeled|labelled|figure|diagram|graph(?:ed)?|"
    rf"plot(?:ted)?|picture|image|below|above|(?:following|this|these|the given) (?:{_DRAWN})s?)\b",
    re.IGNORECASE,
)


def course_codes(label: str) -> list[str]:
    """Purdue course codes in a course name: "Fall 2026 MA 16100-LEC" -> ["MA16100"].
    Old three-digit numbers are read as the five-digit ones (MA 161 -> MA16100)."""
    out = []
    for subject, number in _COURSE_CODE.findall(label.upper()):
        if len(number) == 3:
            number += "00"
        if len(number) == 5:
            out.append(subject + number)
    return out


def spaced(code: str) -> str:
    """MA16100 -> MA 16100."""
    return re.sub(r"^([A-Z]+)", r"\1 ", code)


def _plain(text: str) -> str:
    """Markdown and LaTeX to plain text, for the AI prompt (the AI can read leftover LaTeX)."""
    text = re.sub(r"\\(?:mathrm|text|texttt|textit|mathbf|textbf|operatorname)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(left|right)\s*", "", text)
    text = re.sub(r"\\(?:quad|qquad|[,;!]|\[|\])", " ", text).replace("~", " ")
    text = text.replace("$", "").replace("**", "")
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------ math for cards

_SYMBOLS = {
    "pi": "π", "infty": "∞", "times": "×", "cdot": "·", "div": "÷", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "neq": "≠", "ne": "≠", "pm": "±", "mp": "∓", "to": "→", "rightarrow": "→",
    "longrightarrow": "→", "Rightarrow": "⇒", "leftarrow": "←", "leftrightarrow": "↔", "approx": "≈",
    "sim": "~", "equiv": "≡", "propto": "∝", "circ": "°", "degree": "°", "prime": "′", "alpha": "α",
    "beta": "β", "gamma": "γ", "Gamma": "Γ", "delta": "δ", "Delta": "Δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "Theta": "Θ", "kappa": "κ", "lambda": "λ",
    "Lambda": "Λ", "mu": "μ", "nu": "ν", "xi": "ξ", "rho": "ρ", "sigma": "σ", "Sigma": "Σ", "tau": "τ",
    "phi": "φ", "varphi": "φ", "Phi": "Φ", "chi": "χ", "psi": "ψ", "Psi": "Ψ", "omega": "ω",
    "Omega": "Ω", "int": "∫", "iint": "∬", "oint": "∮", "sum": "Σ", "prod": "Π", "partial": "∂",
    "nabla": "∇", "in": "∈", "notin": "∉", "cup": "∪", "cap": "∩", "subset": "⊂", "subseteq": "⊆",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃", "neg": "¬", "land": "∧",
    "wedge": "∧", "lor": "∨", "vee": "∨", "oplus": "⊕", "ldots": "…", "dots": "…", "cdots": "⋯",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "hbar": "ħ", "ell": "ℓ", "star": "*", "ast": "*",
    "mid": "|", "vert": "|", "lvert": "|", "rvert": "|", "Vert": "‖", "backslash": "∖", "%": "%",
    "quad": " ", "qquad": " ", "displaystyle": "", "limits": "", "left": "", "right": "", "big": "",
    "Big": "", "bigg": "", "Bigg": "", "nonumber": "",
}
_FUNCS = {"sin", "cos", "tan", "sec", "csc", "cot", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
          "ln", "log", "exp", "lim", "max", "min", "det", "gcd", "deg", "dim", "ker", "sup", "inf", "arg"}
_SUP = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUB = str.maketrans("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
_SUP_OK = set("0123456789+-=()ni")
_SUB_OK = set("0123456789+-=()aehijklmnoprstuvx")
_TEXTLIKE = re.compile(r"\\(?:mathrm|text|textrm|texttt|textit|textbf|mathbf|mathit|mathsf|boldsymbol|"
                       r"operatorname|mbox|hbox|emph)\s*\{([^{}]*)\}")
_FRAC = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT = re.compile(r"\\sqrt\s*(?:\[([^\]]*)\])?\s*\{([^{}]*)\}")
_GROUP = re.compile(r"([\^_])\s*\{([^{}]*)\}")


_STRUCTURAL = {"frac", "dfrac", "tfrac", "sqrt", "mathrm", "text", "textrm", "texttt", "textit", "textbf",
               "mathbf", "mathit", "mathsf", "boldsymbol", "operatorname", "mbox", "hbox", "emph"}
# Stand-ins for characters LaTeX escapes, put back at the end.
_KEEP = {"\\$": "\x01", "\\{": "\x02", "\\}": "\x03", "\\_": "\x04", "\\&": "\x05", "\\#": "\x06"}
_KEPT = {"\x01": "$", "\x02": "{", "\x03": "}", "\x04": "_", "\x05": "&", "\x06": "#"}


def _wrap(part: str) -> str:
    """A fraction's top or bottom, in brackets unless it's a single term."""
    part = part.strip()
    return part if re.fullmatch(r"[\w.′π∞]+|\(.*\)|√\(.*\)|√[\w.]+", part) else f"({part})"


def _script(mark: str, part: str) -> str:
    """x^{2} -> x², x_{2} -> x₂, f^{′} -> f′; otherwise x^(n+1)."""
    part = part.strip()
    if not part:
        return ""
    compact = part.replace(" ", "")
    if mark == "^" and set(compact) <= _SUP_OK | {"′"}:
        return compact.translate(_SUP)
    if mark == "_" and set(part) <= _SUB_OK:
        return part.translate(_SUB)
    return f"{mark}({part})" if len(part) > 1 else f"{mark}{part}"


def readable(text: str) -> str | None:
    """Write markdown and LaTeX math as plain text with symbols, for a card:
    "\\frac{1}{e}" -> "1/e", "x^{2}" -> "x²", "\\sqrt{2}" -> "√2", "Zn(OH)_{2}" -> "Zn(OH)₂".
    None when something can't be written out readably (tables, arrays, unknown commands)."""
    if re.search(r"\\begin\{|<[a-z]+[ >]|!\[", text):
        return None
    t = text.replace("**", "")
    for escaped, keep in _KEEP.items():
        t = t.replace(escaped, keep)
    t = t.replace("$", "").replace("\\\\", " ")
    t = re.sub(r"\\[\[\]()]", " ", t)
    t = re.sub(r"\\[,;:! ]", " ", t).replace("~", " ")
    # lim_{x\to 0} -> lim(x→0), before the arrows inside it are converted.
    t = re.sub(r"\\lim\s*_\s*\{([^{}]*)\}", "lim\x0e\\1\x0f ", t)

    unknown: list[str] = []

    def command(m: re.Match) -> str:
        name = m.group(1)
        if name in _STRUCTURAL:
            return m.group(0)  # handled below
        nxt = m.string[m.end():m.end() + 1]
        prev = m.string[m.start() - 1:m.start()] if m.start() else ""
        if name in _FUNCS:
            return (" " if prev.isalnum() else "") + name + (" " if nxt.isalnum() or nxt == "\\" else "")
        if name in _SYMBOLS:
            return _SYMBOLS[name]
        unknown.append(name)
        return m.group(0)

    t = re.sub(r"\\([A-Za-z]+|%)", command, t)
    if unknown:
        return None
    t = t.replace("^′", "′")
    for _ in range(10):  # innermost first, until nothing changes
        before = t
        t = _TEXTLIKE.sub(r"\1", t)
        t = _GROUP.sub(lambda m: _script(m.group(1), m.group(2)), t)
        t = _FRAC.sub(lambda m: f"{_wrap(m.group(1))}/{_wrap(m.group(2))}", t)
        t = _SQRT.sub(lambda m: (f"{m.group(1)}√" if m.group(1) else "√") + _wrap(m.group(2)), t)
        if t == before:
            break
    t = re.sub(r"\^([0-9n])", lambda m: m.group(1).translate(_SUP), t)
    t = re.sub(r"(?<=[A-Za-z)])_([0-9])", lambda m: m.group(1).translate(_SUB), t)
    t = t.replace("\x0e", "(").replace("\x0f", ")").replace("{", "").replace("}", "")
    if "\\" in t:
        return None
    for keep, char in _KEPT.items():
        t = t.replace(keep, char)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.;:?!)])", r"\1", t)
    t = re.sub(r"([(])\s+", r"\1", t)
    return t or None


# ------------------------------------------------------------------ questions


@dataclass
class Example:
    prompt: str  # for the AI prompt
    options: list[str]
    answer: int
    topic: str
    qid: str = ""
    exam_id: str = ""
    card_prompt: str | None = None  # readable versions, when the question can be a card
    card_options: list[str] | None = None
    keep_order: bool = False  # options refer to each other ("both A and B"), so don't shuffle


def _example(q: dict[str, Any], topic: str) -> Example | None:
    data = q.get("data") or {}
    body = str(data.get("body") or "")
    choices = sorted(data.get("answerChoices") or [], key=lambda c: c.get("index", 0))
    options = [str(c.get("body") or "").strip() for c in choices]
    solution = data.get("solution") or []
    if q.get("type") != "MULTIPLE_CHOICE" or q.get("children") or len(options) < 3 or len(solution) != 1:
        return None
    if not body.strip() or "![" in body or "<img" in body or _NEEDS_PICTURE.search(body):
        return None
    # Options that are only "A", "B"... point at a picture.
    if any(len(o) <= 1 or "![" in o or "<img" in o for o in options):
        return None
    if not isinstance(solution[0], int) or not 0 <= solution[0] < len(options):
        return None
    prompt = _plain(body)
    plain = [_plain(o) for o in options]
    # Options drawn as arrays or tables come out empty or unreadable as text.
    if len(prompt) > 600 or any(len(o) < 1 or "\\begin{" in o or re.search(r"\|\s*:?-{3}", o) for o in plain):
        return None
    card_prompt = readable(body)
    card_options = [readable(o) for o in options]
    card_ok = (card_prompt is not None and len(card_prompt) <= 400 and all(card_options)
               and all(len(o or "") <= 160 for o in card_options) and len(set(card_options)) == len(card_options))
    keep_order = (bool((q.get("flags") or {}).get("disable_random_answers"))
                  or any(re.search(r"\b(above|below|both|neither|none of|all of)\b", o, re.I) for o in plain))
    return Example(
        prompt, [o[:160] for o in plain], solution[0], topic, qid=str(q.get("id") or ""),
        exam_id=str(q.get("exam_id") or ""), card_prompt=card_prompt if card_ok else None,
        card_options=[o or "" for o in card_options] if card_ok else None, keep_order=keep_order,
    )


class Boilerexams:
    def __init__(self, http: httpx.AsyncClient | None = None, pause: float = 0.2) -> None:
        self.http = http or httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "StudyDeck"})
        self.pause = pause  # between question reads, to be gentle on their server
        self._courses: tuple[float, set[str]] | None = None
        self._topics: dict[str, tuple[float, list[tuple[str, list[str]]]]] = {}
        self._exams: dict[str, tuple[float, dict[str, str]]] = {}
        self._questions: dict[str, tuple[float, Example | None]] = {}
        self._lock = asyncio.Lock()

    async def _get(self, path: str) -> Any:
        # A transient catalog/topic failure should not make a supported course
        # disappear. Retry public reads once, then let the caller show the error.
        for attempt in range(2):
            try:
                resp = await self.http.get(f"{API}{path}")
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt:
                    raise
            except httpx.HTTPStatusError as exc:
                if attempt or (exc.response.status_code != 429 and exc.response.status_code < 500):
                    raise
            await asyncio.sleep(0.3)

    async def courses(self) -> set[str]:
        """Codes of the courses on Boilerexams, like "MA16100"."""
        if self._courses and time.time() - self._courses[0] < CACHE_SECONDS:
            return self._courses[1]
        subjects = await self._get("/courses/subjects")
        codes = {
            f"{c.get('abbreviation', '')}{c.get('number', '')}"
            for s in subjects for c in s.get("courses") or []
            if (c.get("flags") or {}).get("published", True)
        }
        self._courses = (time.time(), codes)
        return codes

    async def match(self, label: str) -> str | None:
        """The Boilerexams course for a game course name, if there is one."""
        codes = course_codes(label)
        if not codes:
            return None
        known = await self.courses()
        return next((c for c in codes if c in known), None)

    async def _topic_list(self, code: str) -> list[tuple[str, list[str]]]:
        cached = self._topics.get(code)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        overview = await self._get(f"/courses/{code}/topics/overview")
        topics = [(str(t.get("name") or ""), [q["id"] for q in t.get("questions") or [] if q.get("id")])
                  for t in overview.get("topics") or []]
        topics = [t for t in topics if t[1]]
        self._topics[code] = (time.time(), topics)
        return topics

    async def exam_name(self, code: str, exam_id: str) -> str:
        """ "Fall 2018" for an exam of this course ("" if unknown)."""
        cached = self._exams.get(code)
        if not cached or time.time() - cached[0] >= CACHE_SECONDS:
            try:
                exams = await self._get(f"/courses/{code}/exams")
            except (httpx.HTTPError, ValueError):
                exams = []
            names = {str(e.get("id")): f"{str(e.get('season') or '').title()} {e.get('year') or ''}".strip()
                     for e in exams if isinstance(e, dict)}
            cached = self._exams[code] = (time.time(), names)
        return cached[1].get(exam_id, "")

    async def _question(self, qid: str, topic: str) -> Example | None:
        cached = self._questions.get(qid)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            ex = _example(await self._get(f"/questions/{qid}"), topic)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        self._questions[qid] = (time.time(), ex)
        await asyncio.sleep(self.pause)
        return ex

    async def _candidates(self, code: str, focus: list[str], *, related_only: bool) -> list[tuple[str, str]]:
        """(topic, question id) in the order to try: first the course's topics that match
        `focus` (the section's modules or topics), then, unless related_only, one from each
        other topic in turn, so a whole-course section sees the whole course."""
        topics = list(await self._topic_list(code))
        rng = random.Random(f"{code}|{'|'.join(focus)}")
        rng.shuffle(topics)
        want = " ".join(focus)
        near = [t for t in topics if want and relevance(t[0], want) > 0]
        near.sort(key=lambda t: -relevance(t[0], want))
        picks = [(name, q) for name, qs in near for q in qs]
        if related_only and want:
            return picks
        queues = [[(name, q) for q in qs] for name, qs in topics if (name, qs) not in near]
        while any(queues):
            for queue in queues:
                if queue:
                    picks.append(queue.pop(0))
        return picks

    async def examples(self, code: str, focus: list[str], *, related_only: bool = False) -> list[Example]:
        """A few text-only questions from this course for the AI to model its questions on."""
        async with self._lock:
            found: list[Example] = []
            for name, qid in (await self._candidates(code, focus, related_only=related_only))[:QUESTIONS_TO_READ * 2]:
                ex = await self._question(qid, name)
                if ex:
                    found.append(ex)
                if len(found) >= EXAMPLES_PER_SECTION:
                    break
            return found

    async def cards(self, code: str, focus: list[str], want: int, skip: set[str]) -> list[Example]:
        """Up to `want` past exam questions that can be dealt as cards for a section about
        `focus` (any topic of the course when focus is empty), leaving out `skip`."""
        if want <= 0:
            return []
        async with self._lock:
            found: list[Example] = []
            tried = 0
            for name, qid in await self._candidates(code, focus, related_only=True):
                if qid in skip:
                    continue
                if tried >= want * 4 + 6:  # don't read a whole course looking for text-only ones
                    break
                tried += qid not in self._questions
                ex = await self._question(qid, name)
                if ex and ex.card_prompt:
                    found.append(ex)
                    if len(found) >= want:
                        break
            return found

    async def style_text(self, code: str, focus: list[str], *, related_only: bool = False) -> tuple[str, int]:
        """Examples for one deck section as prompt text, and how many there are."""
        chosen = await self.examples(code, focus, related_only=related_only)
        lines = []
        for n, ex in enumerate(chosen, 1):
            prompt = ex.card_prompt or ex.prompt
            options = ex.card_options or ex.options
            opts = "\n".join(f"   {i + 1}) {o}" for i, o in enumerate(options))
            # Keep the existing letter notation for ordinary multiple-choice exams.
            if len(options) <= 8:
                opts = "\n".join(f"   {'ABCDEFGH'[i]}) {o}" for i, o in enumerate(options))
                correct = 'ABCDEFGH'[ex.answer]
            else:
                correct = str(ex.answer + 1)
            lines.append(f"{n}. [{ex.topic}] {prompt}\n{opts}\n   Correct: {correct} — {options[ex.answer]}")
        return "\n".join(lines), len(chosen)

    async def close(self) -> None:
        await self.http.aclose()
