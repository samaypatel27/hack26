"""Embed the posts that have no up-to-date vector yet (linking Stage 3). Run it on purpose.

Run from backend/ after applying schema_linking.sql:
    .venv\\Scripts\\python backfill_embeddings.py              # embed missing or changed posts
    .venv\\Scripts\\python backfill_embeddings.py --dry-run    # only count them and estimate the cost

It never creates tables. Needs DATABASE_URL and OPENAI_API_KEY (backend/.env). Safe to repeat:
posts whose problem text has not changed since they were embedded are skipped.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_db as db  # noqa: E402
import linking  # noqa: E402

BATCH = 64                 # texts per embedding call
CHARS_PER_TOKEN = 4        # rough, for the dry-run cost estimate


def posts_to_embed(conn) -> list[tuple[int, str, str]]:
    """[(post_id, problem text, content hash)] for posts with no vector or an outdated one."""
    rows = conn.execute(db.POST_SELECT + " ORDER BY c.id").fetchall()
    stored = linking.stored_hashes(conn)
    todo = []
    for post in rows:
        text = linking.problem_text(post)
        digest = linking.content_hash(text)
        if text and stored.get(post["id"]) != digest:
            todo.append((post["id"], text, digest))
    return todo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count and estimate, write nothing")
    args = parser.parse_args()
    load_dotenv(HERE / ".env")
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set")
        return 1
    with db.connect() as conn:
        if not linking.embeddings_ready(conn):
            print("Table agent_post_embeddings is missing. Apply backend/schema_linking.sql first.")
            return 1
        todo = posts_to_embed(conn)
        tokens = sum(len(text) for _, text, _ in todo) // CHARS_PER_TOKEN
        print(f"{len(todo)} post(s) to embed, about {tokens} tokens, "
              f"about ${tokens * linking.EMBED_PRICE_PER_MTOK / 1e6:.6f}")
        if args.dry_run or not todo:
            return 0
        if linking.openai_client() is None:
            print("OPENAI_API_KEY is not set")
            return 1
        done = 0
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            vectors = linking.embed([text for _, text, _ in batch], timeout=60.0)
            if vectors is None:
                print(f"embedding call failed after {done} post(s); run again to continue")
                return 1
            for (post_id, _, digest), vector in zip(batch, vectors):
                linking.store_embedding(conn, post_id, vector, digest)
            conn.commit()
            done += len(batch)
            print(f"embedded {done}/{len(todo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
