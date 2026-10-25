import os

import pytest
from fastapi.testclient import TestClient

import main
from test_api import ROW, fake
from test_usage_database import database

client = TestClient(main.app)


def test_related_returns_shared_tags_and_both_statuses_without_writes(fake):
    conn = fake([{"id": 7}], [
        {**ROW, "id": 8, "shared_tags": ["cors", "fastapi"]},
        {**ROW, "id": 9, "solution": "Fix", "shared_tags": ["cors"]},
    ])
    response = client.get("/api/posts/7/related")
    assert response.status_code == 200
    assert [p["id"] for p in response.json()] == [8, 9]
    assert [p["is_open"] for p in response.json()] == [True, False]
    assert response.json()[0]["shared_tags"] == ["cors", "fastapi"]
    assert all("INSERT" not in query and "UPDATE" not in query for query, _ in conn.calls)


def test_missing_source_is_404(fake):
    conn = fake([])
    assert client.get("/api/posts/999/related").status_code == 404
    assert len(conn.calls) == 1


def test_no_matching_tags_returns_empty_list(fake):
    fake([{"id": 7}], [])
    assert client.get("/api/posts/7/related").json() == []


@pytest.mark.skipif(os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Opt-in isolated PostgreSQL ranking check")
def test_real_related_ranks_limits_excludes_self_and_includes_other_forums(database):
    connect, source_id = database
    with connect() as conn:
        forum = conn.execute("SELECT id FROM forums LIMIT 1").fetchone()["id"]
        other_forum = conn.execute("INSERT INTO forums(slug,name) VALUES ('backend','Backend') RETURNING id").fetchone()["id"]
        tag_ids = [conn.execute("INSERT INTO tags(slug) VALUES (%s) RETURNING id", (tag,)).fetchone()["id"] for tag in ("python", "networking", "docker")]
        for tag_id in tag_ids[:2]:
            conn.execute("INSERT INTO post_tags(post_id,tag_id) VALUES (%s,%s)", (source_id, tag_id))
        ids = []
        # Both 2-tag matches outrank the newest 1-tag match. Equal timestamps tie by ID.
        for label, indices, solution, forum_id in (
            ("Two tags open", [0, 1], None, other_forum),
            ("Two tags solved", [0, 1], "Fix", forum),
            ("One tag newest", [0], None, forum),
            ("One tag older", [1], "Fix", forum),
            ("Unrelated", [2], "Fix", forum),
        ):
            post_id = conn.execute("INSERT INTO posts(description,value,solution,forum_id,created_at) VALUES (%s,'v',%s,%s,'2026-01-01') RETURNING id", (label, solution, forum_id)).fetchone()["id"]
            ids.append(post_id)
            for index in indices:
                conn.execute("INSERT INTO post_tags(post_id,tag_id) VALUES (%s,%s)", (post_id, tag_ids[index]))
        conn.execute("UPDATE posts SET created_at='2026-02-01' WHERE id=%s", (ids[2],))
    response = client.get(f"/api/posts/{source_id}/related")
    assert response.status_code == 200
    related = response.json()
    assert [p["id"] for p in related] == [ids[1], ids[0], ids[2]]
    assert [p["shared_tags"] for p in related] == [["networking", "python"], ["networking", "python"], ["python"]]
    assert [p["is_open"] for p in related] == [False, True, True]
    assert related[1]["forum"] == "backend"
    # Untagged sources get no arbitrary fallback recommendations.
    with connect() as conn:
        conn.execute("DELETE FROM post_tags WHERE post_id=%s", (source_id,))
    assert client.get(f"/api/posts/{source_id}/related").json() == []
