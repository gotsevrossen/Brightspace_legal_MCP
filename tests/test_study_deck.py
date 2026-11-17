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
from study_deck.genai import GenAIStudio, parse_json_block, validate_questions

from fakes import FakeSource, FakeWriter

H = {"X-Study-Deck": "1"}
# Games created with this key get the offline test writer instead of Purdue GenAI Studio.
TEST_KEY = "test-genai-key"


@pytest.fixture
def bare_app(tmp_path, monkeypatch):
    """The app as it ships: no Brightspace configured, no GenAI key."""
    import study_deck.app as app_mod

    # Never read a GenAI Studio key saved in the real OS keychain: tests must use
    # the test writer, not call Purdue GenAI Studio with the developer's key.
    monkeypatch.setattr(app_mod, "_keyring", lambda: None)
    monkeypatch.setattr(app_mod, "GenAIStudio",
                        lambda url, model, key: FakeWriter() if key == TEST_KEY else GenAIStudio(url, model, key))
    settings = StudyDeckSettings(
        session_secret="test-secret",
        brightspace=Settings(state_dir=tmp_path, min_seconds_between_requests=0.0),
    )
    return create_app(settings)


@pytest.fixture
def app(bare_app):
    """With a fake Brightspace that has two small courses."""
    bare_app.state.deck.source_factory = FakeSource
    return bare_app


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=H)


async def connect_brightspace(c: httpx.AsyncClient) -> dict[str, str]:
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
    if state.get("card") and not state.get("outcome"):
        await c.post(f"/api/sessions/{code}/answer", json={"question_id": state["card"]["id"], "answer": "", "skip": True})
    return (await c.post(f"/api/sessions/{code}/draw")).json()


async def make_game(app, **overrides) -> tuple[httpx.AsyncClient, str, dict[str, str]]:
    host = client(app)
    keys = await connect_brightspace(host)
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
        "genai_key": TEST_KEY,
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
    assert state["held_back"] == [{"course": "BIOL 1100 · Cell Biology", "waiting_on": ["Sam"]}]

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
    await connect_brightspace(player)
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

    # The host checks answers; the physical board decides turns and scoring.
    state = (await host.post(f"/api/sessions/{code}/draw")).json()
    card = state["card"]
    assert "answer" not in json.dumps(card) and "answer_index" not in card
    assert state["answering"] is None and state["outcome"] is None
    keys = {p["name"]: p["key"] for p in state["players"]}
    q = deck.current
    right = q.answer_index if q.type == "mcq" else q.answer
    seen = (await player.get(f"/api/sessions/{code}")).json()
    assert seen["answering"] is None and "expected" not in json.dumps(seen["card"])
    r = await player.post(f"/api/sessions/{code}/answer", json={"question_id": card["id"], "answer": right})
    assert r.status_code == 403
    # Even an older client's player selection must not attribute the answer to anyone.
    r = await host.post(f"/api/sessions/{code}/answer",
                        json={"question_id": card["id"], "answer": right, "player": keys["Sam"]})
    assert r.json()["outcome"]["correct"] is True and "name" not in r.json()["outcome"]
    again = await host.post(f"/api/sessions/{code}/answer", json={"question_id": card["id"], "answer": right})
    assert again.status_code == 409
    seen = (await player.get(f"/api/sessions/{code}")).json()
    assert "me" not in seen["outcome"] and seen["outcome"]["expected"] == q.expected
    scores = {p["name"]: p["score"] for p in r.json()["players"]}
    assert scores == {"Hana": 0, "Sam": 0}
    # The next card is also unassigned.
    state = (await host.post(f"/api/sessions/{code}/draw")).json()
    assert state["answering"] is None

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
    await connect_brightspace(late)
    assert (await late.post(f"/api/sessions/{code}/join", json={"name": "Lee"})).status_code == 200


async def test_course_permission_is_required(app):
    host = client(app)
    keys = await connect_brightspace(host)
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
    await connect_brightspace(c)
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
    assert me["genai"]["configured"] is False

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

    keys = await connect_brightspace(c)
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


