"""Text matching for /v1 search and Open Question matching.

Mirrors the teammate's backend/matching.py (origin/main): same tokenizer and stopwords, word
overlap (Jaccard). One addition from the hook's mock backend (plugin/hook/dev/mock_backend.py):
the query is compared with the whole description AND with each of its lines, keeping the best,
so a short pasted error line still matches a long write-up. Without it, a two-line error against
a twenty-line post scores far below any threshold.

If the teammate prefers one module, `words`/`jaccard` here can be replaced by matching.words /
matching.jaccard with no behavior change.
"""

import re
from typing import Any

SEARCH_MIN_SCORE = 0.2   # minimum score for a "text" search result (docs/hook-contracts.md)
ATTACH_MIN_SCORE = 0.5   # a new post/question this close to an Open Question is "the same problem"

# Same list as origin/main backend/matching.py.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have", "i", "in",
    "is", "it", "its", "me", "my", "no", "not", "of", "on", "or", "so", "that", "the", "this", "to",
    "was", "when", "with", "works", "work", "still", "only", "after", "before", "same", "fails",
}


def words(text: str | None) -> set[str]:
    """Same tokenizer as origin/main backend/matching.py."""
    tokens = re.findall(r"[a-z][a-z0-9_.\-]*", (text or "").lower())
    return {t.strip(".-") for t in tokens if t.strip(".-") and t not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def text_score(query: set[str], description: str) -> float:
    """Best Jaccard overlap between the query and the description, or any one of its lines."""
    candidates = [description] + [ln for ln in description.splitlines() if ln.strip()]
    return round(max(jaccard(query, words(c)) for c in candidates), 3)


def best_text_match(query: set[str], posts: list[dict[str, Any]], min_score: float,
                    tags: list[str] | None = None) -> tuple[dict[str, Any], float] | None:
    """The closest post scoring at least `min_score` (sharing a tag, when `tags` is given), or None."""
    best: tuple[dict[str, Any], float] | None = None
    for post in posts:
        if tags and not set(tags) & set(post["tags"]):
            continue
        score = text_score(query, post["description"])
        if score >= min_score and (best is None or score > best[1]):
            best = (post, score)
    return best


def rank(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fingerprint matches first, then by score, then solved before open, then upvotes, then newest."""
    return sorted(results, key=lambda r: (r["match"] != "fingerprint", -r["score"],
                                          r["status"] != "solved", -r["upvotes"], -r["id"]))
