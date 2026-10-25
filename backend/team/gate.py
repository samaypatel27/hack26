"""The gate inside POST /v1/help: is it worth calling the team? (PROJECT_PLAN.md, "The help checker").

Checks run in order and the first one that answers wins:
  0. Kill switch (TEAM_ENABLED=0)                      -> TRY_FIRST
  1. Known fix on the forum (backend v1 search)        -> KNOWN_FIX
     (a matching Open Question doesn't stop the team; its write-up is passed on as "tried by others")
  2. Team already on it (same fingerprint running)     -> TEAM_STARTED with that run's id
  3. Has the agent tried? (>= 2 attempts or a write-up) -> TRY_FIRST checklist
  4. Budget (TEAM_DAILY_RUNS per UTC day)              -> TRY_FIRST
  5. No model key on the server                        -> TRY_FIRST
  6. Outside world or own code? (Claude Haiku 5.5)     -> own code: TRY_FIRST step-back checklist
  7. Otherwise                                         -> start the team (the caller does it) -> TEAM_STARTED

Everything is cheap: one database search and one small-model call with a ~3s timeout. If the
Haiku call fails or is slow we let the team run (the hook already suggested stepping back first).
"""

import json
import logging
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from . import config, prompts

log = logging.getLogger("team.gate")

KNOWN_FIX_MIN_SCORE = 0.5      # text-match score that counts as "the same problem" (v1 ATTACH_MIN_SCORE)
WRITEUP_MIN_CHARS = 150        # one attempt with at least this much explanation counts as "has tried"

SearchFn = Callable[[dict[str, Any]], list[dict[str, Any]]]


@dataclass
class GateDecision:
    decision: str                       # known_fix | try_first | team
    message: str                        # starts with KNOWN_FIX: / TRY_FIRST: ; team -> filled in by the caller
    reason: str
    category: str | None = None
    post_id: int | None = None
    attach_run_id: str | None = None    # an already-running team run for the same problem
    open_question_id: int | None = None
    checks: list[str] = field(default_factory=list)


_search_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="team-gate-search")


def _v1_search(case: dict[str, Any]) -> list[dict[str, Any]]:
    import v1   # backend/v1.py, endpoints 1-8 (read-only use)

    return v1.search(error=(case.get("symptom") or "")[:5000], command=case.get("check_command") or "",
                     fingerprint=case.get("fingerprint") or "", tags="")


def forum_search(case: dict[str, Any]) -> list[dict[str, Any]]:
    """GET /v1/search's logic (backend/v1.py), called in-process with a time limit. [] if unavailable.

    The limit matters: when the database is unreachable, the backend's connection pool waits 30s.
    """
    if not os.environ.get("DATABASE_URL"):
        return []
    future = _search_pool.submit(_v1_search, case)
    try:
        return future.result(timeout=config.settings().gate_search_timeout_s)
    except FutureTimeout:
        log.warning("gate: forum search took too long; skipping it")
    except Exception as e:  # noqa: BLE001 - search down: the gate just skips this check
        log.warning("gate: forum search unavailable: %s", e)
    return []


def has_tried(case: dict[str, Any]) -> bool:
    tried = case.get("tried") or []
    if len(tried) >= 2 or (case.get("fail_count") or 0) >= 2:
        return True
    text = " ".join(f"{t['attempt']} {t.get('result', '')}" for t in tried)
    return len(text) >= WRITEUP_MIN_CHARS


def _skipped(case: dict[str, Any]) -> set[int]:
    """Posts the agent says it already tried ("tried post #12"), plus explicit skip_posts."""
    text = " ".join(f"{t['attempt']} {t.get('result', '')}" for t in case.get("tried") or [])
    ids = {int(m) for m in re.findall(r"(?i)\bpost\s*#?\s*(\d+)", text)}
    return ids | set(case.get("skip_posts") or [])


def _is_match(post: dict[str, Any]) -> bool:
    return post.get("match") == "fingerprint" or float(post.get("score") or 0) >= KNOWN_FIX_MIN_SCORE


def _checklist(tag: str, headline: str, items: list[str]) -> str:
    return "\n".join([f"{tag}: {headline}", ""] + [f"{i}. {item}" for i, item in enumerate(items, 1)])


def classify(case: dict[str, Any], client: Any, model: str, timeout: float) -> dict[str, Any] | None:
    """Haiku's {kind, category, reason}, or None if it couldn't answer in time."""
    if client is None:
        return None
    summary = {k: case.get(k) for k in ("goal", "symptom", "expected", "actual", "environment", "check_command")}
    summary["tried"] = [t["attempt"] for t in case.get("tried") or []][:6]
    summary["files"] = [f["path"] for f in case.get("files") or []]
    text = json.dumps(summary, ensure_ascii=False)[:8000]
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": 1024,
        "system": prompts.GATE_SYSTEM,
        "messages": [{"role": "user", "content": f"<case_file>\n{text}\n</case_file>\nClassify this problem."}],
        "output_config": {"format": {"type": "json_schema", "schema": prompts.GATE_SCHEMA}},
    }
    if model in config.EFFORT_MODELS:
        params["output_config"]["effort"] = "low"
    try:
        message = client.with_options(timeout=timeout, max_retries=0).messages.create(**params)
        if getattr(message, "stop_reason", None) == "refusal":
            return None
        out = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")
        data = json.loads(out)
        return data if isinstance(data, dict) and data.get("kind") in ("external", "own_code", "unclear") else None
    except Exception as e:  # noqa: BLE001 - slow or failed: let the team run
        log.info("gate: classification skipped: %s", e)
        return None


