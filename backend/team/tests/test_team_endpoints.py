"""POST /api/help -> GET /api/help/{run_id} -> POST /api/rerun through FastAPI's TestClient (fake models, memory store)."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from team import config
from team import store as store_mod
from team.fakes import FakeAnthropic, FakeOpenAI

API_KEY = "test-key"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture()
def api(monkeypatch, prisma_case):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import team_api

    monkeypatch.setenv("API_KEY", API_KEY)
    store = store_mod.MemoryStore()
    store_mod.set_store(store)
    fake = FakeAnthropic(delays={"docs": 0.2, "issues": 0.2})
    monkeypatch.setattr(team_api.STATE, "clients", config.Clients(anthropic=fake, openai=FakeOpenAI()))
    monkeypatch.setattr(team_api.STATE, "search_fn", lambda case: [])
    monkeypatch.setattr(team_api.STATE, "executor", ThreadPoolExecutor(max_workers=2))
    app = FastAPI()
    app.include_router(team_api.router)
    with TestClient(app) as client:
        client.store = store
        client.fake = fake
        yield client
    store_mod.set_store(None)


def poll(api, run_id, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = api.get(f"/api/help/{run_id}").json()
        if body["status"] != "working":
            return body
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def sample():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "samples" / "prisma_alpine_docker.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_help_needs_the_api_key(api):
    assert api.post("/api/help", json=sample()).status_code == 401


def test_help_rejects_an_empty_case(api):
    r = api.post("/api/help", json={"tried": []}, headers=AUTH)
    assert r.status_code == 400 and "symptom" in r.json()["detail"]


def test_full_flow_help_then_check_help(api):
    t0 = time.monotonic()
    r = api.post("/api/help", json=sample(), headers=AUTH)
    assert r.status_code == 200 and time.monotonic() - t0 < 1.0
    body = r.json()
    assert body["message"].startswith("TEAM_STARTED: tr_") and body["decision"] == "team_started"
    run_id = body["run_id"]

    first = api.get(f"/api/help/{run_id}").json()
    assert first["status"] in ("working", "report")
    if first["status"] == "working":
        assert first["message"].startswith("TEAM_WORKING: run " + run_id)

    done = poll(api, run_id)
    assert done["status"] == "report" and done["message"].startswith("TEAM_REPORT: run " + run_id)
    assert done["report"]["hypotheses"][0]["rank"] == 1
    assert done["report"]["models"]["chair"] == "claude-opus-5-5"
    members = {e["member"] for e in done["events"]}
    assert {"docs", "issues", "community", "chair", "runner"} <= members
    ids = [e["id"] for e in done["events"]]
    later = api.get(f"/api/help/{run_id}", params={"since": ids[-2]}).json()
    assert [e["id"] for e in later["events"]] == ids[-1:]


def test_mcp_tool_body_works(api):
    """Exactly what plugin/mcp/server.py ask_for_help sends today."""
    body = {"description": "API crashes in Docker", "command": "docker compose up --build api",
            "error": "PrismaClientInitializationError: could not locate the Query Engine for linux-musl-openssl-3.0.x",
            "tried": ["npm ci again", "prisma generate on the laptop"]}
    r = api.post("/api/help", json=body, headers=AUTH).json()
    assert r["message"].startswith("TEAM_STARTED: ")
    assert poll(api, r["run_id"])["status"] == "report"


def test_known_fix_short_circuits(api, monkeypatch):
    import team_api

    monkeypatch.setattr(team_api.STATE, "search_fn", lambda case: [
        {"id": 1, "solution": "Add binaryTargets.", "match": "fingerprint", "score": 1.0}])
    r = api.post("/api/help", json=sample(), headers=AUTH).json()
    assert r["message"].startswith("KNOWN_FIX:") and r["run_id"] is None


def test_unknown_run_is_404(api):
    assert api.get("/api/help/tr_nope").status_code == 404


def test_failed_run_reads_team_failed(api, monkeypatch):
    import team_api

    monkeypatch.setattr(team_api.STATE, "clients", config.Clients(
        anthropic=FakeAnthropic(lambda k, kw: RuntimeError("down")), openai=None))
    monkeypatch.setattr(team_api.gate, "classify", lambda *a, **k: None)
    r = api.post("/api/help", json=sample(), headers=AUTH).json()
    done = poll(api, r["run_id"])
    assert done["status"] == "failed" and done["message"].startswith("TEAM_FAILED")


def test_stale_running_row_becomes_failed(api):
    api.store.create_run("tr_stale", case={}, models={}, source="help", deadline_s=1)
    api.store._runs["tr_stale"]["deadline_at"] -= timedelta(seconds=60)
    body = api.get("/api/help/tr_stale").json()
    assert body["status"] == "failed" and body["message"].startswith("TEAM_FAILED")


def test_rerun_runs_open_questions_with_the_chosen_model(api, monkeypatch):
    import team_api

    questions = [{"id": 3, "description": "Gradle hangs on CI only\n\nTried --no-daemon", "solution": None,
                  "command": "./gradlew build", "fingerprint": None, "tags": ["gradle"]},
                 {"id": 4, "description": "already solved", "solution": "x", "tags": []}]
    notified = []
    monkeypatch.setattr(team_api.STATE, "open_questions_fn", lambda ids, limit: questions)
    monkeypatch.setattr(team_api.STATE, "notify_fn", lambda qid, msg: notified.append((qid, msg)))
    assert api.post("/api/rerun", json={"model": "claude-opus-5-5"}).status_code == 401
    assert api.post("/api/rerun", json={"open_question_ids": "all"}, headers=AUTH).status_code == 400
    r = api.post("/api/rerun", json={"model": "claude-fable-5-1", "open_question_ids": [3, 4]}, headers=AUTH).json()
    assert len(r["run_ids"]) == 1 and r["open_question_ids"] == [3]
    assert r["models"]["chair"] == "claude-fable-5-1" and r["models"]["docs"] == "claude-fable-5-1"
    assert "Possible answer" in r["note"]
    done = poll(api, r["run_ids"][0])
    assert done["status"] == "report" and done["source"] == "rerun" and done["open_question_id"] == 3
    assert done["report"]["models"]["chair"] == "claude-fable-5-1"
    time.sleep(0.2)
    assert notified and notified[0][0] == 3 and "possible answer" in notified[0][1]
    models_used = {kw["model"] for _, kw in api.fake.calls}
    assert "claude-fable-5-1" in models_used and "claude-sonnet-5-5" not in models_used


def test_rerun_with_an_openai_model_sets_the_community_seat():
    assert config.override_for("gpt-6.1-sol") == {"community": "gpt-6.1-sol"}
