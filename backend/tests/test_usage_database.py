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
            for table in ("forums", "posts", "tags", "post_tags"):
                conn.execute(sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)))
            # Production may already have the additive content columns on repeat test runs.
            for column in ("problem", "relevancy", "general_description", "general_solution"):
                conn.execute(sql.SQL("ALTER TABLE posts DROP COLUMN IF EXISTS {}").format(sql.Identifier(column)))
            migration = (Path(__file__).parents[1] / "migrations/20261024_post_usage.sql").read_text()
            conn.execute(migration.replace("public.", schema + "."))
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


def test_new_posts_and_legacy_reads_share_the_contract(database):
    _, old_id = database
    client = TestClient(main.app)
    auth = {"X-API-Key": "integration-test"}
    created = client.post("/api/posts", json={"problem": "Shared symptom", "description": "Reusable explanation",
        "relevancy": "Applies when a proxy is active", "general_solution": "Check the proxy", "tags": []}, headers=auth)
    # Insert a known fallback tag first: create_post resolves unknown tags to general.
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
