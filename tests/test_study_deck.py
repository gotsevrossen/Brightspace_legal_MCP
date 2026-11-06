from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest

from purdue_brightspace_mcp.config import Settings
from study_deck.app import create_app
from study_deck.config import StudyDeckSettings, check_genai_url
from study_deck.extract import chunk_text, extract_text, relevance
from study_deck.game import Question, check_answer
from study_deck.genai import GenAIStudio, OfflineGenerator, parse_json_block, validate_questions

H = {"X-Study-Deck": "1"}


@pytest.fixture
def app(tmp_path):
    settings = StudyDeckSettings(
        demo_mode=True,
        session_secret="test-secret",
        brightspace=Settings(state_dir=tmp_path, min_seconds_between_requests=0.0),
    )
    return create_app(settings)


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=H)


async def connect_demo(c: httpx.AsyncClient) -> dict[str, str]:
    r = await c.get("/auth/brightspace/start")
    assert r.status_code == 303
    courses = (await c.get("/api/courses")).json()["courses"]
    return {x["name"].split(" ")[0]: x["key"] for x in courses}


async def wait_for(c: httpx.AsyncClient, code: str, status: str) -> dict:
    for _ in range(200):
        state = (await c.get(f"/api/sessions/{code}")).json()
        if state["status"] == status:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"session never reached {status}: {state}")


async def make_game(app, **overrides) -> tuple[httpx.AsyncClient, str, dict[str, str]]:
    host = client(app)
    keys = await connect_demo(host)
    modules = (await host.get(f"/api/courses/{keys['BIOL']}/modules")).json()["modules"]
    week2 = next(m["id"] for m in modules if "Photosynthesis" in m["title"])
    body = {
        "name": "Hana",
        "strata": [
            {"course": keys["BIOL"], "kind": "module", "modules": [week2]},
            {"course": keys["BIOL"], "kind": "topic", "topic": "glycolysis"},
            {"manual_course": "PSY 1200", "kind": "topic", "topic": "memory"},
        ],
        "types": ["mcq", "fill_blank"],
        "deck_size": 8,
        "ai_permission_courses": [keys["BIOL"]],
        **overrides,
    }
    r = await host.post("/api/sessions", json=body)
    assert r.status_code == 200, r.text
    return host, r.json()["code"], keys


PSY_NOTES = (
    "Working memory holds a small amount of information for a short time. Long-term memory stores "
    "information for long periods. Encoding is the process of getting information into memory. "
    "Retrieval is the process of getting information back out of memory when it is needed. "
    "The serial position effect means people remember the first and last items in a list best."
)