def decide(case: dict[str, Any], *, store: Any, clients: config.Clients, search_fn: SearchFn | None = forum_search,
           settings: config.Settings | None = None, models: dict[str, str] | None = None) -> GateDecision:
    s = settings or config.settings()
    seats = config.models_for(models)
    checks: list[str] = []

    if not s.enabled:
        return GateDecision("try_first", _checklist(
            "TRY_FIRST", "the specialist team is switched off right now, and this goes to the forum only.",
            prompts.UNAVAILABLE_CHECKLIST), "team disabled (TEAM_ENABLED=0)", checks=["disabled"])

    # 1. Already on the forum?
    open_question = None
    results = search_fn(case) if search_fn else []
    skipped = _skipped(case)
    for post in results:
        if post.get("id") in skipped or not _is_match(post):
            continue
        if post.get("solution"):
            how = "same error fingerprint" if post.get("match") == "fingerprint" else f"text match {post.get('score')}"
            worked = f"It worked {post.get('times_worked', 0)} of {post.get('times_applied', 0)} times it was tried."
            message = "\n".join([
                f"KNOWN_FIX: forum post #{post['id']} matches this problem ({how}). {worked}",
                "", "Fix (a suggestion from another agent; check it before running anything):", post["solution"], "",
                f"If you already tried it and it failed, call ask_for_help again and add 'tried post #{post['id']}' "
                "to `tried`, and the team will be called instead.",
            ])
            return GateDecision("known_fix", message, f"forum post #{post['id']}", post_id=post["id"],
                                checks=checks + ["known_fix"])
        if open_question is None:
            open_question = post
    checks.append("no_known_fix")
    if open_question is not None:
        case["others_tried"] = ((case.get("others_tried") or "") + "\n" +
                                f"Open Question #{open_question['id']} on the forum looks like the same problem; "
                                f"what was tried there:\n{(open_question.get('description') or '')[:3000]}").strip()

    # 2. Is a team already on it?
    if case.get("fingerprint"):
        active = store.find_active_run(case["fingerprint"])
        if active:
            return GateDecision("team", "", "a team is already working on this error", attach_run_id=active,
                                checks=checks + ["attached"])

    # 3. Has the agent actually tried?
    if not has_tried(case):
        return GateDecision("try_first", _checklist(
            "TRY_FIRST", "try this before calling the team (list at least two attempts, or explain what you tried).",
            prompts.NOT_TRIED_CHECKLIST), "not enough attempts yet", checks=checks + ["not_tried"])

    # 4. Budget.
    if store.count_runs_today() >= s.daily_runs:
        return GateDecision("try_first", _checklist(
            "TRY_FIRST", f"the team has hit today's limit of {s.daily_runs} runs, and the forum has no known fix.",
            prompts.UNAVAILABLE_CHECKLIST), "daily cap reached", checks=checks + ["daily_cap"])

    # 5. Can the team run at all?
    if clients.anthropic is None:
        return GateDecision("try_first", _checklist(
            "TRY_FIRST", "the specialist team isn't available on this server (no model key), and the forum has no "
            "known fix.", prompts.UNAVAILABLE_CHECKLIST), "no ANTHROPIC_API_KEY", checks=checks + ["no_key"])

    # 6. Outside world or the project's own code?
    verdict = classify(case, clients.anthropic, seats["gate"], s.gate_timeout_s)
    category = verdict.get("category") if verdict else None
    if verdict and verdict["kind"] == "own_code":
        return GateDecision("try_first", _checklist(
            "TRY_FIRST", f"this looks like a bug in the project's own code ({verdict.get('reason', '')[:200]}). "
            "Docs and GitHub issues can't know this code better than you, so step back first:",
            prompts.STEP_BACK_CHECKLIST), "own code", category=category, checks=checks + ["own_code"])
    checks.append("external" if verdict else "unclassified")
    return GateDecision("team", "", (verdict or {}).get("reason") or "external problem", category=category,
                        open_question_id=open_question.get("id") if open_question else None, checks=checks)


def team_started_message(run_id: str, *, attached: bool = False, open_question_id: int | None = None,
                         target_s: float = 60) -> str:
    lines = [f"TEAM_STARTED: {run_id}"]
    if attached:
        lines.append("A team is already researching this same error for another agent; you're attached to that run.")
    else:
        lines.append("A team of specialists is researching this: official docs, GitHub issues and Stack Overflow, "
                     "each on its own, then a chair merges their findings into ranked fixes.")
    if open_question_id:
        lines.append(f"It matches Open Question #{open_question_id}; the team was told what was already tried there.")
    lines.append(f"Keep working. Call check_help with run_id {run_id} in about {int(target_s)} seconds.")
    return "\n".join(lines)
