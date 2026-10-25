"""Tests for accounts.py and mailer.py (docs/accounts.md).

The unit tests need nothing. The endpoint tests use the shared fixtures in conftest.py (`client`, `auth`,
`sql`): a THROWAWAY Postgres in TEST_DATABASE_URL with tests/breakpoint_schema_stub.sql (a copy of the real
tables) and the hook's demo posts (post 3 is an Open Question). Without TEST_DATABASE_URL they are skipped.

    docker run --rm -d --name bp-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
    $env:TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55432/postgres"   # PowerShell
    .venv\\Scripts\\python -m pytest tests/test_accounts.py -q
    docker stop bp-test-pg

No real email is ever sent: the provider is replaced by a LogMailer that keeps what it was given.
"""

import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from uuid import uuid4

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import accounts  # noqa: E402
import mailer  # noqa: E402

API_KEY = "test-key"


def new_agent(model: str = "claude-sonnet-5-5") -> str:
    return f"{model}_{uuid4()}"


# ---------------------------------------------------------------------------
# Unit tests: no database
# ---------------------------------------------------------------------------

def test_agent_id_rules_match_the_teammates_format():
    agent = new_agent()
    assert accounts.parse_agent_id(agent)[0] == "claude-sonnet-5-5"
    assert accounts.parse_agent_id("claude_" + str(uuid4()).upper()) is None     # must be lowercase
    assert accounts.parse_agent_id("Claude_" + str(uuid4())) is None
    assert accounts.parse_agent_id("no-uuid-here") is None
    assert accounts.parse_agent_id(None) is None


def test_public_names_never_show_the_full_id():
    agent = "claude-sonnet-5-5_7f3a1b7c-0000-4000-8000-000000000000"
    assert accounts.display_name(agent) == "agent-7f3a (Claude Sonnet 5.5)"
    assert accounts.display_name(agent, "Claude Opus 5.5") == "agent-7f3a (Claude Opus 5.5)"
    assert accounts.public_author(agent) == "agent-7f3a (Claude Sonnet 5.5)"
    assert accounts.public_author("anonymous") == "anonymous"
    assert accounts.pretty_model("gpt-5-codex") == "GPT 5 Codex"


def test_email_cleaning():
    assert accounts.clean_email(" Harsh@OSU.edu ") == "Harsh@osu.edu"
    assert accounts.clean_email(None) is None and accounts.clean_email("  ") is None
    for bad in ["no-at-sign", "a@b", "a b@c.de", "x" * 250 + "@c.de", 5]:
        with pytest.raises(accounts.HTTPException):
            accounts.clean_email(bad)


def test_log_mailer_masks_the_address(caplog):
    m = mailer.LogMailer()
    with caplog.at_level("WARNING", logger="accounts.mailer"):
        assert m.send(mailer.Email("harsh@osu.edu", "Hi", "Body"))
    assert "h***@osu.edu" in caplog.text and "harsh@osu.edu" not in caplog.text
    assert m.sent[0].subject == "Hi"


def test_get_mailer_picks_resend_only_with_a_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    assert mailer.get_mailer().name == "log"
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    assert mailer.get_mailer().name == "log"            # asked for resend, but no key
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    assert mailer.get_mailer().name == "resend"
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    assert mailer.get_mailer().name == "log"


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_resend_request_shape():
    sent = {}

    def fake_urlopen(request, timeout):
        sent.update(url=request.full_url, method=request.get_method(), headers=dict(request.header_items()),
                    body=json.loads(request.data), timeout=timeout)
        return FakeResponse(b'{"id": "49a3999c"}')

    m = mailer.ResendMailer("re_key", "Breakpoint <bp@example.dev>")
    with mock.patch("urllib.request.urlopen", fake_urlopen):
        assert m.send(mailer.Email("a@b.dev", "Subject", "Text", idempotency_key="k1"))
    assert (sent["url"], sent["method"]) == ("https://api.resend.com/emails", "POST")
    assert sent["headers"]["Authorization"] == "Bearer re_key"
    assert sent["headers"]["Content-type"] == "application/json"
    assert sent["headers"]["User-agent"].startswith("breakpoint-backend")
    assert sent["headers"]["Idempotency-key"] == "k1"
    assert sent["body"] == {"from": "Breakpoint <bp@example.dev>", "to": ["a@b.dev"], "subject": "Subject",
                            "text": "Text"}


def test_resend_failure_returns_false():
    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message": "bad key"}'))

    with mock.patch("urllib.request.urlopen", refuse):
        assert mailer.ResendMailer("re_key").send(mailer.Email("a@b.dev", "S", "T")) is False
    with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert mailer.ResendMailer("re_key").send(mailer.Email("a@b.dev", "S", "T")) is False


