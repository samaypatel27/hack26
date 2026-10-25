# Related questions: keywords plus tags

User authorized extending the existing tag links with keywords from problem and
description. Keep the same read-only API, three-result cap, open/solved inclusion,
cross-forum candidates, and self-exclusion. Duplicate detection remains unchanged.

Rank with 60% Jaccard keyword overlap and 40% Jaccard tag overlap, using current
content fields with legacy fallback. Ignore common stopwords and generic forum/demo
boilerplate. A shared tag remains sufficient; keyword-only links require at least
two shared terms and keyword overlap >= 0.1. Tie by shared-tag count, recency, ID.
Return shared_tags, up to six shared_keywords, and similarity_score; show both
match reasons in the existing sidebar, not a misleading similarity percentage.

Read lightweight content across the current small catalog, rank in Python, hydrate
only three full cards. Future large-catalog optimization can use indexed search;
no schema, dependencies, or environment matching changes in this iteration.

Tests: combined > tag-only, keyword-only across differing tags, casing, self/limit/
tie behavior, generic boilerplate and incidental-word rejection, explicit content
fallback, no matches, read-only endpoint, and actual isolated PostgreSQL queries.
