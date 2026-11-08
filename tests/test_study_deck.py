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
def app(tmp_path, monkeypatch):
    import study_deck.app as app_mod

    # Never read a GenAI Studio key saved in the real OS keychain: tests must use
    # the offline writer, not call Purdue GenAI Studio with the developer's key.
    monkeypatch.setattr(app_mod, "_keyring", lambda: None)
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
    # Polls slowly enough to stay under the app's 150 requests/minute per-user limit.
    for _ in range(100):
        state = (await c.get(f"/api/sessions/{code}")).json()
        if state.get("status") == status:
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"session never reached {status}: {state}")


async def draw_next(c: httpx.AsyncClient, code: str) -> dict:
    """Answer the current card with "I don't know" if needed, then draw the next one."""
    state = (await c.get(f"/api/sessions/{code}")).json()
    if state.get("card") and not state.get("my_answer"):
        await c.post(f"/api/sessions/{code}/answer", json={"question_id": state["card"]["id"], "answer": "", "skip": True})
    return (await c.post(f"/api/sessions/{code}/draw")).json()


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
    # Stratified: every section gets an even share of the deck (in random order).
    shares = [sum(q.stratum_id == s.id for q in deck.deck) for s in deck.strata]
    assert min(shares) >= 1 and max(shares) - min(shares) <= 1, shares
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
        await draw_next(host, code)
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


# ------------------------------------------------------------ personal sign-in


@pytest.fixture
def personal_app(tmp_path, monkeypatch):
    import study_deck.app as app_mod

    class FakeBrowser:
        def __init__(self, base_url, app_url="http://127.0.0.1:8000"):
            self.app_url = app_url
            self.closed = False
            self.shown: list[str] = []

        async def open(self):
            pass

        def app_pages(self):
            return []

        async def app_cookie(self, name):
            return None

        async def show_app(self, url=None):
            self.shown.append(url)

        async def sign_in(self, timeout=300.0):
            pass

        async def shutdown(self):
            pass

        async def get_json(self, url):
            if url.endswith("/d2l/api/versions/"):
                return []
            if "/enrollments/myenrollments/" in url:
                return {"Items": [{"OrgUnit": {"Id": 4242, "Name": "CHM 11500 Real Course", "Code": "CHM11500"},
                                   "Access": {"IsActive": True, "CanAccess": True}}],
                        "PagingInfo": {"HasMoreItems": False}}
            if url.endswith("/content/toc"):
                return {"Modules": [{"ModuleId": 7, "Title": "Week 1", "Topics": [
                    {"TopicId": 70, "Title": "Atoms slides", "TypeIdentifier": "File", "Url": "/x/atoms.txt"},
                    {"TopicId": 71, "Title": "Exam 1 answer key", "TypeIdentifier": "File", "Url": "/x/key.txt"},
                ]}]}
            raise AssertionError(url)

        async def get_bytes(self, url, max_bytes):
            assert url.endswith("/content/topics/70/file"), url
            return (b"An atom has a nucleus made of protons and neutrons. Electrons occupy orbitals around "
                    b"the nucleus. The atomic number is the number of protons in the nucleus. Isotopes "
                    b"have the same number of protons but different numbers of neutrons."), "text/plain"

        async def close(self):
            self.closed = True

    monkeypatch.setattr(app_mod, "BrowserTransport", FakeBrowser)
    monkeypatch.setattr(app_mod, "GenAIStudio", lambda *a, **k: OfflineGenerator())
    monkeypatch.setattr(app_mod, "_keyring", lambda: None)
    settings = StudyDeckSettings(
        demo_mode=False, browser_mode=True, session_secret="test-secret",
        brightspace=Settings(state_dir=tmp_path, min_seconds_between_requests=0.0),
    )
    return create_app(settings)