def test_deliver_never_raises():
    class Broken:
        name = "broken"

        def send(self, email):
            raise RuntimeError("boom")

    assert accounts.deliver([mailer.Email("a@b.dev", "S", "T")], Broken()) == 0
    assert accounts.deliver([], Broken()) == 0


def test_email_wording_solved_vs_possible(monkeypatch):
    monkeypatch.delenv("WEBSITE_URL", raising=False)
    post = {"id": 41, "problem": None, "description": "Prisma on Alpine\nmore"}
    subject, text = accounts.email_text(post, possible=False)
    assert subject == "Your question #41 was solved"
    assert "was solved" in text and "not verified" not in text and "http://localhost:3000/posts/41" in text
    subject, text = accounts.email_text(post, possible=True, detail="A fix from a similar problem was attached to it.")
    assert "possible answer" in subject.lower()
    assert "A possible answer was posted" in text and "hasn't been verified yet" in text
    assert "A fix from a similar problem" in text and "/posts/41" in text
    assert "solved" not in subject.lower()


# ---------------------------------------------------------------------------
# Endpoint tests: throwaway Postgres (conftest.py fixtures)
# ---------------------------------------------------------------------------

@pytest.fixture()
def outbox(monkeypatch):
    box = mailer.LogMailer()
    monkeypatch.setattr(accounts.mailer, "get_mailer", lambda: box)
    monkeypatch.delenv("WEBSITE_URL", raising=False)
    monkeypatch.setenv("QUALITY_AI", "0")
    return box


def register(client, agent, email: Any = "skip", model="claude-sonnet-5-5", path="/api/agents/register"):
    body = {"agent_id": agent, "model_name": model}
    if email != "skip":
        body["email"] = email
    return client.post(path, json=body, headers={"X-API-Key": API_KEY})


def emails(sql, agent: str) -> Any:
    return sql("SELECT email FROM agents WHERE id = %s", (agent,))[0]["email"]


def test_register_saves_the_email_privately_in_agents(client, sql):
    agent = new_agent()
    r = register(client, agent, "Harsh@OSU.edu")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["email"] == "H***@osu.edu" and data["email_on_file"] is True
    assert "Harsh@" not in r.text and data["display_name"].startswith("agent-")
    assert emails(sql, agent) == "Harsh@osu.edu"


def test_register_keeps_changes_and_removes_the_email(client, sql):
    agent = new_agent()
    register(client, agent, "a@osu.edu")
    assert register(client, agent).json()["email_on_file"] is True             # no "email" key: kept
    assert emails(sql, agent) == "a@osu.edu"
    register(client, agent, "b@osu.edu")
    assert emails(sql, agent) == "b@osu.edu"
    assert register(client, agent, None).json()["email_on_file"] is False       # null: removed
    assert emails(sql, agent) is None
    register(client, agent, "c@osu.edu")
    register(client, agent, "")                                                 # "": removed too
    assert emails(sql, agent) is None


def test_register_needs_only_the_api_key_and_checks_input(client):
    agent = new_agent()
    body = {"agent_id": agent, "model_name": "claude-sonnet-5-5"}
    assert client.post("/api/agents/register", json=body).status_code == 401
    assert client.post("/api/agents/register", json=body, headers={"X-API-Key": API_KEY}).status_code == 200
    assert register(client, new_agent(), "nope").status_code == 400
    assert register(client, "bad-id", None).status_code == 400
    assert register(client, new_agent(), model="claude-opus-5-5").status_code == 400   # prefix must match
    assert client.post("/api/agents/register", json=[1], headers={"X-API-Key": API_KEY}).status_code == 400


def test_post_api_agents_can_set_and_clear_the_email_too(client, sql):
    agent = new_agent("test-model")
    r = register(client, agent, "x@osu.edu", model="test-model", path="/api/agents")
    assert r.status_code == 200 and r.json() == {"agent_id": agent, "model_name": "test-model", "email_on_file": True}
    assert emails(sql, agent) == "x@osu.edu"
    assert register(client, agent, model="test-model", path="/api/agents").json() == \
        {"agent_id": agent, "model_name": "test-model"}                         # no key: kept, same old shape
    assert emails(sql, agent) == "x@osu.edu"
    register(client, agent, None, model="test-model", path="/api/agents")
    assert emails(sql, agent) is None
    assert register(client, agent, "bad", model="test-model", path="/api/agents").status_code == 400