def test_material_is_labelled_by_source_for_the_ai():
    from study_deck.game import Chunk, GameSession, Stratum
    from study_deck.genai import SYSTEM_PROMPT, SOURCE_LINES

    st = Stratum(id="s1", course_label="BIOL 1100", org_id=5, kind="course")
    s = GameSession(code="ABCDEF", host_id="h", strata=[st], types=["mcq"], deck_size=8, generator=FakeWriter())
    s.add_chunks([Chunk(text="The instructor's own wording.", source="brightspace", contributor="h", org_id=5),
                  Chunk(text="A player's own notes.", source="notes", contributor="p", stratum_id="s1"),
                  Chunk(text="A player's uploaded file.", source="upload", contributor="p", stratum_id="s1")])
    window = s.next_window(st)
    assert f"{SOURCE_LINES['brightspace']}\nThe instructor's own wording." in window
    assert f"{SOURCE_LINES['notes']}\nA player's own notes." in window
    assert f"{SOURCE_LINES['upload']}\nA player's uploaded file." in window
    # The instructions name every label and say how to use them.
    assert all(line in SYSTEM_PROMPT for source, line in SOURCE_LINES.items() if source != "boilerexams")
    from study_deck.genai import BOILEREXAMS_PROMPT
    assert SOURCE_LINES["boilerexams"] in BOILEREXAMS_PROMPT
    for rule in ("Follow the material's wording strictly", "use the Brightspace wording",
                 "copy their structure", "do not reuse a question from the material word for word"):
        assert rule in SYSTEM_PROMPT


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

        async def sign_in(self, timeout=300.0, intro_url=None):
            self.intro_url = intro_url

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
    guests: list = []

    def fake_guest(window):
        guests.append(FakeBrowser("https://purdue.brightspace.com"))
        return guests[-1]

    monkeypatch.setattr(app_mod, "GuestTransport", fake_guest)
    monkeypatch.setattr(app_mod, "GenAIStudio", lambda *a, **k: FakeWriter())
    monkeypatch.setattr(app_mod, "_keyring", lambda: None)
    settings = StudyDeckSettings(
        browser_mode=True, session_secret="test-secret",
        brightspace=Settings(state_dir=tmp_path, min_seconds_between_requests=0.0),
    )
    app = create_app(settings)
    app.state.guests = guests
    return app


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

    def __init__(self, close_tab=False, journey=None):
        """journey: the addresses a sign-in tab shows, one per check, after its first
        page, with whether Brightspace says the tab is signed in at each."""
        self.close_tab = close_tab
        self.journey = list(journey or [("https://purdue.brightspace.com/d2l/home", True)])
        self.browsers: list = []
        self.fronted: list = []
        self.cleared: list = []
        self.whoami: list = []
        outer = self
        state = {"signed_in": False}

        self.files: dict = {}  # url -> bytes served
        self.requests: list = []

        class Resp:
            def __init__(self, status=200, body=b'{"ok": true}', head=False):
                self.status, self._body = status, body
                self.headers = {"content-length": str(len(body)), "content-type": "application/pdf"}
                if head:
                    self._body = b""

            async def text(self):
                return self._body.decode()

            async def body(self):
                return self._body

        class Request:
            async def get(self, url, **kw):
                return await self.fetch(url, method="GET", **kw)

            async def fetch(self, url, method="GET", **kw):
                outer.requests.append((method, url))
                if url.endswith("/users/whoami"):
                    outer.whoami.append(state["signed_in"])
                    return Resp(200 if state["signed_in"] else 403)
                if url in outer.files:
                    served = outer.files[url]
                    if isinstance(served, Exception):
                        raise served
                    return Resp(body=served, head=method == "HEAD")
                return Resp()

        class Page:
            def __init__(self, ctx):
                self.ctx, self.url, self.closed, self.started = ctx, "about:blank", False, False

            async def goto(self, url):
                self.url, self.started = url, True

            async def bring_to_front(self):
                outer.fronted.append(self.url)

            def is_closed(self):
                # The person in front of the sign-in tab moves on between checks.
                if self.started and not self.closed and self.ctx.pages[-1] is self:
                    if outer.close_tab:
                        self.closed = True
                        self.ctx.pages.remove(self)
                    elif outer.journey:
                        self.url, state["signed_in"] = outer.journey.pop(0)
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


async def _signed_in(c: httpx.AsyncClient) -> dict:
    for _ in range(100):
        bs = (await c.get("/api/me")).json()["brightspace"]
        if not bs["signing_in"]:
            return bs
        await asyncio.sleep(0.01)
    raise AssertionError("sign-in never finished")


async def test_players_on_phones_sign_in_on_the_host_computer(personal_app):
    host = client(personal_app)
    await host.get("/auth/brightspace/start")
    assert (await _signed_in(host))["connected"]
    key = (await host.get("/api/courses")).json()["courses"][0]["key"]
    code = (await host.post("/api/sessions", json={
        "name": "Ana", "strata": [{"course": key, "kind": "course"}], "types": ["mcq"], "deck_size": 4,
        "ai_permission_courses": [key], "genai_key": "k" * 20})).json()["code"]

    phone = httpx.AsyncClient(transport=httpx.ASGITransport(app=personal_app, client=("10.0.0.5", 5000)),
                              base_url="http://10.0.0.2:8000", headers=H)
    me = (await phone.get("/api/me")).json()["brightspace"]
    assert me["can_connect"] and me["signin_on_host"]
    # Someone on the network who isn't in a game can't open windows on the host's screen.
    r = await phone.get("/auth/brightspace/start")
    assert r.status_code == 303 and r.headers["location"] == "/#signin-join-first"
    assert personal_app.state.guests == []

    await phone.post(f"/api/sessions/{code}/join", json={"name": "Sam"})
    state = (await host.get(f"/api/sessions/{code}")).json()
    assert state["held_back"] == [{"course": "CHM 11500 Real Course", "waiting_on": ["Sam"]}]
    r = await phone.get("/auth/brightspace/start", params={"next": f"/#/game/{code}"})
    assert r.status_code == 303 and r.headers["location"] == f"/#/game/{code}"
    bs = await _signed_in(phone)
    assert bs["connected"] and bs["courses"] == 1
    # Sam got a private window of their own, opening on a page with their name.
    (guest,) = personal_app.state.guests
    assert guest is not personal_app.state.deck.window
    assert guest.intro_url.startswith("http://127.0.0.1:8000/signin-for/")
    # Now every player is verified, so the course's Brightspace material is used.
    assert (await host.get(f"/api/sessions/{code}")).json()["held_back"] == []
    # Disconnecting closes Sam's window only.
    await phone.post("/api/brightspace/disconnect")
    assert guest.closed and not personal_app.state.deck.window.closed


async def test_sign_in_intro_page_names_the_player_and_is_local_only(personal_app):
    import time as time_mod

    deck = personal_app.state.deck
    deck.intros["tok"] = ("<Sam>", time_mod.time() + 60)
    r = await client(personal_app).get("/signin-for/tok")
    assert r.status_code == 200 and "Sign-in for &lt;Sam&gt;" in r.text and "<Sam>" not in r.text
    assert "https://purdue.brightspace.com/d2l/home" in r.text
    remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=personal_app, client=("10.0.0.5", 5000)),
                               base_url="http://test", headers=H)
    assert (await remote.get("/signin-for/tok")).status_code == 404
    assert (await client(personal_app).get("/signin-for/nope")).status_code == 404


async def test_guest_sign_in_uses_its_own_private_window(monkeypatch):
    import playwright.async_api
    from study_deck.brightspace import BrowserTransport, GuestTransport

    pw = _FakePlaywright()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: pw)
    window = BrowserTransport("https://purdue.brightspace.com", "http://127.0.0.1:8000")
    await window.show_app()
    guest = GuestTransport(window)
    await guest.sign_in(intro_url="http://127.0.0.1:8000/signin-for/t")
    (browser,) = pw.browsers  # same browser, a second context: a separate window with its own cookies
    host_ctx, guest_ctx = browser.contexts
    assert guest.signed_in and not window.signed_in
    assert [p.url for p in host_ctx.pages] == ["http://127.0.0.1:8000/"] and guest_ctx.pages == []
    assert await guest.get_json("https://purdue.brightspace.com/d2l/api/x") == {"ok": True}
    await guest.close()
    assert not guest.is_open and window.is_open
    await window.shutdown()


