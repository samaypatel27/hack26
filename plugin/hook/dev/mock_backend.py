"""Fake AgentOverflow backend for local testing. Standard library only.

Implements the contract in docs/hook-contracts.md with in-memory data seeded from mock_data.json.
Every request is printed and appended to mock_requests.log, so you can see exactly what the hook
and the MCP server send.

    python plugin/hook/dev/mock_backend.py                  # http://localhost:8000
    python plugin/hook/dev/mock_backend.py --port 9000 --api-key secret
    python plugin/hook/dev/mock_backend.py --selftest       # exercise every endpoint, PASS/FAIL

Endpoints (hook):  GET /v1/search, POST /v1/outcomes, GET /v1/updates, POST /v1/judge
Endpoints (MCP):   GET /v1/posts/{id}, POST /v1/posts, POST /v1/open-questions,
                   POST /v1/help, GET /v1/help/{run_id}  (the team: a canned run, see help_report in mock_data.json)
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

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE / "mock_data.json"
DEFAULT_LOG = HERE / "mock_requests.log"

TEXT_MATCH_MIN = 0.2      # minimum Jaccard score for a "text" match
JUDGE_WINDOW = 5          # judge looks at this many recent actions
JUDGE_FAILS = 3           # ... and says stuck if at least this many are not ok

SEARCH_FIELDS = ["id", "description", "value", "solution", "tags", "upvotes", "status",
                 "times_worked", "times_applied"]


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, seed: dict):
        self.lock = threading.Lock()
        self.posts: dict[int, dict] = {p["id"]: p for p in copy.deepcopy(seed.get("posts", []))}
        self.updates: list[dict] = copy.deepcopy(seed.get("updates", []))
        self.next_id = max(self.posts, default=0) + 1
        self.help_report: dict = copy.deepcopy(seed.get("help_report") or {"hypotheses": []})
        self.help_events: list[dict] = copy.deepcopy(seed.get("help_events") or [])
        self.runs: dict[str, dict] = {}     # run_id -> {"polls": n, "case": body}

    def new_post(self, fields: dict) -> dict:
        post = {
            "id": self.next_id, "upvotes": 0, "times_worked": 0, "times_applied": 0,
            "fingerprint": None, "command": None, **fields,
        }
        self.posts[post["id"]] = post
        self.next_id += 1
        return post


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
    results = []
    for post in store.posts.values():
        if fingerprint and post.get("fingerprint") == fingerprint:
            match, score = "fingerprint", 1.0
        else:
            if tags and not set(tags) & set(post.get("tags", [])):
                continue
            score = round(text_score(query, post["description"]), 3)
            if score < TEXT_MATCH_MIN:
                continue
            match = "text"
        item = {k: post.get(k) for k in SEARCH_FIELDS}
        item.update(match=match, score=score)
        results.append(item)
    # fingerprint matches first, then by score, then solved before open, then upvotes
    results.sort(key=lambda r: (r["match"] != "fingerprint", -r["score"],
                                r["status"] != "solved", -r["upvotes"]))
    return results


def start_help(store: Store, case: dict) -> dict:
    """POST /v1/help: KNOWN_FIX for a solved post's fingerprint (or a close text match), else start a run."""
    fp = case.get("fingerprint") or ""
    hits = search(store, f"{case.get('symptom', '')} {case.get('error', '')}", case.get("command", ""), fp, [])
    top = hits[0] if hits else None
    if top and top["status"] == "solved" and (top["match"] == "fingerprint" or top["score"] >= 0.5):
        return {"status": "known_fix", "post_id": top["id"],
                "message": f"KNOWN_FIX: post #{top['id']} ({top['times_worked']} of {top['times_applied']} worked)\n"
                           f"{top['description'].splitlines()[0]}\nSolution: {top['solution']}"}
    run_id = f"run-{len(store.runs) + 1}"
    store.runs[run_id] = {"polls": 0, "case": case}
    return {"status": "started", "run_id": run_id,
            "message": f"TEAM_STARTED: {run_id}\nThe team is researching. Keep working, and call check_help with "
                       f"run_id {run_id} in a few minutes."}


