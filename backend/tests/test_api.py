"""API tests with a fake connection: no database needed."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import main

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
    assert "ORDER BY useful_count DESC, visited_count DESC" in sql


def test_status_filter_uses_the_solution(fake):
    conn = fake([])
    client.get("/api/posts", params={"filter": "open"})
    assert "COALESCE(general_solution, solution) IS NULL" in conn.calls[0][0]


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
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [{"id": 99}], [])
    r = client.post("/api/posts", json=NEW_SOLVED, headers=AUTH).json()
    assert r["post_id"] == 99 and r["forum"] == "databases"
    assert r["tags"] == ["prisma", "docker"] and r["dropped_tags"] == ["bogus"]
    insert_sql, insert_params = conn.calls[3 - 1]
    assert "INSERT INTO posts" in insert_sql
    assert insert_params[-2:] == (3, "solved")
    tag_sql, tag_params = conn.calls[3]
    assert "INSERT INTO post_tags" in tag_sql and tag_params == (99, ["prisma", "docker"])


def test_unknown_forum_falls_back_to_general(fake):
    conn = fake([], [{"id": 12, "slug": "general"}], KNOWN_TAGS, [{"id": 5}], [])
    r = client.post("/api/posts", json={**NEW_SOLVED, "forum": "no-such-forum"}, headers=AUTH).json()
    assert r["forum"] == "general"
    assert conn.calls[1][1] == ("general",)


def test_solution_fills_in_an_open_question(fake):
    conn = fake([{"id": 3, "slug": "databases"}], KNOWN_TAGS, [{"id": 41}])
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