async def test_big_course_files_are_skipped_before_downloading(monkeypatch):
    import playwright.async_api
    from purdue_brightspace_mcp.client import BrightspaceHTTPError
    from study_deck.brightspace import BrowserTransport
    from study_deck.extract import UnsupportedFile

    pw = _FakePlaywright()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: pw)
    t = BrowserTransport("https://purdue.brightspace.com", "http://127.0.0.1:8000")
    await t.show_app()
    await t.sign_in()
    base = "https://purdue.brightspace.com/d2l/api/le/1.0/1/content/topics"
    pw.files = {f"{base}/1/file": b"x" * 2000, f"{base}/2/file": b"small",
                f"{base}/3/file": RuntimeError("Timeout 30000ms exceeded.\n - cookie: d2lSessionVal=SECRET")}
    pw.requests.clear()
    with pytest.raises(UnsupportedFile):
        await t.get_bytes(f"{base}/1/file", max_bytes=1000)
    assert pw.requests == [("HEAD", f"{base}/1/file")]  # never downloaded
    assert await t.get_bytes(f"{base}/2/file", max_bytes=1000) == (b"small", "application/pdf")
    # A failed request is a plain "skip this file" error that never carries the session cookies.
    with pytest.raises(BrightspaceHTTPError) as err:
        await t.get_bytes(f"{base}/3/file", max_bytes=1000)
    assert "SECRET" not in str(err.value) and err.value.__suppress_context__
    await t.shutdown()


async def test_sign_in_waits_until_brightspace_confirms_it(monkeypatch):
    """Clicking through to Brightspace shows /d2l/home for a moment before Purdue's
    login page; that must not count as signed in (the window used to close right there)."""
    import playwright.async_api
    from study_deck.brightspace import BrowserTransport, GuestTransport

    pw = _FakePlaywright(journey=[
        ("https://purdue.brightspace.com/d2l/home", False),  # on the way to the login page
        ("https://login.microsoftonline.com/common/oauth2/authorize", False),
        ("https://login.microsoftonline.com/common/SAS/ProcessAuth", False),
        ("https://purdue.brightspace.com/d2l/home", True),  # signed in, same address as before
    ])
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: pw)
    monkeypatch.setattr(BrowserTransport, "poll_seconds", 0.0)
    window = BrowserTransport("https://purdue.brightspace.com", "http://127.0.0.1:8000")
    await window.show_app()
    guest = GuestTransport(window)
    await guest.sign_in(intro_url="http://127.0.0.1:8000/signin-for/t")
    assert guest.signed_in and pw.whoami == [False, True] and pw.journey == []
    await window.shutdown()


def test_personal_mode_needs_acknowledgement_and_local_url(monkeypatch):
    monkeypatch.delenv("STUDY_DECK_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED", raising=False)
    s = StudyDeckSettings.from_env()
    assert not s.browser_mode and not s.brightspace_enabled
    monkeypatch.setenv("STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED", "true")
    s = StudyDeckSettings.from_env()
    assert s.browser_mode and s.brightspace_enabled
    monkeypatch.setenv("STUDY_DECK_PUBLIC_URL", "https://deck.example.org")
    with pytest.raises(ValueError):
        StudyDeckSettings.from_env()


