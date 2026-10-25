"""Tests for backend/linking.py (docs/linking.md). No real API calls: OpenAI and Anthropic are fakes.

Part 1 needs nothing. Part 2 needs TEST_DATABASE_URL pointing at a THROWAWAY Postgres (plain postgres:16,
no extensions needed). These tests build their own posts on it and never use the real database.

    docker run --rm -d --name bp-linking-mem-pg -p 55437:5432 -e POSTGRES_PASSWORD=test postgres:16
    $env:TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55437/postgres"
    .venv\\Scripts\\python -m pytest tests/test_linking.py -q
    docker stop bp-linking-mem-pg

Part 2 uses only tests/breakpoint_schema_stub.sql plus the real columns linking reads that the stub may
lack (posts.fingerprint, posts.solved_at), added by this file's fixture. It does not go through main.py.

Seed posts: 1 Prisma/Alpine solved (fingerprint 51749bd5), 2 ERR_REQUIRE_ESM solved, 3 pytest/conftest
OPEN (78b50510; title in posts.problem), 4 docker-compose Postgres "connection refused" solved (d25c0b1e).
"""

import json
import math
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_db
import linking

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
STUB_SCHEMA = Path(__file__).with_name("breakpoint_schema_stub.sql")


def test_existing_solution_requires_same_problem_not_just_same_topic(monkeypatch):
    candidate = linking.Match({"id": 7, "solution": "Check the interpreter"}, ["words"], score=0.9)
    def matches(*args, **kwargs):
        assert kwargs["kind"] == "solved" and kwargs["confirm"] is True
        return [candidate]
    monkeypatch.setattr(linking, "find_matches", matches)
    assert linking.find_existing_solution({"description": "Import fails"}).id == 7
    candidate.verdict = {"same_problem": False, "fix_applies": False, "confidence": 0.99}
    assert linking.find_existing_solution({"description": "Import fails"}) is None
    candidate.verdict = None
    candidate.score = 0.2
    assert linking.find_existing_solution({"description": "Import fails"}) is None


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
    ("gradle", "daemon"),
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

    def inputs(self) -> list[str]:
        return [t for c in self.calls for t in c["input"]]


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


def test_problem_text_uses_the_problem_column():
    separate = {"problem": "Gradle build hangs", "description": "Error: daemon disappeared\nother", "tags": []}
    assert linking.problem_text(separate).startswith("Gradle build hangs\n\nKey errors:\nError: daemon disappeared")
    # main.py stores "problem\n\ndetails" in description: the title is not repeated
    joined = {"problem": "Gradle build hangs", "description": "Gradle build hangs\n\nError: daemon disappeared"}
    assert linking.problem_text(joined).count("Gradle build hangs") == 1
    assert linking.words_text(separate) == "Gradle build hangs\nError: daemon disappeared\nother"
    assert linking.words_text(joined) == joined["description"]


def test_embed_query_cache_and_shape():
    fake = FakeOpenAI()
    linking.set_clients(openai=fake)
    v1 = linking._embed_query_now("same text", 1.0)
    v2 = linking._embed_query_now("same text", 1.0)
    assert v1 == v2 and len(v1) == linking.EMBED_DIMS and len(fake.calls) == 1   # second one from the cache
    assert fake.calls[0]["model"] == "text-embedding-3-small" and fake.options[0]["max_retries"] == 0
    linking.set_clients(openai=FakeOpenAI(dims=10))
    assert linking.embed(["x"], 1.0) is None                       # wrong length: skipped
    linking.set_clients(openai=FakeOpenAI(fail=True))
    assert linking.embed(["x"], 1.0) is None


def test_slow_query_embedding_is_skipped_in_time():
    linking.set_clients(openai=FakeOpenAI(delay=1.5))
    start = time.monotonic()
    future = linking.start_query_embedding("anything at all")
    assert linking.wait_for(future, time.monotonic() + 0.2) is None
    assert time.monotonic() - start < 0.6


