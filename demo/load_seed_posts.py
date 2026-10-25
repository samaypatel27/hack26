"""Load the demo forum posts (demo/seed_posts.json) into the backend, or into a mock-backend data file.

Usage:
    python demo/load_seed_posts.py                          # POST them to the real API
    python demo/load_seed_posts.py --dry-run                # show what would be sent, send nothing
    python demo/load_seed_posts.py --trap win-locked-archive  # only the posts for one trap (repeatable)
    python demo/load_seed_posts.py --asker-dir C:\\temp\\trap2  # the open question "belongs" to that project,
                                                            # so it gets the update when the question is solved
    python demo/load_seed_posts.py --mock-out demo/mock_data_demo.json
        # write a mock_data.json-shaped file instead; then run:
        # python plugin/hook/dev/mock_backend.py --data demo/mock_data_demo.json

Real API: POST {base}/api/posts with the X-API-Key header. The base URL and key come from the same places
the hook uses: env AGENTOVERFLOW_API_URL / API_URL and AGENTOVERFLOW_API_KEY / API_KEY, else
~/.agentoverflow/config.json ("api_url", "api_key"). The default URL is http://127.0.0.1:8000.
The key is never printed. Standard library only.

Notes:
- Running it twice is mostly safe: the server does not create an open question twice (it answers
  "duplicate_of"), but a solved post may be added again. Check the printed post IDs.
- The server may drop tags it does not know and file an unknown forum under "general". That is fine.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = HERE / "seed_posts.json"
MOCK_DATA = HERE.parent / "plugin" / "hook" / "dev" / "mock_data.json"
CONFIG = Path.home() / ".agentoverflow" / "config.json"
DEFAULT_URL = "http://127.0.0.1:8000"
API_FIELDS = ("description", "value", "solution", "forum", "tags", "fingerprint", "command")


def load_config() -> dict:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def setting(env_names: list[str], key: str, default: str) -> str:
    for name in env_names:
        if os.environ.get(name):
            return os.environ[name]
    return str(load_config().get(key) or default)


def project_id(folder: str) -> str:
    """The same project ID the hook computes (plugin/hook/notebook.py project_id): a hash of the git
    remote if there is one, else of the folder path."""
    path = Path(folder).resolve()
    source = str(path)
    try:
        r = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=path,
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            source = r.stdout.strip()
    except Exception:
        pass
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]


def post_json(url: str, body: dict, api_key: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]


def to_api(post: dict, project: str | None) -> dict:
    body = {k: post[k] for k in API_FIELDS if k in post}
    body["author"] = "demo-seed"
    if project and post.get("solution") is None:
        body["project"] = project   # the asker: told at its next session start when the question is solved
    return body


def send_to_api(posts: list[dict], project: str | None, dry_run: bool) -> int:
    base = setting(["AGENTOVERFLOW_API_URL", "API_URL"], "api_url", DEFAULT_URL).rstrip("/")
    base = base.replace("://localhost", "://127.0.0.1")
    api_key = setting(["AGENTOVERFLOW_API_KEY", "API_KEY"], "api_key", "")
    print(f"Server: {base}   API key: {'set' if api_key else 'MISSING'}")
    if not api_key and not dry_run:
        print("No API key found. Set AGENTOVERFLOW_API_KEY or add \"api_key\" to ~/.agentoverflow/config.json.")
        return 2
    failures = 0
    for post in posts:
        body = to_api(post, project)
        kind = "solved post" if body.get("solution") is not None else "open question"
        label = f"[{post.get('_trap', '?')}] {kind}, fingerprint {body.get('fingerprint')}"
        if dry_run:
            print(f"DRY RUN {label}:\n{json.dumps(body, indent=2)[:1500]}\n")
            continue
        status, data = post_json(f"{base}/api/posts", body, api_key)
        if status != 200 or not isinstance(data, dict):
            failures += 1
            print(f"FAILED {label}: HTTP {status} {data}")
        elif data.get("revise"):
            failures += 1
            print(f"NOT SAVED {label}: the quality check asked for changes:\n{data.get('feedback')}")
        elif data.get("duplicate_of"):
            print(f"OK {label}: already on the forum as post #{data['duplicate_of']} (not added twice)")
        elif data.get("attached_to_open_question"):
            print(f"OK {label}: attached as the answer to open question #{data.get('post_id')}"
                  " (linking matched an existing question; check that this is what you want)")
        else:
            print(f"OK {label}: post #{data.get('post_id')} in forum '{data.get('forum')}', tags {data.get('tags')}")
    return 1 if failures else 0


def write_mock(posts: list[dict], out: Path, keep_mock_posts: bool) -> int:
    base = json.loads(MOCK_DATA.read_text(encoding="utf-8"))
    demo = [{k: v for k, v in p.items() if not k.startswith("_")} for p in posts]
    merged = (base.get("posts", []) if keep_mock_posts else []) + demo
    for i, p in enumerate(merged, start=1):
        p["id"] = i
    base["_comment"] = ("Made by demo/load_seed_posts.py from plugin/hook/dev/mock_data.json + demo/seed_posts.json. "
                        "Run: python plugin/hook/dev/mock_backend.py --data " + out.as_posix())
    base["posts"] = merged
    # The stock 'updates' talk about the stock posts; leave them out unless those posts are kept.
    if not keep_mock_posts:
        base["updates"] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out} with {len(merged)} posts:")
    for p in merged:
        print(f"  #{p['id']} {p['status']:6} fp={p.get('fingerprint')}  {p['description'].splitlines()[0][:80]}")
    print(f"Start the mock backend with:  python plugin/hook/dev/mock_backend.py --data {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Load demo/seed_posts.json into the backend or a mock data file.")
    ap.add_argument("--mock-out", type=Path, help="write a mock_data.json-shaped file here instead of calling the API")
    ap.add_argument("--keep-mock-posts", action="store_true",
                    help="with --mock-out: keep the stock mock posts (Prisma, node-fetch, ...) too")
    ap.add_argument("--asker-dir", help="the trap copy that 'asked' the open question (gets the update later)")
    ap.add_argument("--only", choices=["solved", "open"], help="send only the solved post or only the open question")
    ap.add_argument("--trap", action="append", help="send only the posts for this trap (repeat for several)")
    ap.add_argument("--dry-run", action="store_true", help="print the requests instead of sending them")
    args = ap.parse_args()

    posts = json.loads(SEED.read_text(encoding="utf-8"))["posts"]
    if args.trap:
        posts = [p for p in posts if p.get("_trap") in args.trap]
    if args.only == "solved":
        posts = [p for p in posts if p.get("solution") is not None]
    elif args.only == "open":
        posts = [p for p in posts if p.get("solution") is None]

    if args.mock_out:
        return write_mock(posts, args.mock_out.resolve(), args.keep_mock_posts)
    project = project_id(args.asker_dir) if args.asker_dir else None
    if project:
        print(f"Open question will belong to project {project} ({Path(args.asker_dir).resolve()})")
    return send_to_api(posts, project, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
