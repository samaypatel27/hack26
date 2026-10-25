"""PostgresStore against a THROWAWAY Postgres (TEAM_TEST_DATABASE_URL), and the memory fallback.

The test applies schema_team_v1.sql itself, on purpose, to its own throwaway database. The app
never does that: choose_store only checks whether the tables exist.
"""

import os
from pathlib import Path

import pytest

from team import store as store_mod

TEST_DB = os.environ.get("TEAM_TEST_DATABASE_URL", "")
SCHEMA = Path(__file__).resolve().parents[2] / "schema_team_v1.sql"

pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEAM_TEST_DATABASE_URL to a throwaway Postgres")


def _sql(query: str) -> None:
    import psycopg

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(query)


def test_missing_tables_fall_back_to_memory_without_creating_them(monkeypatch):
    _sql("DROP TABLE IF EXISTS v1_team_events; DROP TABLE IF EXISTS v1_team_runs;")
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    chosen = store_mod.choose_store()
    assert chosen.kind == "memory"
    assert not store_mod.PostgresStore(TEST_DB).tables_exist()   # nothing was created


def test_postgres_store_round_trip(monkeypatch):
    _sql(SCHEMA.read_text(encoding="utf-8"))
    _sql(SCHEMA.read_text(encoding="utf-8"))   # idempotent
    _sql("TRUNCATE v1_team_events, v1_team_runs")
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    st = store_mod.choose_store()
    assert st.kind == "postgres"
    st.create_run("tr_pg1", case={"fingerprint": "51749bd5", "project": "p"}, models={"chair": "x"},
                  source="help", deadline_s=100)
    st.mark_started("tr_pg1", 100)
    assert st.find_active_run("51749bd5") == "tr_pg1"
    assert st.count_runs_today() == 1
    st.add_event("tr_pg1", "docs", "searching", "prisma musl", {"query": "prisma musl"})
    st.add_event("tr_pg1", "docs", "finished", "1 finding", None)
    events = st.list_events("tr_pg1")
    assert [e["type"] for e in events] == ["searching", "finished"] and events[0]["data"] == {"query": "prisma musl"}
    assert [e["type"] for e in st.list_events("tr_pg1", since=events[0]["id"])] == ["finished"]
    report = {"status": "report", "hypotheses": [{"rank": 1}], "cost_usd": 0.42}
    st.finish_run("tr_pg1", "report", report, None)
    run = st.get_run("tr_pg1")
    assert run["status"] == "report" and run["report"]["hypotheses"][0]["rank"] == 1 and float(run["cost_usd"]) == 0.42
    assert st.find_active_run("51749bd5") is None

    st.create_run("tr_pg2", case={}, models={}, source="rerun", deadline_s=1, open_question_id=3)
    _sql("UPDATE v1_team_runs SET deadline_at = now() - interval '1 minute' WHERE id = 'tr_pg2'")
    assert st.expire_stale() == 1
    assert st.get_run("tr_pg2")["status"] == "failed"
