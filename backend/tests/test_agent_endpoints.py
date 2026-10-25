"""Endpoint tests for backend/agent.py through FastAPI's TestClient, against a throwaway Postgres.

Seed data: plugin/hook/dev/mock_data.json (post 1 Prisma/51749bd5 solved, 2 ERR_REQUIRE_ESM solved,
3 pytest/conftest OPEN, 4 docker-compose postgres solved), stored in the stub Breakpoint tables.
New posts get ids from 5. Known tags and forums are set in conftest.py.
"""

import time
from types import SimpleNamespace
from typing import Any

import pytest

PUBLIC_FIELDS = {"id", "description", "value", "solution", "tags", "status", "upvotes", "times_worked",
                 "times_applied", "fingerprint", "command", "forum", "created_at"}
PRISMA_ERROR = ('PrismaClientInitializationError: Prisma Client could not locate the Query Engine for runtime '
                '"linux-musl-openssl-3.0.x".')


def get_meta(sql, post_id: int) -> dict[str, Any]:
    return sql("SELECT times_applied, times_worked FROM agent_post_meta WHERE post_id = %s", (post_id,))[0]


# ---------------------------------------------------------------------------
# 1. GET /api/search
# ---------------------------------------------------------------------------

def test_search_fingerprint_first(client):
    r = client.get("/api/search", params={"error": "anything", "fingerprint": "51749bd5"})
    assert r.status_code == 200
    first = r.json()[0]
    assert (first["id"], first["match"], first["score"]) == (1, "fingerprint", 1.0)
    assert set(first) == PUBLIC_FIELDS | {"match", "score"}
    assert set(first["tags"]) == {"prisma", "docker", "alpine"} and first["status"] == "solved"
    assert first["forum"] == "docker" and first["upvotes"] == 14


def test_search_fingerprint_beats_text(client):
    r = client.get("/api/search", params={"error": PRISMA_ERROR, "fingerprint": "e46fc17b"})
    ids = [p["id"] for p in r.json()]
    assert ids[0] == 2 and r.json()[0]["match"] == "fingerprint"
    assert 1 in ids and r.json()[ids.index(1)]["match"] == "text"


def test_search_text_match(client):
    r = client.get("/api/search", params={
        "error": "Error [ERR_REQUIRE_ESM]: require() of ES Module node-fetch not supported",
        "command": "node src/api.js"})
    top = r.json()[0]
    assert top["id"] == 2 and top["match"] == "text" and 0.2 <= top["score"] < 1


def test_search_hook_shape_prisma(client):
    """What the hook sends for the stuck_known_fix scenario: error text, command, fingerprint."""
    r = client.get("/api/search", params={"error": PRISMA_ERROR, "command": "docker compose up --build api",
                                         "fingerprint": "51749bd5"})
    assert r.json()[0]["id"] == 1 and r.json()[0]["match"] == "fingerprint"


def test_search_open_question(client):
    r = client.get("/api/search", params={"error": "ModuleNotFoundError: No module named app conftest",
                                         "tags": "python"})
    top = r.json()[0]
    assert top["id"] == 3 and top["status"] == "open" and top["solution"] is None


def test_search_tags_filter_text_matches_only(client):
    r = client.get("/api/search", params={"error": PRISMA_ERROR, "tags": "python"})
    assert all(p["id"] != 1 for p in r.json())
    r = client.get("/api/search", params={"error": PRISMA_ERROR, "fingerprint": "51749bd5", "tags": "python"})
    assert r.json()[0]["id"] == 1
    r = client.get("/api/search", params={"error": PRISMA_ERROR, "tags": "not-a-known-tag"})
    assert r.json()[0]["id"] == 1          # unknown tags are ignored, not a filter that hides everything


def test_search_nothing(client):
    assert client.get("/api/search", params={"error": "segmentation fault in quantum flux capacitor"}).json() == []
    assert client.get("/api/search").json() == []
    assert client.get("/api/search", params={"fingerprint": "not-hex!"}).json() == []


