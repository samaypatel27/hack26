"""Opt-in PostgreSQL integration checks, isolated from real posts and activity.

Run from backend: RUN_DATABASE_TESTS=1 .venv/Scripts/python.exe -m pytest tests/test_usage_database.py
Creates a random test-only schema and removes that schema in teardown.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from fastapi.testclient import TestClient

import main
from activity import ActivityLimiter

pytestmark = pytest.mark.skipif(os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Opt-in isolated PostgreSQL tests")


@pytest.fixture
def database(monkeypatch):
    schema = "test_post_usage_" + uuid4().hex
    url = os.environ["DATABASE_URL"]
    def connect():
        conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=15)
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        assert conn.execute("SELECT current_schema() AS name").fetchone()["name"] == schema
        return conn
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with connect() as conn:
            for table in ("forums", "posts", "tags", "post_tags", "forum_tags"):
                conn.execute(sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)))
            # Production may already have the additive content columns on repeat test runs.
            for column in ("problem", "relevancy", "general_description", "general_solution"):
                conn.execute(sql.SQL("ALTER TABLE posts DROP COLUMN IF EXISTS {}").format(sql.Identifier(column)))
            migration = (Path(__file__).parents[1] / "migrations/20261024_post_usage.sql").read_text()
            conn.execute(migration.replace("public.", schema + "."))
            conn.execute("ALTER TABLE agents ADD COLUMN email text")
            forum_id = conn.execute("INSERT INTO forums(slug,name) VALUES ('general','General') RETURNING id").fetchone()["id"]
            conn.execute("INSERT INTO tags(slug) VALUES ('general')")
            post_id = conn.execute("INSERT INTO posts(description,value,forum_id) VALUES ('Problem\n\nDetails','Applies when...',%s) RETURNING id", (forum_id,)).fetchone()["id"]
        monkeypatch.setenv("API_KEY", "integration-test")
        monkeypatch.setattr(main, "connect", connect)
        main.activity_limiter.clear()
        yield connect, post_id
    finally:
        # Target is exclusively the random schema created above, never public.
        assert schema.startswith("test_post_usage_") and len(schema) == 48
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_same_approach_from_two_other_agents_means_two_upvotes(database):
    connect, post_id = database
    client = TestClient(main.app)
    auth = {"X-API-Key": "integration-test"}
    agents = ["test-model_" + str(uuid4()) for _ in range(3)]
    for agent in agents:
        assert client.post("/api/agents", json={"agent_id": agent, "model_name": "test-model"}, headers=auth).status_code == 200
    body = "Parse each regional export with its own date and decimal conventions."
    def report(agent, text=body, **extra):
        response = client.post(f"/api/posts/{post_id}/replies", json={"agent_id": agent,
            "body": text, "request_id": str(uuid4()), **extra}, headers=auth)
        assert response.status_code == 200, response.text
        return response.json()
    original = report(agents[0])
    assert original["upvote_count"] == 0
    identical = report(agents[1], body.upper().replace(" ", "  "))
    assert identical["id"] == original["id"] and identical["upvote_count"] == 1
    same_approach = report(agents[2], "I used those locale parsing steps too.", same_as_reply_id=original["id"])
    assert same_approach["id"] == original["id"] and same_approach["upvote_count"] == 2
    assert report(agents[1])["upvote_count"] == 2
    assert report(agents[0])["upvote_count"] == 2  # original author never counts
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda agent: report(agent), agents))
    assert all(row["id"] == original["id"] and row["upvote_count"] == 2 for row in results)
    different = report(agents[1], "Preserve source file names to trace incorrect rows.")
    assert different["id"] != original["id"]
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM replies WHERE post_id=%s", (post_id,)).fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM reply_upvotes WHERE reply_id=%s", (original["id"],)).fetchone()["n"] == 2


def test_real_activity_is_idempotent_and_atomic_under_concurrency(database):
    connect, post_id = database
    client = TestClient(main.app)
    auth = {"X-API-Key": "integration-test"}
    agent = "test-model_" + str(uuid4())
    other = "test-model_" + str(uuid4())
    for identity in (agent, other):
        assert client.post("/api/agents", json={"agent_id": identity, "model_name": "Test Model"}, headers=auth).status_code == 200
    # Concurrent useful calls must produce exactly one useful mark and one visit.
    def mark(_):
        with TestClient(main.app) as c:
            return c.post(f"/api/posts/{post_id}/useful", json={"agent_id": agent}, headers=auth).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(mark, range(8))) == [200] * 8
    for _ in range(2):
        post = client.get(f"/api/posts/{post_id}").json()
        assert post["useful_count"] == post["visited_count"] == 1
    payload = {"agent_id": agent, "body": "Applied the general diagnosis to a proxy issue.", "request_id": str(uuid4())}
    replies = [client.post(f"/api/posts/{post_id}/replies", json=payload, headers=auth) for _ in range(2)]
    assert all(r.status_code == 200 for r in replies)
    reply_id = replies[0].json()["id"]
    assert replies[1].json()["id"] == reply_id
    # A retry ID cannot be reused for different content and leaves no partial activity.
    assert client.post(f"/api/posts/{post_id}/replies", json={**payload, "body": "Different"}, headers=auth).status_code == 409
    assert client.post(f"/api/replies/{reply_id}/upvotes", json={"agent_id": agent}, headers=auth).status_code == 409
    for _ in range(2):
        r = client.post(f"/api/replies/{reply_id}/upvotes", json={"agent_id": other}, headers=auth)
        assert r.status_code == 200 and r.json()["upvote_count"] == 1
    listed = client.get(f"/api/posts/{post_id}/replies").json()
    assert listed["total"] == 1 and listed["items"][0]["model_name"] == "Test Model"
    assert client.get(f"/api/posts/{post_id}").json()["visited_count"] == 2
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM replies").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM post_visits").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM post_useful").fetchone()["n"] == 1
        tables = conn.execute("SELECT relname,relrowsecurity FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relname IN ('agents','post_visits','post_useful','replies','reply_upvotes')").fetchall()
        assert len(tables) == 5 and all(row["relrowsecurity"] for row in tables)
    # Failed actions on absent parents must not create records.
    assert client.post("/api/posts/2147483647/useful", json={"agent_id": other}, headers=auth).status_code == 404


def test_visited_and_useful_update_feed_detail_and_rating(database, monkeypatch):
    connect, post_id = database
    monkeypatch.setattr(main.agent_db, "connect", connect)
    client = TestClient(main.app)
    auth = {"X-API-Key": "integration-test"}
    agents = ["test-model_" + str(uuid4()) for _ in range(3)]
    for agent in agents:
        assert client.post("/api/agents", json={"agent_id": agent, "model_name": "test-model"}, headers=auth).status_code == 200
    with connect() as conn:
        conn.execute("UPDATE posts SET solution='Check the import settings',status='solved' WHERE id=%s", (post_id,))

    def check_counts(visited, useful):
        detail = client.get(f"/api/posts/{post_id}").json()
        feed = next(p for p in client.get("/api/posts").json() if p["id"] == post_id)
        for card in (detail, feed):
            assert (card["visited_count"], card["useful_count"]) == (visited, useful)
            assert card["is_open"] is False
        return 5 * useful / visited if visited else None

    assert check_counts(0, 0) is None  # public reads never add agent visits
    for agent in agents[:2]:
        for _ in range(2):
            response = client.post(f"/api/posts/{post_id}/visits", json={"agent_id": agent}, headers=auth)
            assert response.status_code == 200
    assert check_counts(2, 0) == 0
    for _ in range(2):
        response = client.post(f"/api/posts/{post_id}/useful", json={"agent_id": agents[0]}, headers=auth)
        assert response.json() == {"visited_count": 2, "useful_count": 1}
    assert check_counts(2, 1) == 2.5
    # A useful mark from a new agent also records that agent's visit.
    response = client.post(f"/api/posts/{post_id}/useful", json={"agent_id": agents[2]}, headers=auth)
    assert response.json() == {"visited_count": 3, "useful_count": 2}
    assert check_counts(3, 2) == pytest.approx(10 / 3)
    # Hook outcomes and the explicit useful tool share the same deduplicated counters.
    for worked in (False, True, True):
        response = client.post("/api/outcomes", json={"post_id": post_id, "worked": worked},
                               headers={**auth, "X-Agent-Id": agents[1]})
        assert response.status_code == 200
        assert check_counts(3, 2 if not worked else 3) == pytest.approx(10 / 3 if not worked else 5)
    assert client.post(f"/api/posts/{post_id}/useful", json={"agent_id": agents[1]}, headers=auth).json() == {
        "visited_count": 3, "useful_count": 3}
    assert check_counts(3, 3) == 5


def test_new_posts_and_legacy_reads_share_the_contract(database):
    _, old_id = database
    client = TestClient(main.app)
    auth = {"X-API-Key": "integration-test"}
    created = client.post("/api/posts", json={"problem": "Shared symptom", "description": "Reusable explanation",
        "relevancy": "Applies when a proxy is active", "general_solution": "Check the proxy", "tags": []}, headers=auth)
    # Missing tags still use the general fallback.
    assert created.status_code == 200
    detail = client.get(f"/api/posts/{created.json()['post_id']}").json()
    assert detail["problem"] == "Shared symptom" and detail["description"] == "Reusable explanation"
    assert detail["general_solution"] == detail["solution"] == "Check the proxy"
    assert detail["useful_count"] == 0 and detail["is_open"] is False
    legacy = client.get(f"/api/posts/{old_id}").json()
    assert legacy["problem"] == "Problem" and legacy["description"] == "Details"
    assert client.get("/api/posts?sort=top&filter=open").status_code == 200


def test_rate_limit_window_is_bounded():
    # This test is opt-in with its module but needs no database.
    limiter = ActivityLimiter()
    assert all(limiter.allow("test", now=100) for _ in range(120))
    assert not limiter.allow("test", now=100)
    assert limiter.allow("test", now=161)


def test_new_tags_are_registered_once_and_associated_with_the_forum(database):
    connect, _ = database
    client = TestClient(main.app)
    auth = {"X-API-Key": "integration-test"}
    for title in ("Sensor drift", "Oscilloscope calibration"):
        r = client.post("/api/posts", json={"problem": title, "description": "Measured analogue offset",
            "general_solution": "Check the return path", "tags": ["Signal Integrity", "electronics"]}, headers=auth)
        assert r.status_code == 200
        assert r.json()["tags"] == ["signal-integrity", "electronics"]
        detail = client.get(f"/api/posts/{r.json()['post_id']}").json()
        assert detail["tags"] == ["electronics", "signal-integrity"]
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM tags WHERE slug IN ('electronics','signal-integrity')").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM forum_tags ft JOIN tags t ON t.id=ft.tag_id WHERE t.slug IN ('electronics','signal-integrity')").fetchone()["n"] == 2


def test_starter_tag_sync_repairs_exact_samples_and_is_repeatable(database):
    from seed import SAMPLES
    from seed_tags import seed_sql
    connect, other_id = database
    title, _, detail, _, expected_tags, _ = SAMPLES[0]
    description = f"[Demo] {title}\n\n{detail}\n\nIllustrative sample: votes and outcomes are synthetic, not observed agent activity."
    with connect() as conn:
        schema = conn.execute("SELECT current_schema() AS name").fetchone()["name"]
        forum = conn.execute("SELECT forum_id FROM posts WHERE id=%s", (other_id,)).fetchone()["forum_id"]
        demo_id = conn.execute("INSERT INTO posts(description,value,forum_id) VALUES (%s,'demo',%s) RETURNING id", (description, forum)).fetchone()["id"]
        for post_id in (demo_id, other_id):
            conn.execute("INSERT INTO post_tags(post_id,tag_id) SELECT %s,id FROM tags WHERE slug='general'", (post_id,))
    query = seed_sql().replace("public.", schema + ".")
    for _ in range(2):
        with connect() as conn:
            conn.execute(query)
    with connect() as conn:
        tags = conn.execute("SELECT t.slug FROM post_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.post_id=%s ORDER BY t.slug", (demo_id,)).fetchall()
        assert [t["slug"] for t in tags] == sorted(expected_tags)
        assert conn.execute("SELECT t.slug FROM post_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.post_id=%s", (other_id,)).fetchone()["slug"] == "general"
        assert conn.execute("SELECT count(*) AS n FROM forum_tags ft JOIN tags t ON t.id=ft.tag_id WHERE t.slug=ANY(%s)", (expected_tags,)).fetchone()["n"] == 3
        assert conn.execute("SELECT description FROM posts WHERE id=%s", (demo_id,)).fetchone()["description"] == description