async def test_personal_sign_in_loads_real_courses_and_files(personal_app):
    c = client(personal_app)
    r = await c.get("/auth/brightspace/start")
    assert r.status_code == 303 and r.headers["location"] == "/#/"
    for _ in range(100):
        bs = (await c.get("/api/me")).json()["brightspace"]
        if not bs["signing_in"]:
            break
        await asyncio.sleep(0.01)
    assert bs["connected"] and bs["mode"] == "personal" and bs["courses"] == 1
    key =(await c.get("/api/courses")).json()["courses"][0]["key"]
    mods = (await c.get(f"/api/courses/{key}/modules")).json()["modules"]
    assert mods == [{"id": "7", "title": "Week 1", "label": "Week 1", "files": 1, "depth": 0, "parent": None}]
    r = await c.post("/api/sessions", json={
        "name": "Ana", "strata": [{"course": key, "kind": "course"}], "types": ["mcq"],
        "deck_size": 6, "ai_permission_courses": [key], "genai_key": "k" * 20})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    await c.post(f"/api/sessions/{code}/build")
    state = await wait_for(c, code, "ready")
    assert any(st["counts"]["brightspace"] for st in state["strata"])


class _FakePlaywright:
    """Just enough of Playwright to follow BrowserTransport: one window with tabs."""

    def __init__(self, close_tab=False):
        self.close_tab = close_tab
        self.browsers: list = []
        self.fronted: list = []
        self.cleared: list = []
        outer = self

        class Resp:
            status = 200
            headers: dict = {}

            async def text(self):
                return '{"ok": true}'

        class Request:
            async def get(self, url, **kw):
                return Resp()

        class Page:
            def __init__(self, ctx):
                self.ctx, self.url, self.closed = ctx, "about:blank", False

            async def goto(self, url):
                self.url = url

            async def bring_to_front(self):
                outer.fronted.append(self.url)

            async def wait_for_url(self, pred, timeout):
                if outer.close_tab:
                    await self.close()
                    raise RuntimeError("Target page, context or browser has been closed")
                self.url = "https://purdue.brightspace.com/d2l/home"

            def is_closed(self):
                return self.closed

            async def close(self):
                self.closed = True
                self.ctx.pages.remove(self)

        class Context:
            def __init__(self):
                self.pages, self.request = [], Request()

            async def new_page(self):
                self.pages.append(Page(self))
                return self.pages[-1]

            async def cookies(self, url):
                return []

            async def clear_cookies(self, domain=None):
                outer.cleared.append(domain)

            async def close(self):
                pass

        class Browser:
            def __init__(self, headless):
                self.headless, self.contexts = headless, []

            def is_connected(self):
                return True

            async def new_context(self, **kw):
                self.contexts.append(Context())
                return self.contexts[-1]

            async def close(self):
                pass

        class Chromium:
            async def launch(self, headless, channel=None, args=None):
                outer.browsers.append(Browser(headless))
                return outer.browsers[-1]

        self.chromium = Chromium()

    async def start(self):
        return self

    async def stop(self):
        pass


async def test_sign_in_opens_as_a_new_tab_in_the_study_deck_window(monkeypatch):
    import playwright.async_api
    from study_deck.brightspace import BrowserTransport

    pw = _FakePlaywright()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: pw)
    t = BrowserTransport("https://purdue.brightspace.com", "http://127.0.0.1:8000")
    await t.show_app()
    assert [p.url for p in t.app_pages()] == ["http://127.0.0.1:8000/"]
    await t.sign_in()
    assert len(pw.browsers) == 1 and not pw.browsers[0].headless  # one window, no new one
    ctx = pw.browsers[0].contexts[0]
    assert [p.url for p in ctx.pages] == ["http://127.0.0.1:8000/"]  # the sign-in tab closed itself
    assert pw.fronted[-2:] == ["https://purdue.brightspace.com/d2l/home", "http://127.0.0.1:8000/"]
    assert t.signed_in and await t.get_json("https://purdue.brightspace.com/d2l/api/x") == {"ok": True}
    await t.shutdown()


@pytest.mark.parametrize("prog_id, first", [("ChromeHTML", "chrome"), ("MSEdgeHTM", "msedge"),
                                             ("FirefoxURL-308046B0AF4A39CB", "msedge"), (None, "msedge")])
def test_the_window_opens_in_the_default_browser(monkeypatch, prog_id, first):
    import sys
    import types
    import study_deck.brightspace as bs_mod

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_key(root, path):
        if prog_id is None:
            raise OSError("no default browser set")
        return Key()

    fake = types.SimpleNamespace(HKEY_CURRENT_USER=0, OpenKey=open_key,
                                 QueryValueEx=lambda k, name: (prog_id, 1))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(bs_mod.sys, "platform", "win32")
    assert bs_mod.BrowserTransport.channels()[0] == first
    monkeypatch.setattr(bs_mod.sys, "platform", "darwin")
    assert bs_mod.BrowserTransport.channels()[0] == "chrome"