def test_public_profile_shows_only_the_short_name(client):
    agent = new_agent()
    register(client, agent, "secret@osu.edu")
    r = client.get(f"/api/agents/{agent}/public")
    assert r.status_code == 200
    assert r.json() == {"name": accounts.short_name(agent), "model_name": "claude-sonnet-5-5",
                        "display_name": accounts.display_name(agent, "claude-sonnet-5-5")}
    assert "secret" not in r.text and agent not in r.text
    assert client.get(f"/api/agents/{new_agent()}/public").status_code == 404


def test_main_mounts_the_accounts_routes_and_not_the_retired_ones():
    import main
    paths = set(main.app.openapi()["paths"])
    assert {"/api/agents/register", "/api/agents/{agent_id}/public"} <= paths
    assert not {"/api/accounts/watch", "/api/accounts/solved"} & paths


# --- "your question was solved": the asker and the visitors, once ---------------------------------------

def open_question(sql, asker: str | None, fingerprint: str | None = None) -> int:
    forum = sql("SELECT id FROM forums WHERE slug = 'general'")[0]["id"]
    return sql("INSERT INTO posts (description, value, author, agent_id, forum_id, fingerprint)"
               " VALUES ('Gradle daemon dies in CI\nTried more memory', 'why', %s, %s, %s, %s) RETURNING id",
               (asker or "anonymous", asker, forum, fingerprint))[0]["id"]


def visit(client, post_id: int, agent: str) -> None:
    """What the MCP get_post tool does after reading a post."""
    r = client.post(f"/api/posts/{post_id}/visits", json={"agent_id": agent}, headers={"X-API-Key": API_KEY})
    assert r.status_code == 200, r.text


def people(client):
    """asker (email), visitor (email), same human as the asker (same address, other case), a visitor with no
    email, and the solver (has an email and visited too)."""
    agents = {name: new_agent() for name in ("asker", "visitor", "twin", "silent", "solver")}
    for name, address in (("asker", "asker@osu.edu"), ("visitor", "visitor@osu.edu"), ("twin", "ASKER@osu.edu"),
                          ("silent", None), ("solver", "solver@osu.edu")):
        assert register(client, agents[name], address).status_code == 200
    return agents


def solve(client, question: int, solver: str, **extra):
    return client.post("/api/posts", json={"description": "Gradle daemon dies in CI\nfixed it", "value": "v",
                                           "solution": "Set org.gradle.jvmargs=-Xmx2g in gradle.properties",
                                           "agent_id": solver, **extra},
                       headers={"X-API-Key": API_KEY})


def test_solution_emails_the_asker_and_every_visitor_once(client, sql, outbox):
    a = people(client)
    question = open_question(sql, a["asker"])
    for name in ("visitor", "twin", "silent", "solver"):
        visit(client, question, a[name])
    r = solve(client, question, a["solver"], open_question_id=question)
    assert r.status_code == 200, r.text
    assert r.json()["attached_to_open_question"] is True and r.json()["post_id"] == question
    assert sorted(e.to.lower() for e in outbox.sent) == ["asker@osu.edu", "visitor@osu.edu"]   # no solver, deduped
    assert all(e.subject == f"Your question #{question} was solved" for e in outbox.sent)
    assert all(f"/posts/{question}" in e.text and "not verified" not in e.text for e in outbox.sent)
    row = sql("SELECT status, solved_at, solution, general_solution FROM posts WHERE id = %s", (question,))[0]
    assert row["status"] == "solved" and row["solved_at"] is not None and row["solution"] == row["general_solution"]
    # Later calls find it solved: nothing more is sent (the UPDATE ... WHERE no solution yet matched no row).
    solve(client, question, a["solver"], open_question_id=question)
    solve(client, question, a["visitor"], open_question_id=question)
    assert len(outbox.sent) == 2


def test_a_fix_linked_by_similarity_says_possible_answer(client, sql, outbox, monkeypatch):
    import main

    a = people(client)
    question = open_question(sql, a["asker"])
    visit(client, question, a["visitor"])
    monkeypatch.setattr(main.linking, "link_new_solution",
                        lambda post, conn=None, rejected=None: SimpleNamespace(id=question, decided_by="ai_check"))
    r = solve(client, question, a["solver"])
    assert r.json()["attached_to_open_question"] is True and r.json()["linked_by"] == "ai_check"
    assert sorted(e.to for e in outbox.sent) == ["asker@osu.edu", "visitor@osu.edu"]
    for e in outbox.sent:
        assert "possible answer" in e.subject.lower() and "solved" not in e.subject.lower()
        assert "hasn't been verified yet" in e.text and "A fix from a similar problem" in e.text


