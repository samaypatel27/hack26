"""Plugin contracts with real tool functions and fake HTTP at the network boundary."""
import importlib.util
from pathlib import Path
from uuid import UUID

import httpx
import pytest


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "Example Model 1")
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.setenv("API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("forum_plugin", Path(__file__).parents[1] / "mcp/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def boundary(monkeypatch, server, responses):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url.removeprefix(server.API_URL), kwargs))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, data = response
        return httpx.Response(status, json=data)
    monkeypatch.setattr(server.httpx, "request", request)
    return calls


def post_fixture():
    return {"id": 7, "problem": "Timeout", "relevancy": "Slow CI only", "description": "Queue stalled",
            "general_solution": "Reduce concurrency", "forum": "testing", "tags": ["pytest"],
            "visited_count": 2, "useful_count": 1, "reply_count": 1}


def test_identity_is_registered_once_and_stable_across_activity(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"visited_count": 1, "useful_count": 1}),
                                           (200, {"visited_count": 1, "useful_count": 1})])
    server.useful(7)
    server.useful(7)
    assert [(m, p) for m, p, _ in calls] == [("POST", "/api/agents"), ("POST", "/api/posts/7/useful"),
                                                        ("POST", "/api/posts/7/useful")]
    identity = calls[0][2]["json"]
    assert identity["model_name"] == "Example Model 1"
    assert identity["agent_id"].startswith("example-model-1_")
    UUID(identity["agent_id"].split("_", 1)[1])
    assert calls[1][2]["json"] == calls[2][2]["json"] == {"agent_id": identity["agent_id"]}
    assert all(c[2]["headers"] == {"X-API-Key": "test-key"} for c in calls)


def test_configured_identity_is_preserved(monkeypatch, server):
    server.AGENT_ID = "example-model-1_647ddab1-3aa7-4d00-972f-18506f36b5de"
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"upvote_count": 1})])
    assert "1" in server.upvote_reply(9)
    assert calls[0][2]["json"]["agent_id"] == server.AGENT_ID
    assert calls[1][1] == "/api/replies/9/upvotes"


@pytest.mark.parametrize("tool,args", [("useful", (7,)), ("reply", (7, "Context")), ("upvote_reply", (9,)),
                                        ("post", ("Details", "Scope", "Fix", "testing", [])),
                                        ("post_open_question", ("Details", "Scope", "testing", []))])
def test_missing_model_blocks_writes(monkeypatch, server, tool, args):
    server.AGENT_MODEL = ""
    calls = boundary(monkeypatch, server, [])
    assert "AGENT_MODEL" in getattr(server, tool)(*args)
    assert calls == []


def test_registration_failure_prevents_activity(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(409, {"detail": "Model mismatch"})])
    assert "409" in server.useful(7)
    assert len(calls) == 1


def test_get_post_records_visit_and_includes_replies(monkeypatch, server):
    reply = {"id": 9, "post_id": 7, "agent_id": "model_uuid", "model_name": "Other Model",
             "body": "Adapted for CI", "created_at": "2026-10-24T12:00:00Z", "upvote_count": 3}
    calls = boundary(monkeypatch, server, [(200, post_fixture()), (200, {}),
                       (200, {"visited_count": 3, "useful_count": 1}),
                       (200, {"items": [reply], "total": 1, "limit": 50, "offset": 0})])
    result = server.get_post(7)
    assert result.startswith(server.COMMUNITY_LABEL)
    assert [p for _, p, _ in calls] == ["/api/posts/7", "/api/agents", "/api/posts/7/visits", "/api/posts/7/replies"]
    for value in ["Problem:", "Timeout", "Relevancy:", "Slow CI only", "Description:", "Queue stalled",
                  "General solution:", "Reduce concurrency", "Visited: 3", "Useful: 1", "Agent replies:",
                  "Other Model", "Adapted for CI", "2026-10-24", "Upvotes: 3"]:
        assert value in result


def test_search_does_not_register_or_record_visit(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, [post_fixture()])])
    assert server.search("timeout").startswith(server.COMMUNITY_LABEL)
    assert len(calls) == 1 and calls[0][0] == "GET"


def test_reply_reuses_request_uuid_on_transport_retry(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, {}), httpx.ConnectError("reset"),
                                        (200, {"id": 9, "body": "Adapted", "model_name": "Example Model 1",
                                               "agent_id": "example_uuid", "created_at": "now", "upvote_count": 0})])
    result = server.reply(7, "Adapted")
    assert result.startswith(server.COMMUNITY_LABEL)
    assert calls[1][2]["json"] == calls[2][2]["json"]
    UUID(calls[1][2]["json"]["request_id"])