def test_no_openai_key_means_no_stage_3_and_no_warm_up(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert linking.openai_client() is None
    assert linking.start_query_embedding("text") is None
    assert linking.warm_up() is False and linking.meaning_ready() is False
    assert linking.cache_status() == {"posts": 0, "warm": False, "warming": False, "last": None}
    assert linking.index_post(1) is False                          # returns before touching the database
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("LINKING_EMBEDDINGS", "0")
    assert linking.openai_client() is None


def test_cache_cosine_order_and_memory_cap(monkeypatch):
    for pid, text in [(1, "prisma musl"), (2, "pytest conftest"), (3, "postgres 5432")]:
        assert linking._store(pid, f"h{pid}", fake_vector(text), linking._warm["gen"])
    near = linking.nearest_posts(fake_vector("pytest no module named"), limit=None)
    assert near[0][0] == 2 and near[0][1] == pytest.approx(1.0, abs=0.03) and near[-1][1] < 0.2
    assert [pid for pid, _ in linking.nearest_posts(fake_vector("pytest"), exclude={2})][:1] != [2]
    assert linking._cache[1][1].typecode == "f"                     # float32: ~6 KB per post
    monkeypatch.setattr(linking, "MAX_CACHED_POSTS", 3)
    assert linking._store(9, "h9", fake_vector("gradle"), linking._warm["gen"])   # full: the oldest goes
    assert sorted(linking._cache) == [2, 3, 9]
    assert linking._store(1, "h1", fake_vector("prisma"), linking._warm["gen"]) is False   # older than all: skipped
    stale = linking._warm["gen"]
    linking.reset()
    assert linking._store(5, "h5", fake_vector("x"), stale) is False   # a warm-up from before reset() can't refill


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
    assert linking.rejected_ids([m(1, ["fingerprint"], 1.0, no), m(2, ["words"], 0.9, wont_fix),
                                 m(3, ["words"], 0.9, unsure)], need_fix=True) == {1, 2}


def test_as_result_shape_and_fallback(monkeypatch):
    row = {**post(7, "desc", None), "value": "v", "forum": "python", "fingerprint": "abc12345", "useful_count": 2,
           "visited_count": 3, "created_at": None, "tags": ["python"]}
    m = linking.Match(row, ["fingerprint", "words"], {"words": 0.4}, score=1.0,
                      verdict={"same_problem": True, "fix_applies": None, "confidence": 0.9, "reason": "r"})
    out = m.as_result()
    assert out["id"] == 7 and out["match"] == "fingerprint" and out["score"] == 1.0 and out["status"] == "open"
    assert out["stages"] == ["fingerprint", "words"] and out["stage_scores"] == {"words": 0.4}
    assert out["verdict"]["reason"] == "r" and out["fingerprint"] == "abc12345"

    def broken(row):
        raise KeyError("some_new_column")
    monkeypatch.setattr(agent_db, "public_post", broken)                  # agent_db's shape changed
    out2 = linking.Match(row, ["words"], {"words": 0.4}, score=0.4).as_result()
    assert out2["id"] == 7 and out2["match"] == "text" and out2["upvotes"] == 0 and out2["fingerprint"] == "abc12345"
    assert out2["useful_count"] == 2 and out2["status"] == "open"


def test_notify_possible_answer_is_a_no_op():
    m = linking.Match(post(3, solution=None), ["meaning"], score=0.6, decided_by="ai_check")
    assert linking.notify_possible_answer(3, m, answer_post_id=9, conn=None, exclude_project="p") == 0


# ---------------------------------------------------------------------------
# Part 2: against a throwaway Postgres (no extensions)
# ---------------------------------------------------------------------------

SEED = [
    # (problem, description, solution, fingerprint, tags)
    (None, "Prisma Client could not locate the Query Engine on Alpine\nPrismaClientInitializationError: Prisma "
     "Client could not locate the Query Engine for runtime \"linux-musl-openssl-3.0.x\".",
     "Add binaryTargets = [\"native\", \"linux-musl-openssl-3.0.x\"] to schema.prisma.", "51749bd5", ["docker"]),
    (None, "Error [ERR_REQUIRE_ESM]: require() of ES Module node-fetch not supported\nnode-fetch v3 is ESM only.",
     "Use node-fetch@2 or switch the project to ES modules.", None, ["node"]),
    ("pytest cannot import app from conftest", "ModuleNotFoundError: No module named 'app'\nconftest.py at the "
     "repo root, code in src/.", None, "78b50510", ["python", "pytest"]),
    (None, "Postgres connection refused on 5432 in docker compose\npsycopg2.OperationalError: connection to server at "
     "localhost port 5432 failed: Connection refused\nThe app container uses localhost.",
     "Use the service name (db) as the host, not localhost.", "d25c0b1e", ["docker"]),
]


@pytest.fixture(scope="module")
def pg() -> str:
    if not TEST_DB:
        pytest.skip("set TEST_DATABASE_URL to a throwaway Postgres to run the database tests")
    if "supabase" in TEST_DB.lower():
        pytest.fail("TEST_DATABASE_URL points at Supabase; the stub schema DROPS tables. Use a throwaway Postgres.")
    import psycopg

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(STUB_SCHEMA.read_text(encoding="utf-8"))
        # Real columns linking reads that the stub may not have yet.
        conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS fingerprint TEXT")
        conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS solved_at TIMESTAMPTZ")
    return TEST_DB


@pytest.fixture()
def world(pg, monkeypatch):
    """Fresh seed posts, agent_db pointed at the throwaway database, fake OpenAI (not warm), no AI check."""
    import psycopg
    from psycopg.rows import dict_row

    def sql(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(pg, row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    sql("TRUNCATE post_tags, posts, tags, forums RESTART IDENTITY CASCADE")
    for slug in ("general", "docker", "python", "node"):
        sql("INSERT INTO forums (slug, name) VALUES (%s, %s)", (slug, slug.title()))
    for slug in ("general", "docker", "python", "pytest", "node"):
        sql("INSERT INTO tags (slug) VALUES (%s)", (slug,))

    def add_post(description: str, solution: str | None, fingerprint: str | None = None,
                 tags: list[str] | None = None, problem: str | None = None) -> int:
        pid = sql("INSERT INTO posts (description, value, solution, problem, fingerprint, forum_id, status)"
                  " VALUES (%s, 'v', %s, %s, %s, 1, %s) RETURNING id",
                  (description, solution, problem, fingerprint, "open" if solution is None else "solved"))[0]["id"]
        for tag in tags or []:
            sql("INSERT INTO post_tags (post_id, tag_id) SELECT %s, id FROM tags WHERE slug = %s", (pid, tag))
        return pid

    for problem, description, solution, fingerprint, tags in SEED:
        add_post(description, solution, fingerprint, tags, problem)

    monkeypatch.setenv("DATABASE_URL", pg)
    agent_db.close_pool()
    fake = FakeOpenAI()
    linking.set_clients(openai=fake, anthropic=None)

    def connect():
        return psycopg.connect(pg, row_factory=dict_row)

    yield SimpleNamespace(sql=sql, add_post=add_post, openai=fake, connect=connect)
    thread = linking._warm["thread"]
    if thread is not None:
        thread.join(5)
    linking.reset()
    agent_db.close_pool()


def warm(world) -> None:
    assert linking.warm_up(wait=True) is True
    world.openai.calls.clear()


def stages_of(matches: list[linking.Match]) -> dict[int, list[str]]:
    return {m.id: m.stages for m in matches}


def test_warm_up_embeds_all_posts_in_batches_and_logs(world, monkeypatch, caplog):
    monkeypatch.setattr(linking, "WARM_BATCH", 3)
    with caplog.at_level("WARNING", logger="linking"):
        assert linking.warm_up(wait=True) is True
    status = linking.cache_status()
    assert status["posts"] == 4 and status["warm"] and not status["warming"]
    assert [len(c["input"]) for c in world.openai.calls] == [3, 1]                  # batched
    assert status["last"] == {"posts": 4, "embedded": 4, "seconds": pytest.approx(status["last"]["seconds"]),
                              "tokens": 40, "cost_usd": pytest.approx(40 * 0.02 / 1e6)}
    assert "meaning cache ready: 4 posts (4 embedded now)" in caplog.text
    assert not any("Use the service name" in t or "binaryTargets" in t for t in world.openai.inputs())   # no fixes
    assert any(t.startswith("pytest cannot import app from conftest") for t in world.openai.inputs())   # problem col

    world.openai.calls.clear()
    assert linking.warm_up(wait=True) is True and world.openai.calls == []        # nothing changed: no call
    world.sql("UPDATE posts SET description = description || ' (edited: also on Debian)' WHERE id = 2")
    world.sql("DELETE FROM posts WHERE id = 1")
    linking.warm_up(wait=True)
    assert len(world.openai.inputs()) == 1 and "edited" in world.openai.inputs()[0]   # only the changed post
    assert sorted(linking._cache) == [2, 3, 4]                                       # the deleted one is gone


def test_warm_up_failure_leaves_stage_3_off_and_retries(world, monkeypatch):
    world.openai.fail = True
    assert linking.warm_up(wait=True) is False
    assert linking.cache_status()["warm"] is False and linking._warm["failed"] > 0
    with world.connect() as conn:
        r = linking.find_matches("Error [ERR_REQUIRE_ESM]: require() of ES Module node-fetch", conn=conn)
    assert r[0].id == 2 and r[0].stages == ["words"]                                # still works without Stage 3
    monkeypatch.setattr(linking, "WARM_RETRY_S", 0.0)
    world.openai.fail = False
    linking.meaning_ready()                                                         # retry starts in the background
    linking._warm["thread"].join(5)
    assert linking.cache_status()["warm"] is True


def test_index_post_adds_and_refreshes_a_vector(world):
    pid = world.add_post("Cart total is off by a cent\nWith a 15% discount the order shows 20.00.", None)
    assert linking.index_post(pid) is True                                           # works before warm-up too
    assert pid in linking._cache and len(world.openai.calls) == 1
    assert linking.index_post(pid) is False and len(world.openai.calls) == 1         # unchanged: no call
    old_hash = linking._cache[pid][0]
    world.sql("UPDATE posts SET description = 'Gradle daemon disappeared unexpectedly' WHERE id = %s", (pid,))
    assert linking.index_post(pid) is True and linking._cache[pid][0] != old_hash    # re-embedded
    near = linking.nearest_posts(fake_vector("gradle daemon"))
    assert near[0][0] == pid
    assert linking.index_post(9999) is False                                         # no such post
    world.openai.fail = True
    world.sql("UPDATE posts SET description = 'changed again' WHERE id = %s", (pid,))
    assert linking.index_post(pid) is False                                          # OpenAI down: no crash


def test_first_search_starts_warm_up_and_skips_stage_3_until_warm(world):
    query = "ORM binary absent when the slim container boots"                         # meaning only, no shared words
    with world.connect() as conn:
        assert linking.find_matches(query, conn=conn) == []                          # not warm: no Stage 3
    thread = linking._warm["thread"]
    assert thread is not None                                                        # ... but warm-up started
    thread.join(5)
    assert all(len(c["input"]) == 4 for c in world.openai.calls)                     # only the warm-up called OpenAI
    with world.connect() as conn:
        r = linking.find_matches(query, conn=conn)
    assert r[0].id == 1 and r[0].stages == ["meaning"] and r[0].score >= 0.5


def test_stage_3_skipped_without_key(world, monkeypatch):
    linking.reset()                                                  # no fake client: the env key decides
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with world.connect() as conn:
        r = linking.find_matches("Error [ERR_REQUIRE_ESM]: require() of ES Module node-fetch", conn=conn)
    assert r[0].id == 2 and r[0].stages == ["words"]
    assert linking.cache_status() == {"posts": 0, "warm": False, "warming": False, "last": None}


def test_fingerprint_comes_from_posts_fingerprint(world):
    with world.connect() as conn:
        r = linking.find_matches("", fingerprint="51749bd5", conn=conn)
        assert [m.id for m in r] == [1] and r[0].stages == ["fingerprint"] and r[0].score == 1.0
        out = r[0].as_result()
        assert out["match"] == "fingerprint" and out["fingerprint"] == "51749bd5" and out["tags"] == ["docker"]
        assert linking.find_matches("", fingerprint="51749bd5", kind="open", conn=conn) == []   # 1 is solved
        assert [m.id for m in linking.find_matches("", fingerprint="78b50510", kind="open", conn=conn)] == [3]
    world.sql("UPDATE posts SET general_solution = 'Set pythonpath = src' WHERE id = 3")   # general solution counts
    with world.connect() as conn:
        assert linking.find_matches("", fingerprint="78b50510", kind="open", conn=conn) == []
        assert linking.find_matches("", fingerprint="78b50510", conn=conn)[0].post["solution"] == "Set pythonpath = src"


def test_words_read_the_problem_column(world):
    pid = world.add_post("The build stops after a while with no output.", "Raise org.gradle.jvmargs.",
                         problem="Gradle daemon disappeared unexpectedly")
    with world.connect() as conn:
        r = linking.find_matches("Gradle daemon disappeared unexpectedly", conn=conn)
    assert r[0].id == pid and "words" in r[0].stages


def test_search_merges_stages(world):
    warm(world)
    prisma = ('PrismaClientInitializationError: Prisma Client could not locate the Query Engine for runtime '
              '"linux-musl-openssl-3.0.x".')
    with world.connect() as conn:
        r = linking.find_matches(prisma, fingerprint="51749bd5", conn=conn)
        assert r[0].id == 1 and r[0].score == 1.0 and r[0].stages == ["fingerprint", "words", "meaning"]
        r = linking.find_matches(prisma, conn=conn)
    assert r[0].stages == ["words", "meaning"]
    assert r[0].score == round(min(max(r[0].scores["words"], linking.meaning_score(r[0].scores["meaning"])) + 0.1,
                                   0.99), 3)
    assert len(world.openai.calls) == 1                              # one query embedding (the 2nd was cached)


def test_meaning_respects_kind_tags_and_exclude(world):
    warm(world)
    with world.connect() as conn:
        q = "pytest conftest no module named"
        assert 3 in stages_of(linking.find_matches(q, kind="open", conn=conn))
        assert 3 not in stages_of(linking.find_matches(q, kind="solved", conn=conn))
        assert 3 not in stages_of(linking.find_matches(q, tags=["node"], conn=conn))
        assert 3 not in stages_of(linking.find_matches(q, exclude_ids={3}, conn=conn))


def test_search_stays_fast_when_embedding_is_slow(world):
    warm(world)
    world.openai.delay = 3.0
    start = time.monotonic()
    with world.connect() as conn:
        r = linking.find_matches("Error [ERR_REQUIRE_ESM]: require() of ES Module", conn=conn)
    assert time.monotonic() - start < linking.QUERY_EMBED_TIMEOUT_S + 0.7
    assert r[0].id == 2 and "meaning" not in r[0].stages


def test_search_never_calls_the_ai_check_and_never_writes(world):
    warm(world)
    fake = FakeAnthropic()
    linking.set_clients(anthropic=fake)
    with world.connect() as conn:
        conn.execute("SET default_transaction_read_only = on")      # any write would raise here
        conn.commit()
        r = linking.find_matches("pytest conftest No module named app", fingerprint="78b50510", conn=conn)
        assert r[0].id == 3 and fake.calls == []
        pid = world.add_post("brand new: gradle daemon", None)
        assert linking.index_post(pid, conn) is True                 # read-only connection is enough


def test_logic_bug_without_error_message_links_by_meaning(world):
    question = world.add_post("Cart total is off by a cent\nWith a 15% discount the order shows 20.00, "
                              "expected 19.99. Tests pass for whole numbers.", None)
    warm(world)
    fake = FakeAnthropic()
    linking.set_clients(anthropic=fake)
    new = {"description": "Receipt sum differs by a penny after percentage discounts\nWe applied rounding per "
           "line item.", "solution": "Round once, on the final total, using Decimal.", "tags": []}
    chosen = linking.link_new_solution(new)
    assert chosen is not None and chosen.id == question and chosen.decided_by == "ai_check"
    assert "meaning" in chosen.stages and "words" not in chosen.stages
    assert "Round once" in fake.calls[0]["messages"][0]["content"]          # the check saw the new fix


def test_ai_check_reads_only_top_3_in_the_funnel(world):
    for i in range(5):
        world.add_post(f"pytest conftest cannot import module variant {i}\nModuleNotFoundError: No module named "
                       f"'pkg{i}'", None, tags=["python"])
    warm(world)
    fake = FakeAnthropic(answer=lambda p: {"same_problem": False, "fix_applies": "no_fix", "confidence": 0.9,
                                           "reason": "different module"})
    linking.set_clients(anthropic=fake)
    rejected: set[int] = set()
    dup = linking.find_duplicate_question({"description": "pytest conftest cannot import module\n"
                                           "ModuleNotFoundError: No module named 'app'"}, rejected=rejected)
    assert dup is None and len(fake.calls) == 3 and len(rejected) == 3


def test_same_error_different_cause_is_not_linked(world):
    """Same 'connection refused' error and fingerprint, different cause: the AI check says no."""
    question = world.add_post("Postgres connection refused on 5432 in docker compose\npsycopg2.OperationalError:"
                              " connection to server at localhost port 5432 failed: Connection refused", None,
                              "d25c0b1e")
    warm(world)
    new = {"description": "Connection refused on 5432 running locally on a Mac\npsycopg2.OperationalError: "
           "connection to server at localhost port 5432 failed: Connection refused\nNo Docker; the Postgres "
           "service was simply not started.", "solution": "brew services start postgresql@16",
           "fingerprint": "d25c0b1e"}
    linking.set_clients(anthropic=FakeAnthropic(answer=lambda p: {
        "same_problem": False, "fix_applies": "no", "confidence": 0.85,
        "reason": "Docker service name vs a stopped local server."}))
    rejected: set[int] = set()
    assert linking.link_new_solution(new, rejected=rejected) is None and question in rejected
    linking.set_clients(anthropic=None)                                     # no AI check: fingerprint fallback
    fallback = linking.link_new_solution(new)
    assert fallback is not None and fallback.id == question and fallback.decided_by == "fallback"


def test_duplicate_question(world):
    warm(world)
    fake = FakeAnthropic(answer=lambda p: {"same_problem": existing_id(p) == 3, "fix_applies": "no_fix",
                                           "confidence": 0.8, "reason": "same import error"})
    linking.set_clients(anthropic=fake)
    with world.connect() as conn:
        dup = linking.find_duplicate_question({
            "description": "pytest fails before collecting: conftest cannot import app\nModuleNotFoundError: "
                           "No module named 'app'", "fingerprint": "78b50510", "tags": ["python"]}, conn=conn)
    assert dup is not None and dup.id == 3 and dup.verdict["fix_applies"] is None
    assert all("<fix" not in c["messages"][0]["content"] for c in fake.calls)
    # A solved post is never a duplicate Open Question.
    assert linking.find_duplicate_question({"description": "ERR_REQUIRE_ESM require() of ES Module node-fetch"}) is None


def test_general_tag_does_not_hide_questions(world):
    linking.set_clients(anthropic=FakeAnthropic())
    dup = linking.find_duplicate_question({"description": "pytest conftest cannot import app\nModuleNotFoundError: "
                                           "No module named 'app'", "tags": ["general"]})
    assert dup is not None and dup.id == 3


def test_link_functions_never_raise(world, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("database gone")
    monkeypatch.setattr(linking, "find_matches", broken)
    assert linking.link_new_solution({"description": "x", "solution": "y"}) is None
    assert linking.find_duplicate_question({"description": "x"}) is None
    monkeypatch.setattr(linking, "posts_by_ids", broken)
    assert linking.index_post(1) is False


def test_cache_is_thread_safe_under_concurrent_use(world):
    warm(world)
    errors: list[Exception] = []

    def writer(base: int) -> None:
        try:
            for i in range(200):
                linking._store(base + i, "h", fake_vector("gradle daemon"), linking._warm["gen"])
                linking.forget(base + i - 1)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader() -> None:
        try:
            for _ in range(50):
                linking.nearest_posts(fake_vector("prisma musl"), limit=None)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(1000 * k,)) for k in (1, 2)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and {1, 2, 3, 4} <= set(linking._cache)
