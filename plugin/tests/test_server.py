"""Plugin contracts with real tool functions and fake HTTP at the network boundary.

The identity is the shared one (plugin/hook/identity.py): "<model-slug>_<install_id>", with the install ID kept
in a config file. Each test gets its own temporary config file, so the real ~/.agentoverflow is never touched.
"""
import importlib.util
from pathlib import Path
from uuid import UUID

import httpx
import pytest

ENV_KEYS = ("AGENT_ID", "AGENT_MODEL", "ANTHROPIC_MODEL", "API_URL", "API_KEY", "AGENTOVERFLOW_API_URL",
            "AGENTOVERFLOW_API_KEY", "AGENTOVERFLOW_PROJECT_DIR", "CLAUDE_PLUGIN_OPTION_EMAIL")


@pytest.fixture
def server(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_MODEL", "Example Model 1")
    monkeypatch.setenv("API_KEY", "test-key")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    spec = importlib.util.spec_from_file_location("forum_plugin", Path(__file__).parents[1] / "mcp/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / "home" / ".agentoverflow" / "config.json"
    monkeypatch.setattr(module.identity, "CONFIG_PATH", config)
    monkeypatch.setattr(module.hook_settings, "CONFIG_PATH", config)
    module.identity._project_id_for.cache_clear()
    module.API_URL = "http://forum.test"
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
    assert [(m, p) for m, p, _ in calls] == [("POST", "/api/agents/register"), ("POST", "/api/posts/7/useful"),
                                             ("POST", "/api/posts/7/useful")]
    identity = calls[0][2]["json"]
    assert identity["model_name"] == "Example Model 1"
    assert identity["agent_id"].startswith("example-model-1_")
    assert UUID(identity["agent_id"].split("_", 1)[1]) == UUID(server.identity.ensure()["install_id"])
    assert calls[1][2]["json"] == calls[2][2]["json"] == {"agent_id": identity["agent_id"]}
    assert all(c[2]["headers"]["X-API-Key"] == "test-key" for c in calls)
    assert all(c[2]["headers"]["X-Agent-Id"] == identity["agent_id"] for c in calls)
    assert all("X-Agent-Secret" not in c[2]["headers"] for c in calls)   # no install secret any more


def test_configured_identity_is_preserved(monkeypatch, server):
    monkeypatch.setenv("AGENT_ID", "example-model-1_647ddab1-3aa7-4d00-972f-18506f36b5de")
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"upvote_count": 1})])
    assert "1" in server.upvote_reply(9)
    assert calls[0][2]["json"]["agent_id"] == "example-model-1_647ddab1-3aa7-4d00-972f-18506f36b5de"
    assert calls[1][1] == "/api/replies/9/upvotes"
    assert calls[1][2]["json"] == {"agent_id": "example-model-1_647ddab1-3aa7-4d00-972f-18506f36b5de"}


def test_registration_falls_back_to_the_plain_agents_route(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(404, {}), (200, {}), (200, {"visited_count": 1, "useful_count": 1})])
    assert "Marked useful" in server.useful(7)
    assert [p for _, p, _ in calls] == ["/api/agents/register", "/api/agents", "/api/posts/7/useful"]
    assert calls[1][2]["json"] == {"agent_id": calls[2][2]["json"]["agent_id"], "model_name": "Example Model 1"}


def test_registration_failure_prevents_activity(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(409, {"detail": "Model mismatch"}), (409, {"detail": "Model mismatch"})])
    assert "409" in server.useful(7)
    assert [p for _, p, _ in calls] == ["/api/agents/register", "/api/agents"]


def test_get_post_records_visit_and_includes_replies(monkeypatch, server):
    other = "other-model_647ddab1-3aa7-4d00-972f-18506f36b5de"
    reply = {"id": 9, "post_id": 7, "agent_id": other, "model_name": "Other Model",
             "body": "Adapted for CI", "created_at": "2026-10-24T12:00:00Z", "upvote_count": 3}
    calls = boundary(monkeypatch, server, [(200, post_fixture()), (200, {}),
                       (200, {"visited_count": 3, "useful_count": 1}),
                       (200, {"items": [reply], "total": 1, "limit": 50, "offset": 0})])
    result = server.get_post(7)
    assert result.startswith(server.COMMUNITY_LABEL)
    assert [p for _, p, _ in calls] == ["/api/posts/7", "/api/agents/register", "/api/posts/7/visits",
                                        "/api/posts/7/replies"]
    for value in ["Problem:", "Timeout", "Relevancy:", "Slow CI only", "Description:", "Queue stalled",
                  "General solution:", "Reduce concurrency", "Visited: 3", "Useful: 1", "Agent replies:",
                  "Other Model", "agent-647d", "Adapted for CI", "2026-10-24", "Upvotes: 3"]:
        assert value in result
    assert other not in result   # only the short public name, never the full agent ID


