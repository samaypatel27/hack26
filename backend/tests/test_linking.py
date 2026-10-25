"""Tests for backend/linking.py (docs/linking.md). No real API calls: OpenAI and Anthropic are fakes.

Part 1 needs nothing. Part 2 needs TEST_DATABASE_URL pointing at a throwaway Postgres WITH pgvector
(for example the pgvector/pgvector:pg16 Docker image); without the extension those tests skip.

    docker run --rm -d --name bp-linking-test-pg -p 55433:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
    $env:TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55433/postgres"
    .venv\\Scripts\\python -m pytest tests -q
    docker stop bp-linking-test-pg

Seed posts (conftest.py): 1 Prisma/Alpine solved (fingerprint 51749bd5), 2 ERR_REQUIRE_ESM solved,
3 pytest/conftest OPEN (78b50510), 4 docker-compose Postgres "connection refused" solved (d25c0b1e).
"""

import json
import math
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import linking

SCHEMA_LINKING = Path(__file__).resolve().parents[1] / "schema_linking.sql"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

# Each topic is one direction in the fake embedding space, so "same topic" = high cosine.
TOPICS = [
    ("prisma", "query engine", "orm binary", "musl"),
    ("err_require_esm", "es module", "esm"),
    ("conftest", "pytest", "no module named"),
    ("5432", "connection refused", "postgres"),
    ("rounding", "penny", "cent", "discount"),
]


def fake_vector(text: str) -> list[float]:
    low = text.lower()
    v = [0.0] * linking.EMBED_DIMS
    for i, words in enumerate(TOPICS):
        v[i] = float(sum(w in low for w in words))
    v[10] = 0.3   # a small shared part, so no vector is all zeros
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class FakeOpenAI:
    def __init__(self, delay: float = 0.0, dims: int = linking.EMBED_DIMS, fail: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.delay, self.dims, self.fail = delay, dims, fail
        self.embeddings = SimpleNamespace(create=self._create)

    def with_options(self, **kw: Any) -> "FakeOpenAI":
        self.options.append(kw)
        return self

    def _create(self, **params: Any) -> Any:
        self.calls.append(params)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        texts = params["input"]
        data = [SimpleNamespace(index=i, embedding=fake_vector(t)[:self.dims]) for i, t in enumerate(texts)]
        return SimpleNamespace(data=data, usage=SimpleNamespace(total_tokens=10 * len(texts)))


class FakeAnthropic:
    """answer(params) -> dict for the JSON reply, or raises. Records every request."""

    def __init__(self, answer=None, delay: float = 0.0, text: str | None = None, stop_reason: str = "end_turn"):
        self.calls: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.answer = answer or (lambda p: {"same_problem": True, "fix_applies": "yes", "confidence": 0.9,
                                            "reason": "same root cause"})
        self.delay, self.text, self.stop_reason = delay, text, stop_reason
        self.messages = SimpleNamespace(create=self._create)

    def with_options(self, **kw: Any) -> "FakeAnthropic":
        self.options.append(kw)
        return self

    def _create(self, **params: Any) -> Any:
        self.calls.append(params)
        if self.delay:
            time.sleep(self.delay)
        out = self.text if self.text is not None else json.dumps(self.answer(params))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=out)], stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


def existing_id(params: dict[str, Any]) -> int:
    text = params["messages"][0]["content"]
    return int(text.split('<existing_report id="')[1].split('"')[0])


def post(pid: int, description: str = "x", solution: str | None = "fix", **kw: Any) -> dict[str, Any]:
    return {"id": pid, "description": description, "solution": solution, "post_votes": 0, "tags": [], **kw}


@pytest.fixture(autouse=True)
def clean_linking():
    linking.reset()
    yield
    linking.reset()


# ---------------------------------------------------------------------------
# Part 1: no database
# ---------------------------------------------------------------------------

def test_meaning_score_scale():
    assert linking.meaning_score(0.40) == 0.0
    assert linking.meaning_score(0.55) == pytest.approx(0.2)
    assert linking.meaning_score(0.75) == pytest.approx(0.5)
    assert linking.meaning_score(1.0) == pytest.approx(1.0)
    assert 0.2 < linking.meaning_score(0.65) < 0.5


