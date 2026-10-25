# Board room (website feature)

A live view of one specialist-team run: the gate, three researchers working in parallel, the chair's ranked
report, and the time and cost. It reads `GET /api/help/{run_id}` (polling every 1.5 s with `?since=`) and never
starts or changes a run. Plain-language guide: [docs/board-room.md](../../../docs/board-room.md).

| Route | What it shows |
| --- | --- |
| `/board-room` | A box to paste a run id (`tr_...`, or the whole `TEAM_STARTED: tr_...` line) |
| `/board-room/<run id>` | That run, live |
| `/board-room/demo` | A recorded run replayed from `lib/board-room/demo-run.json` (no backend needed) |

## Files

All of it is new; no existing file was changed.

```
frontend/app/board-room/            routes (this README, page.tsx, demo/page.tsx, [runId]/page.tsx)
frontend/components/board-room/     BoardRoom, ResearcherCard, ChairPanel, LiveBoardRoom, DemoBoardRoom, RunIdForm, parts
frontend/lib/board-room/            types, api (fetchHelp), useLiveRun (poller), useDemoRun (replay), derive, demo-run.json
docs/board-room.md                  the plain-language guide
```

It uses the site's existing `SiteHeader`, colors, fonts, `.btn` classes and the `wash` / `landing-rise`
animations from `app/globals.css`, without editing them. The API base URL is the same `NEXT_PUBLIC_API_URL`
(default `http://localhost:8000`) that `lib/api.ts` uses.

## Remove it

Delete these four paths, then rebuild:

```
frontend/app/board-room/
frontend/components/board-room/
frontend/lib/board-room/
docs/board-room.md
```

If someone added a nav tab for it in `frontend/components/SiteHeader.tsx`, remove that line too.
