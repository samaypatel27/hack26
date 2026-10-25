"""The specialist team's endpoints: 9-11 of docs/api-endpoints.md.

    POST /v1/help            case file in -> KNOWN_FIX / TRY_FIRST / TEAM_STARTED text (within ~1-3s)
    GET  /v1/help/{run_id}   TEAM_WORKING / TEAM_REPORT / TEAM_NO_ANSWER / TEAM_FAILED text, plus
                             status, events and report for the website's board room
    POST /v1/rerun           cold-case re-run of stored Open Questions with a chosen model

Include it in main.py with one line (next to the v1 router):
    from team_v1 import router as team_router; app.include_router(team_router)

Same rules as backend/v1.py: POSTs need X-API-Key = env API_KEY, GETs are public, rejected input is
400 {"detail"}, unknown ids are 404. Team runs execute in a small thread pool inside this process
(TEAM_MAX_CONCURRENT_RUNS), so the request returns at once and the agent polls check_help.
"""

import hmac
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException

from team import case_file, config, gate, report, runner
from team import store as store_mod

log = logging.getLogger("team.api")

router = APIRouter(prefix="/v1", tags=["team"])

QUEUE_GRACE_S = 120         # deadline slack for a run waiting in the thread pool queue
MAX_RERUN = 25


class _State:
    """What the endpoints use. Tests replace these; None means "use the real thing"."""
    clients: config.Clients | None = None
    search_fn: Any = gate.forum_search
    open_questions_fn: Any = None   # (ids | "all", limit) -> list of Open Question posts
    notify_fn: Any = None           # (open_question_id, message) -> None
    executor: ThreadPoolExecutor | None = None
    lock = threading.Lock()


STATE = _State()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Same rule as v1.require_api_key."""
    expected = os.environ.get("API_KEY")
    if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _clients() -> config.Clients:
    return STATE.clients if STATE.clients is not None else config.make_clients()


def _executor() -> ThreadPoolExecutor:
    with STATE.lock:
        if STATE.executor is None:
            STATE.executor = ThreadPoolExecutor(max_workers=config.settings().max_concurrent_runs,
                                                thread_name_prefix="team-run")
        return STATE.executor


def _execute(store: Any, run_id: str, case: dict[str, Any], models: dict[str, str] | None,
             clients: config.Clients, source: str, open_question_id: int | None) -> None:
    s = config.settings()
    try:
        store.mark_started(run_id, s.hard_stop_s)

        def on_event(member: str, kind: str, text: str, data: dict[str, Any] | None = None) -> None:
            store.add_event(run_id, member, kind, text, data)

        result = runner.run_team(case, on_event, models, clients=clients, settings=s)
    except Exception as e:  # noqa: BLE001 - a crashed run must end as failed, not stay "running"
        log.exception("team run %s crashed", run_id)
        result = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300], "hypotheses": []}
    try:
        store.finish_run(run_id, result["status"], result, result.get("error"))
    except Exception:  # noqa: BLE001
        log.exception("could not save team run %s", run_id)
        return
    if source == "rerun" and open_question_id and result["status"] == "report":
        _notify_possible_answer(open_question_id, run_id, result)


def start_run(case: dict[str, Any], *, models: dict[str, str] | None = None, source: str = "help",
              open_question_id: int | None = None) -> str:
    store = store_mod.get_store()
    s = config.settings()
    run_id = store_mod.new_run_id()
    store.create_run(run_id, case=case, models=config.models_for(models), source=source,
                     deadline_s=s.hard_stop_s + QUEUE_GRACE_S, open_question_id=open_question_id)
    _executor().submit(_execute, store, run_id, case, models, _clients(), source, open_question_id)
    return run_id


# ---------------------------------------------------------------------------
# 9. POST /v1/help
# ---------------------------------------------------------------------------

@router.post("/help", dependencies=[Depends(require_api_key)])
def ask_for_help(raw: Any = Body(default=None)) -> dict[str, Any]:
    try:
        case = case_file.normalize(raw)
    except case_file.CaseFileError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    store = store_mod.get_store()
    store.expire_stale()
    s = config.settings()
    decision = gate.decide(case, store=store, clients=_clients(), search_fn=STATE.search_fn, settings=s)
    body: dict[str, Any] = {"decision": decision.decision, "reason": decision.reason, "category": decision.category,
                            "run_id": None, "post_id": decision.post_id}
    if decision.decision != "team":
        return {"message": decision.message, **body}
    if decision.attach_run_id:
        run_id = decision.attach_run_id
        message = gate.team_started_message(run_id, attached=True, target_s=s.target_s)
    else:
        run_id = start_run(case)
        message = gate.team_started_message(run_id, open_question_id=decision.open_question_id, target_s=s.target_s)
    return {"message": message, **body, "decision": "team_started", "run_id": run_id}


# ---------------------------------------------------------------------------
# 10. GET /v1/help/{run_id}
# ---------------------------------------------------------------------------

def _elapsed(run: dict[str, Any]) -> float:
    start = run.get("started_at") or run.get("created_at")
    end = run.get("finished_at") or datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds()) if start else 0.0


@router.get("/help/{run_id}")
def check_help(run_id: str, since: int = 0) -> dict[str, Any]:
    """Progress or the final report. `since` = last event id you have, to fetch only new events."""
    store = store_mod.get_store()
    store.expire_stale()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Team run not found")
    events = store.list_events(run_id, since=max(0, since))
    status = "working" if run["status"] == "running" else run["status"]
    rep = run.get("report")
    if status == "working":
        all_events = events if since <= 0 else store.list_events(run_id)
        message = report.render_working(run_id, all_events, _elapsed(run), config.settings().target_s)
    elif status == "failed" and not rep:
        rep = {"status": "failed", "error": run.get("error"), "hypotheses": []}
        message = report.render_report(run_id, rep)
    else:
        message = report.render_report(run_id, rep or {"status": status, "hypotheses": []})
    return {
        "run_id": run_id,
        "status": status,
        "message": message,
        "events": events,
        "report": rep,
        "source": run.get("source"),
        "open_question_id": run.get("open_question_id"),
        "elapsed_s": round(_elapsed(run), 1),
        "store": getattr(store, "kind", "unknown"),
    }


# ---------------------------------------------------------------------------
# 11. POST /v1/rerun
# ---------------------------------------------------------------------------

def _load_open_questions(ids: list[int] | str, limit: int) -> list[dict[str, Any]]:
    """Open Question posts from the backend's tables (backend/db_v1.py, read-only use)."""
    if STATE.open_questions_fn is not None:
        return STATE.open_questions_fn(ids, limit)
    if not os.environ.get("DATABASE_URL"):
        raise HTTPException(status_code=400, detail="Re-runs need the database (DATABASE_URL) to read Open Questions.")
    import db_v1

    with db_v1.connect() as conn:
        if ids == "all":
            posts = db_v1.candidate_posts(conn, open_only=True)[:limit]
        else:
            posts = [p for p in (db_v1.get_post(conn, i) for i in ids) if p]
    return [{**p, "tags": list(p.get("tags") or [])} for p in posts]


