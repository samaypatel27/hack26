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
