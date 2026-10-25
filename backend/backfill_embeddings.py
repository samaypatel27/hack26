"""Warm-up benchmark for linking Stage 3 (the in-memory meaning cache). READ-ONLY on the database.

There is no embeddings table any more, so there is nothing to backfill: the server embeds every post into
memory when it starts (linking.warm_up()). This script runs that same warm-up once and prints how long it
took and what it cost, so you know what a restart costs. Nothing is stored anywhere.

Run from backend/ (needs DATABASE_URL and OPENAI_API_KEY in backend/.env):
    .venv\\Scripts\\python backfill_embeddings.py                    # warm up, print time and cost
    .venv\\Scripts\\python backfill_embeddings.py --dry-run          # count posts and estimate, no OpenAI call
    .venv\\Scripts\\python backfill_embeddings.py --query "pytest conftest cannot import app"   # + one search
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_db as db  # noqa: E402
import linking  # noqa: E402

CHARS_PER_TOKEN = 4        # rough, for the dry-run estimate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count posts and estimate the cost; no OpenAI call")
    parser.add_argument("--query", help="after warming up, run one search (stages 1-3, no AI check) and time it")
    args = parser.parse_args()
    load_dotenv(HERE / ".env")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set")
        return 1

    with db.connect() as conn:
        rows = conn.execute(linking.WARM_SELECT, (linking.MAX_CACHED_POSTS,)).fetchall()
        conn.rollback()                      # nothing was written; end the read-only transaction
    chars = sum(len(linking.problem_text(r)) for r in rows)
    tokens = chars // CHARS_PER_TOKEN
    print(f"{len(rows)} post(s), about {tokens} tokens, about ${tokens * linking.EMBED_PRICE_PER_MTOK / 1e6:.6f}")
    if args.dry_run:
        return 0
    if linking.openai_client() is None:
        print("OPENAI_API_KEY is not set (or LINKING_EMBEDDINGS=0)")
        return 1

    start = time.monotonic()
    linking.warm_up(wait=True)
    status = linking.cache_status()
    if not status["warm"]:
        print("warm-up failed; see the log line above")
        return 1
    last = status["last"]
    print(f"warm-up: {last['posts']} vectors in {time.monotonic() - start:.2f}s, "
          f"{last['tokens']} tokens, ${last['cost_usd']:.6f}")

    if args.query:
        start = time.monotonic()
        matches = linking.find_matches(args.query, top_k=5)
        took = time.monotonic() - start
        print(f"search took {took:.2f}s")
        for m in matches:
            title = (m.post.get("problem") or (m.post.get("description") or "").split("\n")[0])[:70]
            print(f"  #{m.id} score={m.score} stages={m.stages} {m.scores}  {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
