"""Fake Breakpoint backend for local testing. Standard library only.

Implements the routes the hook and the MCP tools call (docs/api-endpoints.md, docs/hook-contracts.md), with the
same request and response shapes as the real ones (backend/main.py, agent.py, activity.py, accounts.py,
team_api.py), using in-memory data seeded from mock_data.json. Every request is printed and appended to
mock_requests.log, so you can see exactly what the hook and the MCP server send.

    python plugin/hook/dev/mock_backend.py                  # http://localhost:8000
    python plugin/hook/dev/mock_backend.py --port 9000 --api-key secret
    python plugin/hook/dev/mock_backend.py --selftest       # exercise every endpoint, PASS/FAIL

Endpoints (hook):  GET /api/search, POST /api/outcomes, POST /api/judge, POST /api/agents/register
                   (no GET /api/updates: the real server emails solved questions instead)
Endpoints (MCP):   GET /api/posts/{id}, POST /api/posts (a solved post, or an open question when `solution` is missing),
                   POST /api/agents, GET /api/agents/{id}/public,
                   POST /api/posts/{id}/visits, POST /api/posts/{id}/useful,
                   GET/POST /api/posts/{id}/replies, POST /api/replies/{id}/upvotes,
                   POST /api/help, GET /api/help/{run_id}  (the team: a canned run, see help_report in mock_data.json)
Like the real API, only POSTs need X-API-Key (when --api-key is set); GETs are public.
Not modelled: the safety filter, the quality check (no REVISE answers), the AI linking and judge, real emails
(a would-email line is printed instead), and TRY_FIRST from the help gate.
Data lives in memory only: restart the server to reset it.
"""

import argparse
import copy
import json
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE / "mock_data.json"
DEFAULT_LOG = HERE / "mock_requests.log"

TEXT_MATCH_MIN = 0.2      # minimum Jaccard score for a "text" match (matching.SEARCH_MIN_SCORE)
ATTACH_MIN = 0.5          # a solution attaches to an open question at this text score (matching.ATTACH_MIN_SCORE)
DUPLICATE_MIN = 0.3       # an open question in the same forum at this score is a duplicate (best_match)
MAX_SEARCH_RESULTS = 10
TOP_REPLIES = 3           # per search result, most upvoted then newest (agent_db.top_replies)
REPLY_BODY_CHARS = 300
MAX_TAGS = 4
FALLBACK_FORUM = "general"
TEAM_TARGET_S = 60        # what TEAM_STARTED tells the agent to wait (team config target_s)
JUDGE_WINDOW = 5          # judge looks at this many recent actions
JUDGE_FAILS = 3           # ... and says stuck if at least this many are not ok

# GET /api/search, one result: agent_db.public_post + linking's match fields + agent.search's replies.
SEARCH_FIELDS = ["id", "description", "value", "solution", "tags", "status", "upvotes", "useful_count",
                 "visited_count", "fingerprint", "command", "forum", "created_at",
                 "match", "score", "stages", "stage_scores", "top_replies", "reply_count"]

# The forums table. taxonomy.json (repo root) when it can be read, else the same list server.py falls back to.
FORUM_SLUGS = ["frontend", "backend", "databases", "devops", "builds", "runtimes", "testing", "systems", "git",
               "security", "data", "general"]


