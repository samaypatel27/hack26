"""Shared fixtures for the agent route tests (test_agent_*.py).

The endpoint tests need a THROWAWAY Postgres in TEST_DATABASE_URL: the stub DROPS and recreates the
tables, and every test empties them and reloads the hook's demo posts. Never point it at the real Supabase
database (the fixture refuses a supabase host). The tables come from tests/breakpoint_schema_stub.sql, a
copy of the real Breakpoint tables.

    docker run --rm -d --name ao-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
    $env:TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55432/postgres"   # PowerShell
    .venv\\Scripts\\python -m pytest tests -q

The app under test is main.app (the real /api app); its connections go to TEST_DATABASE_URL.
"""

import os
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
API_KEY = "test-key"
STUB_SCHEMA = Path(__file__).with_name("breakpoint_schema_stub.sql")
FORUMS = ["general", "docker", "python"]
TAGS = ["general", "docker", "python", "pytest", "ci", "gradle", "kotlin", "node", "frontend"]


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    """main.py loads backend/.env, so real keys could reach the tests. Blank the ones that would call a
    paid API or send an email: linking (OpenAI, Anthropic) and accounts (Resend). Tests use fakes."""
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RESEND_API_KEY", "EMAIL_PROVIDER"):
        monkeypatch.setenv(name, "")


@pytest.fixture(scope="session")
def database() -> str:
    if not TEST_DB:
        pytest.skip("set TEST_DATABASE_URL to a throwaway Postgres to run the endpoint tests")
    if "supabase" in TEST_DB.lower():
        pytest.exit("TEST_DATABASE_URL points at Supabase: the tests drop tables. Use a throwaway Postgres.")
    import psycopg

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(STUB_SCHEMA.read_text(encoding="utf-8"))
    return TEST_DB


@pytest.fixture()
def client(database: str, monkeypatch):
    import psycopg
    from fastapi.testclient import TestClient
    from psycopg.rows import dict_row

    import main
    import seed_agent

    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("API_KEY", API_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")   # judge tests mock the client; never call the real API
    monkeypatch.setenv("OPENAI_API_KEY", "")      # linking Stage 3 off here; test_linking.py mocks it
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE reply_upvotes, replies, post_useful, post_visits, post_tags, forum_tags, posts, agents,"
                     " tags, forums RESTART IDENTITY CASCADE")
        for slug in FORUMS:
            conn.execute("INSERT INTO forums (slug, name) VALUES (%s, %s)", (slug, slug.title()))
        for slug in TAGS:
            conn.execute("INSERT INTO tags (slug) VALUES (%s)", (slug,))
        seed_agent.load_mock_posts(conn)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


@pytest.fixture()
def sql(database: str):
    """Run one SQL query against the test database and return the rows as dicts."""
    import psycopg
    from psycopg.rows import dict_row

    def run(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(database, row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    return run
