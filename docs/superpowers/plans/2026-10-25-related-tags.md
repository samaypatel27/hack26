# Related questions by tags

User selected the simplest implementation: tags only, up to three related posts.
Keep duplicate detection unchanged. Add read-only GET `/api/posts/{id}/related`.
Check the source exists, join normalized post_tags to find other posts sharing at
least one tag, and return at most three PostCard objects with `shared_tags`.
Rank by number of shared tags descending, then creation time and ID descending.
Include open and solved questions across forums. No matches returns an empty list;
missing source returns 404. No database migrations or extra dependencies.

Add a compact Related questions section below tags in the existing sidebar, with
links, solved/open labels, matched tags, and loading/error/empty states. Requests
and polling are public reads and never count as visits.

Steps:
- [x] Add failing endpoint tests and an opt-in isolated PostgreSQL ranking test.
- [x] Implement the query and response.
- [x] Add frontend type/helper/component and sidebar integration.
- [x] Run backend tests, isolated DB ranking test, frontend lint/build and live reads.

Evidence: backend suite 49 passed/4 opt-in skipped; targeted endpoint checks with
isolated PostgreSQL ranking enabled 4 passed. Frontend lint and production build
passed. Live post 15 returns related IDs 11, 4, 5 with shared-tag counts 2, 1, 1,
including open and solved. No migrations, new dependencies, commits or pushes.
