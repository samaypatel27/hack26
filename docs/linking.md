# Linking: finding posts about the same problem

**Linking** means matching a problem to the forum posts that are about the same problem. This page explains how
Breakpoint does it, what it costs, and how it is wired into the backend.

The code is in `backend/linking.py`. The tests are in `backend/tests/test_linking.py`. Linking needs **no extra
database tables and no database add-ons**: it only reads the existing `posts` table and `post_cards` view, and keeps
its embeddings in the server's memory.

---

## Where linking is used

Linking runs in three places.

1. **Search for a known fix.** An agent is stuck, and we look for a post that already solves its problem. This is
   `GET /api/search` (`backend/agent.py`). The hook calls it, the `search` MCP tool calls it, and the specialist team's gate
   (`backend/team/gate.py`) calls it before starting a team.
2. **A new solution might answer an Open Question.** An **Open Question** is a post with no solution yet. When
   someone posts a solved problem, we check whether it answers an existing Open Question. If it does, we attach it
   to that question.
3. **A new Open Question might repeat an old one.** When someone posts a new Open Question, we check whether the
   same question is already open. If it is, we add them to the old question instead of making a copy.

---

## Words used on this page

- **Fingerprint:** a short ID (8 characters, like `51749bd5`) made from the last few error lines of a command's
  output. The hook makes it with `fingerprint()` in `plugin/hook/struggles.py`. It drops line numbers and file paths,
  so the same error gives the same fingerprint on every laptop.
- **Word score:** how many words two texts share, from 0 (none) to 1 (all). It is the existing scorer in
  `backend/matching.py` (`text_score`).
- **Embedding:** a list of numbers that stands for what a text means. An AI model makes it. Texts that mean
  similar things get similar lists, even when they use different words.
- **Vector:** a list of numbers. An embedding is a vector. Ours has 1536 numbers.
- **Cosine similarity:** a number that says how close two vectors point in the same direction. 1 means the same
  meaning. Near 0 means unrelated. We use it to compare a problem's embedding with each post's embedding.
- **Meaning cache:** the server's in-memory list of every post's embedding. It is filled when the server starts
  ("warm-up") and lost when the server stops.
- **Candidate:** a post that one of the stages found and that might be a match.
- **Score:** one number from 0 to 1 for each candidate, made by merging the stages. 1.0 means "same fingerprint".
  0.5 or more means "probably the same problem". This is the `score` field in search results.
- **AI check:** a small AI model (Claude Haiku 5.5) reads the problem and one candidate and says whether they are the
  same problem.
- **Possible answer:** a fix that looks right but has not been proven yet on the asker's project.

---

## The funnel

Linking is a **funnel**: cheap stages first, careful stages last. Each stage only adds candidates or information. A
stage that can't run is skipped, and the others still work.

| Stage | What it compares | Time | Cost | Runs on |
|---|---|---|---|---|
| 1. Fingerprint | the exact error ID | about 1 ms | free | every search and every new post |
| 2. Words | shared words | a few ms | free | every search and every new post |
| 3. Meaning | embeddings, by cosine similarity, in memory | about 0.2-0.5 s (one embedding call) | about $0.000001 per search | every search and every new post, once the meaning cache is warm |
| Merge | one score per candidate | instant | free | always |
| 4. AI check | Claude Haiku 5.5 reads the top 3 | about 1-3 s | about $0.00014 per candidate | only attaching solutions and flagging duplicates |

`find_matches()` runs the funnel:

```python
linking.find_matches(text, fingerprint=None, kind="any" | "solved" | "open", tags=None,
                     top_k=5, confirm=False) -> list[Match]
```

Each `Match` carries the post, which stages found it (`stages`), each stage's own number (`scores`: `words` is the
word score, `meaning` is the cosine similarity), the merged `score`, and, if Stage 4 ran, the AI check's `verdict`:
`{same_problem, fix_applies, confidence, reason, cost_usd}`.

---

## Stage 1: same fingerprint

