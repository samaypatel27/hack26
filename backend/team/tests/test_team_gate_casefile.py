"""The gate's decisions and the case file's validation, scrubbing and size cap."""

import json

import pytest

from team import case_file, config, gate
from team.fakes import FakeAnthropic, message, text_block
from team.store import MemoryStore


def gate_says(kind, category="environment_platform"):
    return FakeAnthropic(lambda k, kw: message([text_block(json.dumps(
        {"kind": kind, "category": category, "reason": f"looks {kind}"}))]))


def decide(case, *, results=(), store=None, anthropic=None, **env):
    s = config.Settings(**{**config.settings().__dict__, **env})
    return gate.decide(case, store=store or MemoryStore(),
                       clients=config.Clients(anthropic=anthropic or gate_says("external")),
                       search_fn=lambda c: list(results), settings=s)


SOLVED = {"id": 1, "solution": "Add the musl binary target.", "match": "fingerprint", "score": 1.0,
          "times_worked": 9, "times_applied": 10, "status": "solved"}
OPEN = {"id": 3, "solution": None, "match": "text", "score": 0.6, "description": "Tried reinstalling prisma.",
        "status": "open"}


def test_external_problem_starts_the_team(prisma_case):
    d = decide(prisma_case)
    assert d.decision == "team" and d.category == "environment_platform"
    assert gate.team_started_message("tr_x").startswith("TEAM_STARTED: tr_x")


def test_known_fix_is_returned(prisma_case):
    d = decide(prisma_case, results=[SOLVED])
    assert d.decision == "known_fix" and d.post_id == 1
    assert d.message.startswith("KNOWN_FIX: forum post #1") and "9 of 10" in d.message


def test_weak_text_match_is_not_a_known_fix(prisma_case):
    d = decide(prisma_case, results=[{**SOLVED, "match": "text", "score": 0.25}])
    assert d.decision == "team"


def test_known_fix_already_tried_goes_to_the_team(prisma_case):
    prisma_case["tried"].append({"attempt": "Applied the fix from forum post #1", "result": "same error"})
    assert decide(prisma_case, results=[SOLVED]).decision == "team"


def test_matching_open_question_is_passed_on(prisma_case):
    d = decide(prisma_case, results=[OPEN])
    assert d.decision == "team" and d.open_question_id == 3
    assert "Open Question #3" in prisma_case["others_tried"]
    assert "Open Question #3" in case_file.render(prisma_case)


def test_not_tried_enough_gets_a_checklist():
    case = case_file.normalize({"description": "build fails", "error": "Error: x", "tried": ["reran it"]})
    d = decide(case)
    assert d.decision == "try_first" and d.message.startswith("TRY_FIRST: try this before calling the team")


def test_a_long_writeup_counts_as_tried():
    case = case_file.normalize({"error": "Error: x", "tried": [{"attempt": "x" * 100, "result": "y" * 80}]})
    assert gate.has_tried(case)
    assert gate.has_tried(case_file.normalize({"error": "e", "fail_count": 3}))


def test_own_code_gets_the_step_back_checklist(prisma_case):
    d = decide(prisma_case, anthropic=gate_says("own_code", "own_code"))
    assert d.decision == "try_first" and "project's own code" in d.message


def test_classifier_failure_lets_the_team_run(prisma_case):
    d = decide(prisma_case, anthropic=FakeAnthropic(lambda k, kw: TimeoutError("slow")))
    assert d.decision == "team" and "unclassified" in d.checks


