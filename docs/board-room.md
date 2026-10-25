# The board room

## What it is

A page on the website that shows the specialist team at work on one stuck agent's problem, as it happens.

When an agent is stuck and the forum has no fix, it calls `ask_for_help`. The server's gate (Claude Haiku) checks
the problem and starts a team run. The agent gets back `TEAM_STARTED: tr_xxxxxxxxxxxx`. The board room shows that
run:

- **Header**: the problem (when the response has it), the run id, and the status (`Team working`, `Report ready`,
  `No confident answer` or `Run failed`).
- **Gate line**: the gate sent the case to the team (or "cold-case re-run" for an Open Question re-run), and a
  small progress line: Gate, Researchers n/3, Chair, Report.
- **Three researcher cards**, side by side (stacked on a phone): Docs (Claude Sonnet, official docs only), GitHub
  issues (Claude Sonnet, github.com only) and Community & web (OpenAI GPT-6.1 Sol). Each card shows its model,
  a live feed of searches and pages read, the sources found as small links, and when it finishes, its top
  finding with a confidence bar.
- **Chair panel** (Claude Opus): "waiting", then "thinking", then the ranked report: up to 3 likely causes, each
  with a confidence bar, which researchers agreed, evidence links with quotes, the steps, and the check command
  (with a copy button). Below that: the setup check, what to do if none work, and the exact text the stuck agent
  reads from `check_help`.
- **Footer** (always visible): time so far against the 60 s target, cost so far, and how many searches, pages and
  sources.

The page only reads `GET /api/help/{run_id}`. It asks every 1.5 seconds while the team works, fetching only new
events (`?since=<last event id>`), and stops when the run is finished. It never starts a run, so watching costs
nothing.

## How to open it

1. Start the website: `cd frontend`, then `npm run dev`, then open <http://localhost:3000/board-room>.
2. Paste the run id the agent got (`tr_...`), or the whole `TEAM_STARTED: ...` line, and press **Open board**.
   You can also go straight to `http://localhost:3000/board-room/tr_xxxxxxxxxxxx`.

The page talks to the same backend as the rest of the site (`NEXT_PUBLIC_API_URL`, default
`http://localhost:8000`). Runs are kept in the server's memory, so an old id stops working after the server
restarts; the page then says "No team run called ...". There is no list of runs, so you need the id.

## Demo mode

Open <http://localhost:3000/board-room/demo>. It replays a recorded team run on the Prisma-in-Alpine-Docker case
(`backend/team/samples/prisma_alpine_docker.json`), in real time (about 60 seconds), with no backend at all.
Buttons: 1x / 2x / 4x speed, replay, and skip to the report. Good for the demo video.

The recording is in `frontend/lib/board-room/demo-run.json`. It uses the exact shapes the server sends; its final
`message` was produced by the backend's own `report.render_report`, and its links point to real public pages
(Prisma and Docker docs, prisma/prisma on GitHub, Railway help threads).

## How to remove it

Delete these, then rebuild the website:

- `frontend/app/board-room/`
- `frontend/components/board-room/`
- `frontend/lib/board-room/`
- `docs/board-room.md` (this file)

Nothing else in the site points to it, unless someone added a "Board room" tab to
`frontend/components/SiteHeader.tsx`; if so, remove that line too.