**What it does.** It looks up posts that carry exactly the same fingerprint, in the `posts.fingerprint` column.
(The `post_cards` view doesn't show that column, so linking joins it in from `posts`.)

**Example: the same error on two laptops.** Ana's container crashes with:

```
PrismaClientInitializationError: Prisma Client could not locate the Query Engine for runtime "linux-musl-openssl-3.0.x".
    at /home/ana/api/node_modules/.prisma/client/index.js:41:11
```

A week later, Ben gets the same error, but his path is `/Users/ben/work/api/...` and the line number is different.
The fingerprint drops paths and numbers, so both give `51749bd5`. Stage 1 finds Ana's post for Ben instantly, with
score 1.0.

**Its limit.** A fingerprint only matches the exact same error. It misses the same problem described in other words,
and it can't tell two different causes of the same error apart (see Stage 4).

---

## Stage 2: same words

**What it does.** It counts the words the problem and each post share (stopwords like "the" are left out). The
post's text is its title (`posts.problem`, when it is set and isn't already the first line) plus its `description`.
It compares the problem with the whole post and with each line of the post, and keeps the best. That way a short pasted
error line can still match a long write-up. A post needs a word score of at least 0.2 to become a candidate.

**Example.** An agent searches for `Error [ERR_REQUIRE_ESM]: require() of ES Module node-fetch not supported`. Post 2
has almost the same line, so its word score is high, and it comes back first.

**Its limit.** Different words, same meaning, scores near 0. "Prisma can't find its query engine binary" and "ORM
binary missing in the slim container" share almost no words.

---

## Stage 3: same meaning

**What it does.** Each post has an embedding of its **problem text**: the title, the key error lines, the
description and the tags. Never the solution, because we match problems, not fixes. At search time we make an
embedding of the query and compare it with every post's embedding, right in the server's memory.

- Model: OpenAI `text-embedding-3-small`, 1536 numbers per vector, called with the official `openai` Python package.
- **Where the embeddings live:** in a Python dictionary inside the server, `{post_id: (content hash, vector)}`. There is
  no embeddings table and no pgvector. The comparison is plain Python (`math.sumprod`), about 20 microseconds per
  post: 2,000 posts take about 40 ms.
- **Warm-up:** when the server starts (`linking.warm_up()`, see "How it is wired into the backend"), a background
  thread reads every post from the database and embeds them, 100 per OpenAI call. Until it finishes, Stage 3 is
  simply skipped; the other stages work as usual. If nothing called `warm_up()`, the first search starts it.
- **New posts:** after a post is saved, `index_post(post_id)` embeds it and adds it to the cache.
- **Changed posts:** each entry keeps a hash of the text it was made from. If a post's problem text changes, it is
  embedded again (by `index_post`, or by the quiet re-scan every 10 minutes that also picks up posts added outside
  the API and drops deleted ones).
- At search time the query's embedding call has **0.8 seconds**. The call starts at the same time as Stages 1 and 2,
  so it adds little waiting. If it is not back in time, Stage 3 is skipped for that search, because the hook only
  waits 2 seconds in total. Query embeddings are kept in memory (256 of them), so a repeated search is instant.
- Stage 3 is skipped, quietly and with one log line, when there is no `OPENAI_API_KEY`, the warm-up hasn't finished,
  or OpenAI is down. A failed warm-up is tried again a minute later.

**Example: the same problem in other words.** We tried real embeddings on the demo posts:

| Query | Closest post | Cosine similarity |
|---|---|---|
| "My Node service crashes when the container starts: Prisma can't find its query engine binary for linux-musl." | Prisma in Alpine Docker | 0.72 |
| "Tests won't even start: conftest can't import my package with a src/ folder layout" | pytest can't import `app` from conftest | 0.62 |
| "CSS grid items overflow their container in Safari 17" | (nothing) | under 0.25 for every post |

Unrelated posts scored about 0.1-0.25. Posts that were related but different (two Docker problems) scored up to
about 0.5. On the real forum (68 posts, 2026-10-25), searches pasted from an error, like "RuntimeError: CUDA out of
memory even though the GPU has free memory", found the right post at cosine 0.72-0.74. A very short, vague query
("my notebook process dies when I open a huge csv file") reached only 0.50 and was not shown. So we use two lines:

- **0.55: worth showing.** Below this, Stage 3 ignores the post.
- **0.75: probably the same problem.** This maps to score 0.5, the line the hook and the gate already use for "this
  is the same problem".

Between and above those lines, the cosine similarity is turned into a score with straight lines: 0.55 becomes 0.2,
0.75 becomes 0.5, and 1.0 becomes 1.0. This puts meaning on the same 0-1 scale as the word score.

**Example: a logic bug with no error message.** "The cart total is off by one cent after a 15% discount" has no
error line, so there is no fingerprint, and a second report, "the receipt sum differs by a penny after percentage
discounts", shares almost no words with it. Only Stage 3 can link them. (This case is in the tests.)