async def test_closing_the_sign_in_tab_says_so(monkeypatch):
    import playwright.async_api
    from study_deck.brightspace import AuthRequired, BrowserTransport

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakePlaywright(close_tab=True))
    t = BrowserTransport("https://purdue.brightspace.com")
    with pytest.raises(AuthRequired, match="sign-in tab was closed before you finished"):
        await t.sign_in()
    assert t.is_open and not t.signed_in  # the Study Deck window stays


async def test_disconnect_clears_only_brightspace_cookies_and_keeps_the_window(monkeypatch):
    import playwright.async_api
    from study_deck.brightspace import AuthRequired, BrowserTransport

    pw = _FakePlaywright()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: pw)
    t = BrowserTransport("https://purdue.brightspace.com")
    await t.sign_in()
    await t.close()
    assert t.is_open and not t.signed_in
    (domain,) = pw.cleared
    assert domain.search("purdue.brightspace.com") and domain.search(".purdue.brightspace.com")
    assert not domain.search("127.0.0.1") and not domain.search("login.microsoftonline.com")
    with pytest.raises(AuthRequired):
        await t.get_json("https://purdue.brightspace.com/d2l/api/x")


async def test_study_deck_opens_in_the_window_as_the_same_player(personal_app):
    c = client(personal_app)
    await c.post("/api/me", json={"name": "Ana"})
    await c.get("/auth/brightspace/start")
    window = personal_app.state.deck.window
    (link,) = window.shown
    assert link.startswith("http://127.0.0.1:8000/auth/window?t=")
    path = link.removeprefix("http://127.0.0.1:8000")
    # A different browser (the app's window) following the link becomes the same player.
    w = client(personal_app)
    r = await w.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/#/"
    assert (await w.get("/api/me")).json()["name"] == "Ana"
    # The link works once, and only on this computer.
    again = client(personal_app)
    await again.get(path)
    assert (await again.get("/api/me")).json()["name"] == ""
    await c.get("/auth/brightspace/start")  # a fresh link
    remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=personal_app, client=("10.0.0.5", 5000)),
                               base_url="http://test", headers=H)
    await remote.get(window.shown[-1].removeprefix("http://127.0.0.1:8000"))
    assert (await remote.get("/api/me")).json()["name"] == ""


async def test_personal_sign_in_is_local_only(personal_app):
    remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=personal_app, client=("10.0.0.5", 5000)),
                               base_url="http://test", headers=H)
    r = await remote.get("/auth/brightspace/start")
    assert r.status_code == 303 and r.headers["location"] == "/#signin-local-only"
    assert not (await remote.get("/api/me")).json()["brightspace"]["signing_in"]


def test_personal_mode_needs_acknowledgement_and_local_url(monkeypatch):
    monkeypatch.delenv("STUDY_DECK_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED", raising=False)
    s = StudyDeckSettings.from_env()
    assert s.demo_mode and not s.browser_mode
    monkeypatch.setenv("STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED", "true")
    s = StudyDeckSettings.from_env()
    assert s.browser_mode and not s.demo_mode
    monkeypatch.setenv("STUDY_DECK_PUBLIC_URL", "https://deck.example.org")
    with pytest.raises(ValueError):
        StudyDeckSettings.from_env()


async def test_host_can_pick_a_custom_deck_size(app):
    host = client(app)
    keys = await connect_demo(host)
    assert (await host.get("/api/me")).json()["max_deck_size"] == 100

    def body(size, strata=1):
        topics = ["glycolysis", "ribosomes", "chloroplast"][:strata]
        return {"name": "Hana", "types": ["mcq"], "deck_size": size, "ai_permission_courses": [keys["BIOL"]],
                "strata": [{"course": keys["BIOL"], "kind": "topic", "topic": t} for t in topics]}

    for size in (1, 37, 100):
        r = await host.post("/api/sessions", json=body(size))
        assert r.status_code == 200, r.text
        assert (await host.get(f"/api/sessions/{r.json()['code']}")).json()["deck_size"] == size
    for size in (0, 101):
        r = await host.post("/api/sessions", json=body(size))
        assert r.status_code == 400 and "1 to 100 cards" in r.json()["error"]
    r = await host.post("/api/sessions", json=body(2, strata=3))
    assert r.status_code == 400 and "at least one per section" in r.json()["error"]