def test_merge_rule():
    m = linking.Match(post(1), ["words"], {"words": 0.42})
    assert linking.combined_score(m) == 0.42
    m = linking.Match(post(1), ["meaning"], {"meaning": 0.75})
    assert linking.combined_score(m) == 0.5
    m = linking.Match(post(1), ["words", "meaning"], {"words": 0.3, "meaning": 0.75})
    assert linking.combined_score(m) == 0.6                       # best (0.5) + agreement bonus
    m = linking.Match(post(1), ["words", "meaning"], {"words": 0.95, "meaning": 0.99})
    assert linking.combined_score(m) == 0.99                      # 1.0 is only for a fingerprint
    m = linking.Match(post(1), ["fingerprint"], {"words": 0.0})
    assert linking.combined_score(m) == 1.0
    m = linking.Match(post(1), ["words"], {"words": 0.3, "meaning": 0.5})   # weak meaning: no stage, no bonus
    assert linking.combined_score(m) == 0.3


def test_problem_text_has_errors_and_tags_but_never_the_solution():
    p = {"description": "Prisma crashes in Docker\n\nIt says:\nPrismaClientInitializationError: could not locate\nok",
         "solution": "SECRET FIX binaryTargets", "tags": ["prisma", "docker"]}
    text = linking.problem_text(p)
    assert text.startswith("Prisma crashes in Docker")
    assert "Key errors:\nPrismaClientInitializationError" in text and "Tags: prisma, docker" in text
    assert "SECRET FIX" not in text


def test_embed_query_cache_and_shape(monkeypatch):
    fake = FakeOpenAI()
    linking.set_clients(openai=fake)
    v1 = linking._embed_query_now("same text", 1.0)
    v2 = linking._embed_query_now("same text", 1.0)
    assert v1 == v2 and len(v1) == linking.EMBED_DIMS and len(fake.calls) == 1   # second one from the cache
    assert fake.calls[0]["model"] == "text-embedding-3-small" and fake.options[0]["max_retries"] == 0
    linking.set_clients(openai=FakeOpenAI(dims=10))
    assert linking.embed(["x"], 1.0) is None                       # wrong length: skipped, not stored
    linking.set_clients(openai=FakeOpenAI(fail=True))
    assert linking.embed(["x"], 1.0) is None


def test_slow_query_embedding_is_skipped_in_time():
    linking.set_clients(openai=FakeOpenAI(delay=1.5))
    start = time.monotonic()
    future = linking.start_query_embedding("anything at all")
    assert linking.wait_for(future, time.monotonic() + 0.2) is None
    assert time.monotonic() - start < 0.6


def test_no_openai_key_means_no_stage_3(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert linking.openai_client() is None
    assert linking.start_query_embedding("text") is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("LINKING_EMBEDDINGS", "0")
    assert linking.openai_client() is None


def test_ai_check_request_shape_and_cost():
    fake = FakeAnthropic()
    verdict = linking.check_one("new problem", post(3, "old problem", None), "the fix", fake, fix_for="existing")
    assert verdict == {"same_problem": True, "fix_applies": True, "confidence": 0.9, "reason": "same root cause",
                       "cost_usd": pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6)}
    params = fake.calls[0]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(params)
    assert fake.options[0]["max_retries"] == 0 and fake.options[0]["timeout"] <= linking.CHECK_TIMEOUT_S
    assert "EXISTING report's problem" in params["messages"][0]["content"]


def test_ai_check_bad_answers_are_none():
    assert linking.check_one("a", post(1), None, FakeAnthropic(text="not json")) is None
    assert linking.check_one("a", post(1), None, FakeAnthropic(stop_reason="refusal")) is None
    assert linking.check_one("a", post(1), None, FakeAnthropic(answer=lambda p: {"same_problem": True})) is None

    def boom(params):
        raise RuntimeError("API down")
    assert linking.check_one("a", post(1), None, FakeAnthropic(answer=boom)) is None