def _notify_possible_answer(open_question_id: int, run_id: str, result: dict[str, Any]) -> None:
    """Tell the Open Question's owner (GET /v1/updates) that a re-run found a possible answer. Never raises."""
    top = (result.get("hypotheses") or [{}])[0]
    models = result.get("models") or {}
    message = (f"A cold-case re-run ({models.get('chair') or models.get('docs')}) found a possible answer for your "
               f"Open Question #{open_question_id}, not verified yet: {top.get('cause', '')[:200]} "
               f"(team run {run_id}; call check_help with that id for the steps, try them, and post the fix if it works).")
    try:
        if STATE.notify_fn is not None:
            STATE.notify_fn(open_question_id, message)
            return
        if not os.environ.get("DATABASE_URL"):
            return
        import db_v1

        with db_v1.connect() as conn:
            for project in db_v1.watchers(conn, open_question_id):
                db_v1.add_update(conn, project, message, open_question_id)
    except Exception:  # noqa: BLE001
        log.exception("could not queue the possible-answer update for Open Question %s", open_question_id)


@router.post("/rerun", dependencies=[Depends(require_api_key)])
def rerun(raw: Any = Body(default=None)) -> dict[str, Any]:
    """{"model": "<model>", "open_question_ids": [..] | "all", "limit"?: 10} -> {"run_ids": [...]}."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 100:
        raise HTTPException(status_code=400, detail="`model` must be a model id, e.g. claude-opus-5-5.")
    ids = raw.get("open_question_ids", "all")
    if ids != "all":
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise HTTPException(status_code=400, detail='`open_question_ids` must be a list of post ids or "all".')
        ids = ids[:MAX_RERUN]
    limit = raw.get("limit", 10)
    if not isinstance(limit, int) or not 1 <= limit <= MAX_RERUN:
        raise HTTPException(status_code=400, detail=f"`limit` must be between 1 and {MAX_RERUN}.")
    s = config.settings()
    if not s.enabled:
        raise HTTPException(status_code=400, detail="The team is switched off (TEAM_ENABLED=0).")
    store = store_mod.get_store()
    questions = [q for q in _load_open_questions(ids, limit) if q.get("solution") is None]
    overrides = config.override_for(model)
    run_ids, skipped = [], []
    budget_left = max(0, s.daily_runs - store.count_runs_today())
    for q in questions:
        if len(run_ids) >= budget_left:
            skipped.append({"open_question_id": q["id"], "reason": "daily run cap reached"})
            continue
        case = case_file.from_open_question(q)
        run_ids.append(start_run(case, models=overrides, source="rerun", open_question_id=q["id"]))
    return {
        "run_ids": run_ids,
        "open_question_ids": [q["id"] for q in questions][: len(run_ids)],
        "skipped": skipped,
        "models": config.models_for(overrides),
        "note": ("Each finished run is a 'Possible answer', not 'Solved (verified)': the server can't run the "
                 "reproduction. The owner is told through GET /v1/updates; their agent tries the fix, and the "
                 "hook records the outcome."),
    }


def wait_for(run_id: str, timeout: float = 150.0, poll: float = 0.5) -> dict[str, Any] | None:
    """Block until a run is finished (CLI and tests). Returns the run row, or None on timeout."""
    store = store_mod.get_store()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        run = store.get_run(run_id)
        if run and run["status"] != "running":
            return run
        time.sleep(poll)
    return None