def test_small_decks_do_not_refill_before_play():
    from study_deck.game import GameSession

    for size, threshold in ((1, 0), (2, 1), (3, 2), (6, 5), (7, 5), (20, 5), (100, 5)):
        s = GameSession(code="ABCDEF", host_id="h", strata=[], types=["mcq"], deck_size=size, generator=OfflineGenerator())
        assert s.refill_threshold() == threshold


def test_max_deck_size_setting_is_capped(monkeypatch):
    monkeypatch.setenv("STUDY_DECK_MAX_DECK_SIZE", "5000")
    assert StudyDeckSettings.from_env().max_deck_size == 300
    monkeypatch.setenv("STUDY_DECK_MAX_DECK_SIZE", "40")
    assert StudyDeckSettings.from_env().max_deck_size == 40


def test_i_dont_know_counts_as_a_miss_and_reveals_the_answer():
    from study_deck.game import GameSession, Player

    s = GameSession(code="ABCDEF", host_id="h", strata=[], types=["fill_blank"], deck_size=4, generator=OfflineGenerator())
    s.players["h"] = Player("h", "Hana", is_host=True)
    s.current = Question("q1", "s1", "L", "fill_blank", "The _____ makes ATP.", "", answer="mitochondrion")
    out = s.submit("h", "q1", "mitochondrion", skipped=True)  # a typed answer is ignored when skipping
    assert out["correct"] is False and out["expected"] == "mitochondrion"
    assert s.answers["h"] == {"correct": False, "given": "", "skipped": True}
    assert s.players["h"].score == 0 and s.players["h"].answered == 1
    with pytest.raises(ValueError):
        s.submit("h", "q1", "", skipped=True)


async def test_deck_refills_with_10_cards_when_five_are_left_and_end_game_deletes_it(app):
    host, code, _ = await make_game(app, deck_size=8)
    assert (await host.post(f"/api/sessions/{code}/build")).status_code == 200
    state = await wait_for(host, code, "ready")
    total = state["deck"]["remaining"]
    assert total >= 8
    deck = app.state.deck.store.get(code)
    asked: list[int] = []
    real_generate = deck.generate

    async def spy(total_cards):
        asked.append(total_cards)
        return await real_generate(total_cards)

    deck.generate = spy
    for n in range(total - 6):  # still 6 left: no refill yet
        state = await draw_next(host, code)
        assert not state["deck"]["refilling"], n
    state = await draw_next(host, code)  # 5 left
    assert state["deck"]["remaining"] == 5
    for _ in range(80):
        if not deck.refilling and len(deck.deck) > total:
            break
        await asyncio.sleep(0.1)
    # 10 new cards are asked for (the tiny sample material may yield fewer offline).
    assert asked == [10] and len(deck.deck) > total

    assert (await host.post(f"/api/sessions/{code}/end")).json() == {"ended": True}
    assert (await host.get(f"/api/sessions/{code}")).status_code == 404


async def test_cards_come_in_random_order_but_each_section_gets_its_share():
    import random
    from study_deck.game import Chunk, GameSession, Stratum

    class Numbered:
        name = "numbered"

        def __init__(self):
            self.n = 0

        async def generate(self, material, label, focus, types, count, avoid):
            out = []
            for _ in range(count):
                self.n += 1
                out.append({"type": "fill_blank", "prompt": f"{label} card {self.n} is _____ here.",
                            "answer": "word", "accept": [], "explanation": ""})
            return out

    strata = [Stratum(id=f"s{i}", course_label=f"C{i}", org_id=None, kind="course") for i in (1, 2)]
    s = GameSession(code="ABCDEF", host_id="h", strata=strata, types=["fill_blank"], deck_size=20,
                    generator=Numbered(), rng=random.Random(7))
    for st in strata:
        s.add_chunks([Chunk(text=f"Material for {st.id}.", source="notes", contributor="h", stratum_id=st.id)])
    assert await s.generate(20) == 20
    first = [q.stratum_id for q in s.deck]
    assert first.count("s1") == first.count("s2") == 10
    assert first != ["s1", "s2"] * 10  # not dealt in strict turns any more
    numbers = [int(q.prompt.split("card ")[1].split(" ")[0]) for q in s.deck if q.stratum_id == "s1"]
    assert numbers != sorted(numbers)  # questions within a section are shuffled too
    assert await s.generate(10) == 10  # a refill batch: 10 cards, 5 per section, shuffled in after
    batch = [q.stratum_id for q in s.deck[20:]]
    assert batch.count("s1") == batch.count("s2") == 5