def test_ai_check_runs_only_on_top_candidates():
    fake = FakeAnthropic()
    linking.set_clients(anthropic=fake)
    matches = [linking.Match(post(i), ["words"], {"words": s}, score=s)
               for i, s in enumerate([0.9, 0.8, 0.7, 0.6, 0.5, 0.25], start=1)]
    linking.check_matches("new problem", matches)
    assert sorted(existing_id(c) for c in fake.calls) == [1, 2, 3]       # CHECK_TOP_K = 3
    assert all(m.verdict for m in matches[:3]) and not any(m.verdict for m in matches[3:])
    fake.calls.clear()
    weak = [linking.Match(post(9), ["words"], {"words": 0.25}, score=0.25)]
    linking.check_matches("new problem", weak)
    assert fake.calls == []                                              # below CHECK_MIN_SCORE: not worth a call


def test_ai_check_timeout_falls_back(monkeypatch):
    monkeypatch.setattr(linking, "CHECK_TIMEOUT_S", 0.1)
    linking.set_clients(anthropic=FakeAnthropic(delay=2.0))
    matches = [linking.Match(post(1, solution=None), ["fingerprint"], score=1.0)]
    start = time.monotonic()
    linking.check_matches("new problem", matches, fix="a fix")
    assert time.monotonic() - start < 1.8 and matches[0].verdict is None
    chosen = linking.decide(matches, need_fix=True)
    assert chosen is matches[0] and chosen.decided_by == "fallback"


def test_decide_rules():
    def m(pid, stages, score, verdict=None):
        return linking.Match(post(pid), stages, score=score, verdict=verdict)

    yes = {"same_problem": True, "fix_applies": True, "confidence": 0.9, "reason": ""}
    no = {"same_problem": False, "fix_applies": None, "confidence": 0.9, "reason": "different cause"}
    unsure = {"same_problem": True, "fix_applies": True, "confidence": 0.4, "reason": ""}
    wont_fix = {"same_problem": True, "fix_applies": False, "confidence": 0.9, "reason": ""}

    assert linking.decide([m(1, ["meaning"], 0.4, yes)], need_fix=True).decided_by == "ai_check"
    assert linking.decide([m(1, ["fingerprint"], 1.0, no)], need_fix=True) is None     # "no" beats a fingerprint
    assert linking.decide([m(1, ["words"], 0.9, unsure)], need_fix=True) is None       # low confidence
    assert linking.decide([m(1, ["words"], 0.9, wont_fix)], need_fix=True) is None     # fix doesn't apply
    assert linking.decide([m(1, ["words"], 0.9, wont_fix)], need_fix=False) is not None
    assert linking.decide([m(1, ["fingerprint"], 1.0)], need_fix=True).decided_by == "fallback"
    assert linking.decide([m(1, ["words"], 0.85)], need_fix=True).decided_by == "fallback"
    assert linking.decide([m(1, ["words", "meaning"], 0.7)], need_fix=True) is None    # too weak without a check
    assert linking.decide([m(1, ["fingerprint"], 1.0, no), m(2, ["words"], 0.5, yes)], need_fix=True).id == 2


# ---------------------------------------------------------------------------
# Part 2: against a throwaway Postgres with pgvector
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def vector_schema(database: str):
    import psycopg

    with psycopg.connect(database, autocommit=True) as conn:
        try:
            conn.execute(SCHEMA_LINKING.read_text(encoding="utf-8"))
        except psycopg.Error as e:
            pytest.skip(f"pgvector not available in TEST_DATABASE_URL ({e.__class__.__name__}); "
                        "use the pgvector/pgvector:pg16 image")
    yield database
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS agent_post_embeddings")


@pytest.fixture()
def linked(vector_schema, client, sql):
    """The seeded posts, all embedded with the fake OpenAI client."""
    if not sql("SELECT to_regclass('agent_post_embeddings') AS t")[0]["t"]:   # a test dropped it
        sql(SCHEMA_LINKING.read_text(encoding="utf-8"))
    fake = FakeOpenAI()
    linking.set_clients(openai=fake, anthropic=None)
    for pid in (1, 2, 3, 4):
        assert linking.index_post(pid) is True
    fake.calls.clear()
    return SimpleNamespace(client=client, openai=fake, sql=sql)