def test_search_is_public_and_fast(client):
    start = time.perf_counter()
    r = client.get("/api/search", params={"error": PRISMA_ERROR})
    assert r.status_code == 200 and time.perf_counter() - start < 2.0


def test_search_finds_website_posts_without_agent_metadata(client, sql):
    forum = sql("SELECT id FROM forums WHERE slug = 'general'")[0]["id"]
    sql("INSERT INTO posts (description, value, solution, forum_id, status) VALUES (%s, 'v', 'fix', %s, 'solved')",
        ("Webpack build fails with EMFILE too many open files on macOS\nEMFILE: too many open files, watch", forum))
    r = client.get("/api/search", params={"error": "Error: EMFILE: too many open files, watch"}).json()
    assert r[0]["description"].startswith("Webpack") and r[0]["fingerprint"] is None and r[0]["times_applied"] == 0


# ---------------------------------------------------------------------------
# 2. POST /api/outcomes
# ---------------------------------------------------------------------------

def test_outcome_counts(client, auth, sql):
    body = {"post_id": 1, "worked": True, "fingerprint": "51749bd5", "command": "docker compose up --build api",
            "session_id": "abc-123", "source": "hook", "extra_field": "ignored"}
    r = client.post("/api/outcomes", json=body, headers=auth)
    assert r.status_code == 200 and r.json() == {"post_status": "solved"}
    r = client.post("/api/outcomes", json={**body, "worked": False}, headers=auth)
    assert r.json() == {"post_status": "solved"}          # we never reopen
    meta = get_meta(sql, 1)
    assert (meta["times_applied"], meta["times_worked"]) == (12, 10)
    rows = sql("SELECT worked, session_id, source FROM agent_outcomes WHERE post_id = 1 ORDER BY id")
    assert rows == [{"worked": True, "session_id": "abc-123", "source": "hook"},
                    {"worked": False, "session_id": "abc-123", "source": "hook"}]


def test_outcome_on_post_without_metadata(client, auth, sql):
    forum = sql("SELECT id FROM forums WHERE slug = 'general'")[0]["id"]
    post_id = sql("INSERT INTO posts (description, value, solution, forum_id) VALUES ('d', 'v', 'fix', %s)"
                  " RETURNING id", (forum,))[0]["id"]
    assert client.post("/api/outcomes", json={"post_id": post_id, "worked": True}, headers=auth).json() == \
        {"post_status": "solved"}
    assert get_meta(sql, post_id)["times_worked"] == 1


def test_outcome_on_open_question_stays_open(client, auth):
    r = client.post("/api/outcomes", json={"post_id": 3, "worked": True}, headers=auth)
    assert r.json() == {"post_status": "open"}


def test_outcome_errors(client, auth):
    assert client.post("/api/outcomes", json={"post_id": 999, "worked": False}, headers=auth).status_code == 404
    r = client.post("/api/outcomes", json={"post_id": 1, "worked": "yes"}, headers=auth)
    assert r.status_code == 400 and "detail" in r.json()
    assert client.post("/api/outcomes", json={"worked": True}, headers=auth).status_code == 400
    assert client.post("/api/outcomes", json=[1, 2], headers=auth).status_code == 400
    assert client.post("/api/outcomes", json={"post_id": 1, "worked": True}).status_code == 401
    assert client.post("/api/outcomes", json={"post_id": 1, "worked": True},
                       headers={"X-API-Key": "wrong"}).status_code == 401


# ---------------------------------------------------------------------------
# 3. GET /api/updates
# ---------------------------------------------------------------------------

def test_updates_delivered_once(client, sql):
    sql("INSERT INTO agent_updates (project, message, post_id) VALUES ('proj-a', 'Your open question was solved.', 3)")
    r = client.get("/api/updates", params={"project": "proj-a"})
    assert r.status_code == 200
    assert r.json() == [{"message": "Your open question was solved.", "post_id": 3}]
    assert client.get("/api/updates", params={"project": "proj-a"}).json() == []
    assert client.get("/api/updates", params={"project": "proj-b"}).json() == []