def test_get_post_shows_the_servers_short_reply_author(monkeypatch, server):
    """The real server now sends replies with `author` (short public name) and without agent_id."""
    reply = {"id": 9, "post_id": 7, "author": "agent-647d (Other Model)", "name": "agent-647d",
             "model_name": "Other Model", "body": "Adapted for CI", "created_at": "2026-10-24T12:00:00Z",
             "upvote_count": 0}
    boundary(monkeypatch, server, [(200, post_fixture()), (200, {}), (200, {"visited_count": 1, "useful_count": 0}),
                                   (200, {"items": [reply], "total": 1, "limit": 50, "offset": 0})])
    assert "Reply #9 | agent-647d (Other Model) |" in server.get_post(7)


def test_post_and_open_question_never_call_the_retired_accounts_routes(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"post_id": 3, "forum": "general", "tags": ["general"],
                                                             "attached_to_open_question": True})])
    server.post("desc", "why", "fix", "general", ["docker"], open_question_id=3)
    calls += boundary(monkeypatch, server, [(200, {"post_id": 3, "forum": "general", "tags": ["general"],
                                                   "duplicate_of": 3})])
    server.post_open_question("Q", "why", "general", [])
    assert not [p for _, p, _ in calls if p.startswith("/api/accounts/")]


def test_search_does_not_register_or_record_visit(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, [post_fixture()])])
    assert server.search("timeout").startswith(server.COMMUNITY_LABEL)
    assert len(calls) == 1 and calls[0][0] == "GET" and calls[0][1] == "/api/search"


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


def test_failed_registration_still_allows_public_read_with_explicit_notice(monkeypatch, server):
    calls = boundary(monkeypatch, server, [(200, post_fixture()), (500, {}), (500, {}),
        (200, {"items": [], "total": 0, "limit": 50, "offset": 0})])
    result = server.get_post(7)
    assert "Timeout" in result and "Visit was not recorded" in result and "register" in result
    assert "/api/posts/7/visits" not in [p for _, p, _ in calls]


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
    server.post("Details", "Scope", "Reduce concurrency", "testing", [], problem="Timeout", relevancy="CI")
    body = calls[1][2]["json"]
    assert calls[1][1] == "/api/posts"
    assert {k: body[k] for k in ("problem", "relevancy", "description", "solution")} == {
        "problem": "Timeout", "relevancy": "CI", "description": "Details", "solution": "Reduce concurrency"}
    assert body["agent_id"] == calls[0][2]["json"]["agent_id"]


@pytest.mark.parametrize("tool,args,kwargs,expected_solution", [
    ("post", ("Details", "", "Reduce concurrency", "testing", []), {"problem": "Timeout", "relevancy": "CI"},
     "Reduce concurrency"),
    ("post_open_question", ("Details", "", "testing", []), {"problem": "Timeout", "relevancy": "CI"}, None),
    ("post", ("Details", "CI", "Legacy fix", "testing", []), {}, "Legacy fix"),
])
def test_post_payload_passes_real_backend_validator(monkeypatch, server, tool, args, kwargs, expected_solution):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "backend"))
    from main import PostIn
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"post_id": 7, "forum": "testing", "tags": []}),
                                           (200, {})])
    result = getattr(server, tool)(*args, **kwargs)
    payload = calls[1][2]["json"]
    parsed = PostIn.model_validate(payload)
    actual_solution = parsed.general_solution if "general_solution" in parsed.model_fields_set else parsed.solution
    assert actual_solution == expected_solution
    assert ("open question" if expected_solution is None else "solved post") in result


def test_backend_rejects_contradictory_solution_fields(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "backend"))
    from main import PostIn
    with pytest.raises(ValueError, match="agree"):
        PostIn.model_validate({"description": "Details", "solution": "Old fix", "general_solution": "Different fix"})


@pytest.mark.parametrize("tool,args,kwargs", [
    ("post_open_question", ("Timeout\n\nDetails", "", "general", []), {}),
    ("post", ("Timeout\n\nDetails", "", "Fix", "general", []), {}),
    ("post", ("Timeout\n\nDetails", "", "Fix", "general", []), {"relevancy": "   "}),
])
def test_post_without_legacy_value_omits_empty_relevancy(monkeypatch, server, tool, args, kwargs):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "backend"))
    from main import PostIn
    calls = boundary(monkeypatch, server, [(200, {}), (200, {"post_id": 7, "forum": "general", "tags": []}),
                                           (200, {})])
    result = getattr(server, tool)(*args, **kwargs)
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


def test_search_results_show_the_title_and_the_details(server):
    result = server.format_post({"id": 3, "description": "Prisma crashes in Docker\nQuery engine missing",
                                 "value": "Base image OS", "solution": "Set binaryTargets", "tags": ["prisma"]})
    assert "SOLVED" in result and "Prisma crashes in Docker" in result and "Query engine missing" in result
    assert "Set binaryTargets" in result


def test_removed_tools_stay_removed(server):
    assert not hasattr(server, "run_specialists") and not hasattr(server, "my_updates")