async def test_full_multiplayer_game(app):
    host, code, keys = await make_game(app)
    player = client(app)
    assert (await player.post(f"/api/sessions/{code}/join", json={"name": "Sam"})).status_code == 200

    # Sam hasn't connected Brightspace, so BIOL Brightspace material is held back.
    state = (await host.get(f"/api/sessions/{code}")).json()
    assert state["held_back"] == [{"course": "BIOL 1100 · Cell Biology (sample)", "waiting_on": ["Sam"]}]

    # Sam adds notes for the manual PSY section.
    psy = next(s["id"] for s in state["strata"] if s["course"] == "PSY 1200")
    r = await player.post(f"/api/sessions/{code}/upload", data={"stratum": psy, "notes": PSY_NOTES, "attest": "yes"})
    assert r.status_code == 200 and r.json()["added"] >= 1

    # Uploading a Word file works too.
    import docx

    doc = docx.Document()
    doc.add_paragraph("Classical conditioning pairs a neutral stimulus with an unconditioned stimulus. " * 3)
    buf = io.BytesIO()
    doc.save(buf)
    r = await player.post(f"/api/sessions/{code}/upload", data={"stratum": psy, "attest": "yes"},
                          files={"files": ("notes.docx", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 200, r.text

    # Once Sam connects, nothing is held back.
    await connect_demo(player)
    state = (await host.get(f"/api/sessions/{code}")).json()
    assert state["held_back"] == []

    # Only the host can build.
    assert (await player.post(f"/api/sessions/{code}/build")).status_code == 403
    assert (await host.post(f"/api/sessions/{code}/build")).status_code == 200
    state = await wait_for(host, code, "ready")
    assert state["deck"]["total"] >= 6
    counts = {s["label"]: s["counts"] for s in state["strata"]}
    assert all(sum(c.values()) > 0 for c in counts.values()), counts

    deck = app.state.deck.store.sessions[code]
    # Assessment files are never read.
    assert not any("assessment file" in c.text for c in deck.chunks)
    # Stratified: the first cards rotate through the sections.
    first = [q.stratum_id for q in deck.deck[:3]]
    assert len(set(first)) == 3, first
    # The topic section only uses passages about the topic.
    topic_stratum = next(s for s in deck.strata if s.topic == "glycolysis")
    assert all("glycolysis" in c.text.lower() for c in deck.material_for(topic_stratum))

    # Play a card: answers are hidden until you answer.
    state = (await host.post(f"/api/sessions/{code}/draw")).json()
    card = state["card"]
    assert "answer" not in json.dumps(card) and "answer_index" not in card
    q = deck.current
    right = q.answer_index if q.type == "mcq" else q.answer
    r = await player.post(f"/api/sessions/{code}/answer", json={"question_id": card["id"], "answer": right})
    assert r.json()["my_answer"]["correct"] is True
    again = await player.post(f"/api/sessions/{code}/answer", json={"question_id": card["id"], "answer": right})
    assert again.status_code == 409
    wrong = (q.answer_index + 1) % len(q.options) if q.type == "mcq" else "zzzz"
    r = await host.post(f"/api/sessions/{code}/answer", json={"question_id": card["id"], "answer": wrong})
    assert r.json()["my_answer"]["correct"] is False
    scores = {p["name"]: p["score"] for p in r.json()["players"]}
    assert scores == {"Hana": 0, "Sam": 1}

    # Drawing down the deck triggers an automatic top-up.
    total = state["deck"]["total"]
    for _ in range(total):
        await host.post(f"/api/sessions/{code}/draw")
    for _ in range(300):
        if not deck.refilling and len(deck.deck) > total:
            break
        await asyncio.sleep(0.01)
    assert len(deck.deck) > total
    # Top-ups don't repeat earlier questions.
    prompts = [q.prompt for q in deck.deck]
    assert len(prompts) == len(set(prompts))

    # Ending the game deletes everything.
    assert (await player.post(f"/api/sessions/{code}/end")).status_code == 403
    assert (await host.post(f"/api/sessions/{code}/end")).status_code == 200
    assert code not in app.state.deck.store.sessions and not deck.chunks
    assert (await host.get(f"/api/sessions/{code}")).status_code == 404


async def test_brightspace_material_needs_every_player_enrolled(app):
    host, code, _ = await make_game(app)
    player = client(app)
    await player.post(f"/api/sessions/{code}/join", json={"name": "Sam"})
    state = (await host.get(f"/api/sessions/{code}")).json()
    psy = next(s["id"] for s in state["strata"] if s["course"] == "PSY 1200")
    await host.post(f"/api/sessions/{code}/upload", data={"stratum": psy, "notes": PSY_NOTES, "attest": "yes"})
    await host.post(f"/api/sessions/{code}/build")
    await wait_for(host, code, "ready")
    deck = app.state.deck.store.sessions[code]
    # Sam never verified BIOL, so no BIOL Brightspace passage was read, and every card is from PSY notes.
    assert not any(c.source == "brightspace" for c in deck.chunks)
    assert deck.deck and all(q.label.startswith("PSY") for q in deck.deck)


async def test_late_joiner_must_verify_when_deck_uses_brightspace(app):
    host, code, _ = await make_game(app)
    await host.post(f"/api/sessions/{code}/build")
    await wait_for(host, code, "ready")
    late = client(app)
    r = await late.post(f"/api/sessions/{code}/join", json={"name": "Lee"})
    assert r.status_code == 403 and "Connect Brightspace" in r.json()["error"]
    await connect_demo(late)
    assert (await late.post(f"/api/sessions/{code}/join", json={"name": "Lee"})).status_code == 200


async def test_course_permission_is_required(app):
    host = client(app)
    keys = await connect_demo(host)
    r = await host.post("/api/sessions", json={
        "name": "Hana", "strata": [{"course": keys["BIOL"], "kind": "course"}],
        "types": ["mcq"], "deck_size": 6, "ai_permission_courses": [],
    })
    assert r.status_code == 400 and "syllabus" in r.json()["error"]


async def test_cannot_use_someone_elses_course(app):
    host = client(app)
    r = await host.post("/api/sessions", json={
        "name": "Hana", "strata": [{"course": "c0000000000000", "kind": "course"}],
        "types": ["mcq"], "deck_size": 6, "ai_permission_courses": ["c0000000000000"],
    })
    assert r.status_code == 403


async def test_post_requires_app_header(app):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    r = await c.post("/api/sessions", json={})
    assert r.status_code == 403


async def test_raw_brightspace_ids_never_reach_the_browser(app):
    c = client(app)
    await connect_demo(c)
    courses = (await c.get("/api/courses")).text
    assert "900101" not in courses and "900202" not in courses


async def test_security_headers(app):
    r = await client(app).get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture
def fake_genai(monkeypatch):
    import study_deck.app as app_mod
    import study_deck.genai as genai_mod

    async def fake_list_models(url, key, http=None):
        if key != "GOOD-KEY-1234":
            raise genai_mod.GenerationError("Purdue GenAI Studio didn't accept that API key.")
        return ["gemma3:12b", "llama3.1:latest"]

    ring = FakeKeyring()
    monkeypatch.setattr(genai_mod, "list_models", fake_list_models)
    monkeypatch.setattr(app_mod, "_keyring", lambda: ring)
    return ring


async def test_settings_save_key_and_use_it_for_games(app, fake_genai):
    c = client(app)
    me = (await c.get("/api/me")).json()
    assert me["genai"]["configured"] is False and me["genai"]["offline_demo"] is True

    bad = await c.post("/api/settings/genai", json={"key": "nope-nope-nope", "model": ""})
    assert bad.status_code == 400 and "didn't accept" in bad.json()["error"]

    models = await c.post("/api/settings/genai/models", json={"key": "GOOD-KEY-1234"})
    assert models.json()["models"] == ["gemma3:12b", "llama3.1:latest"]

    r = await c.post("/api/settings/genai", json={"key": "GOOD-KEY-1234", "model": "gemma3:12b"})
    assert r.status_code == 200
    g = r.json()["genai"]
    assert g["configured"] and g["model"] == "gemma3:12b" and g["source"] == "yours" and g["key_hint"] == "…1234"
    # The key itself never comes back to the browser.
    assert "GOOD-KEY-1234" not in r.text and "GOOD-KEY-1234" not in (await c.get("/api/me")).text
    assert fake_genai.store == {}  # not remembered unless asked

    keys = await connect_demo(c)
    code = (await c.post("/api/sessions", json={
        "name": "Hana", "strata": [{"course": keys["BIOL"], "kind": "course"}], "types": ["mcq"],
        "deck_size": 6, "ai_permission_courses": [keys["BIOL"]],
    })).json()["code"]
    gen = app.state.deck.store.sessions[code].generator
    assert gen.name == "Purdue GenAI Studio" and gen.model == "gemma3:12b" and gen.api_key == "GOOD-KEY-1234"

    assert (await c.post("/api/settings/genai", json={"model": "not-a-model"})).status_code == 400

    forgot = (await c.post("/api/settings/genai/forget")).json()["genai"]
    assert forgot["configured"] is False


async def test_settings_remember_uses_keychain_locally_only(app, fake_genai):
    c = client(app)  # httpx's ASGI transport connects from 127.0.0.1
    r = await c.post("/api/settings/genai", json={"key": "GOOD-KEY-1234", "model": "llama3.1:latest", "remember": True})
    assert r.status_code == 200 and fake_genai.store[("study-deck", "genai_api_key")] == "GOOD-KEY-1234"

    # A new browser on this computer picks up the remembered key.
    other = client(app)
    g = (await other.get("/api/me")).json()["genai"]
    assert g["configured"] and g["source"] == "saved" and g["model"] == "llama3.1:latest"

    # Someone connecting from another machine never gets it.
    remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.168.1.20", 5000)),
                               base_url="http://test", headers=H)
    g = (await remote.get("/api/me")).json()["genai"]
    assert g["configured"] is False and g["can_remember"] is False
    r = await remote.post("/api/settings/genai", json={"key": "GOOD-KEY-1234", "remember": True})
    assert r.status_code == 400

    await c.post("/api/settings/genai/forget")
    assert fake_genai.store == {}


async def test_list_models_parses_genai_studio_reply():
    from study_deck.genai import GenerationError, list_models, models_url

    assert models_url("https://genai.rcac.purdue.edu/api/chat/completions") == "https://genai.rcac.purdue.edu/api/models"

    def handler(request):
        if request.headers["authorization"] != "Bearer K":
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"id": "llama3.1:latest"}, {"id": "gemma3:12b"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await list_models("https://genai.rcac.purdue.edu/api/chat/completions", "K", http) == ["gemma3:12b", "llama3.1:latest"]
    with pytest.raises(GenerationError):
        await list_models("https://genai.rcac.purdue.edu/api/chat/completions", "bad", http)


async def test_connect_returns_only_to_game_pages(app):
    c = client(app)
    r = await c.get("/auth/brightspace/start", params={"next": "/#/game/ABC234"})
    assert r.headers["location"] == "/#/game/ABC234"
    for bad in ["https://evil.example", "//evil.example", "/#/game/ABC234/../x"]:
        r = await c.get("/auth/brightspace/start", params={"next": bad})
        assert r.headers["location"] == "/#connected"


async def test_notes_with_empty_file_part_are_accepted(app):
    host, code, _ = await make_game(app)
    state = (await host.get(f"/api/sessions/{code}")).json()
    r = await host.post(f"/api/sessions/{code}/upload",
                        data={"stratum": state["strata"][2]["id"], "notes": PSY_NOTES, "attest": "yes"},
                        files={"files": ("", b"", "application/octet-stream")})
    assert r.status_code == 200 and r.json()["skipped"] == []


async def test_upload_requires_attestation(app):
    host, code, _ = await make_game(app)
    state = (await host.get(f"/api/sessions/{code}")).json()
    r = await host.post(f"/api/sessions/{code}/upload", data={"stratum": state["strata"][0]["id"], "notes": PSY_NOTES})
    assert r.status_code == 400 and "allowed to share" in r.json()["error"]


def test_check_answer():
    fill = Question("q", "s", "L", "fill_blank", "The _____ makes ATP.", "", answer="mitochondrion",
                    accept=["mitochondria"])
    assert check_answer(fill, "Mitochondrion")
    assert check_answer(fill, "the mitochondria")
    assert check_answer(fill, "mitocondrion")  # one typo
    assert not check_answer(fill, "ribosome")
    assert not check_answer(fill, "")
    num = Question("q", "s", "L", "fill_blank", "_____ ATP", "", answer="36")
    assert check_answer(num, "36.0") and not check_answer(num, "38")
    mcq = Question("q", "s", "L", "mcq", "?", "", options=["a", "b", "c", "d"], answer_index=2)
    assert check_answer(mcq, 2) and check_answer(mcq, "2") and not check_answer(mcq, 1) and not check_answer(mcq, "x")


def test_validate_questions_rejects_bad_items():
    raw = [
        {"type": "mcq", "prompt": "Which organelle makes ATP?", "options": ["Mitochondrion", "Ribosome", "Golgi", "Lysosome"], "answer_index": 0},
        {"type": "mcq", "prompt": "Bad index question here", "options": ["a", "b", "c", "d"], "answer_index": 9},
        {"type": "mcq", "prompt": "Duplicate options question", "options": ["a", "a", "b", "c"], "answer_index": 0},
        {"type": "fill_blank", "prompt": "The answer is mitochondrion and _____", "answer": "mitochondrion"},
        {"type": "fill_blank", "prompt": "No blank in this one at all", "answer": "x"},
        {"type": "Fill in the blank", "prompt": "Glycolysis happens in the ____.", "answer": "cytoplasm"},
        {"type": "essay", "prompt": "Explain everything about biology", "answer": "x"},
    ]
    good = validate_questions(raw, {"mcq", "fill_blank"}, avoid=[])
    assert [g["type"] for g in good] == ["mcq", "fill_blank"]
    assert good[1]["prompt"] == "Glycolysis happens in the _____."
    assert validate_questions(raw, {"mcq"}, avoid=["Which organelle makes ATP?"]) == []


def test_parse_json_block_handles_fences_and_chatter():
    text = 'Sure! Here you go:\n```json\n{"questions": [{"type": "mcq"}]}\n```\nEnjoy'
    assert parse_json_block(text) == [{"type": "mcq"}]
    assert parse_json_block('[{"type": "fill_blank"}]') == [{"type": "fill_blank"}]


def test_genai_endpoint_must_be_purdue():
    assert check_genai_url("https://genai.rcac.purdue.edu/api/chat/completions")
    for bad in ["https://api.openai.com/v1/chat/completions", "http://genai.rcac.purdue.edu/x",
                "https://purdue.edu.evil.example/x"]:
        with pytest.raises(ValueError):
            check_genai_url(bad)


async def test_genai_studio_request_contains_material_only():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        reply = {"questions": [{"type": "fill_blank", "prompt": "Photosynthesis happens in the _____.",
                                "answer": "chloroplast", "accept": ["chloroplasts"], "explanation": "Stated in notes."}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    gen = GenAIStudio("https://genai.rcac.purdue.edu/api/chat/completions", "llama3.1:latest", "KEY",
                      http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await gen.generate("Photosynthesis happens in the chloroplast.", "BIOL · Week 2", None, ["fill_blank"], 1, [])
    assert out[0]["answer"] == "chloroplast" and seen["auth"] == "Bearer KEY"
    assert seen["body"]["model"] == "llama3.1:latest" and seen["body"]["stream"] is False


async def test_offline_generator_writes_valid_cards():
    material = "Merge sort divides the list in half and merges the sorted halves together. " * 2 + \
        "Binary search repeatedly halves a sorted list and runs in logarithmic time. A stack is last in, first out."
    cards = await OfflineGenerator(seed=1).generate(material, "CS", None, ["mcq", "fill_blank"], 4, [])
    assert cards and all(c["type"] in ("mcq", "fill_blank") for c in cards)


def test_extract_office_files_and_chunking():
    import docx
    from pptx import Presentation

    d = docx.Document()
    d.add_paragraph("Osmosis moves water across a membrane.")
    b = io.BytesIO()
    d.save(b)
    assert "Osmosis" in extract_text(b.getvalue(), "a.docx")

    p = Presentation()
    slide = p.slides.add_slide(p.slide_layouts[1])
    slide.shapes.title.text = "Diffusion"
    slide.placeholders[1].text = "Particles spread from high to low concentration. Email me at prof@purdue.edu"
    b = io.BytesIO()
    p.save(b)
    text = extract_text(b.getvalue(), "s.pptx")
    assert "Diffusion" in text and "prof@purdue.edu" not in text

    with pytest.raises(ValueError):
        extract_text(b"\x00", "virus.exe")
    chunks = chunk_text("Sentence one is here. " * 200)
    assert len(chunks) > 1 and all(len(c) <= 1300 for c in chunks)
    assert relevance("Glycolysis splits glucose into pyruvate.", "glycolysis") > relevance("The Calvin cycle.", "glycolysis")