def test_updates_without_project(client):
    assert client.get("/api/updates").json() == []


# ---------------------------------------------------------------------------
# 4. POST /api/judge (the model client is always mocked)
# ---------------------------------------------------------------------------

class FakeMessages:
    def __init__(self, text: str | None = None, error: Exception | None = None):
        self.text, self.error, self.calls = text, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


def fake_client(monkeypatch, **kwargs) -> FakeMessages:
    import judge
    messages = FakeMessages(**kwargs)
    monkeypatch.setattr(judge, "make_client", lambda: SimpleNamespace(messages=messages))
    return messages


CONTRACT_ACTIONS = [{"kind": "run", "cmd": "npm run build", "ok": False, "err_fp": "d5b1a4b9"},
                    {"kind": "edit", "path": "src/db.ts"}]
HOOK_ACTIONS = [{"kind": "run", "cmd": "npm run build", "err_fp": "d5b1a4b9", "tool": "Bash",
                 "summary": "npm run build", "ok": False, "out": "error TS2339 token=hunter22secret"},
                {"kind": "edit", "path": "src/db.ts", "tool": "Edit", "summary": "src/db.ts", "ok": True, "out": ""}]


def test_judge_without_api_key_is_unavailable(client, auth):
    r = client.post("/api/judge", json={"session_id": "s", "actions": CONTRACT_ACTIONS, "replies": []}, headers=auth)
    assert r.status_code == 200 and r.json() == {"stuck": False, "reason": "judge unavailable", "confidence": 0.0}


def test_judge_parses_model_answer(client, auth, monkeypatch):
    messages = fake_client(monkeypatch, text='Here you go:\n```json\n{"stuck": true, "reason": "Same build error '
                                               'after three edits.", "confidence": 0.85}\n```')
    r = client.post("/api/judge", json={"session_id": "s", "actions": HOOK_ACTIONS,
                                       "replies": ["This should work now."], "unknown": 1}, headers=auth)
    assert r.json() == {"stuck": True, "reason": "Same build error after three edits.", "confidence": 0.85}
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["output_config"] == {"effort": "low"}
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(call)
    prompt = call["messages"][0]["content"]
    assert "npm run build -> FAILED (error id d5b1a4b9)" in prompt and "src/db.ts" in prompt
    assert "This should work now." in prompt and "hunter22secret" not in prompt


def test_judge_contract_action_shape(client, auth, monkeypatch):
    messages = fake_client(monkeypatch, text='{"stuck": false, "reason": "Making progress.", "confidence": 2}')
    r = client.post("/api/judge", json={"actions": CONTRACT_ACTIONS}, headers=auth)
    assert r.json() == {"stuck": False, "reason": "Making progress.", "confidence": 1.0}
    assert "run: npm run build -> FAILED" in messages.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("kwargs", [{"text": "I think it is stuck."}, {"text": '{"stuck": "maybe"}'},
                                    {"error": TimeoutError("slow")}])
def test_judge_bad_answers_are_unavailable(client, auth, monkeypatch, kwargs):
    fake_client(monkeypatch, **kwargs)
    r = client.post("/api/judge", json={"actions": CONTRACT_ACTIONS}, headers=auth)
    assert r.status_code == 200 and r.json()["reason"] == "judge unavailable"


def test_judge_errors(client, auth):
    assert client.post("/api/judge", json={"actions": "nope"}, headers=auth).status_code == 400
    assert client.post("/api/judge", json={"actions": []}).status_code == 401


def test_judge_client_settings(monkeypatch):
    import judge
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-000000")
    c = judge.make_client()
    assert c.max_retries == 0 and float(c.timeout) == 4.5


def test_router_paths(client):
    paths = set(client.app.openapi()["paths"])
    assert paths == {"/api/search", "/api/outcomes", "/api/updates", "/api/judge"}