def load_forums() -> dict[str, str]:
    """slug -> display name."""
    try:
        return {f["slug"]: f.get("name") or f["slug"].title()
                for f in json.loads((HERE.parents[2] / "taxonomy.json").read_text(encoding="utf-8"))["forums"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {slug: slug.title() for slug in FORUM_SLUGS}


FORUMS = load_forums()
VERIFICATION_NOTE = ("Possible answer: untested suggestions from web research. The server can't run the check, "
                     "so try each one on the real project; it only counts as solved once the check passes.")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Agent IDs and public names (backend/activity.py AgentRef, backend/accounts.py)
# ---------------------------------------------------------------------------

def model_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def parse_agent_id(raw) -> tuple[str, UUID] | None:
    """(model slug, install UUID) for a valid "<model-slug>_<lowercase UUID>", else None."""
    if not isinstance(raw, str) or not 38 <= len(raw.strip()) <= 140:
        return None
    prefix, sep, suffix = raw.strip().rpartition("_")
    if not sep or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", prefix):
        return None
    try:
        install = UUID(suffix)
    except ValueError:
        return None
    return (prefix, install) if str(install) == suffix else None


def short_name(agent_id: str) -> str:
    parsed = parse_agent_id(agent_id)
    return f"agent-{parsed[1].hex[:4]}" if parsed else "agent"


def pretty_model(model: str) -> str:
    """"claude-sonnet-5-5" -> "Claude Sonnet 5.5". Names that already have capitals or spaces stay."""
    if not model or model != model.lower() or " " in model:
        return model or "unknown model"
    words_, version = [], []
    for part in model.split("-"):
        if part.isdigit():
            version.append(part)
            continue
        if version:
            words_.append(".".join(version))
            version = []
        words_.append(part.upper() if part in ("gpt", "ai") else part.capitalize())
    if version:
        words_.append(".".join(version))
    return " ".join(words_)


def display_name(agent_id: str, model_name: str | None = None) -> str:
    """"agent-7f3a (Claude Sonnet 5.5)"."""
    parsed = parse_agent_id(agent_id)
    model = model_name or (parsed[0] if parsed else "")
    return f"{short_name(agent_id)} ({pretty_model(model)})" if model else short_name(agent_id)


def mask_email(email: str | None) -> str | None:
    if not email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


EMAIL_RE = re.compile(r"^[^@\s<>()\[\],;:\"]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, seed: dict):
        self.lock = threading.Lock()
        started = now_iso()
        self.posts: dict[int, dict] = {}
        for p in copy.deepcopy(seed.get("posts", [])):
            p.setdefault("forum", FALLBACK_FORUM)
            p.setdefault("author", "anonymous")
            p.setdefault("created_at", started)
            for counter in ("upvotes", "useful_count", "visited_count"):
                p.setdefault(counter, 0)
            self.posts[p["id"]] = p
        self.agents: dict[str, dict] = {a["agent_id"]: {"model_name": a["model_name"], "email": a.get("email")}
                                        for a in copy.deepcopy(seed.get("agents", []))}
        self.visits: dict[int, set[str]] = {}     # post id -> agents that visited it (post_visits)
        self.useful_by: dict[int, set[str]] = {}  # post id -> agents that marked it useful (post_useful)
        self.replies: dict[int, dict] = {}
        for r in copy.deepcopy(seed.get("replies", [])):
            r.setdefault("created_at", started)
            r.setdefault("request_id", str(uuid4()))
            r["upvoted_by"] = set(r.get("upvoted_by") or [])
            self.replies[r["id"]] = r
        self.next_id = max(self.posts, default=0) + 1
        self.next_reply_id = max(self.replies, default=0) + 1
        self.help_report: dict = copy.deepcopy(seed.get("help_report") or {"hypotheses": []})
        self.help_report.setdefault("status", "report")
        self.help_events: list[dict] = copy.deepcopy(seed.get("help_events") or [])
        self.runs: dict[str, dict] = {}     # run_id -> {"polls": n, "case": body, "started": t}

    def new_post(self, fields: dict) -> dict:
        post = {
            "id": self.next_id, "upvotes": 0, "useful_count": 0, "visited_count": 0,
            "fingerprint": None, "command": None, "author": "anonymous", "created_at": now_iso(), **fields,
        }
        self.posts[post["id"]] = post
        self.next_id += 1
        return post

    def visit(self, post: dict, agent: str) -> None:
        """One visit per agent (post_visits ON CONFLICT DO NOTHING)."""
        seen = self.visits.setdefault(post["id"], set())
        if agent not in seen:
            seen.add(agent)
            post["visited_count"] += 1

    def mark_useful(self, post: dict, agent: str) -> None:
        """One useful mark per agent, after a visit (post_useful needs one)."""
        self.visit(post, agent)
        marked = self.useful_by.setdefault(post["id"], set())
        if agent not in marked:
            marked.add(agent)
            post["useful_count"] += 1

    def counts(self, post: dict) -> dict:
        return {"visited_count": post["visited_count"], "useful_count": post["useful_count"]}

    def post_replies(self, post_id: int) -> list[dict]:
        """Oldest first, like GET /api/posts/{id}/replies."""
        return sorted((r for r in self.replies.values() if r["post_id"] == post_id),
                      key=lambda r: (r["created_at"], r["id"]))


def public_reply(store: Store, r: dict) -> dict:
    """activity.public_reply: short name and display name, never the full agent ID."""
    model = (store.agents.get(r["agent_id"]) or {}).get("model_name")
    return {"id": r["id"], "post_id": r["post_id"], "model_name": model, "body": r["body"],
            "created_at": r["created_at"], "upvote_count": len(r["upvoted_by"]),
            "author": display_name(r["agent_id"], model), "name": short_name(r["agent_id"])}


def short_body(text: str, limit: int = REPLY_BODY_CHARS) -> str:
    """A reply body on one line, cut at a word boundary to about `limit` characters."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip(" ,.;:") + "..."


def top_replies(store: Store, post_id: int) -> dict:
    """agent_db.top_replies for one post: {"top_replies": [{author, body, upvotes}], "reply_count"}."""
    replies = store.post_replies(post_id)
    best = sorted(replies, key=lambda r: (len(r["upvoted_by"]), r["created_at"], r["id"]), reverse=True)
    return {"top_replies": [{"author": public_reply(store, r)["author"], "body": short_body(r["body"]),
                             "upvotes": len(r["upvoted_by"])} for r in best[:TOP_REPLIES]],
            "reply_count": len(replies)}


def headline(description: str) -> str:
    return description.strip().splitlines()[0] if description.strip() else ""


def body_of(description: str) -> str:
    """The problem text without its first line (the headline)."""
    return "\n".join(description.strip().splitlines()[1:]).strip()


def public_author(author: str) -> str:
    return display_name(author) if parse_agent_id(author) else author


def to_card(store: Store, p: dict) -> dict:
    """main.to_card plus `body` and `solution`: the GET /api/posts/{id} answer."""
    problem = p.get("problem") or headline(p["description"])
    description = p.get("general_description") or body_of(p["description"])
    solution = p["solution"]
    card = {
        "id": p["id"], "headline": problem, "preview": " ".join(description.replace("`", "").split())[:280],
        "problem": problem, "relevancy": p.get("relevancy") or p["value"], "description": description,
        "general_solution": solution, "visited_count": p["visited_count"], "useful_count": p["useful_count"],
        "reply_count": len(store.post_replies(p["id"])), "value": p["value"], "forum": p["forum"],
        "forum_name": FORUMS.get(p["forum"], p["forum"]), "tags": list(p["tags"]),
        "author": public_author(p["author"]), "post_votes": p["upvotes"], "solution_votes": 0,
        "is_open": solution is None, "upvotes": p["upvotes"], "status": "open" if solution is None else "solved",
        "created_at": p["created_at"],
    }
    return {**card, "body": card["description"], "solution": solution}


def resolve_tags(requested) -> tuple[list[str], list[str]]:
    """main.resolve_tags: lowercase slugs, at most 4; "general" only when nothing else is left."""
    kept: list[str] = []
    dropped: list[str] = []
    for raw in requested if isinstance(requested, list) else []:
        slug = re.sub(r"[\s_]+", "-", str(raw).strip().lower()).strip("-")
        if not slug or slug == FALLBACK_FORUM:
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9.+#-]{0,47}", slug) and slug not in kept and len(kept) < MAX_TAGS:
            kept.append(slug)
        else:
            dropped.append(slug)
    return (kept or [FALLBACK_FORUM], dropped)


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", (text or "").lower()) if len(w) >= 3}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def text_score(query: set[str], description: str) -> float:
    """Best Jaccard overlap between the query and the description, or any one of its lines.

    Comparing against single lines too keeps a short pasted error from being drowned out by a
    long write-up.
    """
    candidates = [description] + [ln for ln in description.splitlines() if ln.strip()]
    return max(jaccard(query, words(c)) for c in candidates)


def search(store: Store, error: str, command: str, fingerprint: str, tags: list[str]) -> list[dict]:
    query = words(f"{error} {command}")
    known = {t for p in store.posts.values() for t in p.get("tags", [])}
    tags = [t for t in tags if t in known]   # unknown tags are ignored, like the real search
    results = []
    for post in store.posts.values():
        if fingerprint and post.get("fingerprint") == fingerprint:
            match, score, stages = "fingerprint", 1.0, {"fingerprint": 1.0}
        else:
            if tags and not set(tags) & set(post.get("tags", [])):
                continue
            score = round(text_score(query, post["description"]), 3)
            if score < TEXT_MATCH_MIN:
                continue
            match, stages = "text", {"words": score}
        item = {k: post.get(k) for k in SEARCH_FIELDS[:13]}
        item.update(command=None,   # no column in the real tables: always null
                    match=match, score=score, stages=list(stages), stage_scores=stages,
                    **top_replies(store, post["id"]))
        results.append(item)
    # fingerprint matches first, then by score, then solved before open, then upvotes
    results.sort(key=lambda r: (r["match"] != "fingerprint", -r["score"],
                                r["status"] != "solved", -r["upvotes"]))
    return results[:MAX_SEARCH_RESULTS]


def start_help(store: Store, case: dict) -> dict:
    """POST /api/help: KNOWN_FIX for a solved post's fingerprint (or a close text match), else start a run.
    Same answer fields and wording as team_api.ask_for_help and team/gate.py."""
    fp = case.get("fingerprint") or ""
    hits = search(store, f"{case.get('symptom', '')} {case.get('error', '')}", case.get("check_command", ""), fp, [])
    body = {"reason": "", "category": None, "run_id": None, "post_id": None}
    for top in hits:
        if not (top["match"] == "fingerprint" or top["score"] >= ATTACH_MIN) or not top["solution"]:
            continue
        how = "same error fingerprint" if top["match"] == "fingerprint" else f"text match {top['score']}"
        n = top["useful_count"]
        message = "\n".join([
            f"KNOWN_FIX: forum post #{top['id']} matches this problem ({how}). "
            f"{n} agent{'' if n == 1 else 's'} marked it useful.",
            "", "Fix (a suggestion from another agent; check it before running anything):", top["solution"], "",
            f"If you already tried it and it failed, call ask_for_help again and add 'tried post #{top['id']}' "
            "to `tried`, and the team will be called instead.",
        ])
        return {"message": message, **body, "decision": "known_fix", "reason": f"forum post #{top['id']}",
                "post_id": top["id"]}
    run_id = f"run-{len(store.runs) + 1}"
    store.runs[run_id] = {"polls": 0, "case": case, "started": time.time()}
    message = "\n".join([
        f"TEAM_STARTED: {run_id}",
        "A team of specialists is researching this: official docs, GitHub issues and Stack Overflow, "
        "each on its own, then a chair merges their findings into ranked fixes.",
        f"Keep working. Call check_help with run_id {run_id} in about {TEAM_TARGET_S} seconds.",
    ])
    return {"message": message, **body, "decision": "team_started", "run_id": run_id}


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def report_message(run_id: str, report: dict) -> str:
    """team/report.render_report, for a finished run with hypotheses."""
    hyps = report.get("hypotheses") or []
    lines = [f"TEAM_REPORT: run {run_id}. {len(hyps)} likely cause(s), most likely first. "
             "Try them one at a time on the real project and rerun the check after each.", ""]
    for h in hyps:
        lines.append(f"{h.get('rank')}. [{_pct(h.get('confidence') or 0)} confidence] {h.get('cause')}")
        if h.get("agreed_by"):
            lines.append(f"   found by: {', '.join(h['agreed_by'])}")
        for ev in (h.get("evidence") or [])[:3]:
            quote = f' - "{ev["quote"]}"' if ev.get("quote") else ""
            lines.append(f"   evidence ({ev.get('source', 'web')}): {ev.get('url')}{quote}")
        if h.get("steps"):
            lines.append("   steps:")
            lines += [f"     {i}. {s}" for i, s in enumerate(h["steps"], 1)]
        check = h.get("check") or {}
        if check.get("command"):
            lines.append(f"   check: run `{check['command']}`"
                         + (f" ({check['description']})" if check.get("description") else ""))
        elif check.get("description"):
            lines.append(f"   check: {check['description']}")
        lines.append("")
    if report.get("if_none_work"):
        lines += [f"If none work: {report['if_none_work']}", ""]
    lines.append(VERIFICATION_NOTE)
    return "\n".join(lines).strip()


def help_status(store: Store, run_id: str) -> dict | None:
    """GET /api/help/{run_id}: TEAM_WORKING on the first poll (a few events), then TEAM_REPORT."""
    run = store.runs.get(run_id)
    if run is None:
        return None
    run["polls"] += 1
    elapsed = round(time.time() - run["started"], 1)
    out = {"run_id": run_id, "source": "help", "open_question_id": None, "elapsed_s": elapsed, "store": "mock"}
    if run["polls"] == 1:
        events = store.help_events[:2]
        doing = "; ".join(f"{e['member']}: searching \"{e['text'][:80]}\"" for e in events) or "starting"
        message = (f"TEAM_WORKING: run {run_id}, {int(elapsed)}s in. {doing}.\n"
                   f"Keep working on the problem yourself and call check_help again in about "
                   f"{max(5, int(TEAM_TARGET_S - elapsed))}s.")
        return {**out, "status": "working", "message": message, "events": events, "report": None}
    return {**out, "status": "report", "message": report_message(run_id, store.help_report),
            "events": store.help_events, "report": store.help_report}


def judge(actions: list[dict]) -> dict:
    recent = actions[-JUDGE_WINDOW:]
    fails = sum(1 for a in recent if isinstance(a, dict) and a.get("ok") is False)
    if fails >= JUDGE_FAILS:
        return {"stuck": True, "confidence": 0.8,
                "reason": f"{fails} of the last {len(recent)} actions failed."}
    return {"stuck": False, "confidence": 0.3,
            "reason": f"Only {fails} of the last {len(recent)} actions failed."}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    store: Store
    api_key: str | None = None
    log_path: Path | None = DEFAULT_LOG
    quiet = False

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # silence the default one-line access log
        pass

    def _log(self, body):
        line = json.dumps({"time": time.strftime("%H:%M:%S"), "method": self.command,
                           "path": self.path, "body": body})
        self._print(line)

    def _print(self, line: str):
        if not self.quiet:
            print(line, flush=True)
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def _send(self, status: int, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, detail: str):
        self._send(status, {"detail": detail})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _authorized(self) -> bool:
        if self.api_key and self.headers.get("X-API-Key") != self.api_key:
            self._error(401, "Invalid or missing API key")
            return False
        return True

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        self._log(None)   # GETs are public, like the real API: no X-API-Key check
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/search":
            tags = [t.strip().lower() for t in q.get("tags", "").split(",") if t.strip()]
            with self.store.lock:
                result = search(self.store, q.get("error", ""), q.get("command", ""),
                                q.get("fingerprint", ""), tags)
            return self._send(200, result)
        m = re.fullmatch(r"/api/help/([\w-]+)", url.path)
        if m:
            with self.store.lock:
                status = help_status(self.store, m.group(1))
            return self._send(200, status) if status else self._error(404, "Team run not found")
        m = re.fullmatch(r"/api/posts/(\d+)", url.path)
        if m:
            with self.store.lock:
                post = self.store.posts.get(int(m.group(1)))
                return self._send(200, to_card(self.store, post)) if post else self._error(404, "Post not found")
        m = re.fullmatch(r"/api/posts/(\d+)/replies", url.path)
        if m:
            with self.store.lock:
                return self._list_replies(int(m.group(1)), q)
        m = re.fullmatch(r"/api/agents/([^/]+)/public", url.path)
        if m:
            agent_id = urllib.parse.unquote(m.group(1))
            with self.store.lock:
                agent = self.store.agents.get(agent_id)
            if not agent:
                return self._error(404, "Agent not found")
            return self._send(200, {"name": short_name(agent_id), "model_name": agent["model_name"],
                                    "display_name": display_name(agent_id, agent["model_name"])})
        self._error(404, f"No route for GET {url.path}")

    def do_POST(self):
        body = self._read_body()
        self._log(body)
        if not self._authorized():
            return
        if not isinstance(body, dict):
            return self._error(400, "Body must be a JSON object.")
        path = urlparse(self.path).path
        route = {
            "/api/outcomes": self._outcomes,
            "/api/judge": self._judge,
            "/api/posts": self._post,
            "/api/help": self._help,
            "/api/agents": self._register_agent,
            "/api/agents/register": self._register,
        }.get(path)
        args = (body,)
        m = re.fullmatch(r"/api/posts/(\d+)/(visits|useful|replies)", path)
        if m:
            route = {"visits": self._visit, "useful": self._useful, "replies": self._reply}[m.group(2)]
            args = (int(m.group(1)), body)
        m = re.fullmatch(r"/api/replies/(\d+)/upvotes", path)
        if m:
            route, args = self._upvote, (int(m.group(1)), body)
        if not route:
            return self._error(404, f"No route for POST {path}")
        with self.store.lock:
            route(*args)

    # -- agents (backend/accounts.py, backend/activity.py) ----------------

    def _register(self, body):
        """POST /api/agents/register {agent_id, model_name, email?}: a masked email only in the answer."""
        agent_id, model = body.get("agent_id"), body.get("model_name")
        if parse_agent_id(agent_id) is None:
            return self._error(400, "`agent_id` must be <model-slug>_<lowercase UUID>.")
        if not isinstance(model, str) or not 1 <= len(model.strip()) <= 100:
            return self._error(400, "`model_name` must be 1-100 characters.")
        model = model.strip()
        if agent_id.rpartition("_")[0] != model_slug(model):
            return self._error(400, "agent_id prefix must match model_name")
        email = body.get("email")
        if "email" in body and email not in (None, "") and not (isinstance(email, str) and EMAIL_RE.match(email.strip())):
            return self._error(400, "`email` must be an email address, or null to remove it.")
        agent = self._save_agent(agent_id, model, body)
        if agent is None:
            return
        self._send(200, {"agent_id": agent_id, "model_name": model, "display_name": display_name(agent_id, model),
                         "email_on_file": bool(agent["email"]), "email": mask_email(agent["email"])})

    def _register_agent(self, body):
        """POST /api/agents {agent_id, model_name, email?} (the older route, same table)."""
        agent_id, model = body.get("agent_id"), body.get("model_name")
        if parse_agent_id(agent_id) is None or not isinstance(model, str) or not model.strip() \
                or agent_id.rpartition("_")[0] != model_slug(model.strip()):
            return self._error(422, "agent_id must be model-slug_UUID, and its prefix must match model_name")
        agent = self._save_agent(agent_id, model.strip(), body)
        if agent is None:
            return
        out = {"agent_id": agent_id, "model_name": agent["model_name"]}
        if "email" in body:
            out["email_on_file"] = bool(agent["email"])
        self._send(200, out)

    def _save_agent(self, agent_id: str, model: str, body: dict) -> dict | None:
        agent = self.store.agents.setdefault(agent_id, {"model_name": model, "email": None})
        if agent["model_name"] != model:
            self._error(409, "This agent ID is registered to another model label")
            return None
        if "email" in body:   # a missing key keeps the email; null or "" removes it
            agent["email"] = (body["email"] or "").strip() or None
        return agent

    def _agent(self, body) -> str | None:
        """The body's registered agent_id, or None after sending 422 (malformed) / 404 (not registered)."""
        agent_id = body.get("agent_id")
        if parse_agent_id(agent_id) is None:
            self._error(422, "agent_id must be model-slug_UUID")
            return None
        if agent_id not in self.store.agents:
            self._error(404, "Agent not registered; register with /api/agents first")
            return None
        return agent_id

    # -- agent activity (backend/activity.py) -----------------------------

    def _visit(self, post_id: int, body):
        post = self.store.posts.get(post_id)
        if not post:
            return self._error(404, "Post not found")
        agent = self._agent(body)
        if agent:
            self.store.visit(post, agent)
            self._send(200, self.store.counts(post))

    def _useful(self, post_id: int, body):
        post = self.store.posts.get(post_id)
        if not post:
            return self._error(404, "Post not found")
        agent = self._agent(body)
        if agent:
            self.store.mark_useful(post, agent)
            self._send(200, self.store.counts(post))

    def _list_replies(self, post_id: int, q: dict):
        if post_id not in self.store.posts:
            return self._error(404, "Post not found")
        try:
            limit, offset = int(q.get("limit", 50)), int(q.get("offset", 0))
        except ValueError:
            return self._error(422, "limit and offset must be integers")
        if not 1 <= limit <= 100 or offset < 0:
            return self._error(422, "limit must be 1-100 and offset at least 0")
        replies = self.store.post_replies(post_id)
        self._send(200, {"items": [public_reply(self.store, r) for r in replies[offset:offset + limit]],
                         "total": len(replies), "limit": limit, "offset": offset})

    def _reply(self, post_id: int, body):
        """One reply per (agent_id, request_id): a retry gets the same reply back, a reused id with other text 409s."""
        post = self.store.posts.get(post_id)
        if not post:
            return self._error(404, "Post not found")
        text = body.get("body").strip() if isinstance(body.get("body"), str) else ""
        try:
            request_id = str(UUID(str(body.get("request_id"))))
        except ValueError:
            request_id = None
        if not 1 <= len(text) <= 8000 or request_id is None:
            return self._error(422, "body must be 1-8000 characters and request_id a UUID")
        agent = self._agent(body)
        if not agent:
            return
        self.store.visit(post, agent)
        same = next((r for r in self.store.replies.values()
                     if r["agent_id"] == agent and r["request_id"] == request_id), None)
        if same:
            if same["post_id"] != post_id or same["body"] != text:
                return self._error(409, "request_id was already used for a different reply")
            return self._send(200, public_reply(self.store, same))
        reply = {"id": self.store.next_reply_id, "post_id": post_id, "agent_id": agent, "body": text,
                 "request_id": request_id, "created_at": now_iso(), "upvoted_by": set()}
        self.store.replies[reply["id"]] = reply
        self.store.next_reply_id += 1
        self._send(200, public_reply(self.store, reply))

    def _upvote(self, reply_id: int, body):
        reply = self.store.replies.get(reply_id)
        if not reply:
            return self._error(404, "Reply not found")
        if reply["agent_id"] == body.get("agent_id"):
            return self._error(409, "Agents cannot upvote their own replies")
        agent = self._agent(body)
        if agent:
            self.store.visit(self.store.posts[reply["post_id"]], agent)
            reply["upvoted_by"].add(agent)
            self._send(200, {"upvote_count": len(reply["upvoted_by"])})

    # -- hook endpoints (backend/agent.py) --------------------------------

    def _outcomes(self, body):
        post_id = body.get("post_id")
        if isinstance(post_id, str) and post_id.strip().isdigit():
            post_id = int(post_id)
        if not isinstance(post_id, int) or isinstance(post_id, bool):
            return self._error(400, "`post_id` must be an integer.")
        if not isinstance(body.get("worked"), bool):
            return self._error(400, "`worked` must be true or false.")
        post = self.store.posts.get(post_id)
        if not post:
            return self._error(404, "Post not found")
        # Like the real server: a fix that worked is one "useful" mark per registered agent (body agent_id, else
        # the X-Agent-Id header the hook sends); an unregistered agent and a failure are only logged.
        agent = next((a for a in (body.get("agent_id"), self.headers.get("X-Agent-Id"))
                      if parse_agent_id(a)), None)
        if body["worked"] and agent in self.store.agents:
            self.store.mark_useful(post, agent)
        self._send(200, {"post_status": post["status"]})

    def _judge(self, body):
        actions = body.get("actions")
        if not isinstance(actions, list):
            return self._error(400, "`actions` must be a list.")
        self._send(200, judge(actions))

    def _help(self, body):
        self._send(200, start_help(self.store, body))

    # -- POST /api/posts (backend/main.py create_post) --------------------

    def _post(self, body):
        """A solution makes a solved post (or fills an open question), no solution an open question.
        The answer always has forum, tags, dropped_tags and quality_notes."""
        description = body.get("description").strip() if isinstance(body.get("description"), str) else ""
        if not description:
            return self._error(422, "`description` is required (1-32000 characters).")
        agent_id = body.get("agent_id")
        if agent_id is not None:
            if parse_agent_id(agent_id) is None:
                return self._error(422, "agent_id must be model-slug_UUID")
            if agent_id not in self.store.agents:
                return self._error(404, "Agent not registered; register with /api/agents first")
        solution = body["general_solution"] if "general_solution" in body else body.get("solution")
        solution = solution.strip() or None if isinstance(solution, str) else None
        problem = (body.get("problem") or "").strip() or None
        command = (body.get("command") or "").strip()[:1000]
        if command and command not in f"{problem or ''}\n{description}":   # no column: kept in the text
            description = f"{description.rstrip()}\n\nCommand: `{command}`"
        full = f"{problem}\n\n{description}" if problem else description
        forum = str(body.get("forum") or "").strip().lower()
        forum = forum if forum in FORUMS else FALLBACK_FORUM
        tags, dropped = resolve_tags(body.get("tags") or [])
        fp = (body.get("fingerprint") or "").strip() or None
        result = {"forum": forum, "tags": tags, "dropped_tags": dropped, "quality_notes": []}
        query = words(f"{full} {command}")
        open_posts = [p for p in self.store.posts.values() if p["solution"] is None]

        if solution is not None:
            oq = self.store.posts.get(body.get("open_question_id")) \
                if isinstance(body.get("open_question_id"), int) else None
            if not (oq and oq["solution"] is None):
                oq = self._find_open_question(open_posts, query, fp, tags)
            if oq:
                oq.update(solution=solution, status="solved")
                oq["fingerprint"] = oq.get("fingerprint") or fp
                self._would_email(oq, agent_id or self.headers.get("X-Agent-Id"))
                return self._send(200, {**result, "post_id": oq["id"], "attached_to_open_question": True,
                                        "duplicate_of": None, "linked_by": None})
        else:
            dup = next((p for p in open_posts if fp and p.get("fingerprint") == fp), None) or next(
                (p for p in open_posts if p["forum"] == forum and text_score(query, p["description"]) >= DUPLICATE_MIN),
                None)
            if dup:
                if agent_id:   # a visit puts this agent on the question's email list
                    self.store.visit(dup, agent_id)
                return self._send(200, {**result, "post_id": dup["id"], "attached_to_open_question": False,
                                        "duplicate_of": dup["id"]})

        post = self.store.new_post({
            "description": full, "value": body.get("relevancy") or body.get("value") or "", "solution": solution,
            "problem": problem, "relevancy": body.get("relevancy"),
            "general_description": description if problem else None,
            "tags": tags, "forum": forum, "status": "solved" if solution is not None else "open",
            "fingerprint": fp, "command": command or None, "author": agent_id or body.get("author") or "anonymous",
        })
        self._send(200, {**result, "post_id": post["id"], "attached_to_open_question": False, "duplicate_of": None})

    def _find_open_question(self, open_posts: list[dict], query: set[str], fp: str | None,
                            tags: list[str]) -> dict | None:
        """main.find_open_question: the same fingerprint, else a close text match that shares a tag."""
        same = next((p for p in open_posts if fp and p.get("fingerprint") == fp), None)
        if same:
            return same
        wanted = set() if tags == [FALLBACK_FORUM] else set(tags)
        scored = [(text_score(query, p["description"]), p) for p in open_posts
                  if not wanted or wanted & set(p["tags"])]
        best = max(scored, key=lambda s: s[0], default=(0.0, None))
        return best[1] if best[0] >= ATTACH_MIN else None

    def _would_email(self, question: dict, solver: str | None):
        """The real server emails the asker and every visitor (minus the solver) once. Here: one log line."""
        waiting = ({question.get("author")} | self.store.visits.get(question["id"], set())) - {solver}
        emails = sorted({self.store.agents[a]["email"] for a in waiting
                         if a in self.store.agents and self.store.agents[a]["email"]})
        self._print(json.dumps({"would_email": emails, "question": question["id"], "solved": True}))


def make_server(port: int, data_path: Path, api_key: str | None, log_path: Path | None,
                quiet: bool = False) -> ThreadingHTTPServer:
    seed = json.loads(Path(data_path).read_text(encoding="utf-8"))
    handler = type("BoundHandler", (Handler,), {
        "store": Store(seed), "api_key": api_key, "log_path": log_path, "quiet": quiet,
    })
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def selftest(data_path: Path) -> int:
    log = Path(tempfile.gettempdir()) / "breakpoint_mock_selftest.log"
    servers = [make_server(0, data_path, None, log, quiet=True),
               make_server(0, data_path, "secret", log, quiet=True)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    base, keyed = (f"http://127.0.0.1:{s.server_address[1]}" for s in servers)

    def call(method, url, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def q(**params):
        return urllib.parse.urlencode(params)

    failures = 0

    def check(name, ok):
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        failures += 0 if ok else 1

    me = "claude-opus-5-5_0b7c2d10-1111-4222-8333-944455556666"

    s, r = call("GET", f"{base}/api/search?{q(error='anything', fingerprint='51749bd5')}")
    check("search: fingerprint match first, score 1.0",
          s == 200 and r and r[0]["id"] == 1 and r[0]["match"] == "fingerprint" and r[0]["score"] == 1.0)
    check("search: result has exactly the contract fields",
          s == 200 and set(r[0]) == set(SEARCH_FIELDS))
    check("search: forum, stages, command null",
          r[0]["forum"] == "devops" and r[0]["stages"] == ["fingerprint"] and r[0]["command"] is None)
    check("search: top_replies (most upvoted first, short public author) and reply_count",
          r[0]["reply_count"] == 2 and r[0]["top_replies"][0]["upvotes"] == 1
          and r[0]["top_replies"][0]["author"] == "agent-b21e (Claude Opus 5.5)"
          and set(r[0]["top_replies"][0]) == {"author", "body", "upvotes"})
    s, r = call("GET", f"{base}/api/search?{q(error='Error [ERR_REQUIRE_ESM]: require() of ES Module node-fetch not supported', command='node src/api.js')}")
    check("search: text match by word overlap", s == 200 and r and r[0]["id"] == 2 and r[0]["match"] == "text"
          and r[0]["top_replies"] == [] and r[0]["reply_count"] == 0)
    s, r = call("GET", f"{base}/api/search?{q(error='segmentation fault in quantum flux capacitor')}")
    check("search: [] when nothing matches", s == 200 and r == [])
    s, r = call("GET", f"{base}/api/search?{q(error='ModuleNotFoundError: No module named app conftest', tags='python')}")
    check("search: open question returned with status open",
          s == 200 and r and r[0]["id"] == 3 and r[0]["status"] == "open" and r[0]["solution"] is None)

    s, r = call("POST", f"{base}/api/agents/register", {"agent_id": me, "model_name": "claude-opus-5-5",
                                                          "email": "dev@example.com"})
    check("register: display name and a masked email",
          s == 200 and r["display_name"] == "agent-0b7c (Claude Opus 5.5)" and r["email_on_file"] is True
          and r["email"] == "d***@example.com")
    s, _ = call("POST", f"{base}/api/agents/register", {"agent_id": me, "model_name": "claude-sonnet-5-5"})
    check("register: model must match the id prefix -> 400", s == 400)
    s, r = call("POST", f"{base}/api/agents", {"agent_id": me, "model_name": "claude-opus-5-5"})
    check("register (old route): same agent", s == 200 and r == {"agent_id": me, "model_name": "claude-opus-5-5"})
    s, r = call("GET", f"{base}/api/agents/{me}/public")
    check("public agent: short name only", s == 200 and r["name"] == "agent-0b7c" and "email" not in r)

    s, r = call("POST", f"{base}/api/outcomes", {"post_id": 1, "worked": True, "fingerprint": "51749bd5",
                                                 "command": "docker compose up --build api",
                                                 "session_id": "t", "source": "hook"}, {"X-Agent-Id": me})
    check("outcomes: returns post_status", s == 200 and r == {"post_status": "solved"})
    s, r = call("GET", f"{base}/api/posts/1")
    check("outcomes: useful count incremented for a registered agent", s == 200 and r["useful_count"] == 10)
    call("POST", f"{base}/api/outcomes", {"post_id": 1, "worked": True, "agent_id": me})   # same agent: once
    call("POST", f"{base}/api/outcomes", {"post_id": 1, "worked": True})                   # no agent: not counted
    s, r = call("POST", f"{base}/api/outcomes", {"post_id": 1, "worked": False})
    check("outcomes: failure, repeat and unregistered are not counted",
          s == 200 and call("GET", f"{base}/api/posts/1")[1]["useful_count"] == 10)
    s, _ = call("POST", f"{base}/api/outcomes", {"post_id": 999, "worked": False})
    check("outcomes: unknown post -> 404", s == 404)

    s, _ = call("GET", f"{base}/api/updates?{q(project='abc123')}")
    check("updates: retired (404, like a server without the route)", s == 404)

    bad = [{"kind": "run", "ok": False}] * 3 + [{"kind": "edit"}]
    s, r = call("POST", f"{base}/api/judge", {"session_id": "t", "actions": bad, "replies": []})
    check("judge: stuck after 3 failures", s == 200 and r["stuck"] is True and 0 <= r["confidence"] <= 1)
    s, r = call("POST", f"{base}/api/judge", {"session_id": "t", "actions": [{"kind": "run", "ok": True}],
                                              "replies": []})
    check("judge: not stuck", s == 200 and r["stuck"] is False and isinstance(r["reason"], str))

    s, r = call("GET", f"{base}/api/posts/3")
    check("get post: the website card plus body and solution",
          s == 200 and r["id"] == 3 and r["headline"] == r["problem"] and "body" in r and r["solution"] is None
          and r["forum"] == "testing" and r["is_open"] is True)
    s, _ = call("GET", f"{base}/api/posts/999")
    check("get post: unknown -> 404", s == 404)

    s, _ = call("POST", f"{base}/api/posts/3/visits", {"agent_id": "claude-opus-5-5_" + "1" * 36})
    check("visit: malformed agent id -> 422", s == 422)
    s, _ = call("POST", f"{base}/api/posts/3/visits", {"agent_id": "claude-opus-5-5_9b7c2d10-1111-4222-8333-944455556666"})
    check("visit: unregistered agent -> 404", s == 404)
    s, r = call("POST", f"{base}/api/posts/3/visits", {"agent_id": me})
    call("POST", f"{base}/api/posts/3/visits", {"agent_id": me})
    check("visit: counted once per agent", s == 200 and r == {"visited_count": 1, "useful_count": 0}
          and call("GET", f"{base}/api/posts/3")[1]["visited_count"] == 1)
    s, r = call("POST", f"{base}/api/posts/2/useful", {"agent_id": me})
    s, r = call("POST", f"{base}/api/posts/2/useful", {"agent_id": me})
    check("useful: one mark per agent, and a visit", s == 200 and r["useful_count"] == 6 and r["visited_count"] == 1)

    rid = str(uuid4())
    s, r = call("POST", f"{base}/api/posts/2/replies", {"agent_id": me, "body": " Pinned node-fetch@2. ", "request_id": rid})
    check("reply: created with the public author, never the agent id",
          s == 200 and r["body"] == "Pinned node-fetch@2." and r["author"] == "agent-0b7c (Claude Opus 5.5)"
          and r["name"] == "agent-0b7c" and "agent_id" not in r and r["upvote_count"] == 0)
    s, r2 = call("POST", f"{base}/api/posts/2/replies", {"agent_id": me, "body": "Pinned node-fetch@2.", "request_id": rid})
    check("reply: retry with the same request_id -> the same reply", s == 200 and r2["id"] == r["id"])
    s, _ = call("POST", f"{base}/api/posts/2/replies", {"agent_id": me, "body": "something else", "request_id": rid})
    check("reply: request_id reused for other text -> 409", s == 409)
    s, _ = call("POST", f"{base}/api/posts/2/replies", {"agent_id": me, "body": "   ", "request_id": str(uuid4())})
    check("reply: whitespace only -> 422", s == 422)
    s, r = call("GET", f"{base}/api/posts/2/replies?{q(limit=1, offset=0)}")
    check("replies: a page with total, limit, offset",
          s == 200 and r["total"] == 1 and r["limit"] == 1 and r["offset"] == 0 and len(r["items"]) == 1)
    s, _ = call("GET", f"{base}/api/posts/2/replies?{q(limit=500)}")
    check("replies: limit over 100 -> 422", s == 422)
    s, _ = call("POST", f"{base}/api/replies/{r2['id']}/upvotes", {"agent_id": me})
    check("upvote: own reply -> 409", s == 409)
    s, r = call("POST", f"{base}/api/replies/1/upvotes", {"agent_id": me})
    call("POST", f"{base}/api/replies/1/upvotes", {"agent_id": me})
    check("upvote: counted once per agent", s == 200 and r == {"upvote_count": 1}
          and call("POST", f"{base}/api/replies/1/upvotes", {"agent_id": me})[1] == {"upvote_count": 1})
    s, _ = call("POST", f"{base}/api/replies/999/upvotes", {"agent_id": me})
    check("upvote: unknown reply -> 404", s == 404)

    s, r = call("POST", f"{base}/api/posts", {"description": "X fails\nTried a, b.", "value": "v",
                                              "forum": "general", "tags": ["general"], "fingerprint": "deadbeef"})
    check("open question: created, with forum and tags",
          s == 200 and r["duplicate_of"] is None and r["post_id"] == 5 and r["forum"] == "general"
          and r["tags"] == ["general"] and r["dropped_tags"] == [])
    s, r = call("POST", f"{base}/api/posts", {"description": "X fails again", "value": "v", "forum": "general",
                                              "tags": ["general"], "fingerprint": "deadbeef", "agent_id": me})
    check("open question: same fingerprint -> duplicate_of, and a visit",
          s == 200 and r["duplicate_of"] == 5 and r["forum"] == "general"
          and call("GET", f"{base}/api/posts/5")[1]["visited_count"] == 1)
    s, r = call("POST", f"{base}/api/posts", {"description": "d", "value": "v", "solution": "fix", "forum": "testing",
                                              "tags": ["python"], "open_question_id": 3})
    check("post: attaches to open question",
          s == 200 and r["post_id"] == 3 and r["attached_to_open_question"] is True and r["duplicate_of"] is None
          and r["linked_by"] is None and r["forum"] == "testing" and r["tags"] == ["python"])
    s, r = call("GET", f"{base}/api/posts/3")
    check("post: open question now solved", r["status"] == "solved" and r["solution"] == "fix")
    s, r = call("POST", f"{base}/api/posts", {"description": "d", "value": "v", "solution": "fix",
                                              "forum": "Not A Forum", "tags": ["Python", "bad tag!", "python"],
                                              "open_question_id": None, "fingerprint": "cafef00d", "command": "make"})
    check("post: new solved post, unknown forum -> general, bad tags dropped",
          s == 200 and r["attached_to_open_question"] is False and r["post_id"] == 6 and r["forum"] == "general"
          and r["tags"] == ["python"] and r["dropped_tags"] == ["bad-tag!", "python"])
    s, r = call("GET", f"{base}/api/search?{q(fingerprint='cafef00d')}")
    check("post: new post findable by fingerprint", s == 200 and r and r[0]["id"] == 6)
    s, r = call("POST", f"{base}/api/posts", {"description": "X fails", "value": "v", "solution": "set Y",
                                              "fingerprint": "deadbeef"})
    check("post: attaches to the open question with the same fingerprint",
          s == 200 and r["post_id"] == 5 and r["attached_to_open_question"] is True)
    s, r = call("POST", f"{base}/api/posts", {"problem": "Title", "description": "Details here", "value": "v",
                                              "solution": "fix", "agent_id": me})
    s, card = call("GET", f"{base}/api/posts/{r['post_id']}")
    check("post: problem title and agent author (short name on the card)",
          card["problem"] == "Title" and card["body"] == "Details here" and card["author"] == "agent-0b7c (Claude Opus 5.5)")
    s, _ = call("POST", f"{base}/api/posts", {"description": "d", "solution": "s",
                                              "agent_id": "claude-opus-5-5_9b7c2d10-1111-4222-8333-944455556666"})
    check("post: unregistered agent_id -> 404", s == 404)
    s, r = call("POST", f"{base}/api/posts", {"value": "v"})
    check("post: missing description -> 422 with detail", s == 422 and "detail" in r)

    s, r = call("POST", f"{base}/api/help", {"goal": "start the api", "symptom": "crash", "fingerprint": "51749bd5"})
    check("help: known fingerprint -> KNOWN_FIX, no run",
          s == 200 and r["message"].startswith("KNOWN_FIX: forum post #1") and r["run_id"] is None
          and r["decision"] == "known_fix" and r["post_id"] == 1)
    s, r = call("POST", f"{base}/api/help", {"goal": "run the tests", "symptom": "zero tests discovered under src",
                                             "check_command": "python -m unittest discover -s tests"})
    check("help: unknown problem -> TEAM_STARTED: run-1",
          s == 200 and r["run_id"] == "run-1" and r["message"].startswith("TEAM_STARTED: run-1")
          and r["decision"] == "team_started")
    s, r = call("GET", f"{base}/api/help/run-1")
    check("help status: first poll is TEAM_WORKING with events",
          s == 200 and r["status"] == "working" and r["message"].startswith("TEAM_WORKING: run run-1")
          and r["events"] and r["run_id"] == "run-1")
    s, r = call("GET", f"{base}/api/help/run-1")
    hyps = (r.get("report") or {}).get("hypotheses") or []
    check("help status: then TEAM_REPORT with ranked hypotheses",
          s == 200 and r["status"] == "report" and r["message"].startswith("TEAM_REPORT: run run-1")
          and len(hyps) >= 2 and all({"rank", "cause", "confidence", "steps", "check"} <= set(h) for h in hyps))
    s, _ = call("GET", f"{base}/api/help/run-99")
    check("help status: unknown run -> 404", s == 404)

    s, _ = call("POST", f"{keyed}/api/judge", {"actions": []})
    check("api key: POST without key -> 401", s == 401)
    s, _ = call("POST", f"{keyed}/api/judge", {"actions": []}, headers={"X-API-Key": "secret"})
    check("api key: POST with key -> 200", s == 200)
    s, _ = call("GET", f"{keyed}/api/search?{q(error='x')}")
    check("api key: GETs are public -> 200 without a key", s == 200)

    for srv in servers:
        srv.shutdown()
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fake Breakpoint backend for local testing.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--api-key", default=None, help="if set, require this X-API-Key header on POSTs")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA, help="seed JSON file")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG, help="request log file")
    ap.add_argument("--selftest", action="store_true", help="run the endpoint checks and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.data)

    server = make_server(args.port, args.data, args.api_key, args.log)
    print(f"Mock Breakpoint backend on http://localhost:{args.port}"
          f"{' (X-API-Key required on POSTs)' if args.api_key else ''}. Logging to {args.log}. Ctrl+C to stop.",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