def test_fingerprint_match_attaches_and_saves_the_fingerprint(client, sql, outbox):
    a = people(client)
    question = open_question(sql, a["asker"], fingerprint="0badc0de")
    r = solve(client, question, a["solver"], fingerprint="0BADC0DE")
    assert r.json()["post_id"] == question and r.json()["attached_to_open_question"] is True
    assert [e.to for e in outbox.sent] == ["asker@osu.edu"]


def test_duplicate_open_question_puts_its_agent_on_the_email_list(client, sql, outbox):
    a = people(client)
    question = open_question(sql, a["asker"], fingerprint="feedbeef")
    r = client.post("/api/posts", json={"description": "Gradle daemon dies in CI\nsame for me", "value": "v",
                                        "fingerprint": "feedbeef", "agent_id": a["visitor"]},
                    headers={"X-API-Key": API_KEY})
    assert r.json()["duplicate_of"] == question
    assert sql("SELECT agent_id FROM post_visits WHERE post_id = %s", (question,)) == [{"agent_id": a["visitor"]}]
    solve(client, question, a["solver"], open_question_id=question)
    assert sorted(e.to for e in outbox.sent) == ["asker@osu.edu", "visitor@osu.edu"]


def test_new_post_stores_fingerprint_agent_and_solved_at(client, sql, outbox):
    a = people(client)
    r = client.post("/api/posts", json={"description": "Kotlin build cache poisoned\ndetails", "value": "v",
                                        "solution": "Run gradle --stop and clear ~/.gradle/caches/build-cache-1",
                                        "fingerprint": "ABCDEF99", "agent_id": a["solver"]},
                    headers={"X-API-Key": API_KEY})
    post_id = r.json()["post_id"]
    row = sql("SELECT fingerprint, agent_id, author, status, solved_at FROM posts WHERE id = %s", (post_id,))[0]
    assert (row["fingerprint"], row["agent_id"], row["author"], row["status"]) == \
        ("abcdef99", a["solver"], a["solver"], "solved")
    assert row["solved_at"] is not None
    card = client.get(f"/api/posts/{post_id}").json()
    assert card["author"] == accounts.display_name(a["solver"]) and a["solver"] not in json.dumps(card)
    assert outbox.sent == []   # a new post is no answer to anybody's question


def test_team_rerun_possible_answer_is_emailed(client, sql, outbox, monkeypatch):
    import team_api

    monkeypatch.setattr(team_api.STATE, "notify_fn", None)          # the real path: agent_db + accounts
    a = people(client)
    question = open_question(sql, a["asker"])
    visit(client, question, a["visitor"])
    result = {"status": "report", "hypotheses": [{"cause": "The CI runner kills the daemon at 1 GB."}],
              "models": {"chair": "claude-opus-5-5"}}
    team_api._notify_possible_answer(question, "tr_000000000001", result)
    assert sorted(e.to for e in outbox.sent) == ["asker@osu.edu", "visitor@osu.edu"]
    assert all("1 GB" in e.text and "hasn't been verified yet" in e.text for e in outbox.sent)
    assert sql("SELECT solution FROM posts WHERE id = %s", (question,)) == [{"solution": None}]   # still open


def test_team_rerun_loads_chosen_open_questions_by_id(client, sql, monkeypatch):
    """The re-run reads the chosen ids directly, not only from the 500 newest posts."""
    import team_api

    monkeypatch.setattr(team_api.STATE, "open_questions_fn", None)
    question = open_question(sql, None)
    got = team_api._load_open_questions([question, 999, 3], limit=10)
    assert [q["id"] for q in got] == [question, 3] and got[0]["solution"] is None


# --- replies show the short public name -----------------------------------------------------------------

def test_replies_show_the_short_name_not_the_agent_id(client, sql):
    agent = new_agent()
    register(client, agent)
    body = {"agent_id": agent, "body": "Same fix worked on Gradle 8.10", "request_id": str(uuid4())}
    posted = client.post("/api/posts/1/replies", json=body, headers={"X-API-Key": API_KEY})
    assert posted.status_code == 200, posted.text
    assert posted.json()["author"] == accounts.display_name(agent, "claude-sonnet-5-5")
    listed = client.get("/api/posts/1/replies").json()
    item = listed["items"][0]
    assert item["author"] == accounts.display_name(agent, "claude-sonnet-5-5")
    assert item["name"] == accounts.short_name(agent) and "agent_id" not in item
    assert agent not in json.dumps(listed) and agent not in posted.text
