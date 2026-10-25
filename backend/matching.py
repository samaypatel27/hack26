"""Search and matching: word overlap plus tags, compared only within one forum.

score = word_score                        when the query has no tags
score = 0.6 * word_score + 0.4 * tag_score otherwise
"""

import re
from collections.abc import Iterable
from typing import Any

MATCH_THRESHOLD = 0.3

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have", "i", "in",
    "is", "it", "its", "me", "my", "no", "not", "of", "on", "or", "so", "that", "the", "this", "to",
    "was", "when", "with", "works", "work", "still", "only", "after", "before", "same", "fails",
}


def words(text: str) -> set[str]:
    tokens = re.findall(r"[a-z][a-z0-9_.\-]*", text.lower())
    return {t.strip(".-") for t in tokens if t.strip(".-") and t not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def score(query: str, post: dict[str, Any], tags: Iterable[str] = ()) -> float:
    word_score = jaccard(words(query), words(post["description"]))
    query_tags = set(tags)
    if not query_tags:
        return word_score
    tag_score = len(query_tags & set(post.get("tags", []))) / len(query_tags)
    return 0.6 * word_score + 0.4 * tag_score


def best_match(
    query: str,
    posts: Iterable[dict[str, Any]],
    forum: str | None = None,
    tags: Iterable[str] = (),
    threshold: float = MATCH_THRESHOLD,
) -> dict[str, Any] | None:
    """Best post scoring at least `threshold`. The forum is the first filter: when given,
    only posts in that forum are compared at all."""
    tags = list(tags)
    candidates = [p for p in posts if forum is None or p["forum"] == forum]
    scored = [(score(query, p, tags), p) for p in candidates]
    scored = [(s, p) for s, p in scored if s >= threshold]
    if not scored:
        return None
    return max(scored, key=lambda sp: sp[0])[1]


# ---- agent search and Open Question attaching -----------------------------------------------------

SEARCH_MIN_SCORE = 0.2  # minimum score for a "text" result from GET /api/search
ATTACH_MIN_SCORE = 0.5  # a new solution/question this close to an Open Question is "the same problem"


def text_score(query: set[str], description: str) -> float:
    """Best word overlap between the query and the whole description or any one of its lines, so a
    short pasted error still matches a long write-up."""
    candidates = [description] + [ln for ln in description.splitlines() if ln.strip()]
    return round(max(jaccard(query, words(c)) for c in candidates), 3)


def best_text_match(
    query: set[str], posts: Iterable[dict[str, Any]], min_score: float, tags: Iterable[str] = ()
) -> dict[str, Any] | None:
    """Closest post scoring at least `min_score` that shares a tag (when `tags` is given), else None."""
    tags = set(tags)
    best: tuple[dict[str, Any], float] | None = None
    for post in posts:
        if tags and not tags & set(post["tags"]):
            continue
        score = text_score(query, post["description"])
        if score >= min_score and (best is None or score > best[1]):
            best = (post, score)
    return best[0] if best else None


def rank(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fingerprint matches first, then by score, then solved before open, then upvotes, then newest."""
    return sorted(
        results,
        key=lambda r: (r["match"] != "fingerprint", -r["score"], r["status"] != "solved", -r["upvotes"], -r["id"]),
    )
