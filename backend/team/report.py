"""The final report: its shape (docs/api-endpoints.md #10), the safety filter, and the text the agent reads.

Report shape:
    {"status": "report" | "no_answer" | "failed",
     "hypotheses": [{"rank", "cause", "confidence", "evidence": [{url, source, quote, seen?}],
                     "steps": [...], "check": {"command", "description"}, "agreed_by": [...]}],
     "if_none_work", "environment_check", "category",
     "models": {"docs", "issues", "community", "chair"}, "cost_usd", "costs": {member: {...}},
     "timings": {...}, "flagged": [...], "notes": [...], "verification": "possible_answer", "error"}

Every proposed fix is a "Possible answer": the server can't run the check command. The stuck
agent (or the owner's agent, for a cold-case re-run) tries it and the hook records the outcome.
"""

import re
from typing import Any

import safety
from scrub import scrub

MAX_HYPOTHESES = 3
VERIFICATION_NOTE = ("Possible answer: untested suggestions from web research. The server can't run the check, "
                     "so try each one on the real project; it only counts as solved once the check passes.")

# Fake or dangerous fixes beyond backend/safety.py's rules (PROJECT_PLAN.md, "Is a solution worth posting?").
# A matching step is dropped from the report.
BLOCK_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\|\|\s*(?:true|:|exit\s+0)\b"), "hides a failure (|| true)"),
    (re.compile(r"--no-verify\b"), "skips git hooks (--no-verify)"),
    (re.compile(r"\brm\s+(?:-\w*\s+)*-\w*[rR]\w*\s+(?:-\w+\s+)*(?:/|~|\*|\$HOME|\$\{HOME\}|\.{1,2})(?:/?\*?)?(?=$|[\s;&|)\"'`])"),
     "deletes a whole folder tree (rm -r on /, ~, *, . or ..)"),
    (re.compile(r"(?i)@pytest\.mark\.skip|pytest\.skip\(|\b(?:it|test|describe)\.skip\(|\bx(?:it|describe|test)\(|"
                r"-DskipTests|--skip-?tests\b|@Ignore\b|@Disabled\b|\bt\.Skip\("),
     "skips tests"),
    (re.compile(r"(?i)\b(?:delete|remove|comment\s+out|disable)\s+(?:the\s+|that\s+|this\s+)?(?:failing\s+)?tests?\b"),
     "deletes or disables tests"),
    (re.compile(r"@ts-ignore|@ts-nocheck|#\s*type:\s*ignore|eslint-disable|#\s*noqa"), "hides the error instead of fixing it"),
    (re.compile(r"(?i)NODE_TLS_REJECT_UNAUTHORIZED\s*=?\s*['\"]?0|\bverify\s*=\s*False\b|--insecure\b|"
                r"\bcurl\s+(?:[^\n]*\s)?-k\b|sslmode=disable|strict-ssl\s+false|--disable-web-security|setenforce\s+0"),
     "turns off a security check (TLS verification etc.)"),
    (re.compile(r"\bchmod\s+(?:-\w+\s+)*0?777\b"), "makes files writable by everyone (chmod 777)"),
    (re.compile(r"\bgit\s+push\b[^\n]*(?:--force\b|\s-f\b)"), "force-pushes"),
    (re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-\w*[fdx])"), "throws away local work (git reset --hard / git clean)"),
]
# Allowed, but shown with a caution in front of the step.
CAUTION_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(?:-\w*\s+)*-\w*[rR]"), "deletes files recursively: check the path first"),
    (re.compile(r"\bsudo\b"), "needs admin rights"),
    (re.compile(r"--force\b|--legacy-peer-deps\b|--break-system-packages\b"), "forces past a safety check"),
    (re.compile(r"(?i)\bdrop\s+(?:table|database|schema)\b|migrate\s+reset\b|--accept-data-loss\b|\btruncate\s+table\b"),
     "can delete data"),
]


def block_reason(text: str | None) -> str | None:
    """Why this step must not be shown, or None."""
    if not text:
        return None
    reason = safety.check(text)
    if reason:
        return reason.removeprefix("Rejected by the safety filter: ").rstrip(".")
    for pattern, why in BLOCK_RULES:
        if pattern.search(text):
            return why
    return None


