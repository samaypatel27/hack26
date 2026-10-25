"""The case file: everything the team is allowed to see about a problem (docs/api-endpoints.md, #9).

`normalize` accepts two shapes and returns one clean dict:
  - the full case file from docs/api-endpoints.md (goal, symptom, tried[{attempt, result}], ...);
  - what plugin/mcp/server.py's ask_for_help sends today: {description, command, error, tried: [str]}.

Every string is scrubbed (backend/scrub.py: secrets, emails, home paths) and capped, and the
whole file is kept under MAX_TOTAL_CHARS (~30 KB) by trimming the diff, then files, then attempts.
The team never sees the user's repo, only this.
"""

import json
from typing import Any

from scrub import scrub

MAX_TOTAL_CHARS = 30_000
FIELD_CAPS = {
    "goal": 2_000, "symptom": 8_000, "expected": 2_000, "actual": 2_000,
    "check_command": 500, "check_description": 1_000, "environment": 3_000,
    "diff": 8_000, "fingerprint": 64, "model": 100, "project": 200, "others_tried": 4_000,
}
MAX_TRIED = 12
TRIED_CAPS = {"attempt": 600, "result": 600}
MAX_FILES = 8
FILE_PATH_CAP = 300
FILE_EXCERPT_CAP = 3_000


class CaseFileError(ValueError):
    """The request can't be turned into a case file (becomes a 400)."""


def _text(value: Any, cap: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    value = (scrub(value) or "").strip()
    if not value:
        return None
    return value if len(value) <= cap else value[:cap].rstrip() + " ...[trimmed]"


def _tried(raw: Any) -> list[dict[str, str]]:
    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, (str, dict)) else [])
    out: list[dict[str, str]] = []
    for item in items[:MAX_TRIED]:
        if isinstance(item, dict):
            attempt = _text(item.get("attempt") or item.get("tried") or item.get("what"), TRIED_CAPS["attempt"])
            result = _text(item.get("result") or item.get("outcome") or item.get("why_failed"), TRIED_CAPS["result"])
        else:
            attempt, result = _text(item, TRIED_CAPS["attempt"]), None
        if attempt:
            out.append({"attempt": attempt, "result": result or ""})
    return out


def _files(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_FILES]:
        if not isinstance(item, dict):
            continue
        path = _text(item.get("path"), FILE_PATH_CAP)
        excerpt = _text(item.get("excerpt") or item.get("content"), FILE_EXCERPT_CAP)
        if path or excerpt:
            out.append({"path": path or "(unnamed)", "excerpt": excerpt or ""})
    return out


def normalize(raw: Any) -> dict[str, Any]:
    """A clean, scrubbed, size-capped case file. Raises CaseFileError if there is no problem in it."""
    if not isinstance(raw, dict):
        raise CaseFileError("Body must be a JSON object (the case file).")
    # ask_for_help (plugin/mcp/server.py) sends description/command/error.
    description = raw.get("description")
    case: dict[str, Any] = {
        "goal": _text(raw.get("goal") or description, FIELD_CAPS["goal"]),
        "symptom": _text(raw.get("symptom") or raw.get("error"), FIELD_CAPS["symptom"]),
        "expected": _text(raw.get("expected"), FIELD_CAPS["expected"]),
        "actual": _text(raw.get("actual"), FIELD_CAPS["actual"]),
        "check_command": _text(raw.get("check_command") or raw.get("command"), FIELD_CAPS["check_command"]),
        "check_description": _text(raw.get("check_description"), FIELD_CAPS["check_description"]),
        "tried": _tried(raw.get("tried")),
        "environment": _text(raw.get("environment"), FIELD_CAPS["environment"]),
        "files": _files(raw.get("files")),
        "diff": _text(raw.get("diff"), FIELD_CAPS["diff"]),
        "fingerprint": _text(raw.get("fingerprint"), FIELD_CAPS["fingerprint"]),
        "model": _text(raw.get("model"), FIELD_CAPS["model"]),
        "project": _text(raw.get("project"), FIELD_CAPS["project"]),
        "others_tried": _text(raw.get("others_tried"), FIELD_CAPS["others_tried"]),
    }
    if case["fingerprint"]:
        case["fingerprint"] = case["fingerprint"].lower()
    # Evidence the hook may add (docs/struggle-signals.md); only used by the gate.
    try:
        case["fail_count"] = max(0, int(raw.get("fail_count") or 0))
    except (TypeError, ValueError):
        case["fail_count"] = 0
    skip = raw.get("skip_posts")
    case["skip_posts"] = [int(p) for p in skip if isinstance(p, int) or (isinstance(p, str) and p.isdigit())] \
        if isinstance(skip, list) else []
    if not case["symptom"] and not case["goal"]:
        raise CaseFileError("The case file needs at least a `symptom` (or `error`) or a `goal` (or `description`).")
    if not case["symptom"]:
        case["symptom"] = case["goal"]
    _fit(case)
    return case