def test_gate_call_shape(prisma_case):
    fake = gate_says("external")
    decide(prisma_case, anthropic=fake)
    kind, kw = fake.calls[0]
    assert kind == "create" and kw["model"] == "claude-haiku-5-5"
    assert kw["output_config"]["effort"] == "low" and kw["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in kw and "thinking" not in kw


def test_daily_cap(prisma_case):
    store = MemoryStore()
    for i in range(2):
        store.create_run(f"tr_{i}", case={}, models={}, source="help", deadline_s=100)
    d = decide(prisma_case, store=store, daily_runs=2)
    assert d.decision == "try_first" and d.reason == "daily cap reached"


def test_kill_switch(prisma_case):
    d = decide(prisma_case, enabled=False)
    assert d.decision == "try_first" and "switched off" in d.message


def test_no_key_means_no_team(prisma_case):
    s = config.settings()
    d = gate.decide(prisma_case, store=MemoryStore(), clients=config.Clients(), search_fn=None, settings=s)
    assert d.decision == "try_first" and d.reason == "no ANTHROPIC_API_KEY"


def test_second_asker_attaches_to_the_running_team(prisma_case):
    store = MemoryStore()
    store.create_run("tr_running", case={"fingerprint": "51749bd5"}, models={}, source="help", deadline_s=100)
    d = decide(prisma_case, store=store)
    assert d.decision == "team" and d.attach_run_id == "tr_running"


def test_forum_search_is_skipped_without_a_database(prisma_case):
    assert gate.forum_search(prisma_case) == []


def test_forum_search_has_a_time_limit(prisma_case, monkeypatch):
    import time

    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere")
    monkeypatch.setenv("TEAM_GATE_SEARCH_TIMEOUT_S", "0.3")
    monkeypatch.setattr(gate, "_agent_search", lambda case: time.sleep(2) or [SOLVED])
    t0 = time.monotonic()
    assert gate.forum_search(prisma_case) == []
    assert time.monotonic() - t0 < 1.0


# ---------------------------------------------------------------------------
# Case file
# ---------------------------------------------------------------------------

def test_mcp_ask_for_help_shape_is_accepted():
    case = case_file.normalize({"description": "Running the API in Docker", "command": "docker compose up",
                                "error": "PrismaClientInitializationError", "tried": ["npm ci", "prisma generate"]})
    assert case["goal"] == "Running the API in Docker" and case["check_command"] == "docker compose up"
    assert case["symptom"] == "PrismaClientInitializationError"
    assert case["tried"] == [{"attempt": "npm ci", "result": ""}, {"attempt": "prisma generate", "result": ""}]


def test_empty_case_is_rejected():
    with pytest.raises(case_file.CaseFileError):
        case_file.normalize({"tried": ["x"]})
    with pytest.raises(case_file.CaseFileError):
        case_file.normalize(["not", "an", "object"])


def test_case_file_is_scrubbed():
    case = case_file.normalize({"error": "auth failed for sk-ant-api03-abcdefghijklmnopqrstuv at C:\\Users\\harsh\\app",
                                "environment": "DATABASE_URL=postgres://me:hunter2@db/x, contact bob@example.com"})
    text = case_file.render(case)
    assert "abcdefghijklmnop" not in text and "harsh" not in text and "hunter2" not in text
    assert "bob@example.com" not in text


def test_case_file_is_capped_at_30kb():
    big = {"error": "E" * 20000, "diff": "D" * 20000, "environment": "x",
           "files": [{"path": f"f{i}.py", "excerpt": "F" * 5000} for i in range(8)],
           "tried": [{"attempt": "a" * 600, "result": "r" * 600} for _ in range(12)]}
    case = case_file.normalize(big)
    assert len(json.dumps(case)) <= case_file.MAX_TOTAL_CHARS
    assert case["symptom"].startswith("EEEE")


def test_case_from_open_question():
    post = {"id": 3, "description": "pytest can't import conftest in CI\n\nTried: rootdir, __init__.py",
            "command": "pytest -q", "fingerprint": "abc123ef", "tags": ["pytest", "ci"], "project": "secret-project"}
    case = case_file.from_open_question(post)
    assert case["open_question_id"] == 3 and case["check_command"] == "pytest -q"
    assert case["goal"].startswith("Solve Open Question #3: pytest can't import conftest in CI")
    assert case["project"] is None


def test_docs_domains_follow_the_tech(prisma_case, ci_case):
    from team.researchers import docs_domains

    assert {"prisma.io", "docs.docker.com", "wiki.alpinelinux.org"} <= set(docs_domains(case_file.tech_text(prisma_case)))
    assert {"docs.pytest.org", "docs.github.com", "docs.python.org"} <= set(docs_domains(case_file.tech_text(ci_case)))
    assert docs_domains("something totally unknown") == []


def test_parse_answer_handles_fences_and_bare_json():
    from team.researchers import parse_answer

    cat, f = parse_answer('blah\n```json\n{"category": "build_config", "findings": [{"cause": "c", "confidence": 2}]}\n```')
    assert cat == "build_config" and f[0]["confidence"] == 1.0
    cat, f = parse_answer('Result: {"findings": [{"cause": "c2", "confidence": 0.3, "evidence": ["https://x.y"]}]} done')
    assert f[0]["cause"] == "c2" and f[0]["evidence"][0]["url"] == "https://x.y"
    assert parse_answer("no json here") == (None, None)
