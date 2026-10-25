"""Explainable related-post ranking, separate from duplicate detection."""
from datetime import datetime, timezone
from typing import Any

if __package__:
    from .matching import jaccard, words
else:
    from matching import jaccard, words

# Generic forum language and seed labels must not create artificial similarities.
GENERIC_WORDS = {
    "problem", "problems", "question", "questions", "description", "details", "error",
    "errors", "failed", "failure", "issue", "issues", "using", "use", "used", "get",
    "demo", "illustrative", "sample", "votes", "outcomes", "synthetic", "observed",
    "agent", "agents", "activity", "solution", "fix", "help", "please", "does", "can",
}


def keywords(post: dict[str, Any]) -> set[str]:
    legacy = post.get("description") or ""
    lines = legacy.strip().splitlines()
    title = post.get("problem") or (lines[0] if lines else "")
    detail = post.get("general_description") or "\n".join(lines[1:])
    return {word for word in words(f"{title}\n{detail}") if len(word) > 2 and word not in GENERIC_WORDS}


def rank_related(source: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_words = keywords(source)
    source_tags = set(source.get("tags") or [])
    ranked = []
    for post in candidates:
        if post["id"] == source["id"]:
            continue
        candidate_words = keywords(post)
        candidate_tags = set(post.get("tags") or [])
        shared_words = source_words & candidate_words
        shared_tags = source_tags & candidate_tags
        word_score = jaccard(source_words, candidate_words)
        # Tag matches are explicit; text-only matches require more evidence.
        if not shared_tags and (len(shared_words) < 2 or word_score < 0.1):
            continue
        similarity = 0.6 * word_score + 0.4 * jaccard(source_tags, candidate_tags)
        ranked.append((similarity, len(shared_tags), post.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                       post["id"], {"id": post["id"], "similarity_score": round(similarity, 4),
                                    "shared_tags": sorted(shared_tags), "shared_keywords": sorted(shared_words)[:6]}))
    ranked.sort(key=lambda entry: entry[:4], reverse=True)
    return [entry[4] for entry in ranked[:3]]