def _size(case: dict[str, Any]) -> int:
    return len(json.dumps(case, ensure_ascii=False))


def _fit(case: dict[str, Any]) -> None:
    """Trim the biggest optional parts until the case file is under MAX_TOTAL_CHARS."""
    if _size(case) <= MAX_TOTAL_CHARS:
        return
    if case["diff"]:
        over = _size(case) - MAX_TOTAL_CHARS
        keep = max(0, len(case["diff"]) - over - 50)
        case["diff"] = (case["diff"][:keep] + " ...[trimmed]") if keep > 200 else None
    while _size(case) > MAX_TOTAL_CHARS and case["files"]:
        case["files"].pop()
    while _size(case) > MAX_TOTAL_CHARS and len(case["tried"]) > 3:
        case["tried"].pop(0)   # keep the most recent attempts
    if _size(case) > MAX_TOTAL_CHARS and case["symptom"]:
        over = _size(case) - MAX_TOTAL_CHARS
        case["symptom"] = case["symptom"][: max(500, len(case["symptom"]) - over - 50)] + " ...[trimmed]"


def from_open_question(post: dict[str, Any]) -> dict[str, Any]:
    """A case file for a cold-case re-run, built from a stored Open Question post (agent_db row).

    The post's description already holds the summary, the error and everything that was tried.
    The owner's project id is deliberately left out.
    """
    description = post.get("description") or ""
    first = next((ln.strip() for ln in description.splitlines() if ln.strip()), "Open Question")
    case = normalize({
        "goal": f"Solve Open Question #{post.get('id')}: {first}",
        "symptom": description,
        "check_command": post.get("command"),
        "fingerprint": post.get("fingerprint"),
        "environment": ", ".join(post.get("tags") or []) or None,
        "others_tried": "Everything in the symptom text was already tried by earlier agents and teams.",
    })
    case["open_question_id"] = post.get("id")
    return case


def render(case: dict[str, Any]) -> str:
    """The case file as the text every researcher and the chair reads."""
    lines = ["<case_file>"]

    def add(title: str, value: str | None) -> None:
        if value:
            lines.extend([f"## {title}", value, ""])

    add("Goal (what the agent was trying to do)", case.get("goal"))
    add("Symptom (error, wrong output, failing test or the user's description)", case.get("symptom"))
    add("Expected", case.get("expected"))
    add("Actual", case.get("actual"))
    if case.get("check_command"):
        add("Check command (passes once it's fixed)", case["check_command"])
    add("How we'll know it's fixed", case.get("check_description"))
    add("Environment", case.get("environment"))
    if case.get("tried"):
        lines.append("## Already tried")
        for i, t in enumerate(case["tried"], 1):
            lines.append(f"{i}. {t['attempt']}" + (f" -> {t['result']}" if t.get("result") else ""))
        lines.append("")
    add("Tried by others before (from the forum)", case.get("others_tried"))
    for f in case.get("files") or []:
        add(f"File excerpt: {f['path']}", f"```\n{f['excerpt']}\n```")
    if case.get("diff"):
        add("Recent changes (git diff)", f"```diff\n{case['diff']}\n```")
    lines.append("</case_file>")
    return "\n".join(lines)


def tech_text(case: dict[str, Any]) -> str:
    """Lowercased text used to guess which technologies are involved (for docs domains)."""
    parts = [case.get("environment"), case.get("symptom"), case.get("goal"), case.get("check_command")]
    parts += [f["path"] + " " + f["excerpt"][:500] for f in case.get("files") or []]
    return " ".join(p for p in parts if p).lower()
