"""Tests for accounts.py and mailer.py (docs/accounts.md).

The unit tests need nothing. The endpoint tests need a THROWAWAY Postgres in TEST_DATABASE_URL (never the
real Supabase database); without it they are skipped:

    docker run --rm -d --name bp-accounts-test-pg -p 55434:5432 -e POSTGRES_PASSWORD=test postgres:16
    $env:TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55434/postgres"   # PowerShell
    .venv\\Scripts\\python -m pytest tests/test_accounts.py -q
    docker stop bp-accounts-test-pg

The fixture creates the test stub schema, a copy of the teammate's `agents` table, schema_agent.sql and
schema_accounts.sql in that throwaway database. No real email is ever sent: the provider is replaced.
"""

import io
import json
import os
import sys
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock
from uuid import uuid4

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import accounts  # noqa: E402
import mailer  # noqa: E402

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
API_KEY = "test-key"
SECRET = "s" * 43
OTHER_SECRET = "o" * 43
STUB_SCHEMA = Path(__file__).with_name("breakpoint_schema_stub.sql")

# TEST ONLY: the teammate's agents table (backend/migrations/20261024_post_usage.sql on origin/main).
AGENTS_STUB = """
CREATE TABLE IF NOT EXISTS public.agents (
    id text PRIMARY KEY CHECK (length(id) BETWEEN 38 AND 140),
    model_name text NOT NULL CHECK (length(btrim(model_name)) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


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


class NoTablesConn:
    """A connection to a database where schema_accounts.sql was never applied."""

    def execute(self, sql, params=()):
        assert "to_regclass" in sql, "nothing but the table check may run without the tables"
        return mock.Mock(fetchone=lambda: {"ok": False})


def bare_client(monkeypatch, conn_factory):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    @contextmanager
    def fake_connect():
        yield conn_factory()

    monkeypatch.setenv("API_KEY", API_KEY)
    monkeypatch.setattr(accounts, "connect", fake_connect)
    app = FastAPI()
    app.include_router(accounts.router)
    return TestClient(app)


def test_missing_tables_degrade_to_503(monkeypatch):
    client = bare_client(monkeypatch, NoTablesConn)
    auth = {"X-API-Key": API_KEY, "X-Agent-Secret": SECRET}
    r = client.post("/api/agents/register", json={"agent_id": new_agent(), "model_name": "claude-sonnet-5-5"},
                    headers=auth)
    assert r.status_code == 503 and "schema_accounts.sql" in r.json()["detail"]
    assert client.post("/api/accounts/solved", json={"post_id": 1}, headers=auth).status_code == 503
    assert accounts.notify_post_solved(NoTablesConn(), 1)["status"] == "accounts_not_set_up"


def test_writes_need_the_api_key_and_a_secret(monkeypatch):
    client = bare_client(monkeypatch, NoTablesConn)
    body = {"agent_id": new_agent(), "model_name": "claude-sonnet-5-5"}
    assert client.post("/api/agents/register", json=body).status_code == 401
    assert client.post("/api/agents/register", json=body, headers={"X-API-Key": API_KEY}).status_code == 401
    r = client.post("/api/agents/register", json={**body, "email": "nope"},
                    headers={"X-API-Key": API_KEY, "X-Agent-Secret": SECRET})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Endpoint tests: throwaway Postgres
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def database() -> str:
    if not TEST_DB:
        pytest.skip("set TEST_DATABASE_URL to a throwaway Postgres to run the accounts endpoint tests")
    import psycopg

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(STUB_SCHEMA.read_text(encoding="utf-8"))
        conn.execute(AGENTS_STUB)
        conn.execute((BACKEND / "schema_agent.sql").read_text(encoding="utf-8"))
        conn.execute((BACKEND / "schema_accounts.sql").read_text(encoding="utf-8"))
        conn.execute((BACKEND / "schema_accounts.sql").read_text(encoding="utf-8"))   # safe to run twice
    return TEST_DB


@pytest.fixture()
def sql(database):
    import psycopg
    from psycopg.rows import dict_row

    def run(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(database, row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    return run


@pytest.fixture()
def outbox(monkeypatch):
    box = mailer.LogMailer()
    monkeypatch.setattr(accounts.mailer, "get_mailer", lambda: box)
    return box


@pytest.fixture()
def client(database, sql, monkeypatch, outbox):
    import psycopg
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from psycopg.rows import dict_row

    sql("TRUNCATE accounts_notices, accounts_post_links, accounts_installs, agents, agent_updates, agent_watchers,"
        " agent_post_meta, post_tags, posts, forums RESTART IDENTITY CASCADE")
    sql("INSERT INTO forums (slug, name) VALUES ('general', 'General')")

    @contextmanager
    def plain_connect():
        with psycopg.connect(database, row_factory=dict_row) as conn:
            yield conn

    monkeypatch.setenv("API_KEY", API_KEY)
    monkeypatch.delenv("WEBSITE_URL", raising=False)
    monkeypatch.setattr(accounts, "connect", plain_connect)
    app = FastAPI()
    app.include_router(accounts.router)
    with TestClient(app) as c:
        yield c


def headers(agent: str | None = None, secret: str = SECRET, project: str | None = None) -> dict[str, str]:
    out = {"X-API-Key": API_KEY, "X-Agent-Secret": secret}
    if agent:
        out["X-Agent-Id"] = agent
    if project:
        out["X-Project-Id"] = project
    return out


def make_post(sql, description="Prisma engine missing on Alpine\nDetails", solution=None, author="anonymous") -> int:
    return sql("INSERT INTO posts (description, value, solution, author, forum_id)"
               " VALUES (%s, 'why', %s, %s, 1) RETURNING id", (description, solution, author))[0]["id"]


def register(client, agent, email: Any = "skip", secret=SECRET, model="claude-sonnet-5-5"):
    body = {"agent_id": agent, "model_name": model}
    if email != "skip":
        body["email"] = email
    return client.post("/api/agents/register", json=body, headers=headers(secret=secret))


def test_register_saves_the_email_privately(client, sql):
    agent = new_agent()
    r = register(client, agent, "Harsh@OSU.edu")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["email"] == "H***@osu.edu" and data["email_on_file"] is True
    assert "Harsh@" not in r.text and data["display_name"].startswith("agent-")
    install = sql("SELECT * FROM accounts_installs")[0]
    assert install["email"] == "Harsh@osu.edu"
    assert install["secret_hash"] == accounts.hash_secret(SECRET) and SECRET not in json.dumps(install, default=str)
    assert sql("SELECT model_name FROM agents WHERE id = %s", (agent,)) == [{"model_name": "claude-sonnet-5-5"}]

    public = client.get(f"/api/agents/{agent}/public").json()
    assert public == {"name": accounts.short_name(agent), "model_name": "claude-sonnet-5-5",
                      "display_name": accounts.display_name(agent, "claude-sonnet-5-5")}


def test_register_updates_keeps_and_removes_the_email(client, sql):
    agent = new_agent()
    register(client, agent, "a@b.dev")
    assert register(client, agent).json()["email"] == "a***@b.dev"           # no "email" key: unchanged
    assert register(client, agent, "c@d.dev").json()["email"] == "c***@d.dev"
    assert register(client, agent, None).json()["email_on_file"] is False   # null removes it
    assert sql("SELECT email FROM accounts_installs") == [{"email": None}]


def test_same_install_two_models_share_one_email(client, sql):
    install = uuid4()
    sonnet, opus = f"claude-sonnet-5-5_{install}", f"claude-opus-5-5_{install}"
    register(client, sonnet, "a@b.dev")
    assert register(client, opus, model="claude-opus-5-5").json()["email_on_file"] is True
    assert len(sql("SELECT * FROM accounts_installs")) == 1 and len(sql("SELECT * FROM agents")) == 2


def test_register_refuses_other_installs_and_bad_input(client):
    agent = new_agent()
    register(client, agent, "a@b.dev")
    assert register(client, agent, "evil@x.dev", secret=OTHER_SECRET).status_code == 403
    assert register(client, agent, model="claude-opus-5-5").status_code == 400    # prefix must match
    r = client.post("/api/agents/register", json={"agent_id": agent, "model_name": "Claude Sonnet 5 5"},
                    headers=headers())
    assert r.status_code == 409    # same slug, different label: the teammate's rule


def test_watch_links_agent_and_project(client, sql):
    agent = new_agent()
    register(client, agent)
    post = make_post(sql)
    r = client.post("/api/accounts/watch", json={"post_id": post}, headers=headers(agent, project="proj-a"))
    assert r.status_code == 200 and r.json()["role"] == "asker"
    assert sql("SELECT agent_id, project, role FROM accounts_post_links") == [
        {"agent_id": agent, "project": "proj-a", "role": "asker"}]
    assert sql("SELECT project FROM agent_watchers WHERE post_id = %s", (post,)) == [{"project": "proj-a"}]
    assert client.post("/api/accounts/watch", json={"post_id": 999}, headers=headers(agent)).status_code == 404
    assert client.post("/api/accounts/watch", json={"post_id": post},
                       headers=headers(agent, OTHER_SECRET)).status_code == 403
    assert client.post("/api/accounts/watch", json={"post_id": post},
                       headers=headers(new_agent())).status_code == 404     # never registered


def test_solved_tells_projects_and_emails_humans_once(client, sql, outbox):
    asker, watcher, quiet, solver = new_agent(), new_agent(), new_agent(), new_agent()
    register(client, asker, "asker@osu.edu")
    register(client, watcher, "watcher@osu.edu")
    register(client, quiet)                                   # no email: agent-only updates
    register(client, solver, "solver@osu.edu")
    post = make_post(sql)
    for agent, role, project in [(asker, "asker", "proj-a"), (watcher, "watcher", "proj-b"),
                                 (quiet, "watcher", "proj-c"), (solver, "watcher", "proj-d")]:
        client.post("/api/accounts/watch", json={"post_id": post, "role": role}, headers=headers(agent, project=project))
    sql("INSERT INTO agent_post_meta (post_id, project) VALUES (%s, 'proj-meta')", (post,))

    r = client.post("/api/accounts/solved", json={"post_id": post}, headers=headers(solver))
    assert r.json()["status"] == "not_solved" and outbox.sent == []

    sql("UPDATE posts SET solution = 'Add binaryTargets' WHERE id = %s", (post,))
    r = client.post("/api/accounts/solved", json={"post_id": post}, headers=headers(solver))
    assert r.status_code == 200, r.text
    assert r.json() == {"post_id": post, "status": "queued", "projects": 5, "emails": 2}
    updates = sql("SELECT project, message, post_id FROM agent_updates ORDER BY project")
    assert [u["project"] for u in updates] == ["proj-a", "proj-b", "proj-c", "proj-d", "proj-meta"]
    assert f"Open Question #{post} was solved: Prisma engine missing on Alpine" in updates[0]["message"]
    assert sorted(e.to for e in outbox.sent) == ["asker@osu.edu", "watcher@osu.edu"]   # solver skipped
    assert f"http://localhost:3000/posts/{post}" in outbox.sent[0].text
    assert outbox.sent[0].idempotency_key and "asker@" not in outbox.sent[0].idempotency_key

    again = client.post("/api/accounts/solved", json={"post_id": post}, headers=headers(solver))
    assert again.json()["status"] == "already_sent"
    assert len(outbox.sent) == 2 and len(sql("SELECT * FROM agent_updates")) == 5
    assert sql("SELECT projects, emails FROM accounts_notices") == [{"projects": 5, "emails": 2}]
    assert client.post("/api/accounts/solved", json={"post_id": 999}, headers=headers()).status_code == 404


def test_post_author_agent_gets_the_email_and_website_url_is_used(client, sql, outbox, monkeypatch):
    monkeypatch.setenv("WEBSITE_URL", "https://breakpoint.example/")
    author = new_agent()
    register(client, author, "author@osu.edu")
    post = make_post(sql, solution="fixed", author=author)   # main.py on origin/main stores the agent ID here
    assert client.post("/api/accounts/solved", json={"post_id": post}, headers=headers()).json()["emails"] == 1
    assert outbox.sent[0].to == "author@osu.edu"
    assert f"https://breakpoint.example/posts/{post}" in outbox.sent[0].text


def test_notify_functions_for_server_side_wiring(database, sql):
    """notify_post_solved + deliver, the way the unified backend can call them inside create_post."""
    import psycopg
    from psycopg.rows import dict_row

    sql("TRUNCATE accounts_notices, accounts_post_links, accounts_installs, agents, agent_updates, agent_watchers,"
        " agent_post_meta, post_tags, posts, forums RESTART IDENTITY CASCADE")
    sql("INSERT INTO forums (slug, name) VALUES ('general', 'General')")
    agent = new_agent()
    with psycopg.connect(database, row_factory=dict_row) as conn:
        accounts.register(conn, agent, "claude-sonnet-5-5", SECRET, "a@b.dev")
    post = make_post(sql, solution="fixed")
    with psycopg.connect(database, row_factory=dict_row) as conn:
        accounts.link_post(conn, post, agent, accounts.parse_agent_id(agent)[1], "proj-z", "asker")
        result = accounts.notify_post_solved(conn, post, write_updates=False)
    assert result["status"] == "queued" and result["projects"] == 0
    assert sql("SELECT * FROM agent_updates") == []
    box = mailer.LogMailer()
    assert accounts.deliver(result["emails"], box) == 1 and box.sent[0].to == "a@b.dev"


# ---------------------------------------------------------------------------
# The wiring in main.py and team_api.py (marked ACCOUNTS there)
# ---------------------------------------------------------------------------

def test_main_mounts_the_accounts_routes():
    import main
    paths = set(main.app.openapi()["paths"])
    assert {"/api/agents/register", "/api/agents/{agent_id}/public", "/api/accounts/watch",
            "/api/accounts/solved"} <= paths


def asker_waiting_on_new_question(database, sql, email="asker@osu.edu", project="proj-a") -> tuple[str, int]:
    """A registered asker (with an email) linked to a fresh Open Question. Returns (agent_id, post_id)."""
    import psycopg
    from psycopg.rows import dict_row

    sql("TRUNCATE accounts_notices, accounts_post_links, accounts_installs, agents, agent_updates, agent_watchers,"
        " agent_post_meta, post_tags, posts, forums RESTART IDENTITY CASCADE")
    sql("INSERT INTO forums (slug, name) VALUES ('general', 'General')")
    asker = new_agent()
    post = make_post(sql)
    with psycopg.connect(database, row_factory=dict_row) as conn:
        accounts.register(conn, asker, "claude-sonnet-5-5", SECRET, email)
        accounts.link_post(conn, post, asker, accounts.parse_agent_id(asker)[1], project, "asker")
    return asker, post


def test_create_post_emails_the_waiting_humans_once(database, sql, outbox, monkeypatch):
    """POST /api/posts fills the Open Question, writes ONE agent_updates row per project (main.py's own) and
    emails the asker. The plugin's later POST /api/accounts/solved then does nothing."""
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("API_KEY", API_KEY)
    solver = new_agent()
    _, post = asker_waiting_on_new_question(database, sql)
    with TestClient(main.app) as c:
        r = c.post("/api/posts", json={"description": "Prisma on Alpine\nfixed", "value": "v",
                                       "solution": "Add binaryTargets", "open_question_id": post},
                   headers={"X-API-Key": API_KEY, "X-Agent-Id": solver})
        assert r.status_code == 200, r.text
        assert r.json()["attached_to_open_question"] is True and r.json()["post_id"] == post
        assert [e.to for e in outbox.sent] == ["asker@osu.edu"]
        assert f"/posts/{post}" in outbox.sent[0].text
        assert [u["project"] for u in sql("SELECT project FROM agent_updates")] == ["proj-a"]
        again = c.post("/api/accounts/solved", json={"post_id": post}, headers=headers(solver))
    assert again.json()["status"] == "already_sent"
    assert len(outbox.sent) == 1 and len(sql("SELECT * FROM agent_updates")) == 1


def test_team_rerun_possible_answer_is_emailed_once_per_run(database, sql, outbox, monkeypatch):
    import team_api

    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setattr(team_api.STATE, "notify_fn", None)          # the real path: agent_db + accounts
    _, post = asker_waiting_on_new_question(database, sql)
    result = {"status": "report", "hypotheses": [{"cause": "The image misses the musl query engine."}],
              "models": {"chair": "claude-opus-5-5"}}
    team_api._notify_possible_answer(post, "tr_000000000001", result)
    updates = sql("SELECT project, message FROM agent_updates")
    assert [u["project"] for u in updates] == ["proj-a"] and "cold-case re-run" in updates[0]["message"]
    assert [e.to for e in outbox.sent] == ["asker@osu.edu"]
    assert "musl query engine" in outbox.sent[0].text and "hasn't been verified yet" in outbox.sent[0].text
    team_api._notify_possible_answer(post, "tr_000000000001", result)   # same run again: no second email
    assert len(outbox.sent) == 1
    team_api._notify_possible_answer(post, "tr_000000000002", result)   # a new run: a new email
    assert len(outbox.sent) == 2


def test_team_rerun_loads_chosen_open_questions_by_id(database, sql, monkeypatch):
    """The re-run reads the chosen ids directly, not only from the 500 newest posts."""
    import team_api

    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setattr(team_api.STATE, "open_questions_fn", None)
    _, post = asker_waiting_on_new_question(database, sql)
    other = make_post(sql, "Another open question\nDetails")
    got = team_api._load_open_questions([other, 999, post], limit=10)
    assert [q["id"] for q in got] == [other, post] and got[0]["solution"] is None
