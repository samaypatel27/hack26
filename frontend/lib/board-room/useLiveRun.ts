"use client";

import { useEffect, useState } from "react";
import { fetchHelp, HelpApiError } from "./api";
import type { BoardState, HelpResponse, TeamEvent } from "./types";

const POLL_MS = 1500;
const RETRY_MS = 3000;
const PAGE_LIMIT = 500; // backend/team/store.py EVENT_LIMIT: a full page means more events are waiting

type Snapshot = {
  response: Omit<HelpResponse, "events"> | null;
  events: TeamEvent[];
  receivedAt: number;
  connection: BoardState["connection"];
};

function mergeEvents(old: TeamEvent[], fresh: TeamEvent[]): TeamEvent[] {
  if (!fresh.length) return old;
  const seen = new Set(old.map((e) => e.id));
  const added = fresh.filter((e) => !seen.has(e.id));
  return added.length ? [...old, ...added].sort((a, b) => a.id - b.id) : old;
}

/**
 * Poll GET /api/help/{runId} every 1.5 s while the team works, asking only for new events (`?since=`).
 * Stops once the run is finished. Read-only: it never starts or changes a run.
 */
export function useLiveRun(runId: string): BoardState {
  const [snap, setSnap] = useState<Snapshot>({ response: null, events: [], receivedAt: 0, connection: "connecting" });
  const [now, setNow] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let since = 0;
    const abort = new AbortController();

    async function tick() {
      try {
        const r = await fetchHelp(runId, since, abort.signal);
        if (cancelled) return;
        const events = Array.isArray(r.events) ? r.events : [];
        for (const e of events) since = Math.max(since, e.id);
        const { events: _ignored, ...rest } = r;
        void _ignored;
        const stamp = Date.now();
        setSnap((prev) => ({ response: rest, events: mergeEvents(prev.events, events), receivedAt: stamp, connection: "live" }));
        setNow(stamp);
        if (r.status === "working") timer = setTimeout(tick, POLL_MS);
        else if (events.length >= PAGE_LIMIT) timer = setTimeout(tick, 50);
      } catch (e) {
        if (cancelled || abort.signal.aborted) return;
        if (e instanceof HelpApiError && e.status === 404) {
          setSnap((prev) => ({ ...prev, connection: "missing" }));
          return;
        }
        setSnap((prev) => ({ ...prev, connection: "offline" }));
        timer = setTimeout(tick, RETRY_MS);
      }
    }

    tick();
    return () => {
      cancelled = true;
      abort.abort();
      if (timer) clearTimeout(timer);
    };
  }, [runId]);

  const working = !snap.response || snap.response.status === "working";
  useEffect(() => {
    if (!working) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [working]);

  const r = snap.response;
  const serverElapsed = r?.elapsed_s ?? 0;
  const elapsedS =
    r && r.status === "working" && snap.receivedAt && now
      ? serverElapsed + Math.max(0, (now - snap.receivedAt) / 1000)
      : (r?.report?.timings?.total_s ?? serverElapsed);

  return {
    runId,
    status: r?.status ?? "working",
    message: r?.message ?? "",
    events: snap.events,
    report: r?.report ?? null,
    elapsedS,
    connection: snap.connection,
    caseFile: r?.case_file ?? null,
    error: r?.report?.error ?? null,
    source: r?.source ?? null,
    openQuestionId: r?.open_question_id ?? null,
  };
}
