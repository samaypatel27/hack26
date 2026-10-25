"""The chair (Claude Opus 5.5, effort high): merges the researchers' findings into the final report.

It also does the jobs of the former "environment checker" (compare the setup with what the tools
require) and "fixer" (write exact steps). It never browses: it only reads the case file and the
findings, so it is fast and can't be steered by new web pages.
"""

import json
from typing import Any

from . import config, prompts
from .costs import CostMeter
from .researchers import MemberResult, normalize_finding

MAX_TOKENS = 16000


class ChairError(Exception):
    pass


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def findings_text(results: list[MemberResult]) -> str:
    """What the chair reads: one block per researcher, findings as JSON (URLs marked seen or not)."""
    blocks = []
    for r in results:
        head = f'<researcher seat="{r.seat}" model="{r.model}" status="{r.status}" category="{r.category or "?"}">'
        if r.status != "ok":
            body = f"(no findings: {r.error})"
        elif r.findings:
            body = json.dumps(r.findings, ensure_ascii=False, indent=1)
        elif r.notes:
            body = "(answer had no JSON; raw notes follow)\n" + r.notes[-4000:]
        else:
            body = "(searched and found nothing useful)"
        blocks.append(f"{head}\n{body}\n</researcher>")
    return "\n\n".join(blocks)


def run_chair(client: Any, model: str, case_text: str, results: list[MemberResult], *, timeout: float,
              meter: CostMeter, effort: str) -> dict[str, Any]:
    """The chair's raw report: {category, environment_check, hypotheses[...], if_none_work}. Raises ChairError."""
    if client is None:
        raise ChairError("ANTHROPIC_API_KEY is not set")
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": prompts.CHAIR_SYSTEM,
        "messages": [{"role": "user", "content": prompts.CHAIR_USER.format(
            case=case_text, findings=findings_text(results))}],
        "output_config": {"format": {"type": "json_schema", "schema": prompts.CHAIR_SCHEMA}},
    }
    if model in config.EFFORT_MODELS:
        params["output_config"]["effort"] = effort
    if model in config.FALLBACK_MODELS:
        params["betas"] = [config.FALLBACK_BETA]
        params["extra_body"] = {"fallbacks": "default"}
    try:
        message = client.beta.messages.create(timeout=timeout, **params)
    except Exception as e:  # noqa: BLE001
        raise ChairError(f"{type(e).__name__}: {e}"[:300]) from e
    meter.add_claude("chair", model, _get(message, "usage"))
    stop = _get(message, "stop_reason")
    if stop == "refusal":
        raise ChairError(f"chair declined (refusal: {_get(_get(message, 'stop_details'), 'category') or 'unknown'})")
    if stop == "max_tokens":
        raise ChairError("chair ran out of output tokens")
    text = "".join(_get(b, "text") or "" for b in _get(message, "content") or [] if _get(b, "type") == "text")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ChairError(f"chair answer was not JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("hypotheses"), list):
        raise ChairError("chair answer had no hypotheses list")
    seats_ok = {r.seat for r in results if r.status == "ok"}
    # A URL counts as seen if some researcher's tools returned it; one no researcher cited is unseen.
    url_seen: dict[str, bool] = {}
    for r in results:
        for f in r.findings:
            for ev in f["evidence"]:
                url_seen[ev["url"]] = url_seen.get(ev["url"], False) or ev.get("seen", True)
    hypotheses = []
    for raw in data["hypotheses"]:
        h = normalize_finding(raw)
        if not h:
            continue
        for ev in h["evidence"]:
            ev["seen"] = url_seen.get(ev["url"], False)
        h["agreed_by"] = [s for s in (raw.get("agreed_by") or []) if s in seats_ok]
        h["already_tried"] = bool(raw.get("already_tried"))
        hypotheses.append(h)
    return {
        "category": data.get("category"),
        "environment_check": str(data.get("environment_check") or "")[:2000],
        "hypotheses": hypotheses,
        "if_none_work": str(data.get("if_none_work") or "")[:2000],
    }