def caution(text: str) -> str | None:
    for pattern, why in CAUTION_RULES:
        if pattern.search(text):
            return why
    return None


def apply_safety(report: dict[str, Any]) -> dict[str, Any]:
    """Scrub every text, drop dangerous steps and injected quotes, flag risky steps. Changes `report` in place."""
    flagged: list[dict[str, Any]] = report.setdefault("flagged", [])
    for hyp in report.get("hypotheses") or []:
        rank = hyp.get("rank")
        hyp["cause"] = scrub(hyp.get("cause")) or ""
        kept: list[str] = []
        for step in hyp.get("steps") or []:
            step = scrub(step) or ""
            why = block_reason(step)
            if why:
                flagged.append({"rank": rank, "step": step[:300], "reason": why, "action": "removed"})
                continue
            warn = caution(step)
            if warn:
                flagged.append({"rank": rank, "step": step[:300], "reason": warn, "action": "caution"})
                step = f"[caution: {warn}] {step}"
            kept.append(step)
        if hyp.get("steps") and not kept:
            kept = ["(Every suggested step was removed by the safety filter; use the cause and evidence only.)"]
        hyp["steps"] = kept
        check = hyp.get("check") or {}
        command = scrub(check.get("command")) if check.get("command") else None
        why = block_reason(command)
        if why:
            flagged.append({"rank": rank, "step": (command or "")[:300], "reason": why, "action": "removed"})
            command = None
        hyp["check"] = {"command": command, "description": scrub(check.get("description")) or ""}
        for ev in hyp.get("evidence") or []:
            ev["quote"] = scrub(ev.get("quote")) or ""
            if safety.check(ev["quote"]):   # a web page trying to give the reader orders
                flagged.append({"rank": rank, "step": ev.get("url", ""), "reason": "quote looked like prompt injection",
                                "action": "removed"})
                ev["quote"] = "(quote removed by the safety filter)"
    for key in ("if_none_work", "environment_check"):
        if report.get(key):
            text = scrub(report[key]) or ""
            report[key] = "(removed by the safety filter)" if block_reason(text) else text
    return report


# ---------------------------------------------------------------------------
# Building the hypotheses
# ---------------------------------------------------------------------------

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_.\-]{2,}", text.lower())}


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.45


def rank_hypotheses(items: list[dict[str, Any]], min_confidence: float) -> tuple[list[dict[str, Any]], bool]:
    """Sorted, numbered, capped at 3. Returns (hypotheses, answered) where answered means one reaches the bar."""
    live = [h for h in items if not h.get("already_tried")]
    live.sort(key=lambda h: -float(h.get("confidence") or 0))
    out = []
    for i, h in enumerate(live[:MAX_HYPOTHESES], 1):
        out.append({
            "rank": i,
            "cause": h.get("cause") or "",
            "confidence": round(min(1.0, max(0.0, float(h.get("confidence") or 0))), 2),
            "evidence": list(h.get("evidence") or []),
            "steps": list(h.get("steps") or []),
            "check": h.get("check") or {"command": None, "description": ""},
            "agreed_by": list(h.get("agreed_by") or []),
        })
    answered = any(h["confidence"] >= min_confidence for h in out)
    return out, answered


def merge_without_chair(results: list[Any]) -> list[dict[str, Any]]:
    """The chair's fallback: every researcher's findings, near-duplicates merged, by confidence."""
    merged: list[dict[str, Any]] = []
    for r in results:
        for f in r.findings:
            same = next((m for m in merged if _similar(m["cause"], f["cause"])), None)
            if same:
                if r.seat not in same["agreed_by"]:
                    same["agreed_by"].append(r.seat)
                if f["confidence"] > same["confidence"]:
                    same.update({"cause": f["cause"], "confidence": f["confidence"], "steps": f["steps"],
                                 "check": f["check"]})
                seen_urls = {e["url"] for e in same["evidence"]}
                same["evidence"] += [e for e in f["evidence"] if e["url"] not in seen_urls]
            else:
                merged.append({**f, "evidence": list(f["evidence"]), "agreed_by": [r.seat]})
    return merged


