"""Question generation through Purdue GenAI Studio, plus an offline generator for demo mode.

Only course material goes to the model: no player names, no grades, no
enrollment data. GenAI Studio is authorized for Sensitive data (excluding
FERPA), which covers course materials.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from typing import Any, Protocol

import httpx

from .config import check_genai_url
from .extract import STOPWORDS

BLANK = "_____"
QUESTION_TYPES = ("mcq", "fill_blank")

SYSTEM_PROMPT = """You write short study questions for a university student's review game.
Rules:
- Use ONLY the course material provided. Never invent facts that are not in it.
- Ask about concepts, definitions, processes and relationships. Never ask about people's names,
  contact details, dates of class meetings, grades or course logistics.
- Keep each question under 220 characters and each answer option under 80 characters.
- Multiple choice ("mcq"): exactly 4 options, exactly one correct, plausible distractors, no
  "all of the above" or "none of the above".
- Fill in the blank ("fill_blank"): one sentence containing "_____" exactly once; the missing
  answer is 1 to 4 words and must not appear elsewhere in the sentence. List acceptable
  alternative spellings or synonyms in "accept".
- Add a one-sentence "explanation" grounded in the material.
- Do not repeat or closely paraphrase any question listed under "Avoid".
Reply with JSON only, no prose, in this shape:
{"questions":[
 {"type":"mcq","prompt":"...","options":["...","...","...","..."],"answer_index":0,"explanation":"..."},
 {"type":"fill_blank","prompt":"... _____ ...","answer":"...","accept":["..."],"explanation":"..."}
]}"""


class Generator(Protocol):
    name: str

    async def generate(
        self, material: str, label: str, focus: str | None, types: list[str], count: int, avoid: list[str]
    ) -> list[dict[str, Any]]: ...


class GenerationError(Exception):
    pass


def _type_plan(types: list[str], count: int) -> str:
    if len(types) == 1:
        return f"Write {count} questions, all of type {types[0]!r}."
    half = count // 2
    return f"Write {count} questions: about {count - half} of type 'mcq' and {half} of type 'fill_blank'."


class GenAIStudio:
    name = "Purdue GenAI Studio"

    def __init__(self, url: str, model: str, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self.url = check_genai_url(url)
        self.model = model
        self.api_key = api_key
        self.http = http or httpx.AsyncClient(timeout=120.0)

    async def generate(self, material, label, focus, types, count, avoid):
        focus_line = f"Focus on this topic: {focus}\n" if focus else ""
        avoid_text = "\n".join(f"- {a}" for a in avoid[-40:]) or "(none)"
        user = (
            f"Section: {label}\n{focus_line}{_type_plan(types, count)}\n\nAvoid:\n{avoid_text}\n\n"
            f"Course material:\n<<<\n{material}\n>>>"
        )
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            "temperature": 0.4,
            "stream": False,
        }
        try:
            resp = await self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise GenerationError("Could not reach Purdue GenAI Studio.") from exc
        if resp.status_code in (401, 403):
            raise GenerationError("Purdue GenAI Studio rejected the API key.")
        if resp.status_code >= 400:
            raise GenerationError(f"Purdue GenAI Studio returned HTTP {resp.status_code}.")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise GenerationError("Purdue GenAI Studio returned an unexpected response.") from exc
        return validate_questions(parse_json_block(content), set(types), avoid)

    async def close(self) -> None:
        await self.http.aclose()


def models_url(chat_url: str) -> str:
    """GenAI Studio lists models at /api/models next to /api/chat/completions."""
    base = check_genai_url(chat_url)
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")] + "/models"
    return base.rstrip("/") + "/models"


async def list_models(chat_url: str, api_key: str, http: httpx.AsyncClient | None = None) -> list[str]:
    """Check a GenAI Studio key and return the model IDs it can use."""
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get(models_url(chat_url), headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise GenerationError(
            "Couldn't reach Purdue GenAI Studio. If you're off campus, try Purdue's VPN."
        ) from exc
    finally:
        if http is None:
            await client.aclose()
    if resp.status_code in (401, 403):
        raise GenerationError("Purdue GenAI Studio didn't accept that API key.")
    if resp.status_code >= 400:
        raise GenerationError(f"Purdue GenAI Studio returned HTTP {resp.status_code} when listing models.")
    try:
        data = resp.json()
    except ValueError as exc:
        raise GenerationError("Purdue GenAI Studio returned an unexpected response.") from exc
    items = data.get("data", []) if isinstance(data, dict) else data
    ids = sorted({str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")})
    if not ids:
        raise GenerationError("That key works, but no models are available to it.")
    return ids


def parse_json_block(text: str) -> list[Any]:
    """Pull the question list out of a model reply, tolerating code fences and chatter."""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = min([i for i in (candidate.find("{"), candidate.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise GenerationError("The model reply contained no JSON.")
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(candidate[start:])
    except json.JSONDecodeError as exc:
        raise GenerationError("The model reply was not valid JSON.") from exc
    if isinstance(data, dict):
        data = data.get("questions", [])
    if not isinstance(data, list):
        raise GenerationError("The model reply had no question list.")
    return data


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _short(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def validate_questions(raw: list[Any], types: set[str], avoid: list[str] | None = None) -> list[dict[str, Any]]:
    """Keep only well-formed questions of the requested types, with no duplicates."""
    seen = {_norm(a) for a in (avoid or [])}
    good: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        qtype = str(item.get("type", "")).strip().lower().replace("-", "_").replace(" ", "_")
        if qtype in {"multiple_choice", "mc"}:
            qtype = "mcq"
        if qtype in {"fill_in_the_blank", "fill_in_blank", "blank", "cloze"}:
            qtype = "fill_blank"
        if qtype not in types:
            continue
        prompt = _short(item.get("prompt") or item.get("question"), 240)
        explanation = _short(item.get("explanation"), 300)
        if len(prompt) < 10 or _norm(prompt) in seen:
            continue
        if qtype == "mcq":
            options = [_short(o, 100) for o in (item.get("options") or []) if _short(o, 100)]
            try:
                index = int(item.get("answer_index"))
            except (TypeError, ValueError):
                continue
            if not 3 <= len(options) <= 5 or len({_norm(o) for o in options}) != len(options):
                continue
            if not 0 <= index < len(options):
                continue
            q = {"type": "mcq", "prompt": prompt, "options": options, "answer_index": index}
        else:
            prompt = re.sub(r"_{3,}|\[blank\]|\(blank\)", BLANK, prompt, flags=re.IGNORECASE)
            answer = _short(item.get("answer"), 60)
            if prompt.count(BLANK) != 1 or not answer or len(answer.split()) > 6:
                continue
            if _norm(answer) and _norm(answer) in _norm(prompt.replace(BLANK, " ")):
                continue
            accept = [_short(a, 60) for a in (item.get("accept") or []) if _short(a, 60)]
            q = {"type": "fill_blank", "prompt": prompt, "answer": answer, "accept": accept[:6]}
        q["explanation"] = explanation
        seen.add(_norm(prompt))
        good.append(q)
    return good


class OfflineGenerator:
    """Demo-mode generator: builds simple cloze and multiple-choice cards from key terms.

    No AI and no network. It exists so the game can be tried end to end on the
    sample courses before Purdue GenAI Studio access is set up.
    """

    name = "Offline demo generator"

    def __init__(self, seed: int | None = None) -> None:
        self.random = random.Random(seed)

    async def generate(self, material, label, focus, types, count, avoid):
        sentences = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+", material.replace("\n", " "))
            if 40 <= len(s.strip()) <= 220
        ]
        words = re.findall(r"[A-Za-z][A-Za-z\-]{4,}", material)
        freq = Counter(w.lower() for w in words if w.lower() not in STOPWORDS)
        keyterms = [w for w, _ in freq.most_common(60)]
        if focus:
            focus_terms = set(re.findall(r"[a-z]{4,}", focus.lower()))
            sentences.sort(key=lambda s: -sum(t in s.lower() for t in focus_terms))
        else:
            self.random.shuffle(sentences)
        raw = []
        for i, sentence in enumerate(sentences):
            if len(raw) >= count * 2:
                break
            candidates = [t for t in keyterms if re.search(rf"\b{re.escape(t)}\b", sentence, re.IGNORECASE)]
            if not candidates:
                continue
            term = max(candidates, key=len)
            match = re.search(rf"\b{re.escape(term)}\b", sentence, re.IGNORECASE)
            assert match
            answer = match.group(0)
            prompt = sentence[: match.start()] + BLANK + sentence[match.end():]
            qtype = types[i % len(types)]
            if qtype == "mcq":
                # Prefer distractors of similar length that aren't already in the sentence.
                distractors = [t for t in keyterms if t != term.lower() and t not in sentence.lower()]
                self.random.shuffle(distractors)
                distractors.sort(key=lambda t: abs(len(t) - len(term)) // 3)
                options = [answer] + [d for d in distractors[:3]]
                if len(options) < 4:
                    continue
                self.random.shuffle(options)
                raw.append({"type": "mcq", "prompt": f"Which term completes this? {prompt}", "options": options,
                            "answer_index": options.index(answer), "explanation": sentence})
            else:
                raw.append({"type": "fill_blank", "prompt": prompt, "answer": answer, "accept": [],
                            "explanation": sentence})
        return validate_questions(raw, set(types), avoid)[:count]

    async def close(self) -> None:
        pass