async def test_host_can_pick_a_custom_deck_size(app):
    host = client(app)
    keys = await connect_brightspace(host)
    assert (await host.get("/api/me")).json()["max_deck_size"] == 100

    def body(size, strata=1):
        topics = ["glycolysis", "ribosomes", "chloroplast"][:strata]
        return {"name": "Hana", "types": ["mcq"], "deck_size": size, "ai_permission_courses": [keys["BIOL"]],
                "genai_key": TEST_KEY,
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
        s = GameSession(code="ABCDEF", host_id="h", strata=[], types=["mcq"], deck_size=size, generator=FakeWriter())
        assert s.refill_threshold() == threshold


def test_max_deck_size_setting_is_capped(monkeypatch):
    monkeypatch.setenv("STUDY_DECK_MAX_DECK_SIZE", "5000")
    assert StudyDeckSettings.from_env().max_deck_size == 300
    monkeypatch.setenv("STUDY_DECK_MAX_DECK_SIZE", "40")
    assert StudyDeckSettings.from_env().max_deck_size == 40


def test_i_dont_know_counts_as_a_miss_and_reveals_the_answer():
    from study_deck.game import GameSession, Player

    s = GameSession(code="ABCDEF", host_id="h", strata=[], types=["fill_blank"], deck_size=4, generator=FakeWriter())
    s.players["h"] = Player("h", "Hana", is_host=True)
    s.current = Question("q1", "s1", "L", "fill_blank", "The _____ makes ATP.", "", answer="mitochondrion")
    out = s.submit("q1", "mitochondrion", skipped=True)  # a typed answer is ignored when skipping
    assert out["correct"] is False and out["expected"] == "mitochondrion"
    assert s.outcome == {"correct": False, "given": "", "skipped": True}
    assert s.players["h"].score == 0 and s.players["h"].answered == 0
    with pytest.raises(ValueError):
        s.submit("q1", "", skipped=True)


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
    # 10 new cards are asked for (the test writer may get fewer out of such short material).
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


async def test_material_added_during_play_comes_up_this_round():
    import random
    from study_deck.game import NEW_MATERIAL_CARDS, Chunk, GameSession, Stratum, make_chunks

    seen = []

    class Recording:
        name = "recording"

        async def generate(self, material, label, focus, types, count, avoid):
            seen.append(material)
            return [{"type": "fill_blank", "prompt": f"{label} {len(seen)}.{i} is _____ here.", "answer": "word",
                     "accept": [], "explanation": ""} for i in range(count)]

    strata = [Stratum(id=f"s{i}", course_label=f"C{i}", org_id=None, kind="course") for i in (1, 2)]
    s = GameSession(code="ABCDEF", host_id="h", strata=strata, types=["fill_blank"], deck_size=20,
                    generator=Recording(), rng=random.Random(3), status="ready")
    for st in strata:
        s.add_chunks([Chunk(text=f"Original material for {st.id}. " * 400, source="notes", contributor="h",
                            stratum_id=st.id)])
    await s.generate(20)
    for _ in range(6):
        s.draw()
    drawn, before = s.deck[: s.cursor], len(s.deck)

    upload = "Brand new material from a player. " * 20
    added = s.add_chunks(make_chunks(upload, source="upload", contributor="p", stratum_id="s2"))
    assert added and s.add_chunks(make_chunks(upload, source="upload", contributor="p", stratum_id="s2")) == []
    seen.clear()
    assert await s.write_from_new("s2", added) == NEW_MATERIAL_CARDS

    # Written from the new material only, and placed among the cards not yet drawn.
    assert len(seen) == 1 and "Brand new material" in seen[0] and "Original" not in seen[0]
    assert len(s.deck) == before + NEW_MATERIAL_CARDS and s.deck[: s.cursor] == drawn
    new = [q for q in s.deck[s.cursor:] if q.prompt.startswith("C2 · whole course 1.")]
    assert len(new) == NEW_MATERIAL_CARDS and all(q.stratum_id == "s2" for q in new)

    # Nothing is added once the game has ended.
    s.status = "ended"
    more = s.add_chunks(make_chunks("Even more new material from someone. " * 20, source="upload",
                                    contributor="p", stratum_id="s1"))
    assert await s.write_from_new("s1", more) == 0


async def test_upload_during_play_writes_cards_right_away(app):
    host, code, _ = await make_game(app)
    await host.post(f"/api/sessions/{code}/build")
    state = await wait_for(host, code, "ready")
    total = state["deck"]["total"]
    psy = next(s["id"] for s in state["strata"] if s["course"] == "PSY 1200")
    r = await host.post(f"/api/sessions/{code}/upload", data={"stratum": psy, "notes": PSY_NOTES, "attest": "yes"})
    assert r.status_code == 200 and r.json()["writing"] is True
    for _ in range(50):
        state = (await host.get(f"/api/sessions/{code}")).json()
        if state["deck"]["total"] > total:
            break
        await asyncio.sleep(0.1)
    assert state["deck"]["total"] > total and state["deck"]["remaining"] > total - 1


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
    keys = await connect_brightspace(host)
    mods = (await host.get(f"/api/courses/{keys['CS']}/modules")).json()["modules"]
    nested = [m for m in mods if m["depth"] == 1]
    assert [m["title"] for m in nested] == ["Lists, stacks and queues", "Hash tables and trees"]
    assert nested[0]["parent"] == next(m["id"] for m in mods if m["title"].startswith("Unit 3"))
    assert nested[1]["label"] == "Unit 3 · Data structures › Hash tables and trees"
    r = await host.post("/api/sessions", json={
        "name": "Ana", "strata": [{"course": keys["CS"], "kind": "module", "modules": [nested[1]["id"]]}],
        "types": ["fill_blank"], "deck_size": 4, "ai_permission_courses": [keys["CS"]], "genai_key": TEST_KEY})
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
    seen = {}

    async def choose_files(self, course, outline, sections, limit):
        seen.update(course=course, outline=outline, sections=sections, limit=limit)
        return '<think>{"x": 1}</think>```json\n{"folders": [132], "files": ["file 1301"]}\n```'

    monkeypatch.setattr(FakeWriter, "choose_files", choose_files, raising=False)
    host = client(app)
    keys = await connect_brightspace(host)
    r = await host.post("/api/sessions", json={
        "name": "Ana", "strata": [{"course": keys["CS"], "kind": "topic", "topic": "binary search trees"}],
        "types": ["fill_blank"], "deck_size": 3, "ai_permission_courses": [keys["CS"]], "genai_key": TEST_KEY})
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
    assert (await pick_files(FakeWriter(), "CHM 115", scope, ["the whole course"], [], 30)).how == "all"
    pick = await pick_files(FakeWriter(), "CHM 115", scope, ["the whole course"], [], 3)
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
    assert r.status_code == 409 and "Enter the answer for this card" in r.json()["error"]
    assert (await host.get(f"/api/sessions/{code}")).json()["card"]["id"] == first["id"]  # still the same card
    await host.post(f"/api/sessions/{code}/answer", json={"question_id": first["id"], "answer": "", "skip": True})
    r = await host.post(f"/api/sessions/{code}/draw")
    assert r.status_code == 200 and r.json()["card"]["id"] != first["id"]


# ------------------------------------------------------------- the shipped app


async def test_no_sample_courses_without_brightspace(bare_app):
    c = client(bare_app)
    me = (await c.get("/api/me")).json()
    assert me["brightspace"]["available"] is False and me["brightspace"]["can_connect"] is False
    assert "demo_mode" not in me
    r = await c.get("/auth/brightspace/start")
    assert r.status_code == 303 and r.headers["location"] == "/#signin-unavailable"
    assert (await c.get("/api/courses")).json() == {"courses": []}


async def test_a_game_needs_a_genai_key(bare_app):
    c = client(bare_app)
    body = {"name": "Hana", "strata": [{"manual_course": "PSY 1200", "kind": "course"}], "types": ["mcq"],
            "deck_size": 6}
    r = await c.post("/api/sessions", json=body)
    assert r.status_code == 400 and "GenAI Studio API key" in r.json()["error"]
    r = await c.post("/api/sessions", json={**body, "genai_key": TEST_KEY})
    assert r.status_code == 200


def _boilerexams_mock(calls: list[str] | None = None, down: bool = False):
    """Boilerexams' API as seen on boilerexams.com: a course list, topics, and questions."""
    from study_deck.boilerexams import Boilerexams

    def mc(qid, body, options, answer):
        return {"id": qid, "type": "MULTIPLE_CHOICE", "children": [],
                "data": {"body": body, "solution": [answer],
                         "answerChoices": [{"index": i, "body": o} for i, o in enumerate(options)]}}

    questions = {
        "q1": mc("q1", r"The domain of $f(x)=\sqrt{x-2}$ is", ["$[2,\\infty)$", "$(2,\\infty)$", "$(-\\infty,2]$", "all reals"], 0),
        "q2": mc("q2", "Which graph shown below is increasing?", ["A", "B", "C", "D"], 1),  # needs a picture
        "q3": mc("q3", r"Find $\lim_{x\to 0} \frac{\sin x}{x}$.", ["$0$", "$1$", "does not exist", "$\\infty$"], 1),
        "q4": mc("q4", "If $f'(c)=0$ and $f''(c)>0$, then $f$ has", ["a local minimum at c", "a local maximum at c",
                                                                  "an inflection point at c", "no conclusion"], 0),
    }
    topics = {"topics": [
        {"name": "Domains", "questions": [{"id": "q1"}, {"id": "q2"}]},
        {"name": "Limits", "questions": [{"id": "q3"}]},
        {"name": "Maximums and Minimums", "questions": [{"id": "q4"}]},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if down:
            return httpx.Response(503)
        path = request.url.path.removeprefix("/v3")
        if path == "/courses/subjects":
            return httpx.Response(200, json=[{"subject": "Mathematics", "courses": [
                {"abbreviation": "MA", "number": 16100, "flags": {"published": True}}]}])
        if path == "/courses/MA16100/topics/overview":
            return httpx.Response(200, json=topics)
        if path.startswith("/questions/") and path.split("/")[-1] in questions:
            return httpx.Response(200, json=questions[path.split("/")[-1]])
        return httpx.Response(404, text="404 page not found")

    return Boilerexams(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), pause=0)


def test_questions_that_need_a_picture_are_left_out():
    from study_deck.boilerexams import _example, _plain

    def q(body, options=("one", "two", "three", "four")):
        return {"type": "MULTIPLE_CHOICE", "children": [],
                "data": {"body": body, "solution": [0], "answerChoices": [{"index": i, "body": o} for i, o in enumerate(options)]}}

    for body in ("Write the preorder traversal of this tree.", "Which graph shown below is increasing?",
                 "How many sigma bonds are in the following molecule?", "Using the table above, find x.",
                 "What is the current in the circuit?\n![](https://example.com/c.png)"):
        assert _example(q(body), "t") is None, body
    assert _example(q("Pick the right letter.", ("A", "B", "C", "D")), "t") is None
    assert _example(q("Show the array after the operations.", (r"$\[ \]$", "x y", r"$\begin{array}{c} a \end{array}$", "z w")), "t") is None
    assert _example(q("Which of the following is the correct formula for zinc hydroxide?"), "t") is not None
    assert _plain(r"Is \texttt{4 3 5} possible? \[ 15.060 \times 1.62= \] \quad $x$") == r"Is 4 3 5 possible? 15.060 \times 1.62= x"


def test_course_codes_are_read_from_course_names():
    from study_deck.boilerexams import course_codes

    assert course_codes("Fall 2026 MA 16100-LEC-001") == ["MA16100"]
    assert course_codes("ma 161 calculus") == ["MA16100"]  # old three-digit number
    assert course_codes("phys 17200") == ["PHYS17200"]
    assert course_codes("CHM11510 · General Chemistry") == ["CHM11510"]
    assert course_codes("PSY 1200") == []  # not a Purdue number


async def test_boilerexams_examples_are_text_only_and_follow_the_section():
    calls: list[str] = []
    bx = _boilerexams_mock(calls)
    assert await bx.match("Spring 2027 MA 16100-LEC") == "MA16100"
    assert await bx.match("CS 18000") is None
    text, n = await bx.style_text("MA16100", ["Maximums and minimums"])
    # The matching topic comes first; the question that needs a picture is left out.
    assert n == 3 and text.index("local minimum") < text.index("domain")
    assert "graph shown below" not in text and "$" not in text and "Correct: A" in text
    # Questions are cached: asking again reads nothing new.
    before = len(calls)
    await bx.style_text("MA16100", [])
    assert len(calls) == before
    await bx.close()


@pytest.mark.parametrize("failure", ["timeout", 503, 429, 404])
async def test_boilerexams_course_lookup_recovers_from_transient_failures(failure):
    from study_deck.boilerexams import Boilerexams

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("Temporary timeout", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json=[{"courses": [{"abbreviation": "PHYS", "number": 17200}]}])

    bx = Boilerexams(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), pause=0)
    try:
        if failure == 404:
            with pytest.raises(httpx.HTTPStatusError):
                await bx.match("phys 17200")
            assert len(calls) == 1
        else:
            assert await bx.match("phys 17200") == "PHYS17200"
            assert len(calls) == 2
            assert await bx.match("PHYS 172") == "PHYS17200"
            assert len(calls) == 2  # successful recovery is cached
    finally:
        await bx.close()


async def test_cards_are_styled_on_boilerexams_for_a_matching_course(bare_app):
    deck = bare_app.state.deck
    deck.boilerexams = _boilerexams_mock()
    host = client(bare_app)
    code = (await host.post("/api/sessions", json={
        "name": "Hana", "strata": [{"manual_course": "MA 16100", "kind": "course"},
                                   {"manual_course": "PSY 1200", "kind": "course"}],
        "types": ["mcq"], "deck_size": 4, "genai_key": TEST_KEY})).json()["code"]
    state = (await host.get(f"/api/sessions/{code}")).json()
    for st in state["strata"]:
        await host.post(f"/api/sessions/{code}/upload",
                        data={"stratum": st["id"], "notes": f"{st['label']}. {PSY_NOTES}", "attest": "yes"})
    await host.post(f"/api/sessions/{code}/build")
    state = await wait_for(host, code, "ready")
    styles = {st["label"]: st["boilerexams"] for st in state["strata"]}
    assert styles == {"MA 16100 · whole course": {"course": "MA16100", "examples": 3, "cards": 1},
                      "PSY 1200 · whole course": None}
    session = deck.store.sessions[code]
    seen = [e for e in session.generator.examples_seen if e]
    assert seen and all("local minimum" in e for e in seen)  # only the MA 16100 section's batches
    assert None in session.generator.examples_seen  # PSY 1200 is written in the usual style
    # One MA 16100 card is a real past exam question, written out readably and labeled.
    (past,) = [q for q in session.deck if q.source]
    assert past.source == "Past exam" and past.stratum_id == "s1" and "$" not in past.prompt
    assert past.prompt in {"The domain of f(x)=√(x-2) is", "Find lim(x→ 0) (sin x)/x.",
                           "If f'(c)=0 and f''(c)>0, then f has"}
    assert "Boilerexams" in past.explanation and past.public()["source"] == "Past exam"


async def test_boilerexams_can_be_turned_off_or_be_down(bare_app):
    deck = bare_app.state.deck
    host = client(bare_app)

    async def play(**extra):
        code = (await host.post("/api/sessions", json={
            "name": "Hana", "strata": [{"manual_course": "MA 16100", "kind": "course"}],
            "types": ["mcq"], "deck_size": 2, "genai_key": TEST_KEY, **extra})).json()["code"]
        st = (await host.get(f"/api/sessions/{code}")).json()["strata"][0]
        await host.post(f"/api/sessions/{code}/upload", data={"stratum": st["id"], "notes": PSY_NOTES, "attest": "yes"})
        await host.post(f"/api/sessions/{code}/build")
        return await wait_for(host, code, "ready"), deck.store.sessions[code]

    calls: list[str] = []
    deck.boilerexams = _boilerexams_mock(calls)
    state, session = await play(boilerexams=False)
    assert calls == [] and state["strata"][0]["boilerexams"] is None
    deck.boilerexams = _boilerexams_mock(down=True)
    state, session = await play()  # the deck is still built, in the usual style, and nothing is said
    assert state["strata"][0]["boilerexams"] is None and set(session.generator.examples_seen) == {None}
    assert not any("Boilerexams" in n for n in state["notices"]) and not any(q.source for q in session.deck)
    # Fill in the blank only: past exam questions (multiple choice) aren't dealt, only their style is used.
    deck.boilerexams = _boilerexams_mock()
    code = (await host.post("/api/sessions", json={
        "name": "Hana", "strata": [{"manual_course": "MA 16100", "kind": "course"}],
        "types": ["fill_blank"], "deck_size": 3, "genai_key": TEST_KEY})).json()["code"]
    st = (await host.get(f"/api/sessions/{code}")).json()["strata"][0]
    await host.post(f"/api/sessions/{code}/upload", data={"stratum": st["id"], "notes": PSY_NOTES, "attest": "yes"})
    await host.post(f"/api/sessions/{code}/build")
    state = await wait_for(host, code, "ready")
    assert state["strata"][0]["boilerexams"] == {"course": "MA16100", "examples": 3, "cards": 0}
    assert not any(q.source for q in deck.store.sessions[code].deck)


async def test_past_exam_cards_stay_about_a_third_even_when_material_runs_low():
    from study_deck.game import GameSession, Stratum, make_chunks

    class Short(FakeWriter):  # the AI manages only one card per section each time
        async def generate(self, *args, **kwargs):
            return (await super().generate(*args, **kwargs))[:1]

    async def past(stratum, want, skip):
        return [{"qid": f"q{len(skip) + i}", "type": "mcq", "prompt": f"Past {len(skip) + i}?",
                 "options": ["a", "b", "c", "d"], "answer_index": 0, "keep_order": True} for i in range(want)]

    # Short writes 4 of 10 cards (one per try), so at most 2 past exam cards join them, not 3.
    for writer, total, most in ((FakeWriter(seed=1), 6, 2), (Short(), 10, 2)):
        s = GameSession(code="X", host_id="h", strata=[Stratum("s1", "MA 161", None, "topic", topic="memory")],
                        types=["mcq"], deck_size=total, generator=writer)
        s.add_chunks(make_chunks(PSY_NOTES, source="notes", contributor="h", stratum_id="s1"))
        s.past_exams = past
        await s.generate(total)
        past_cards = [q for q in s.deck if q.source]
        assert 1 <= len(past_cards) <= most and len(past_cards) * 2 <= len(s.deck)


async def test_past_exam_cards_only_come_from_matching_topics():
    bx = _boilerexams_mock()
    cards = await bx.cards("MA16100", ["Maximums and minimums"], 3, set())
    assert [c.card_prompt for c in cards] == ["If f'(c)=0 and f''(c)>0, then f has"]
    assert await bx.cards("MA16100", ["Integration by parts"], 3, set()) == []  # no topic fits
    assert len(await bx.cards("MA16100", [], 5, set())) == 3  # whole course: any text-only question
    assert await bx.cards("MA16100", [], 5, {"q1", "q3", "q4"}) == []  # already used
    await bx.close()


@pytest.mark.parametrize("types,topic", [(["mcq"], None), (["fill_blank"], "Maximums and minimums")])
async def test_boilerexams_only_build_and_refill_without_brightspace_or_uploads(bare_app, types, topic):
    from study_deck.genai import BOILEREXAMS_PROMPT, SOURCE_LINES

    deck = bare_app.state.deck
    deck.boilerexams = _boilerexams_mock()
    host = client(bare_app)
    assert (await host.get("/api/courses")).json() == {"courses": []}
    assert (await host.get("/api/boilerexams")).json()["courses"] == ["MA16100"]
    selection = {"manual_course": "MA 16100", "kind": "topic" if topic else "course", "topic": topic}
    response = await host.post("/api/sessions", json={
        "name": "Hana", "strata": [selection], "types": types, "deck_size": 6, "genai_key": TEST_KEY})
    assert response.status_code == 200
    code = response.json()["code"]
    session = deck.store.sessions[code]
    requests = []

    def answer(request):
        body = json.loads(request.content)
        requests.append(body)
        import re
        count = int(re.search(r"Write (\d+) questions", body["messages"][1]["content"])[1])
        questions = []
        for i in range(count):
            n = f"{len(requests)}-{i}"
            q = ({"type": "mcq", "prompt": f"Practice variant {n}: which value is the minimum?",
                  "options": ["negative two", "zero", "positive two", "positive four"], "answer_index": 0}
                 if types == ["mcq"] else
                 {"type": "fill_blank", "prompt": f"Practice variant {n}: positive curvature at a stationary point indicates a local _____.",
                  "answer": "minimum", "accept": []})
            questions.append(q)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"questions": questions})}}]})

    session.generator = GenAIStudio("https://genai.rcac.purdue.edu/api/chat/completions", "test", "fake",
                                   http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    await host.post(f"/api/sessions/{code}/build")
    state = await wait_for(host, code, "ready")
    assert not session.chunks and not state["has_brightspace"]
    assert len(session.deck) == 6
    assert state["strata"][0]["boilerexams"]["material"] is True
    assert state["strata"][0]["counts"] == {"brightspace": 0, "upload": 0, "notes": 0}
    assert requests and all(r["messages"][0]["content"] == BOILEREXAMS_PROMPT for r in requests)
    prompt = requests[0]["messages"][1]["content"]
    assert SOURCE_LINES["boilerexams"] in prompt and "Correct: A — a local minimum at c" in prompt
    assert "style guide only" not in prompt
    if topic:
        assert "domain of" not in prompt and "Find lim" not in prompt  # no unrelated topic fallback
    real = [q for q in session.deck if q.source.startswith("Past exam")]
    ai = [q for q in session.deck if q.source == "AI practice · based on Boilerexams"]
    assert ai and all(q.public()["source"] == q.source for q in ai)
    assert bool(real) == (types == ["mcq"])
    before = len(session.deck)
    await session.generate(3)
    assert len(session.deck) > before and len(session.past_used) == session.past_cards.get("s1", 0)
    assert len({q.prompt for q in session.deck}) == len(session.deck)
    # Uploading material later switches AI requests back to that material and the normal prompt.
    from study_deck.game import make_chunks
    from study_deck.genai import SYSTEM_PROMPT
    session.add_chunks(make_chunks(PSY_NOTES, source="notes", contributor=session.host_id, stratum_id="s1"))
    await session.generate(2)
    assert requests[-1]["messages"][0]["content"] == SYSTEM_PROMPT
    assert PSY_NOTES in requests[-1]["messages"][1]["content"]
    await session.generator.close()
    await deck.boilerexams.close()


@pytest.mark.parametrize("mode", ["disabled", "down", "unsupported", "unmatched_topic"])
async def test_boilerexams_only_missing_source_does_not_generate_from_unrelated_examples(bare_app, mode):
    deck = bare_app.state.deck
    calls = []
    deck.boilerexams = _boilerexams_mock(calls, down=mode == "down")
    host = client(bare_app)
    code = (await host.post("/api/sessions", json={
        "name": "Hana", "strata": [{"manual_course": "PHYS 21400" if mode == "unsupported" else "MA 16100",
                                    "kind": "topic" if mode == "unmatched_topic" else "course",
                                    "topic": "Integration by parts"}],
        "types": ["mcq"], "deck_size": 6, "genai_key": TEST_KEY,
        "boilerexams": mode != "disabled"})).json()["code"]
    await host.post(f"/api/sessions/{code}/build")
    state = await wait_for(host, code, "lobby")
    session = deck.store.sessions[code]
    assert not session.deck and not session.exam_material and not session.generator.examples_seen
    assert any("No study material found" in n for n in state["notices"])
    if mode == "disabled":
        assert calls == []
    await deck.boilerexams.close()


def test_math_is_written_out_readably_on_cards():
    from study_deck.boilerexams import readable

    assert readable(r"$\frac{2+e}{e^{2}}$") == "(2+e)/e²"
    assert readable(r"$\sqrt{x-2}-\frac{1}{\sqrt{3-x}}$") == "√(x-2)-1/√(3-x)"
    assert readable(r"$f^{\prime \prime}(x)=15 x^{4}$") == "f′′(x)=15 x⁴"
    assert readable(r"Acetone is $\left(\mathrm{CH}_{3}\right)_{2} \mathrm{CO}$.") == "Acetone is (CH₃)₂ CO."
    assert readable(r"$[2, \infty)$ and $T(n)=O(n\log{(n)})$") == "[2, ∞) and T(n)=O(n log(n))"
    assert readable(r"It costs \$5, 20\% more") == "It costs $5, 20% more"
    assert readable(r"$\begin{array}{c} a \end{array}$") is None  # can't be written out: not a card
    assert readable(r"$\overrightarrow{AB}$") is None


async def test_host_can_remove_a_player(bare_app):
    host = client(bare_app)
    code = (await host.post("/api/sessions", json={
        "name": "Hana", "strata": [{"manual_course": "PSY 1200", "kind": "course"}], "types": ["fill_blank"],
        "deck_size": 4, "genai_key": TEST_KEY})).json()["code"]
    phones = [httpx.AsyncClient(transport=httpx.ASGITransport(app=bare_app, client=(f"192.168.1.{n}", 5000)),
                                base_url="http://192.168.1.10:8000", headers=H) for n in (21, 22)]
    for phone, name in zip(phones, ("Sam", "Lee")):
        await phone.post(f"/api/sessions/{code}/join", json={"name": name})
    sam, lee = phones
    state = (await sam.get(f"/api/sessions/{code}")).json()
    keys = {p["name"]: p["key"] for p in state["players"]}
    await sam.post(f"/api/sessions/{code}/upload",
                   data={"stratum": state["strata"][0]["id"], "notes": PSY_NOTES, "attest": "yes"})
    session = bare_app.state.deck.store.sessions[code]
    assert len(session.chunks) > 0
    # Only the host can remove players, and never themselves.
    assert (await lee.post(f"/api/sessions/{code}/kick", json={"player": keys["Sam"]})).status_code == 403
    assert (await host.post(f"/api/sessions/{code}/kick", json={"player": keys["Hana"]})).status_code == 404
    r = await host.post(f"/api/sessions/{code}/kick", json={"player": keys["Sam"]})
    assert [p["name"] for p in r.json()["players"]] == ["Hana", "Lee"]
    assert session.chunks == []  # Sam's notes left with them
    # Sam can't see the game or join it again; Lee is unaffected.
    r = await sam.get(f"/api/sessions/{code}")
    assert r.status_code == 403 and r.json()["error"] == "The host removed you from this game."
    assert (await sam.post(f"/api/sessions/{code}/join", json={"name": "Sam"})).status_code == 403
    assert (await lee.get(f"/api/sessions/{code}")).status_code == 200


def test_removing_a_player_does_not_change_the_current_card_or_assign_answers():
    from study_deck.game import GameSession, Player

    s = GameSession(code="ABCDEF", host_id="h", strata=[], types=["fill_blank"], deck_size=4, generator=FakeWriter())
    for pid, name in (("h", "Hana"), ("s", "Sam"), ("l", "Lee")):
        s.players[pid] = Player(pid, name, is_host=pid == "h")
    s.deck = [Question(f"q{i}", "s1", "L", "fill_blank", "The _____ makes ATP.", "", answer="mitochondrion")
              for i in range(5)]
    s.draw()
    s.submit("q0", "mitochondrion")
    s.draw()
    s.remove_player("s", kicked=True)
    assert s.current.id == "q1" and s.outcome is None and "s" in s.kicked
    s.submit("q1", "nope")
    s.draw()
    assert s.current.id == "q2" and s.outcome is None
    assert all(p.score == 0 and p.answered == 0 for p in s.players.values())


async def test_files_added_in_several_goes_all_count(bare_app):
    """The page sends a big pick (a whole folder) a few files at a time; every batch adds up."""
    host = client(bare_app)
    code = (await host.post("/api/sessions", json={
        "name": "Hana", "strata": [{"manual_course": "PSY 1200", "kind": "course"}], "types": ["fill_blank"],
        "deck_size": 4, "genai_key": TEST_KEY})).json()["code"]
    state = (await host.get(f"/api/sessions/{code}")).json()
    assert state["me"]["uploads"] == 0 and state["me"]["max_uploads"] == 60
    stratum = state["strata"][0]["id"]
    for batch in (["week1.txt", "week2.txt"], ["week3.txt"]):
        files = [("files", (name, f"{name}: {PSY_NOTES}".encode(), "text/plain")) for name in batch]
        r = await host.post(f"/api/sessions/{code}/upload", data={"stratum": stratum, "attest": "yes"}, files=files)
        assert r.status_code == 200 and r.json()["added"] > 0, r.text
    state = (await host.get(f"/api/sessions/{code}")).json()
    assert state["me"]["uploads"] == 3 and state["players"][0]["uploads"] == 3


async def test_uploaded_notes_only_game_for_several_players(bare_app):
    host = client(bare_app)
    code = (await host.post("/api/sessions", json={
        "name": "Hana", "strata": [{"manual_course": "PSY 1200", "kind": "course"}], "types": ["fill_blank"],
        "deck_size": 4, "genai_key": TEST_KEY})).json()["code"]
    phones = [httpx.AsyncClient(transport=httpx.ASGITransport(app=bare_app, client=(f"192.168.1.{n}", 5000)),
                                base_url="http://192.168.1.10:8000", headers=H) for n in (21, 22)]
    for phone, name in zip(phones, ("Sam", "Lee")):
        assert (await phone.post(f"/api/sessions/{code}/join", json={"name": name})).status_code == 200
    state = (await phones[0].get(f"/api/sessions/{code}")).json()
    r = await phones[0].post(f"/api/sessions/{code}/upload",
                             data={"stratum": state["strata"][0]["id"], "notes": PSY_NOTES, "attest": "yes"})
    assert r.status_code == 200
    await host.post(f"/api/sessions/{code}/build")
    await wait_for(host, code, "ready")
    card = (await host.post(f"/api/sessions/{code}/draw")).json()["card"]
    for phone in phones:
        seen = (await phone.get(f"/api/sessions/{code}")).json()
        assert seen["card"]["id"] == card["id"] and [p["name"] for p in seen["players"]] == ["Hana", "Sam", "Lee"]


async def test_join_address_and_qr_code(bare_app, monkeypatch):
    import study_deck.app as app_mod

    monkeypatch.setattr(app_mod, "lan_address", lambda: "192.168.1.10")
    # On the computer running it, players are pointed at its Wi-Fi address, not localhost.
    local = httpx.AsyncClient(transport=httpx.ASGITransport(app=bare_app), base_url="http://127.0.0.1:8000", headers=H)
    assert (await local.get("/api/me")).json()["join_base"] == "http://192.168.1.10:8000"
    # A phone that's already on the network keeps the address it came in on.
    phone = httpx.AsyncClient(transport=httpx.ASGITransport(app=bare_app, client=("192.168.1.21", 5000)),
                              base_url="http://192.168.1.10:8000", headers=H)
    assert (await phone.get("/api/me")).json()["join_base"] == "http://192.168.1.10:8000"

    code = (await local.post("/api/sessions", json={
        "name": "Hana", "strata": [{"manual_course": "PSY 1200", "kind": "course"}], "types": ["mcq"],
        "deck_size": 4, "genai_key": TEST_KEY})).json()["code"]
    r = await local.get(f"/api/sessions/{code}/qr.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and b"<svg" in r.content
    # Only players in the game get its QR code.
    assert (await phone.get(f"/api/sessions/{code}/qr.svg")).status_code == 403


async def test_join_address_stays_local_when_the_server_is_private(tmp_path, monkeypatch):
    import study_deck.app as app_mod

    monkeypatch.setattr(app_mod, "lan_address", lambda: "192.168.1.10")
    app = create_app(StudyDeckSettings(host="127.0.0.1", session_secret="x",
                                       brightspace=Settings(state_dir=tmp_path)))
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000", headers=H)
    me = (await c.get("/api/me")).json()
    assert me["join_base"] == "http://127.0.0.1:8000" and me["on_network"] is False


def test_server_listens_on_the_network_by_default(monkeypatch):
    for name in ("STUDY_DECK_HOST", "STUDY_DECK_PORT", "PORT", "STUDY_DECK_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    s = StudyDeckSettings.from_env()
    assert (s.host, s.port, s.public_url) == ("0.0.0.0", 8000, "http://127.0.0.1:8000")
    monkeypatch.setenv("STUDY_DECK_PORT", "9000")
    monkeypatch.setenv("STUDY_DECK_HOST", "127.0.0.1")
    s = StudyDeckSettings.from_env()
    assert (s.host, s.port, s.public_url) == ("127.0.0.1", 9000, "http://127.0.0.1:9000")
