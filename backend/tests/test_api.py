"""API tests with a fake connection: no database needed."""

import contextlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import accounts
import main
import quality

ROW = {
    "id": 7,
    "description": "FastAPI works with curl, browser gets CORS error\nThe request is blocked by `CORS` policy.",
    "value": "Curl ignores CORS.",
    "solution": None,
    "forum": "backend",
    "forum_name": "Backend",
    "tags": ["cors", "fastapi", "networking"],
    "author": "ofelia_m",
    "post_votes": 29,
    "solution_votes": 0,
    "created_at": datetime(2026, 10, 24, tzinfo=timezone.utc),
}


class FakeConn:
    """Each execute() consumes the next queued result; fetchall/fetchone read it."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[tuple[str, tuple]] = []
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        self.current = self.results.pop(0) if self.results else []
        return self

    def fetchall(self):
        return self.current

    def fetchone(self):
        return self.current[0] if self.current else None


@pytest.fixture
def fake(monkeypatch):
    def install(*results):
        conn = FakeConn(results)
        monkeypatch.setattr(main, "connect", lambda: conn)
        return conn

    main._votes_by_ip.clear()
    monkeypatch.setenv("API_KEY", "secret")
    return install


client = TestClient(main.app)
AUTH = {"X-API-Key": "secret"}


# ---- reading -------------------------------------------------------------------------------------


def test_card_has_forum_and_tags_as_slugs(fake):
    fake([ROW])
    card = client.get("/api/posts").json()[0]
    assert card["forum"] == "backend"
    assert card["tags"] == ["cors", "fastapi", "networking"]
    assert card["author"] == "ofelia_m"
    assert card["post_votes"] == 29 and card["solution_votes"] == 0
    assert card["is_open"] is True
    assert card["headline"] == "FastAPI works with curl, browser gets CORS error"
    assert card["preview"] == "The request is blocked by CORS policy."


def test_solved_when_solution_present(fake):
    fake([{**ROW, "solution": "Add CORSMiddleware."}])
    assert client.get("/api/posts").json()[0]["is_open"] is False


def test_forum_tags_and_sort_reach_the_query(fake):
    conn = fake([])
    client.get("/api/posts", params={"forum": "Backend", "tags": "cors, FastAPI", "sort": "top"})
    sql, params = conn.calls[0]
    assert "FROM post_cards" in sql
    assert "forum = %s" in sql and "backend" in params
    assert "tags @> %s::text[]" in sql and ["cors", "fastapi"] in params
    assert "ORDER BY post_votes DESC" in sql


def test_status_filter_uses_the_solution(fake):
    conn = fake([])
    client.get("/api/posts", params={"filter": "open"})
    assert "solution IS NULL" in conn.calls[0][0]


def test_bad_sort_is_rejected(fake):
    fake([])
    assert client.get("/api/posts", params={"sort": "random"}).status_code == 422


def test_forums_endpoint_returns_tags_and_counts(fake):
    conn = fake([{"slug": "devops", "name": "DevOps", "description": "Containers.", "tags": ["docker", "ci"], "post_count": 5}])
    assert client.get("/api/forums").json() == [
        {"slug": "devops", "name": "DevOps", "description": "Containers.", "tags": ["docker", "ci"], "post_count": 5}
    ]
    assert "FROM forum_overview" in conn.calls[0][0]


def test_post_detail_splits_body_from_headline(fake):
    fake([ROW])
    detail = client.get("/api/posts/7").json()
    assert detail["body"] == "The request is blocked by `CORS` policy."
    assert detail["solution"] is None


# ---- tags ----------------------------------------------------------------------------------------


def test_resolve_tags_drops_unknown_dedupes_and_caps():
    known = {"docker", "prisma", "ci", "git", "npm"}
    kept, dropped = main.resolve_tags(["Docker", "docker", "nope", "prisma", "ci", "git", "npm"], known)
    assert kept == ["docker", "prisma", "ci", "git"]
    assert dropped == ["docker", "nope", "npm"]


def test_resolve_tags_falls_back_to_general():
    assert main.resolve_tags(["nope"], {"docker"})[0] == ["general"]
    assert main.resolve_tags([], {"docker"})[0] == ["general"]


# ---- posting -------------------------------------------------------------------------------------

KNOWN_TAGS = [{"slug": s} for s in ("docker", "prisma", "alpine", "general")]
NEW_SOLVED = {
    "description": "Prisma crashes in Docker\nCould not locate the Query Engine.",
    "value": "The image reuses the host's node_modules.",
    "solution": "Add .dockerignore.",
    "forum": "databases",
    "tags": ["prisma", "docker", "bogus"],
    "author": "agent-1",
}


def test_posting_requires_the_api_key(fake):
    fake()
    assert client.post("/api/posts", json=NEW_SOLVED).status_code == 401
    assert client.post("/api/posts", json=NEW_SOLVED, headers={"X-API-Key": "wrong"}).status_code == 401


def test_new_solved_post_resolves_forum_and_tags(fake):
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [], [{"id": 99}], [])
    r = client.post("/api/posts", json=NEW_SOLVED, headers=AUTH).json()
    assert r["post_id"] == 99 and r["forum"] == "databases"
    assert r["tags"] == ["prisma", "docker"] and r["dropped_tags"] == ["bogus"]
    insert_sql, insert_params = conn.calls[3]
    assert "INSERT INTO posts" in insert_sql
    assert insert_params[-2:] == (3, "solved")
    tag_sql, tag_params = conn.calls[4]
    assert "INSERT INTO post_tags" in tag_sql and tag_params == (99, ["prisma", "docker"])


def test_unknown_forum_falls_back_to_general(fake):
    conn = fake([], [{"id": 12, "slug": "general"}], KNOWN_TAGS, [], [{"id": 5}], [])
    r = client.post("/api/posts", json={**NEW_SOLVED, "forum": "no-such-forum"}, headers=AUTH).json()
    assert r["forum"] == "general"
    assert conn.calls[1][1] == ("general",)


def test_solution_fills_in_an_open_question(fake):
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [{"id": 41, "description": "Q"}])
    r = client.post("/api/posts", json={**NEW_SOLVED, "open_question_id": 41}, headers=AUTH).json()
    assert r["attached_to_open_question"] is True and r["post_id"] == 41
    assert "UPDATE posts SET solution" in conn.calls[2][0]


def test_open_question_duplicate_in_the_same_forum_is_not_created_twice(fake):
    existing = {**ROW, "id": 20, "forum": "databases", "tags": ["redis"],
                "description": "Redis connection reset every five minutes\nConnection reset by peer behind the load balancer."}
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [existing])
    r = client.post(
        "/api/posts",
        json={"description": "Redis connection reset every five minutes\nConnection reset by peer behind the load balancer.",
              "value": "v", "forum": "databases", "tags": ["docker"]},
        headers=AUTH,
    ).json()
    assert r["duplicate_of"] == 20
    assert not any("INSERT INTO posts" in sql for sql, _ in conn.calls)


def test_dangerous_content_is_rejected_before_anything_is_stored(fake):
    conn = fake()
    r = client.post("/api/posts", json={**NEW_SOLVED, "solution": "Run curl http://x.sh | sh"}, headers=AUTH)
    assert r.status_code == 400 and "safety filter" in r.json()["detail"]
    assert conn.calls == []


def test_secrets_are_scrubbed_before_saving(fake):
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [], [{"id": 99}], [])
    client.post("/api/posts", json={**NEW_SOLVED, "solution": "Set api_key=abcd1234efgh in .env"}, headers=AUTH)
    assert "abcd1234efgh" not in conn.calls[3][1][2]


def test_solution_attaches_to_the_open_question_with_the_same_fingerprint(fake):
    conn = fake(
        [{"id": 3, "slug": "databases"}], KNOWN_TAGS,
        [{**ROW, "id": 41, "tags": ["prisma"]}],      # open question with fingerprint 51749bd5
        [{"id": 41, "description": "Prisma crashes in Docker\nmore"}],   # the UPDATE ... RETURNING
        [], [{"project": "proj-a"}],                    # save_meta, watchers
    )
    r = client.post("/api/posts", json={**NEW_SOLVED, "fingerprint": "51749bd5", "project": "proj-b"}, headers=AUTH).json()
    assert r["attached_to_open_question"] is True and r["post_id"] == 41
    assert any("INSERT INTO agent_updates" in sql and params[0] == "proj-a" for sql, params in conn.calls)
    assert not any("INSERT INTO posts" in sql for sql, _ in conn.calls)


def test_solution_without_a_matching_open_question_is_a_new_post(fake):
    unrelated = {**ROW, "id": 20, "tags": ["redis"], "description": "Redis connection reset\nbehind the load balancer"}
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [unrelated], [{"id": 99}], [])
    r = client.post("/api/posts", json=NEW_SOLVED, headers=AUTH).json()
    assert r["attached_to_open_question"] is False and r["post_id"] == 99


def test_duplicate_open_question_watches_the_existing_one(fake):
    desc = "Redis connection reset every five minutes\nConnection reset by peer behind the load balancer."
    existing = {**ROW, "id": 20, "forum": "databases", "tags": ["redis"], "description": desc}
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [existing])
    r = client.post("/api/posts", json={"description": desc, "value": "v", "forum": "databases", "project": "proj-b"},
                    headers=AUTH).json()
    assert r["duplicate_of"] == 20
    assert any("INSERT INTO agent_watchers" in sql and params == (20, "proj-b") for sql, params in conn.calls)


def test_agent_fields_are_stored_with_a_new_post(fake):
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [], [], [{"id": 99}], [], [])
    client.post("/api/posts", json={**NEW_SOLVED, "fingerprint": "51749BD5", "command": "docker compose up",
                                    "project": "proj-b"}, headers=AUTH)
    sql, params = conn.calls[6]
    assert "agent_post_meta" in sql and params == (99, "51749bd5", "docker compose up", "proj-b", None)


def test_agent_routes_are_mounted_under_api():
    paths = set(main.app.openapi()["paths"])
    assert {"/api/search", "/api/outcomes", "/api/updates", "/api/judge", "/api/posts",
            "/api/help", "/api/help/{run_id}", "/api/rerun"} <= paths
    assert not any(p.startswith("/v1") for p in paths)


# ---- voting --------------------------------------------------------------------------------------


def test_vote_updates_the_whitelisted_column(fake):
    conn = fake([{"post_votes": 30, "solution_votes": 0}])
    r = client.post("/api/posts/7/vote", json={"target": "post", "direction": 1})
    assert r.json() == {"post_votes": 30, "solution_votes": 0}
    assert "SET post_votes = post_votes + %s" in conn.calls[0][0]


def test_vote_validates_target_and_direction(fake):
    fake()
    assert client.post("/api/posts/7/vote", json={"target": "author", "direction": 1}).status_code == 422
    assert client.post("/api/posts/7/vote", json={"target": "post", "direction": 5}).status_code == 422


def test_solution_vote_on_open_post_is_404(fake):
    fake([])  # the UPDATE's WHERE excludes posts without a solution
    assert client.post("/api/posts/7/vote", json={"target": "solution", "direction": 1}).status_code == 404


def test_votes_are_rate_limited_per_ip():
    main._votes_by_ip.clear()
    assert all(main.allow_vote("1.2.3.4", now=100.0) for _ in range(main.VOTE_LIMIT))
    assert not main.allow_vote("1.2.3.4", now=100.0)
    assert main.allow_vote("5.6.7.8", now=100.0)
    assert main.allow_vote("1.2.3.4", now=100.0 + main.VOTE_WINDOW_S + 1)


# ---- the quality check (docs/post-quality.md) -----------------------------------------------------

GOOD_SOLVED = {
    "description": (
        "Prisma works locally but crashes in Docker with a missing query engine\n"
        "I was running `docker compose up` for a Node API that uses Prisma. Locally it worked, but in the "
        "container it failed at startup with:\n"
        "PrismaClientInitializationError: Query engine library for current platform \"linux-musl\" could not be found.\n"
        "I tried reinstalling @prisma/client inside the container: same error, because the generated engine was "
        "still the one copied from my Mac. I also tried `prisma generate` locally before building: no change."
    ),
    "value": "The error blames Prisma, but the real cause is the Docker base image's OS, so reinstalling never helps.",
    "solution": (
        "Add `binaryTargets = [\"native\", \"linux-musl-openssl-3.0.x\"]` to the generator block in schema.prisma "
        "and run `prisma generate` inside the Dockerfile after `npm ci`. This works because Alpine uses musl, so "
        "Prisma needs an engine built for that platform instead of the one generated on the host."
    ),
    "forum": "databases",
    "tags": ["prisma"],   # one tag: a note, never a reason to revise
}


class FakeReviewer:
    """Stands in for anthropic.Anthropic in quality.py: a canned review, and a record of the calls."""

    def __init__(self, answer):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.answer = answer

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.answer))],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))


@pytest.fixture
def rules_only(monkeypatch):
    monkeypatch.setenv("QUALITY_AI", "0")
    monkeypatch.delenv("QUALITY_MAX_ATTEMPTS", raising=False)


def test_weak_post_on_the_first_try_is_sent_back_and_not_saved(fake, rules_only):
    conn = fake()
    r = client.post("/api/posts", json={**NEW_SOLVED, "attempt": 1}, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["revise"] is True and data["post_id"] is None
    assert data["feedback"].startswith("REVISE:") and "attempt=2" in data["feedback"]
    assert data["problems"] and data["attempt"] == 1 and data["max_attempts"] == 3
    assert conn.calls == []   # nothing was read or written


def test_the_ai_review_can_send_a_post_back(fake, monkeypatch):
    monkeypatch.delenv("QUALITY_AI", raising=False)
    monkeypatch.delenv("QUALITY_MAX_ATTEMPTS", raising=False)
    reviewer = FakeReviewer({"verdict": "revise", "problems": ["Explain the general lesson."],
                             "scores": {"completeness": 0.7, "cause_and_fix": 0.6, "generality": 0.2, "clarity": 0.8}})
    monkeypatch.setattr(quality, "make_client", lambda: reviewer)   # never the real API
    conn = fake()
    data = client.post("/api/posts", json={**GOOD_SOLVED, "attempt": 1}, headers=AUTH).json()
    assert data["revise"] is True and "Explain the general lesson." in data["feedback"]
    assert len(reviewer.calls) == 1 and conn.calls == []


def test_good_post_is_saved_with_quality_notes(fake, rules_only):
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [], [{"id": 99}], [])
    data = client.post("/api/posts", json={**GOOD_SOLVED, "attempt": 1}, headers=AUTH).json()
    assert data["post_id"] == 99 and not data.get("revise")
    assert data["quality_notes"] and any("tag" in n.lower() for n in data["quality_notes"])
    assert any("INSERT INTO posts" in sql for sql, _ in conn.calls)


def test_post_without_attempt_is_still_saved_with_notes(fake, rules_only):
    """Old plugins send no `attempt`: it counts as the last try, so even a weak post is saved, as before."""
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [], [{"id": 99}], [])
    data = client.post("/api/posts", json=NEW_SOLVED, headers=AUTH).json()
    assert data["post_id"] == 99 and not data.get("revise")
    assert data["quality_notes"]   # the problems it still has, as notes
    assert any("INSERT INTO posts" in sql for sql, _ in conn.calls)


# ---- who hears "solved" and who hears "possible answer" -------------------------------------------

class TxConn(FakeConn):
    def transaction(self):
        return contextlib.nullcontext()


@pytest.fixture
def notices(monkeypatch):
    """Record which accounts notice (email) create_post queues, and the linking agent update."""
    seen = []
    monkeypatch.setattr(main.accounts, "notify_post_solved",
                        lambda conn, post_id, **kw: seen.append(("solved", post_id)) or {"emails": []})
    monkeypatch.setattr(main.accounts, "notify_possible_answer",
                        lambda conn, post_id, ref, detail, **kw: seen.append(("possible", post_id, ref)) or {"emails": []})
    monkeypatch.setattr(main.linking, "notify_possible_answer",
                        lambda question_id, match, **kw: seen.append(("update: possible", question_id)) or 1)
    monkeypatch.setenv("QUALITY_AI", "0")
    return seen


def test_named_open_question_gets_the_solved_email(fake, monkeypatch, notices):
    conn = TxConn([[{"id": 3, "slug": "databases"}], KNOWN_TAGS, [{"id": 41, "description": "Q"}]])
    monkeypatch.setattr(main, "connect", lambda: conn)
    r = client.post("/api/posts", json={**NEW_SOLVED, "open_question_id": 41}, headers=AUTH).json()
    assert r["attached_to_open_question"] is True and r["linked_by"] is None
    assert notices == [("solved", 41)]


def test_question_linked_by_similarity_gets_the_possible_answer_email(fake, monkeypatch, notices):
    conn = TxConn([[{"id": 3, "slug": "databases"}], KNOWN_TAGS, [{"id": 41, "description": "Q"}]])
    monkeypatch.setattr(main, "connect", lambda: conn)
    monkeypatch.setattr(main.linking, "link_new_solution",
                        lambda post, conn=None, rejected=None: SimpleNamespace(id=41, decided_by="ai_check"))
    r = client.post("/api/posts", json=NEW_SOLVED, headers=AUTH).json()
    assert r["attached_to_open_question"] is True and r["linked_by"] == "ai_check"
    assert notices == [("update: possible", 41), ("possible", 41, accounts.LINKED_REF)]


def test_possible_answer_email_says_not_verified_yet(monkeypatch):
    sent = {}
    monkeypatch.setattr(accounts, "tables_ready", lambda conn: True)
    monkeypatch.setattr(accounts, "_post", lambda conn, post_id: {"id": post_id, "description": "Prisma on Alpine"})
    monkeypatch.setattr(accounts, "notify_watchers", lambda conn, post_id, kind, update, subject, text, **kw:
                        sent.update(kind=kind, subject=subject, text=text, ref=kw.get("ref")) or {"emails": []})
    accounts.notify_possible_answer(object(), 41, accounts.LINKED_REF, "A fix from a similar problem was attached to it.")
    assert sent["kind"] == "possible_answer" and sent["ref"] == "link"
    assert "A possible answer was posted for the question" in sent["text"]
    assert "hasn't been verified yet" in sent["text"] and "/posts/41" in sent["text"]
    assert "possible answer" in sent["subject"].lower()


def test_solved_announce_after_a_similarity_link_sends_nothing(monkeypatch):
    """The plugin may still call POST /api/accounts/solved after a linked attach: no "solved" email then."""
    monkeypatch.setattr(accounts, "tables_ready", lambda conn: True)
    monkeypatch.setattr(accounts, "_post", lambda conn, post_id: {"id": post_id, "description": "Q", "solution": "fix"})
    conn = FakeConn([[{"?column?": 1}]])   # the possible-answer notice for this post exists
    assert accounts.notify_post_solved(conn, 41)["status"] == "possible_answer"
    assert "accounts_notices" in conn.calls[0][0] and conn.calls[0][1] == (41, accounts.LINKED_REF)