def add_post(sql, description: str, solution: str | None, fingerprint: str | None = None) -> int:
    forum = sql("SELECT id FROM forums WHERE slug = 'general'")[0]["id"]
    pid = sql("INSERT INTO posts (description, value, solution, forum_id, status) VALUES (%s, 'v', %s, %s, %s)"
              " RETURNING id", (description, solution, forum, "open" if solution is None else "solved"))[0]["id"]
    if fingerprint:
        sql("INSERT INTO agent_post_meta (post_id, fingerprint) VALUES (%s, %s)", (pid, fingerprint))
    return pid


def test_pgvector_round_trip(linked):
    rows = linked.sql("SELECT post_id, model, dims, vector_dims(embedding) AS n FROM agent_post_embeddings ORDER BY 1")
    assert [(r["post_id"], r["model"], r["dims"], r["n"]) for r in rows] == \
        [(i, "text-embedding-3-small", 1536, 1536) for i in (1, 2, 3, 4)]
    assert linking.index_post(1) is False and linked.openai.calls == []     # unchanged: no new call
    import agent_db
    with agent_db.connect() as conn:
        near = linking.nearest_posts(conn, fake_vector("Prisma query engine musl"))
        assert near[0][0] == 1 and near[0][1] == pytest.approx(1.0, abs=1e-4)
        assert [pid for pid, _ in linking.nearest_posts(conn, fake_vector("pytest"), kind="open")] == [3]
    linked.sql("DELETE FROM posts WHERE id = 2")                              # ON DELETE CASCADE
    assert [r["post_id"] for r in linked.sql("SELECT post_id FROM agent_post_embeddings ORDER BY 1")] == [1, 3, 4]


def test_search_finds_same_problem_in_other_words(linked):
    """No shared words worth counting, same meaning: only Stage 3 finds it."""
    r = linked.client.get("/api/search", params={"error": "ORM binary absent when the slim container boots"}).json()
    top = r[0]
    assert top["id"] == 1 and top["match"] == "text" and top["stages"] == ["meaning"]
    assert top["score"] >= 0.5 and top["stage_scores"]["meaning"] > 0.9
    assert len(linked.openai.calls) == 1


def test_search_merges_stages(linked):
    prisma = ('PrismaClientInitializationError: Prisma Client could not locate the Query Engine for runtime '
              '"linux-musl-openssl-3.0.x".')
    r = linked.client.get("/api/search", params={"error": prisma, "fingerprint": "51749bd5"}).json()
    assert r[0]["id"] == 1 and r[0]["match"] == "fingerprint" and r[0]["score"] == 1.0
    assert r[0]["stages"] == ["fingerprint", "words", "meaning"]
    r = linked.client.get("/api/search", params={"error": prisma}).json()
    assert r[0]["stages"] == ["words", "meaning"]
    assert r[0]["score"] == round(min(max(r[0]["stage_scores"]["words"],
                                          linking.meaning_score(r[0]["stage_scores"]["meaning"])) + 0.1, 0.99), 3)


def test_search_without_table_or_key_still_works(linked, monkeypatch):
    query = {"error": "Error [ERR_REQUIRE_ESM]: require() of ES Module node-fetch not supported"}
    linked.sql("DROP TABLE agent_post_embeddings")
    linking.reset()
    linking.set_clients(openai=linked.openai)
    r = linked.client.get("/api/search", params=query).json()
    assert r[0]["id"] == 2 and r[0]["stages"] == ["words"] and linked.openai.calls == []   # no wasted call
    assert linking.index_post(2) is False

    linked.sql(SCHEMA_LINKING.read_text(encoding="utf-8"))
    linking.reset()                             # back to "no client given": the env key decides
    monkeypatch.setenv("OPENAI_API_KEY", "")
    r = linked.client.get("/api/search", params=query).json()
    assert r[0]["id"] == 2 and r[0]["stages"] == ["words"]


def test_search_stays_fast_when_embedding_is_slow(linked):
    linked.openai.delay = 3.0
    start = time.monotonic()
    r = linked.client.get("/api/search", params={"error": "Error [ERR_REQUIRE_ESM]: require() of ES Module"}).json()
    assert time.monotonic() - start < linking.QUERY_EMBED_TIMEOUT_S + 0.7
    assert r[0]["id"] == 2 and "meaning" not in r[0]["stages"]


