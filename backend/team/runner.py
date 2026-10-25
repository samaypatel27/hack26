"""One team run: three researchers in parallel, then the chair, with timeouts, cost cap and events.

    report = run_team(case_file, on_event, models=None)

`case_file` is a dict from case_file.normalize. `on_event(member, type, text, data)` is called for
every step (the board room); it must not raise (errors in it are swallowed). The returned report
always has a `status`: "report", "no_answer" or "failed" (see report.py for the shape).

Timing rules (env TEAM_*): each researcher gets TEAM_RESEARCHER_TIMEOUT_S (60s); the chair starts as
soon as every researcher is done or out of time, with at least one finished; the whole run stops
at TEAM_HARD_STOP_S (120s). Failures: chair failure -> the researchers' findings sorted by
confidence; no researcher finished -> "failed" (never "no_answer", which means "we looked and
found nothing").
"""

import concurrent.futures as cf
import logging
import time
from collections.abc import Callable
from typing import Any

from . import case_file as cf_mod
from . import config, report as report_mod
from .chair import ChairError, run_chair
from .costs import CostMeter
from .researchers import Context, MemberResult, build_researchers

log = logging.getLogger("team.runner")

OnEvent = Callable[[str, str, str, dict[str, Any] | None], None]
CHAIR_MIN_SECONDS = 10.0     # don't start the chair with less time than this before the hard stop
HARD_STOP_MARGIN = 2.0


def _safe_emitter(on_event: OnEvent | None) -> OnEvent:
    def emit(member: str, kind: str, text: str, data: dict[str, Any] | None = None) -> None:
        if on_event is None:
            return
        try:
            on_event(member, kind, text, data)
        except Exception:  # noqa: BLE001 - a broken event sink must never break the run
            log.exception("on_event failed")
    return emit


def run_team(case: dict[str, Any], on_event: OnEvent | None = None, models: dict[str, str] | None = None, *,
             clients: config.Clients | None = None, settings: config.Settings | None = None) -> dict[str, Any]:
    s = settings or config.settings()
    seats = config.models_for(models)
    clients = clients if clients is not None else config.make_clients()
    emit = _safe_emitter(on_event)
    meter = CostMeter(s.run_cost_cap_usd)
    t0 = time.monotonic()
    hard_deadline = t0 + s.hard_stop_s
    researcher_deadline = min(t0 + s.researcher_timeout_s, hard_deadline - CHAIR_MIN_SECONDS)
    timings: dict[str, Any] = {}
    case_text = cf_mod.render(case)

    def finish(rep: dict[str, Any]) -> dict[str, Any]:
        rep["cost_usd"] = round(meter.total, 4)
        rep["costs"] = meter.summary()
        timings["total_s"] = round(time.monotonic() - t0, 1)
        rep["timings"] = timings
        rep["min_confidence"] = s.min_confidence
        rep.setdefault("verification", "possible_answer")
        report_mod.apply_safety(rep)
        emit("runner", "done", f"{rep['status']} in {timings['total_s']}s, ${rep['cost_usd']:.3f}",
             {"status": rep["status"], "cost_usd": rep["cost_usd"], "timings": timings})
        return rep

    emit("runner", "started", "team run started", {"models": seats})
    if clients.anthropic is None and clients.openai is None:
        return finish({"status": "failed", "error": "no model API key is set on the server (ANTHROPIC_API_KEY)",
                       "hypotheses": [], "models": {}, "notes": []})

    # 1. Researchers, in parallel and independent.
    researchers = build_researchers(case, seats, clients)
    pool = cf.ThreadPoolExecutor(max_workers=len(researchers), thread_name_prefix="team-researcher")
    futures: dict[cf.Future[MemberResult], Any] = {}
    for r in researchers:
        ctx = Context(deadline=researcher_deadline, meter=meter, settings=s,
                      min_call_s=min(3.0, s.researcher_timeout_s / 10),
                      emit=lambda kind, text, data=None, seat=r.seat: emit(seat, kind, text, data))
        futures[pool.submit(r.run_safe, case_text, ctx)] = r
    done, not_done = cf.wait(futures, timeout=max(0.0, researcher_deadline - time.monotonic()) + 1.0)
    results: list[MemberResult] = []
    for fut, r in futures.items():
        if fut in done:
            results.append(fut.result())
        else:   # still running: give up on it (its HTTP timeout will end the thread soon)
            emit(r.seat, "timeout", f"no answer within {int(s.researcher_timeout_s)}s", None)
            results.append(MemberResult(r.seat, r.model, status="timeout", error="ran out of time",
                                        elapsed_s=round(time.monotonic() - t0, 1)))
    pool.shutdown(wait=False, cancel_futures=True)
    timings["researchers_s"] = round(time.monotonic() - t0, 1)
    for r in results:
        timings[f"{r.seat}_s"] = r.elapsed_s
    models_used = {r.seat: r.model for r in results}
    notes: list[str] = []
    ok = [r for r in results if r.status == "ok"]
    for r in results:
        if r.status != "ok":
            notes.append(f"{r.seat} researcher {r.status}: {r.error}")
    if not ok:
        return finish({"status": "failed", "error": "no researcher finished (" +
                       "; ".join(f"{r.seat}: {r.error}" for r in results) + ")",
                       "hypotheses": [], "models": models_used, "notes": notes})

    # 2. The chair.
    chair_out: dict[str, Any] | None = None
    remaining = hard_deadline - time.monotonic() - HARD_STOP_MARGIN
    if meter.exceeded:
        notes.append(f"run cost cap (${s.run_cost_cap_usd:.2f}) reached before the chair; findings merged without it")
    elif remaining < CHAIR_MIN_SECONDS:
        notes.append("no time left for the chair; findings merged without it")
    else:
        emit("chair", "chair_started", f"chair merging {len(ok)} researcher report(s)", {"model": seats["chair"]})
        c0 = time.monotonic()
        try:
            chair_out = run_chair(clients.anthropic, seats["chair"], case_text, results, timeout=remaining,
                                  meter=meter, effort=s.chair_effort)
            models_used["chair"] = seats["chair"]
            emit("chair", "chair_done", f"{len(chair_out['hypotheses'])} hypothesis(es) in "
                 f"{round(time.monotonic() - c0, 1)}s, ${meter.member_cost('chair'):.3f}",
                 {"elapsed_s": round(time.monotonic() - c0, 1), "cost_usd": round(meter.member_cost("chair"), 4)})
        except ChairError as e:
            notes.append(f"chair failed ({e}); showing the researchers' findings sorted by confidence")
            emit("chair", "failed", str(e), {"error": str(e)})
        timings["chair_s"] = round(time.monotonic() - c0, 1)

    if chair_out is not None:
        candidates = chair_out["hypotheses"]
        extra = {"category": chair_out.get("category"), "environment_check": chair_out.get("environment_check"),
                 "if_none_work": chair_out.get("if_none_work")}
    else:
        models_used["chair"] = "none (fallback merge)"
        candidates = report_mod.merge_without_chair(ok)
        cats = [r.category for r in ok if r.category]
        extra = {"category": max(set(cats), key=cats.count) if cats else None, "environment_check": "",
                 "if_none_work": "Re-check the versions and platform in the case file against each library's "
                                 "documented requirements, then file an Open Question with everything tried."}
    hypotheses, answered = report_mod.rank_hypotheses(candidates, s.min_confidence)
    return finish({"status": "report" if answered else "no_answer", "hypotheses": hypotheses,
                   "models": models_used, "notes": notes, "chair_fallback": chair_out is None, **extra})
