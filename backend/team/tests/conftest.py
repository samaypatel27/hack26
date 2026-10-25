"""Shared fixtures for the specialist team tests. No test calls a real API: every client is a fake
from team/fakes.py. Run from the repo root:

    backend\\.venv\\Scripts\\python -m pytest backend/team/tests -q

The Postgres store test needs a throwaway database in TEAM_TEST_DATABASE_URL (it is skipped otherwise):

    docker run --rm -d --name team-test-pg -p 55433:5432 -e POSTGRES_PASSWORD=test postgres:16
    $env:TEAM_TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55433/postgres"
"""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture(autouse=True)
def team_env(monkeypatch):
    """Fast, predictable settings; no real keys or database."""
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABASE_URL"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("TEAM_ENABLED", "1")
    monkeypatch.setenv("TEAM_DAILY_RUNS", "50")
    monkeypatch.setenv("TEAM_RUN_COST_CAP_USD", "1.50")
    monkeypatch.setenv("TEAM_RESEARCHER_TIMEOUT_S", "20")
    monkeypatch.setenv("TEAM_HARD_STOP_S", "40")


@pytest.fixture()
def prisma_case():
    from team import case_file

    return case_file.normalize(json.loads((SAMPLES / "prisma_alpine_docker.json").read_text(encoding="utf-8")))


@pytest.fixture()
def ci_case():
    from team import case_file

    return case_file.normalize(json.loads((SAMPLES / "pytest_ci_import.json").read_text(encoding="utf-8")))


@pytest.fixture()
def clients():
    from team import config
    from team.fakes import FakeAnthropic, FakeOpenAI

    return config.Clients(anthropic=FakeAnthropic(), openai=FakeOpenAI())


@pytest.fixture()
def events():
    """A list that collects (member, type, text, data) from on_event."""
    collected = []

    def on_event(member, kind, text, data=None):
        collected.append((member, kind, text, data))

    on_event.items = collected
    return on_event