def test_search_never_calls_the_ai_check(linked):
    fake = FakeAnthropic()
    linking.set_clients(anthropic=fake)
    linked.client.get("/api/search", params={"error": "pytest conftest No module named app", "fingerprint": "78b50510"})
    assert fake.calls == []


def test_logic_bug_without_error_message_links_by_meaning(linked):
    question = add_post(linked.sql, "Cart total is off by a cent\nWith a 15% discount the order shows 20.00, "
                        "expected 19.99. Tests pass for whole numbers.", None)
    linking.index_post(question)
    fake = FakeAnthropic()
    linking.set_clients(anthropic=fake)
    new = {"description": "Receipt sum differs by a penny after percentage discounts\nWe applied rounding per "
           "line item.", "solution": "Round once, on the final total, using Decimal.", "tags": []}
    chosen = linking.link_new_solution(new)
    assert chosen is not None and chosen.id == question and chosen.decided_by == "ai_check"
    assert "meaning" in chosen.stages and "words" not in chosen.stages
    assert "Round once" in fake.calls[0]["messages"][0]["content"]          # the check saw the new fix


def test_same_error_different_cause_is_not_linked(linked):
    """Same 'connection refused' error and fingerprint, different cause: the AI check says no."""
    question = add_post(linked.sql, "Postgres connection refused on 5432 in docker compose\npsycopg2.OperationalError:"
                        " connection to server at localhost port 5432 failed: Connection refused", None, "d25c0b1e")
    linking.index_post(question)
    new = {"description": "Connection refused on 5432 running locally on a Mac\npsycopg2.OperationalError: "
           "connection to server at localhost port 5432 failed: Connection refused\nNo Docker; the Postgres "
           "service was simply not started.", "solution": "brew services start postgresql@16",
           "fingerprint": "d25c0b1e"}
    linking.set_clients(anthropic=FakeAnthropic(answer=lambda p: {
        "same_problem": False, "fix_applies": "no", "confidence": 0.85,
        "reason": "Docker service name vs a stopped local server."}))
    assert linking.link_new_solution(new) is None
    linking.set_clients(anthropic=None)                                     # no AI check: fingerprint fallback
    fallback = linking.link_new_solution(new)
    assert fallback is not None and fallback.id == question and fallback.decided_by == "fallback"


def test_duplicate_question(linked):
    fake = FakeAnthropic(answer=lambda p: {"same_problem": existing_id(p) == 3, "fix_applies": "no_fix",
                                           "confidence": 0.8, "reason": "same import error"})
    linking.set_clients(anthropic=fake)
    dup = linking.find_duplicate_question({
        "description": "pytest fails before collecting: conftest cannot import app\nModuleNotFoundError: "
                       "No module named 'app'", "fingerprint": "78b50510", "tags": ["python"]})
    assert dup is not None and dup.id == 3 and dup.verdict["fix_applies"] is None
    assert all("<fix" not in c["messages"][0]["content"] for c in fake.calls)
    # A solved post is never a duplicate Open Question.
    assert linking.find_duplicate_question({"description": "ERR_REQUIRE_ESM require() of ES Module node-fetch"}) is None