# ------------------------------------------------- folders inside folders, AI picking


def _refs(spec):
    """spec: list of (topic_id, title, [(folder_id, folder_title), ...])"""
    from study_deck.brightspace import FileRef
    return [FileRef(t, title, tuple(i for i, _ in path), tuple(n for _, n in path), title) for t, title, path in spec]


ONE_BIG_FOLDER = [
    (1, "Syllabus", [(10, "Course content")]),
    (2, "Stoichiometry slides", [(10, "Course content"), (11, "Unit 2"), (12, "Week 5")]),
    (3, "Mole ratios worksheet", [(10, "Course content"), (11, "Unit 2"), (12, "Week 5")]),
    (4, "Limiting reagent notes", [(10, "Course content"), (11, "Unit 2"), (13, "Week 6")]),
    (5, "Gas laws slides", [(10, "Course content"), (14, "Unit 3")]),
]


async def test_nested_folders_are_listed_and_can_each_be_a_section(app):
    host = client(app)
    keys = await connect_demo(host)
    mods = (await host.get(f"/api/courses/{keys['CS']}/modules")).json()["modules"]
    nested = [m for m in mods if m["depth"] == 1]
    assert [m["title"] for m in nested] == ["Lists, stacks and queues", "Hash tables and trees"]
    assert nested[0]["parent"] == next(m["id"] for m in mods if m["title"].startswith("Unit 3"))
    assert nested[1]["label"] == "Unit 3 · Data structures › Hash tables and trees"
    r = await host.post("/api/sessions", json={
        "name": "Ana", "strata": [{"course": keys["CS"], "kind": "module", "modules": [nested[1]["id"]]}],
        "types": ["fill_blank"], "deck_size": 4, "ai_permission_courses": [keys["CS"]]})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    await host.post(f"/api/sessions/{code}/build")
    state = await wait_for(host, code, "ready")
    assert state["strata"][0]["label"].endswith("Hash tables and trees")
    (picked,) = state["picked"]
    assert picked["how"] == "all" and picked["files"] == [
        "Unit 3 · Data structures › Hash tables and trees › Hashing and trees notes"]
    # Only that subfolder's material went into the deck.
    deck = app.state.deck.store.get(code)
    assert all(c.module_path == (13, 132) for c in deck.chunks if c.source == "brightspace")


async def test_ai_opens_folders_as_deep_as_it_needs(app, monkeypatch):
    from study_deck.genai import OfflineGenerator

    seen = {}

    async def choose_files(self, course, outline, sections, limit):
        seen.update(course=course, outline=outline, sections=sections, limit=limit)
        return '<think>{"x": 1}</think>```json\n{"folders": [132], "files": ["file 1301"]}\n```'

    monkeypatch.setattr(OfflineGenerator, "choose_files", choose_files, raising=False)
    host = client(app)
    keys = await connect_demo(host)
    r = await host.post("/api/sessions", json={
        "name": "Ana", "strata": [{"course": keys["CS"], "kind": "topic", "topic": "binary search trees"}],
        "types": ["fill_blank"], "deck_size": 3, "ai_permission_courses": [keys["CS"]]})
    code = r.json()["code"]
    await host.post(f"/api/sessions/{code}/build")
    state = await wait_for(host, code, "ready")
    assert seen["sections"] == ["binary search trees"] and seen["limit"] == 30
    # Titles only: the outline has folder and file titles and ids, never file contents.
    assert "[folder 132] Hash tables and trees" in seen["outline"]
    assert "    [file 13200] Hashing and trees notes" in seen["outline"]
    assert "contiguous memory" not in seen["outline"]
    (picked,) = state["picked"]
    assert picked["how"] == "ai"
    assert picked["folders"] == ["Unit 3 · Data structures › Hash tables and trees"]
    assert picked["files"] == ["Unit 3 · Data structures › Hash tables and trees › Hashing and trees notes"]