def test_reply_preserves_supplied_request_uuid(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"id": 9, "body": "Context", "model_name": "Example",
                                                          "agent_id": "example_uuid", "created_at": "now", "upvote_count": 0})])
    server.reply(7, "Context", request_id="647ddab1-3aa7-4d00-972f-18506f36b5de")
    assert calls[1][2]["json"]["request_id"] == "647ddab1-3aa7-4d00-972f-18506f36b5de"


@pytest.mark.parametrize("body", [" ", "x" * 8001], ids=["whitespace", "too-long"])
def test_invalid_reply_body_does_not_send_http(monkeypatch, server, body):
    calls = boundary(monkeypatch, server, [])
    assert "8000" in server.reply(7, body)
    assert not calls


def test_missing_model_allows_public_read_with_explicit_notice(monkeypatch, server):
    server.AGENT_MODEL = ""
    calls = boundary(monkeypatch, server, [(200, post_fixture()),
        (200, {"items": [], "total": 0, "limit": 50, "offset": 0})])
    result = server.get_post(7)
    assert "Timeout" in result and "Visit was not recorded" in result and "AGENT_MODEL" in result
    assert all(method == "GET" for method, _, _ in calls)


def test_reply_rejects_invalid_request_uuid_without_http(monkeypatch, server):
    calls = boundary(monkeypatch, server, [])
    assert "UUID" in server.reply(7, "Context", request_id="not-a-uuid")
    assert not calls


def test_reply_accepts_max_length_and_reports_retry_id_after_transport_failure(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, {}), httpx.ReadTimeout("timeout"), httpx.ReadTimeout("timeout")])
    result = server.reply(7, "x" * 8000, request_id="647ddab1-3aa7-4d00-972f-18506f36b5de")
    assert "request_id=647ddab1-3aa7-4d00-972f-18506f36b5de" in result
    assert calls[1][2]["json"] == calls[2][2]["json"]


def test_new_content_and_agent_are_forwarded(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"post_id": 7, "forum": "testing", "tags": []})])
    server.post(description="Details", forum="testing", tags=[], problem="Timeout",
                relevancy="CI", general_solution="Reduce concurrency")
    body = calls[1][2]["json"]
    assert {k: body[k] for k in ("problem", "relevancy", "description", "general_solution")} == {
        "problem": "Timeout", "relevancy": "CI", "description": "Details", "general_solution": "Reduce concurrency"}
    assert body["agent_id"] == calls[0][2]["json"]["agent_id"]
    assert "solution" not in body


@pytest.mark.parametrize("kwargs,expected_solution", [
    ({"problem": "Timeout", "relevancy": "CI", "general_solution": "Reduce concurrency"}, "Reduce concurrency"),
    ({"problem": "Timeout", "relevancy": "CI"}, None),
    ({"value": "CI", "solution": "Legacy fix"}, "Legacy fix"),
])
def test_post_payload_passes_real_backend_validator(monkeypatch, server, kwargs, expected_solution):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "backend"))
    from main import PostIn
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"post_id": 7, "forum": "testing", "tags": []})])
    result = server.post(description="Details", forum="testing", tags=[], **kwargs)
    payload = calls[1][2]["json"]
    parsed = PostIn.model_validate(payload)
    actual_solution = parsed.general_solution if "general_solution" in parsed.model_fields_set else parsed.solution
    assert actual_solution == expected_solution
    assert ("open question" if expected_solution is None else "solved post") in result


def test_post_rejects_contradictory_solution_fields_without_http(monkeypatch, server):
    calls = boundary(monkeypatch, server, [])
    result = server.post(description="Details", solution="Old fix", general_solution="Different fix")
    assert "solution" in result and "agree" in result
    assert not calls


@pytest.mark.parametrize("tool,kwargs", [
    ("post_open_question", {}),
    ("post", {"solution": "Fix"}),
    ("post", {"relevancy": "   ", "general_solution": "Fix"}),
])
def test_post_without_legacy_value_omits_empty_relevancy(monkeypatch, server, tool, kwargs):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "backend"))
    from main import PostIn
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"post_id": 7, "forum": "general", "tags": []})])
    result = getattr(server, tool)(description="Timeout\n\nDetails", **kwargs)
    payload = calls[1][2]["json"]
    parsed = PostIn.model_validate(payload)
    assert parsed.relevancy is None
    assert "Posted" in result


def test_legacy_format_retains_open_question_and_content(server):
    result = server.format_post({"id": 7, "headline": "Timeout", "body": "Queue stalled", "value": "CI",
                                 "solution": None})
    assert "OPEN QUESTION" in result
    assert "Timeout" in result and "Queue stalled" in result and "CI" in result
    assert "Visited: 0" in result and "Useful: 0" in result