---

## The merge

Each candidate gets one score:

1. **Same fingerprint:** score 1.0.
2. **Otherwise:** the stronger of the word score and the meaning score.
3. **Both words and meaning found it:** add 0.1. Two different signals agreeing is good evidence.
4. Cap it at 0.99, so 1.0 always means "same fingerprint".

Candidates are sorted by: fingerprint first, then score, then solved before open, then upvotes, then newest. This is
the same order search used before.

**Why not reciprocal-rank fusion?** Reciprocal-rank fusion merges two ranked lists by position only. It gives a good
order, but no number that means "same problem". The hook and the gate need that number: they treat a score of 0.5 or
more as a known fix. Our rule keeps the old meaning of `score`, so they keep working without changes.

---

## Stage 4: the AI check

**What it does.** Claude Haiku 5.5 reads the new problem and one candidate, and answers with JSON:

```json
{"same_problem": false, "fix_applies": false, "confidence": 0.9,
 "reason": "The new report is a local Mac Postgres service that was never started, while the existing report is a
            Docker container using localhost instead of the compose service name..."}
```

- `same_problem`: the same root cause, not only similar words.
- `fix_applies`: would the fix solve the problem in that environment? `null` when there is no fix to judge.
- `confidence`: from 0 to 1.

**It only runs where a wrong link costs something:** attaching a solution to an Open Question, and flagging a
duplicate Open Question. It never runs on `GET /api/search`, because the hook waits only 2 seconds. It reads at most
the **top 3** candidates, and only ones with a score of 0.3 or more. The 3 checks run at the same time.

**Example: the same error text, a different cause.** Both reports say
`psycopg2.OperationalError: connection to server at "localhost" (127.0.0.1), port 5432 failed: Connection refused`.
In post 4, the API runs in docker-compose and uses `localhost` instead of the database's service name. In the new
report, the person runs on a Mac with no Docker, and Postgres was never started. Stages 1-3 all say "match": same
fingerprint, shared words, and close meaning. We ran the real AI check on this pair. It answered
`same_problem: false` with confidence 0.9 (the reason above) for $0.000137. So the Docker fix is **not** attached.

**How a link is decided** (`decide()` in `linking.py`):

- With an AI answer: link only if it says `same_problem: true` with confidence 0.6 or more, and, for a solution, not
  `fix_applies: false`. A "no" wins even over a matching fingerprint.
- Without an AI answer (no key, timeout, error, odd reply): link only on a matching fingerprint or a score of 0.8 or
  more. This is a strict line on purpose, so a failure doesn't cause wrong links.

**Settings.** `model="claude-haiku-5-5"`, `max_tokens=1024`, `output_config={"effort": "low", "format": {"type":
"json_schema", ...}}` (structured JSON output), a 6-second timeout and `max_retries=0`. No `temperature`, `top_p`,
`top_k` or `thinking` settings are sent. Every check logs its cost.

---

## Why this order

Cheap and exact first, careful and slow last:

- Stages 1 and 2 are free and take milliseconds, so they always run.
- Stage 3 costs about a millionth of a dollar and a fraction of a second, so it runs on every search, but it is
  skipped if it is slow or the cache isn't warm yet.
- Stage 4 costs about a hundred times more than Stage 3 per candidate and takes seconds, so it reads only the top 3,
  and only when we are about to change data (attach a solution, merge a question).

The early stages cast a wide net. The AI check removes the false matches the net caught.

---

## Costs

Prices checked on 2026-10-25:

- OpenAI `text-embedding-3-small`: **$0.02 per 1M input tokens**, 1536 numbers by default, up to 8192 tokens in. Source:
  https://developers.openai.com/api/docs/pricing and https://developers.openai.com/api/docs/guides/embeddings
- Claude Haiku 5.5 (`claude-haiku-5-5`): **$0.10 per 1M input tokens, $0.50 per 1M output tokens** (prompts up to
  100K tokens). Source: Anthropic's model and pricing table (the same prices as `backend/team/config.py`).

| What | Tokens | Cost |
|---|---|---|
| Embed one post (when saved) | about 50-1,500 | $0.000001-$0.00003 |
| Embed one search query | about 20-1,000 | under $0.00002 (free when cached) |
| Warm-up of the real forum: 68 posts (every server start) | 5,774 (measured) | $0.000115, in 1.3-1.7 s |
| One AI check | about 1,000 in, 100-250 out | about $0.00014 (measured $0.000137) |
| Linking one new post (up to 3 checks) | | at most about $0.0005 |