async def test_picker_keeps_to_scope_and_limit():
    from study_deck.picker import pick_files

    scope = _refs(ONE_BIG_FOLDER)

    class AI:
        async def choose_files(self, course, outline, sections, limit):
            return '{"folders": [11, 999], "files": [5, 2, 424242]}'

    pick = await pick_files(AI(), "CHM 115", scope, ["stoichiometry"], ["stoichiometry"], 3)
    assert pick.how == "ai"
    assert [f.topic_id for f in pick.files] == [2, 3, 4]  # Unit 2 opened whole, cut at the limit
    assert pick.folders == ["Course content › Unit 2"]


async def test_picker_falls_back_to_titles_when_the_ai_fails():
    from study_deck.picker import pick_files

    class Broken:
        async def choose_files(self, *a):
            return "I think you should read the slides."

    scope = _refs(ONE_BIG_FOLDER)
    pick = await pick_files(Broken(), "CHM 115", scope, ["limiting reagent", "gas laws"],
                            ["limiting reagent", "gas laws"], 10)
    assert pick.how == "titles" and [f.topic_id for f in pick.files] == [4, 5]


async def test_picker_reads_everything_when_it_fits_and_spreads_when_it_does_not():
    from study_deck.picker import pick_files

    scope = _refs(ONE_BIG_FOLDER)
    assert (await pick_files(OfflineGenerator(), "CHM 115", scope, ["the whole course"], [], 30)).how == "all"
    pick = await pick_files(OfflineGenerator(), "CHM 115", scope, ["the whole course"], [], 3)
    # Takes turns across folders instead of reading the first three in one folder.
    assert pick.how == "all" and [f.topic_id for f in pick.files] == [1, 2, 4]


async def test_folders_without_readable_files_are_hidden(tmp_path):
    from purdue_brightspace_mcp.policy import AuditLog
    from study_deck.brightspace import LiveSource

    settings = StudyDeckSettings(brightspace=Settings(state_dir=tmp_path))
    source = LiveSource(settings, object(), AuditLog(tmp_path / "a.jsonl", False))
    file = lambda tid, title="Notes": {"TopicId": tid, "Title": title, "TypeIdentifier": "File", "Url": "/x.pdf"}
    source._toc[1] = {"Modules": [
        {"ModuleId": 1, "Title": "Welcome", "Topics": [{"TopicId": 9, "Title": "Zoom link", "TypeIdentifier": "Link"}]},
        {"ModuleId": 2, "Title": "Course content", "Topics": [], "Modules": [
            {"ModuleId": 3, "Title": "Unit 1", "Topics": [file(30)], "Modules": [
                {"ModuleId": 4, "Title": "Empty week", "Topics": [], "Modules": [
                    {"ModuleId": 5, "Title": "Still empty", "Topics": []}]},
            ]},
            {"ModuleId": 6, "Title": "Quizzes", "Topics": [file(60, "Quiz 1 answer key")]},
        ]},
        {"ModuleId": 7, "Title": "Hidden", "IsHidden": True, "Topics": [file(70)]},
    ]}
    mods = await source.modules(1)
    assert [(m.id, m.depth, m.file_count) for m in mods] == [(2, 0, 1), (3, 1, 1)]
    assert mods[1].label == "Course content › Unit 1"


async def test_host_must_answer_before_drawing_the_next_card(app):
    host, code, _ = await make_game(app, deck_size=4)
    await host.post(f"/api/sessions/{code}/build")
    await wait_for(host, code, "ready")
    first = (await host.post(f"/api/sessions/{code}/draw")).json()["card"]
    r = await host.post(f"/api/sessions/{code}/draw")
    assert r.status_code == 409 and "Answer this card" in r.json()["error"]
    assert (await host.get(f"/api/sessions/{code}")).json()["card"]["id"] == first["id"]  # still the same card
    await host.post(f"/api/sessions/{code}/answer", json={"question_id": first["id"], "answer": "", "skip": True})
    r = await host.post(f"/api/sessions/{code}/draw")
    assert r.status_code == 200 and r.json()["card"]["id"] != first["id"]