def report_message(run_id: str, report: dict) -> str:
    lines = [f"TEAM_REPORT: {run_id}", "Ranked likely causes (verify each before applying):"]
    for h in report.get("hypotheses", []):
        lines.append(f"{h.get('rank')}. {h.get('cause')} (confidence {int(100 * (h.get('confidence') or 0))}%)")
        lines += [f"   step: {step}" for step in h.get("steps", [])]
        check = h.get("check") or {}
        if check.get("command") or check.get("description"):
            lines.append(f"   check: {check.get('command') or check.get('description')}")
        lines += [f"   evidence: {e['url']}" for e in h.get("evidence", []) if e.get("url")]
    if report.get("if_none_work"):
        lines.append(f"If none work: {report['if_none_work']}")
    return "\n".join(lines)


def help_status(store: Store, run_id: str) -> dict | None:
    """GET /v1/help/{run_id}: TEAM_WORKING on the first poll (a few events), then TEAM_REPORT."""
    run = store.runs.get(run_id)
    if run is None:
        return None
    run["polls"] += 1
    if run["polls"] == 1:
        events = store.help_events[:2]
        return {"status": "working", "events": events, "report": None,
                "message": f"TEAM_WORKING: {run_id}\n" + "\n".join(f"- {e['member']}: {e['text']}" for e in events)
                           + "\nCall check_help again in a minute."}
    return {"status": "report", "events": store.help_events, "report": store.help_report,
            "message": report_message(run_id, store.help_report)}


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
            self._error(401, "Missing or wrong X-API-Key.")
            return False
        return True

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        self._log(None)
        if not self._authorized():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/v1/search":
            tags = [t.strip() for t in q.get("tags", "").split(",") if t.strip()]
            with self.store.lock:
                result = search(self.store, q.get("error", ""), q.get("command", ""),
                                q.get("fingerprint", ""), tags)
            return self._send(200, result)
        if url.path == "/v1/updates":
            project = q.get("project")
            with self.store.lock:
                due = [u for u in self.store.updates
                       if not u.get("delivered") and u.get("project") in (None, project)]
                for u in due:
                    u["delivered"] = True
            return self._send(200, [{"message": u["message"], "post_id": u.get("post_id")} for u in due])
        m = re.fullmatch(r"/v1/help/([\w-]+)", url.path)
        if m:
            with self.store.lock:
                status = help_status(self.store, m.group(1))
            return self._send(200, status) if status else self._error(404, "Run not found.")
        m = re.fullmatch(r"/v1/posts/(\d+)", url.path)
        if m:
            post = self.store.posts.get(int(m.group(1)))
            return self._send(200, post) if post else self._error(404, "Post not found.")
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
            "/v1/outcomes": self._outcomes,
            "/v1/judge": self._judge,
            "/v1/posts": self._create_post,
            "/v1/open-questions": self._open_question,
            "/v1/help": self._help,
        }.get(path)
        if not route:
            return self._error(404, f"No route for POST {path}")
        with self.store.lock:
            route(body)

    # -- POST endpoints ---------------------------------------------------

    def _outcomes(self, body):
        try:
            post = self.store.posts.get(int(body.get("post_id")))
        except (TypeError, ValueError):
            post = None
        if not post:
            return self._error(404, "Post not found.")
        if not isinstance(body.get("worked"), bool):
            return self._error(400, "`worked` must be true or false.")
        post["times_applied"] += 1
        if body["worked"]:
            post["times_worked"] += 1
        self._send(200, {"post_status": post["status"]})

    def _judge(self, body):
        actions = body.get("actions")
        if not isinstance(actions, list):
            return self._error(400, "`actions` must be a list.")
        self._send(200, judge(actions))

    def _missing(self, body, fields) -> bool:
        missing = [f for f in fields if not body.get(f)]
        if missing:
            self._error(400, f"Missing required fields: {', '.join(missing)}")
        return bool(missing)

    def _create_post(self, body):
        if self._missing(body, ["description", "value", "solution", "tags"]):
            return
        oq_id = body.get("open_question_id")
        oq = self.store.posts.get(oq_id) if isinstance(oq_id, int) else None
        if oq and oq["status"] == "open":
            oq.update(solution=body["solution"], status="solved")
            for f in ("fingerprint", "command"):
                oq[f] = oq.get(f) or body.get(f)
            return self._send(200, {"post_id": oq["id"], "attached_to_open_question": True,
                                    "merged_into": None})
        post = self.store.new_post({
            "description": body["description"], "value": body["value"],
            "solution": body["solution"], "tags": body["tags"][:4], "status": "solved",
            "fingerprint": body.get("fingerprint"), "command": body.get("command"),
        })
        self._send(200, {"post_id": post["id"], "attached_to_open_question": False,
                         "merged_into": None})

    def _help(self, body):
        self._send(200, start_help(self.store, body))

    def _open_question(self, body):
        if self._missing(body, ["description", "value", "tags"]):
            return
        fp = body.get("fingerprint")
        if fp:  # one open question per fingerprint
            for p in self.store.posts.values():
                if p["status"] == "open" and p.get("fingerprint") == fp:
                    return self._send(200, {"post_id": p["id"], "duplicate_of": p["id"]})
        post = self.store.new_post({
            "description": body["description"], "value": body["value"], "solution": None,
            "tags": body["tags"][:4], "status": "open",
            "fingerprint": fp, "command": body.get("command"),
        })
        self._send(200, {"post_id": post["id"], "duplicate_of": None})


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
    log = Path(tempfile.gettempdir()) / "agentoverflow_mock_selftest.log"
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

    s, r = call("GET", f"{base}/v1/search?{q(error='anything', fingerprint='51749bd5')}")
    check("search: fingerprint match first, score 1.0",
          s == 200 and r and r[0]["id"] == 1 and r[0]["match"] == "fingerprint" and r[0]["score"] == 1.0)
    check("search: result has exactly the contract fields",
          s == 200 and set(r[0]) == set(SEARCH_FIELDS) | {"match", "score"})
    s, r = call("GET", f"{base}/v1/search?{q(error='Error [ERR_REQUIRE_ESM]: require() of ES Module node-fetch not supported', command='node src/api.js')}")
    check("search: text match by word overlap", s == 200 and r and r[0]["id"] == 2 and r[0]["match"] == "text")
    s, r = call("GET", f"{base}/v1/search?{q(error='segmentation fault in quantum flux capacitor')}")
    check("search: [] when nothing matches", s == 200 and r == [])
    s, r = call("GET", f"{base}/v1/search?{q(error='ModuleNotFoundError: No module named app conftest', tags='python')}")
    check("search: open question returned with status open",
          s == 200 and r and r[0]["id"] == 3 and r[0]["status"] == "open" and r[0]["solution"] is None)

    s, r = call("POST", f"{base}/v1/outcomes", {"post_id": 1, "worked": True, "fingerprint": "51749bd5",
                                                 "command": "docker compose up --build api",
                                                 "session_id": "t", "source": "hook"})
    check("outcomes: returns post_status", s == 200 and r == {"post_status": "solved"})
    s, r = call("GET", f"{base}/v1/posts/1")
    check("outcomes: counters incremented", s == 200 and r["times_applied"] == 11 and r["times_worked"] == 10)
    s, _ = call("POST", f"{base}/v1/outcomes", {"post_id": 999, "worked": False})
    check("outcomes: unknown post -> 404", s == 404)

    s, r = call("GET", f"{base}/v1/updates?{q(project='abc123')}")
    check("updates: pending update delivered", s == 200 and len(r) == 1 and r[0]["post_id"] == 2)
    s, r = call("GET", f"{base}/v1/updates?{q(project='abc123')}")
    check("updates: delivered only once", s == 200 and r == [])

    bad = [{"kind": "run", "ok": False}] * 3 + [{"kind": "edit"}]
    s, r = call("POST", f"{base}/v1/judge", {"session_id": "t", "actions": bad, "replies": []})
    check("judge: stuck after 3 failures", s == 200 and r["stuck"] is True and 0 <= r["confidence"] <= 1)
    s, r = call("POST", f"{base}/v1/judge", {"session_id": "t", "actions": [{"kind": "run", "ok": True}],
                                              "replies": []})
    check("judge: not stuck", s == 200 and r["stuck"] is False and isinstance(r["reason"], str))

    s, r = call("GET", f"{base}/v1/posts/3")
    check("get post: full post", s == 200 and r["id"] == 3 and "description" in r)
    s, _ = call("GET", f"{base}/v1/posts/999")
    check("get post: unknown -> 404", s == 404)

    s, r = call("POST", f"{base}/v1/open-questions", {"description": "X fails\nTried a, b.", "value": "v",
                                                       "tags": ["general"], "fingerprint": "deadbeef"})
    check("open question: created", s == 200 and r["duplicate_of"] is None and r["post_id"] == 5)
    s, r = call("POST", f"{base}/v1/open-questions", {"description": "X fails again", "value": "v",
                                                       "tags": ["general"], "fingerprint": "deadbeef"})
    check("open question: same fingerprint -> duplicate_of", s == 200 and r["duplicate_of"] == 5)
    s, r = call("POST", f"{base}/v1/posts", {"description": "d", "value": "v", "solution": "fix",
                                              "tags": ["python"], "open_question_id": 3})
    check("post: attaches to open question",
          s == 200 and r == {"post_id": 3, "attached_to_open_question": True, "merged_into": None})
    s, r = call("GET", f"{base}/v1/posts/3")
    check("post: open question now solved", r["status"] == "solved" and r["solution"] == "fix")
    s, r = call("POST", f"{base}/v1/posts", {"description": "d", "value": "v", "solution": "fix",
                                              "tags": ["python"], "open_question_id": None,
                                              "fingerprint": "cafef00d", "command": "make"})
    check("post: new solved post", s == 200 and r["attached_to_open_question"] is False and r["post_id"] == 6)
    s, r = call("GET", f"{base}/v1/search?{q(fingerprint='cafef00d')}")
    check("post: new post findable by fingerprint", s == 200 and r and r[0]["id"] == 6)
    s, r = call("POST", f"{base}/v1/posts", {"description": "d"})
    check("post: missing fields -> 400 with detail", s == 400 and "detail" in r)

    s, r = call("POST", f"{base}/v1/help", {"goal": "start the api", "symptom": "crash", "fingerprint": "51749bd5"})
    check("help: known fingerprint -> KNOWN_FIX, no run",
          s == 200 and r["message"].startswith("KNOWN_FIX: post #1") and "run_id" not in r)
    s, r = call("POST", f"{base}/v1/help", {"goal": "run the tests", "symptom": "zero tests discovered under src",
                                             "check_command": "python -m unittest discover -s tests"})
    check("help: unknown problem -> TEAM_STARTED: run-1",
          s == 200 and r["run_id"] == "run-1" and r["message"].startswith("TEAM_STARTED: run-1"))
    s, r = call("GET", f"{base}/v1/help/run-1")
    check("help status: first poll is TEAM_WORKING with events",
          s == 200 and r["status"] == "working" and r["message"].startswith("TEAM_WORKING") and r["events"])
    s, r = call("GET", f"{base}/v1/help/run-1")
    hyps = (r.get("report") or {}).get("hypotheses") or []
    check("help status: then TEAM_REPORT with ranked hypotheses",
          s == 200 and r["status"] == "report" and r["message"].startswith("TEAM_REPORT: run-1")
          and len(hyps) >= 2 and all({"rank", "cause", "confidence", "steps", "check"} <= set(h) for h in hyps))
    s, _ = call("GET", f"{base}/v1/help/run-99")
    check("help status: unknown run -> 404", s == 404)

    s, _ = call("GET", f"{keyed}/v1/search?{q(error='x')}")
    check("api key: request without key -> 401", s == 401)
    s, _ = call("GET", f"{keyed}/v1/search?{q(error='x')}", headers={"X-API-Key": "secret"})
    check("api key: request with key -> 200", s == 200)

    for srv in servers:
        srv.shutdown()
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fake AgentOverflow backend for local testing.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--api-key", default=None, help="if set, require this X-API-Key header")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA, help="seed JSON file")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG, help="request log file")
    ap.add_argument("--selftest", action="store_true", help="run the endpoint checks and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.data)

    server = make_server(args.port, args.data, args.api_key, args.log)
    print(f"Mock AgentOverflow backend on http://localhost:{args.port}"
          f"{' (X-API-Key required)' if args.api_key else ''}. Logging to {args.log}. Ctrl+C to stop.",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