def test_link_functions_never_raise(linked, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("database gone")
    monkeypatch.setattr(linking, "find_matches", broken)
    assert linking.link_new_solution({"description": "x", "solution": "y"}) is None
    assert linking.find_duplicate_question({"description": "x"}) is None
    monkeypatch.setattr(linking, "posts_by_ids", broken)
    assert linking.index_post(1) is False


def test_notify_possible_answer(linked):
    linked.sql("UPDATE agent_post_meta SET project = 'proj-asker' WHERE post_id = 3")
    linked.sql("INSERT INTO agent_watchers (post_id, project) VALUES (3, 'proj-later')")
    m = linking.Match(post(3, solution=None), ["meaning"], score=0.6, decided_by="ai_check")
    assert linking.notify_possible_answer(3, m, answer_post_id=9) == 2
    for project in ("proj-asker", "proj-later"):
        got = linked.client.get("/api/updates", params={"project": project}).json()
        assert len(got) == 1 and got[0]["post_id"] == 3 and "Possible answer" in got[0]["message"]
        assert "not verified" in got[0]["message"]


# ---------------------------------------------------------------------------
# Part 3: POST /api/posts uses linking first, the teammate's matcher second (backend/main.py, marked LINKING)
# ---------------------------------------------------------------------------

AUTH = {"X-API-Key": "test-key"}   # conftest.API_KEY


def test_create_post_links_a_solution_in_other_words(linked):
    """No open_question_id, few shared words: linking finds question 3 by meaning, the AI check agrees, and the
    asker hears "Possible answer", not "solved"."""
    linked.sql("UPDATE agent_post_meta SET project = 'proj-asker' WHERE post_id = 3")
    fake = FakeAnthropic(answer=lambda p: {"same_problem": existing_id(p) == 3, "fix_applies": "yes",
                                           "confidence": 0.9, "reason": "same import problem"})
    linking.set_clients(anthropic=fake)
    r = linked.client.post("/api/posts", json={
        "description": "The test runner stops in conftest\nOur pytest setup could not see the package folder.",
        "value": "v", "solution": "Add pythonpath = src to pytest.ini.", "tags": ["python"], "project": "proj-solver",
    }, headers=AUTH).json()
    assert r["attached_to_open_question"] is True and r["post_id"] == 3 and r["linked_by"] == "ai_check"
    got = linked.client.get("/api/updates", params={"project": "proj-asker"}).json()
    assert len(got) == 1 and "Possible answer" in got[0]["message"] and "not verified" in got[0]["message"]
    assert linked.client.get("/api/updates", params={"project": "proj-solver"}).json() == []


def test_create_post_ai_no_beats_the_fingerprint_fallback(linked):
    """Same fingerprint, different cause: the AI check says no, so neither linking nor the teammate's
    fingerprint rule attaches it. A new post is made and embedded after the response."""
    question = add_post(linked.sql, "Postgres connection refused on 5432 in docker compose\npsycopg2.OperationalError:"
                        " connection to server at localhost port 5432 failed: Connection refused", None, "d25c0b1e")
    linking.set_clients(anthropic=FakeAnthropic(answer=lambda p: {
        "same_problem": False, "fix_applies": "no", "confidence": 0.9, "reason": "a stopped local server"}))
    r = linked.client.post("/api/posts", json={
        "description": "Connection refused on 5432 on a Mac\npsycopg2.OperationalError: connection to server at "
                       "localhost port 5432 failed: Connection refused\nNo Docker; Postgres was not started.",
        "value": "v", "solution": "brew services start postgresql@16", "fingerprint": "d25c0b1e",
    }, headers=AUTH).json()
    assert r["attached_to_open_question"] is False and r["post_id"] not in (question, 4) and not r.get("linked_by")
    assert linked.sql("SELECT solution FROM posts WHERE id = %s", (question,))[0]["solution"] is None
    assert linked.sql("SELECT 1 FROM agent_post_embeddings WHERE post_id = %s", (r["post_id"],))   # index_post ran


def test_create_post_duplicate_open_question_by_linking(linked):
    linking.set_clients(anthropic=FakeAnthropic(answer=lambda p: {
        "same_problem": existing_id(p) == 3, "fix_applies": "no_fix", "confidence": 0.8, "reason": "same"}))
    r = linked.client.post("/api/posts", json={
        "description": "pytest fails before collecting: conftest cannot import app\nModuleNotFoundError: "
                       "No module named 'app'", "value": "v", "forum": "docker", "tags": ["python"],
        "project": "proj-late"}, headers=AUTH).json()
    assert r["duplicate_of"] == 3                      # found across forums (3 is in "python", asked in "docker")
    assert {"project": "proj-late"} in linked.sql("SELECT project FROM agent_watchers WHERE post_id = 3")


def test_general_tag_does_not_hide_questions(linked):
    """/api gives "general" to posts with no valid tag; that must not filter out question 3 (python)."""
    linking.set_clients(anthropic=FakeAnthropic())
    dup = linking.find_duplicate_question({"description": "pytest conftest cannot import app\nModuleNotFoundError: "
                                           "No module named 'app'", "tags": ["general"]})
    assert dup is not None and dup.id == 3