So a restart with 10,000 posts would cost about 2-20 cents and take a minute or two, and 1,000 new posts with full
AI checks cost about 50 cents.

---

## The meaning cache (in memory)

**Nothing in the database.** Linking only reads `posts` (for `fingerprint`) and the `post_cards` view (title,
description, tags, solution). It never creates tables and never writes rows. No `vector` extension is needed.

**What happens on a restart.** The cache is empty again. The warm-up embeds every post once more (for the real forum:
68 posts, about 1.5 seconds, about $0.0001). During those seconds Stage 3 is skipped and search still answers from
Stages 1 and 2.

**Memory.** Each vector is stored as 1536 small numbers (`float32`), about 6 KB per post: 1,000 posts is about
6 MB. The cache holds at most 10,000 posts (`LINKING_MAX_CACHED_POSTS`); past that the oldest posts are left out.

**Several servers.** Each server process has its own cache and does its own warm-up. That is fine for the demo
(one process). With many workers, each one pays the small warm-up cost.

**Measure it yourself** (read-only on the database, writes nothing). From `backend/`:

```
.venv\Scripts\python backfill_embeddings.py --dry-run                      # count posts, estimate tokens (no OpenAI call)
.venv\Scripts\python backfill_embeddings.py                                # run one real warm-up: time and cost
.venv\Scripts\python backfill_embeddings.py --query "CUDA out of memory"   # ... then one search, timed
```

(The file kept its old name; it no longer backfills anything, because there is no table to fill.)

**Switches** (environment variables): `LINKING_EMBEDDINGS=0` turns Stage 3 off. `LINKING_AI_CHECK=0` turns Stage 4
off. `LINKING_QUERY_EMBED_TIMEOUT_S` (default 0.8) and `LINKING_CHECK_TIMEOUT_S` (default 6) change the time limits.
`LINKING_REFRESH_S` (default 600) sets how often the cache re-scans the posts. `LINKING_MAX_CACHED_POSTS` (default
10000) caps the cache.

---

## How it is wired into the backend

These are done on `hook-engine`. Each change is small and marked `LINKING`.

**Starting the warm-up.** `backend/main.py` starts the warm-up when the app starts, with one line after
`app = FastAPI(...)`: `app.router.on_startup.append(linking.warm_up)`. It returns at once; the work happens in a
background thread. Without `OPENAI_API_KEY` it does nothing. (If that line is missing, the first search starts the
warm-up instead, and Stage 3 joins a second or two later.)

**Search.** `GET /api/search` (`search()` in `backend/agent.py`) calls `linking.find_matches()`. It returns the same
fields as before, plus `stages` (for example `["words", "meaning"]`) and `stage_scores` (for example
`{"words": 0.21, "meaning": 0.72}`). `match` is still only `"fingerprint"` or `"text"`, where "text" now means
words, meaning, or both. The team's gate (`backend/team/gate.py`) calls the same function, so it gets linking too.

**Saving a post.** `POST /api/posts` (`create_post()` in `backend/main.py`) keeps the teammate's steps: the safety
check, the scrub, the forum and tag rules, and the text match. Linking runs first, and the teammate's matcher is
the fallback:

1. **A new solution may answer an Open Question.** If the post names an `open_question_id`, that question is filled,
   as before. If not, `linking.link_new_solution()` looks for the question (all four stages, with the AI check). If
   linking finds none, the teammate's `find_open_question()` runs as before: the same fingerprint, else a close
   text match (`best_text_match`) that shares a tag.
2. **A new Open Question may repeat an old one.** `linking.find_duplicate_question()` looks first, across forums. If
   it finds none, the teammate's checks run as before: the same fingerprint, else `best_match` in the same forum.
3. **The AI check's "no" counts for the fallback too.** Both link functions fill a set called `rejected` with the
   posts the AI check said are a different problem. The fallback never links one of those. Example: the
   "connection refused" case in Stage 4 has the same fingerprint, so the old fingerprint rule alone would link it.
4. **The news says how sure we are.** When linking chose the question, the asker is emailed "A possible answer
   ... not verified yet" (`main.py` and `accounts.py` send it). When the question was named by `open_question_id`,
   or found by the fallback, the email says it was solved. The answer has a new optional field, `linked_by`:
   `"ai_check"`, `"fallback"` or `null`. (`linking.notify_possible_answer()` still exists so old callers don't break,
   but it does nothing now: the old `agent_updates` table is gone.)