# ---------------------------------------------------------------------------
# Text for the agent (what check_help prints)
# ---------------------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _hypothesis_lines(h: dict[str, Any]) -> list[str]:
    lines = [f"{h['rank']}. [{_pct(h['confidence'])} confidence] {h['cause']}"]
    if h.get("agreed_by"):
        lines.append(f"   found by: {', '.join(h['agreed_by'])}")
    for ev in h.get("evidence", [])[:3]:
        quote = f' - "{ev["quote"]}"' if ev.get("quote") else ""
        unseen = " (unconfirmed link)" if ev.get("seen") is False else ""
        lines.append(f"   evidence ({ev.get('source', 'web')}): {ev['url']}{unseen}{quote}")
    if h.get("steps"):
        lines.append("   steps:")
        lines += [f"     {i}. {s}" for i, s in enumerate(h["steps"], 1)]
    check = h.get("check") or {}
    if check.get("command"):
        lines.append(f"   check: run `{check['command']}`" + (f" ({check['description']})" if check.get("description") else ""))
    elif check.get("description"):
        lines.append(f"   check: {check['description']}")
    return lines


def render_report(run_id: str, report: dict[str, Any]) -> str:
    status = report.get("status")
    hyps = report.get("hypotheses") or []
    if status == "failed":
        return (f"TEAM_FAILED: run {run_id} could not finish ({report.get('error') or 'unknown error'}).\n"
                "Keep debugging locally. You can call ask_for_help again later; if nothing works, record it with "
                "post_open_question.")
    lines: list[str]
    if status == "no_answer":
        lines = [f"TEAM_NO_ANSWER: run {run_id}. The team found no cause it is confident about "
                 f"(none reached {_pct(report.get('min_confidence', 0.4))})."]
        if hyps:
            lines += ["", "Low-confidence guesses, if you want to try them:"]
    else:
        lines = [f"TEAM_REPORT: run {run_id}. {len(hyps)} likely cause(s), most likely first. "
                 "Try them one at a time on the real project and rerun the check after each."]
        lines.append("")
    for h in hyps:
        lines += _hypothesis_lines(h)
        lines.append("")
    if report.get("environment_check"):
        lines += [f"Setup check: {report['environment_check']}", ""]
    if report.get("if_none_work"):
        lines += [f"If none work: {report['if_none_work']}", ""]
    removed = [f for f in report.get("flagged") or [] if f.get("action") == "removed"]
    if removed:
        lines += [f"Safety filter removed {len(removed)} step(s): " + "; ".join(sorted({f['reason'] for f in removed})), ""]
    for note in report.get("notes") or []:
        lines.append(f"Note: {note}")
    if status == "no_answer":
        lines.append("Next: if your own attempts also fail, call post_open_question with everything you and the team "
                     "tried, so a stronger model can take another look later.")
    else:
        lines.append(VERIFICATION_NOTE)
    return "\n".join(lines).strip()


def render_working(run_id: str, events: list[dict[str, Any]], elapsed_s: float, target_s: float) -> str:
    """TEAM_WORKING text: what each member is doing right now."""
    latest: dict[str, str] = {}
    for e in events:
        member, kind = e.get("member"), e.get("type")
        if member in ("docs", "issues", "community", "chair"):
            if kind == "searching":
                latest[member] = f"searching \"{(e.get('text') or '')[:80]}\""
            elif kind == "fetching":
                latest[member] = "reading a page"
            elif kind in ("finished", "chair_done"):
                latest[member] = "done"
            elif kind in ("failed", "timeout", "skipped"):
                latest[member] = kind
            elif kind in ("started", "chair_started"):
                latest.setdefault(member, "started")
    parts = [f"{m}: {latest[m]}" for m in ("docs", "issues", "community", "chair") if m in latest]
    left = max(5, int(target_s - elapsed_s)) if elapsed_s < target_s else 20
    return (f"TEAM_WORKING: run {run_id}, {int(elapsed_s)}s in. " + ("; ".join(parts) or "starting") +
            f".\nKeep working on the problem yourself and call check_help again in about {left}s.")
