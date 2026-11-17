"""Test doubles: a fake Brightspace with two small courses, and an offline card writer.

These used to ship in the app as "demo mode". The app no longer has sample data;
the tests plug these in instead of calling Brightspace or Purdue GenAI Studio.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field

from study_deck.brightspace import Course, FileRef, MaterialPiece, Module
from study_deck.extract import ASSESSMENT_TITLE, STOPWORDS
from study_deck.genai import BLANK, strip_sources, validate_questions


@dataclass
class _Module:
    id: int
    title: str
    topics: list[tuple[str, str]] = field(default_factory=list)
    children: list["_Module"] = field(default_factory=list)


_COURSES: dict[int, tuple[Course, list[_Module]]] = {
    900101: (
        Course(900101, "BIOL 1100 · Cell Biology", "BIOL1100"),
        [
            _Module(1, "Week 1 · Cells and organelles", [
                ("Lecture 1 slides", "The cell is the basic unit of life. Prokaryotic cells lack a nucleus, while eukaryotic cells keep their DNA inside a membrane-bound nucleus. The plasma membrane is a phospholipid bilayer that controls what enters and leaves the cell. Ribosomes are the site of protein synthesis and are found in both prokaryotic and eukaryotic cells.\n\nThe mitochondrion produces most of the cell's ATP through cellular respiration. The endoplasmic reticulum comes in two forms: rough endoplasmic reticulum is studded with ribosomes and folds proteins, while smooth endoplasmic reticulum makes lipids and detoxifies chemicals. The Golgi apparatus modifies, sorts and packages proteins for transport. Lysosomes contain digestive enzymes that break down worn-out organelles."),
                ("Quiz 1 answer key", "This assessment file is excluded automatically."),
            ]),
            _Module(2, "Week 2 · Photosynthesis", [
                ("Lecture 2 notes", "Photosynthesis converts light energy into chemical energy stored in glucose. It takes place in the chloroplast, an organelle found in plant cells and algae. The light-dependent reactions occur in the thylakoid membranes and produce ATP and NADPH while splitting water and releasing oxygen.\n\nThe Calvin cycle takes place in the stroma and uses ATP and NADPH to fix carbon dioxide into sugar. The enzyme rubisco catalyzes the first step of carbon fixation. Chlorophyll is the green pigment that absorbs mostly red and blue light and reflects green light."),
            ]),
            _Module(3, "Week 3 · Cellular respiration", [
                ("Lecture 3 slides", "Cellular respiration breaks down glucose to release energy as ATP. Glycolysis happens in the cytoplasm and splits one glucose molecule into two molecules of pyruvate. The citric acid cycle, also called the Krebs cycle, runs in the mitochondrial matrix and releases carbon dioxide.\n\nThe electron transport chain sits in the inner mitochondrial membrane and produces most of the ATP through oxidative phosphorylation. Oxygen is the final electron acceptor and combines with hydrogen to form water. Without oxygen, cells rely on fermentation, which regenerates NAD+ but yields far less ATP."),
            ]),
        ],
    ),
    900202: (
        Course(900202, "CS 1800 · Problem Solving and Programming", "CS1800"),
        [
            _Module(11, "Unit 1 · Algorithms and complexity", [
                ("Unit 1 reading", "An algorithm is a finite sequence of well-defined steps that solves a problem. Big-O notation describes how an algorithm's running time grows as the input size grows. Linear search checks each element in turn and runs in linear time, while binary search repeatedly halves a sorted list and runs in logarithmic time.\n\nSelection sort and insertion sort run in quadratic time in the worst case. Merge sort divides the list in half, sorts each half recursively and merges the results, giving a running time of n log n. A stable sorting algorithm keeps equal elements in their original relative order."),
            ]),
            _Module(12, "Unit 2 · Recursion", [
                ("Unit 2 slides", "Recursion is a technique where a method solves a problem by calling itself on a smaller instance of the same problem. Every recursive method needs a base case that stops the recursion; without one, the calls continue until the program runs out of stack space and throws a stack overflow error.\n\nEach recursive call gets its own frame on the call stack with its own copies of local variables. Tail recursion is when the recursive call is the last operation in the method. The factorial function and the Fibonacci sequence are classic examples of recursion, and memoization stores earlier results so repeated subproblems are not recomputed."),
            ]),
            _Module(13, "Unit 3 · Data structures", children=[
                _Module(131, "Lists, stacks and queues", [
                    ("Linear structures notes", "An array stores elements in contiguous memory and allows constant-time access by index. A linked list stores elements in nodes that each point to the next node, which makes insertion at the front fast but access by index slow. A stack is a last-in, first-out structure with push and pop operations.\n\nA queue is a first-in, first-out structure where elements are added at the rear and removed from the front. A deque allows insertion and removal at both ends, and a circular buffer reuses a fixed-size array for a queue by wrapping its indexes around."),
                ]),
                _Module(132, "Hash tables and trees", [
                    ("Hashing and trees notes", "A hash table maps keys to values using a hash function and offers average constant-time lookup. Two keys that hash to the same slot cause a collision, which chaining resolves by keeping a list in each slot. A binary search tree keeps smaller keys in the left subtree and larger keys in the right subtree.\n\nA balanced binary search tree such as an AVL tree keeps its height logarithmic, so search, insertion and deletion stay fast. A heap is a complete binary tree where each parent is at least as large as its children, which makes it a good priority queue."),
                ]),
            ]),
        ],
    ),
}


class FakeSource:
    """Two courses every test player is 'enrolled' in."""

    kind = "fake"

    async def courses(self) -> list[Course]:
        return [course for course, _ in _COURSES.values()]

    @staticmethod
    def _walk(mods: list[_Module], path: tuple[_Module, ...] = ()):
        for m in mods:
            yield m, (*path, m)
            yield from FakeSource._walk(m.children, (*path, m))

    @staticmethod
    def _count(m: _Module) -> int:
        return sum(1 for t, _ in m.topics if not ASSESSMENT_TITLE.search(t)) + sum(
            FakeSource._count(c) for c in m.children)

    async def modules(self, org_id: int) -> list[Module]:
        _, modules = _COURSES.get(org_id, (None, []))
        return [Module(m.id, m.title, self._count(m), len(path) - 1, path[-2].id if len(path) > 1 else None,
                       tuple(x.title for x in path))
                for m, path in self._walk(modules) if self._count(m)]

    async def files(self, org_id: int, module_ids: list[int] | None) -> list[FileRef]:
        _, modules = _COURSES.get(org_id, (None, []))
        wanted = set(module_ids or [])
        return [
            FileRef(m.id * 100 + n, title, tuple(x.id for x in path), tuple(x.title for x in path), title)
            for m, path in self._walk(modules)
            if not wanted or any(x.id in wanted for x in path)
            for n, (title, _) in enumerate(m.topics)
            if not ASSESSMENT_TITLE.search(title)
        ]

    async def read(self, org_id: int, files: list[FileRef]) -> list[MaterialPiece]:
        _, modules = _COURSES.get(org_id, (None, []))
        texts = {m.id * 100 + n: text for m, _ in self._walk(modules) for n, (_, text) in enumerate(m.topics)}
        return [MaterialPiece(texts[f.topic_id], f.path[0], f.path_titles[0], f.title, f.path)
                for f in files if f.topic_id in texts]

    async def material(self, org_id: int, module_ids: list[int] | None) -> list[MaterialPiece]:
        return await self.read(org_id, await self.files(org_id, module_ids))

    async def close(self) -> None:
        pass


class FakeWriter:
    """Builds simple cloze and multiple-choice cards from key terms. No AI, no network."""

    name = "Test writer"

    def __init__(self, *args, seed: int | None = None, **kwargs) -> None:
        self.random = random.Random(seed)
        self.examples_seen: list[str | None] = []

    async def generate(self, material, label, focus, types, count, avoid, examples=None):
        self.examples_seen.append(examples)
        material = strip_sources(material)
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