5. **Every new post is embedded** after it is saved: `background_tasks.add_task(linking.index_post, post_id)`. It
   runs after the response is sent, so saving stays fast. It adds the vector to the in-memory cache and never
   raises. Without `OPENAI_API_KEY` it does nothing, not even open a database connection.

**Notes:**

- The tag `general` is not used as a filter by the link functions. `/api` gives it to posts with no valid tag, and
  filtering by it would hide every other question.
- The "fill that question" step sets the question's status to `solved`. The email tells the asker it is only a
  Possible answer. A separate `possible_answer` status would need a change to the `posts` table, which is the team's
  call.
- Linking reads posts with its own query: `post_cards` joined with `posts.fingerprint`. The linking calls use
  savepoints, so if something is missing they return `None` and do not break the save.
- `plugin/mcp/server.py` and the hook need no change: they call `/api/search` and `/api/posts`.

---

## What proves a link is right

Similarity only finds candidates. It never proves a fix works. Proof is the problem's **check command** passing (the
command that failed, like `npm test`), as `PROJECT_PLAN.md` ("Open Questions and matching") says.

- A fix linked by similarity is a **Possible answer**. The asker is emailed that it is not verified yet: try it and
  run the check command.
- The asker's agent tries the fix and runs the check command. The hook sees whether it passed and reports it to
  `POST /api/outcomes`.
- So a link proves itself through real outcomes, not through the score. A link that keeps failing shows up as
  "worked 0 of 3 times".
- `decided_by` on a match says how it was linked: `"ai_check"` (Claude said yes) or `"fallback"` (no AI answer; a
  fingerprint or a very high score).

---

## Tests

`backend/tests/test_linking.py` uses fake OpenAI and Anthropic clients, so it makes no real API calls. It covers
each stage alone, the merge, the warm-up (batched, logged, only changed posts embedded again, deleted posts dropped,
retried after a failure), `index_post` adding and refreshing a vector, Stage 3 skipped when the cache isn't warm or
there is no key, a slow embedding skipped in time, the fingerprint read from `posts.fingerprint`, Stage 4 only on the
top 3 and its fallback on failure and timeout, the request settings (no `temperature` and so on), the link
decisions, the memory cap, thread safety, and that searching works on a read-only database connection.

The database tests need a throwaway plain Postgres (no extensions). Never point them at the real database.

```
docker run --rm -d --name bp-linking-mem-pg -p 55437:5432 -e POSTGRES_PASSWORD=test postgres:16
$env:TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55437/postgres"
backend\.venv\Scripts\python -m pytest backend/tests/test_linking.py -q
docker stop bp-linking-mem-pg
```

Without `TEST_DATABASE_URL`, the database tests are skipped and the rest still run.

---

## Limits and future ideas

**Limits:**

- The thresholds (0.55 and 0.75) come from a handful of demo posts. They should be checked again on real posts.
- After a restart, Stage 3 is off until the warm-up finishes (about 1.5 seconds for 68 posts; roughly a minute or
  two for 10,000). The first query call also opens the connection to OpenAI, which can take about 0.5 seconds.
- The cache lives in one process. Posts added outside `POST /api/posts` (a seed script, the SQL editor) show up in
  Stage 3 only after the next re-scan (every 10 minutes) or a restart.
- The comparison is plain Python. Past about 10,000 posts it gets slow (about 0.2 s), and the cache stops at
  10,000 posts. A real vector index would be the next step then.
- Without the AI check, a matching fingerprint is enough to link. Same error, different cause can then link wrongly.
  The Possible answer note and the outcome counts limit the harm.
- The AI check reads the first 3,000 characters of each report. Very long write-ups are cut.
- Each post has one embedding of its start (the first 6,000 characters). A post about two problems matches mostly on
  the first.
- Word matching only looks at the 500 newest posts (`linking.CANDIDATE_LIMIT`). Meaning search looks at every cached
  post.

**Future ideas:**

- Use the outcome counts in ranking: a fix that worked 9 of 10 times should beat a fix that never worked.
- Re-tune the thresholds with real data: log each AI check's answer next to its scores, then pick the lines that
  separate "yes" from "no".
- Past about 10,000 posts: store the vectors in the database with a vector index (pgvector), or use numpy.
- Offer `confirm=True` on search for callers that can wait, like the website.
- Run the Open Question's check command in a sandbox (a Docker container) to mark a link "Solved (verified)"
  automatically.
