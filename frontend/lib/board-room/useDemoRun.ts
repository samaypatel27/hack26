"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import fixture from "./demo-run.json";
import type { BoardState, CaseSummary, RunStatus, TeamEvent, TeamReport } from "./types";

type DemoEvent = TeamEvent & { at: number };
type Fixture = {
  run_id: string;
  case_file: CaseSummary;
  events: DemoEvent[];
  final: { status: RunStatus; elapsed_s: number; report: TeamReport; message: string };
};

const DEMO = fixture as unknown as Fixture;
const END_AT = Math.max(DEMO.final.elapsed_s, ...DEMO.events.map((e) => e.at));
const TARGET_S = 60;

/** The same TEAM_WORKING text the backend writes (backend/team/report.py render_working). */
function renderWorking(runId: string, events: TeamEvent[], elapsed: number): string {
  const latest: Record<string, string> = {};
  for (const e of events) {
    const m = e.member;
    if (!["docs", "issues", "community", "chair"].includes(m)) continue;
    if (e.type === "searching") latest[m] = `searching "${(e.text || "").slice(0, 80)}"`;
    else if (e.type === "fetching") latest[m] = "reading a page";
    else if (e.type === "finished" || e.type === "chair_done") latest[m] = "done";
    else if (["failed", "timeout", "skipped"].includes(e.type)) latest[m] = e.type;
    else if ((e.type === "started" || e.type === "chair_started") && !latest[m]) latest[m] = "started";
  }
  const parts = ["docs", "issues", "community", "chair"].filter((m) => latest[m]).map((m) => `${m}: ${latest[m]}`);
  const left = elapsed < TARGET_S ? Math.max(5, Math.floor(TARGET_S - elapsed)) : 20;
  return (
    `TEAM_WORKING: run ${runId}, ${Math.floor(elapsed)}s in. ${parts.join("; ") || "starting"}.\n` +
    `Keep working on the problem yourself and call check_help again in about ${left}s.`
  );
}

export const DEMO_SPEEDS = [1, 2, 4] as const;
export type DemoSpeed = (typeof DEMO_SPEEDS)[number];

/**
 * Plays the recorded run in demo-run.json as if it were live: events appear at their recorded times
 * (scaled by `speed`), and the report arrives at the end. No network.
 */
export function useDemoRun(initialSpeed: DemoSpeed = 1) {
  const [speed, setSpeed] = useState<DemoSpeed>(initialSpeed);
  // Playback position in run seconds. `segment` restarts the clock after a reset or a skip.
  const [t, setT] = useState(0);
  const [segment, setSegment] = useState(0);
  const position = useRef(0);

  useEffect(() => {
    const startedAt = Date.now();
    const base = position.current;
    if (base >= END_AT) return;
    const id = setInterval(() => {
      const next = Math.min(END_AT, base + ((Date.now() - startedAt) / 1000) * speed);
      position.current = next;
      setT(next);
      if (next >= END_AT) clearInterval(id);
    }, 100);
    return () => clearInterval(id);
  }, [speed, segment]);

  const jump = useCallback((to: number) => {
    position.current = to;
    setT(to);
    setSegment((n) => n + 1);
  }, []);
  const restart = useCallback(() => jump(0), [jump]);
  const skipToEnd = useCallback(() => jump(END_AT), [jump]);

  const state = useMemo<BoardState>(() => {
    const base = Date.parse("2026-10-25T15:04:00Z");
    const events: TeamEvent[] = DEMO.events
      .filter((e) => e.at <= t)
      .map(({ at, ...e }) => ({ ...e, t: new Date(base + at * 1000).toISOString() }));
    const done = t >= END_AT;
    return {
      runId: DEMO.run_id,
      status: done ? DEMO.final.status : "working",
      message: done ? DEMO.final.message : renderWorking(DEMO.run_id, events, t),
      events,
      report: done ? DEMO.final.report : null,
      elapsedS: done ? DEMO.final.elapsed_s : t,
      connection: "live",
      caseFile: DEMO.case_file,
      error: null,
      source: "help",
    };
  }, [t]);

  return { state, speed, setSpeed, restart, skipToEnd, progress: t / END_AT, done: t >= END_AT };
}
