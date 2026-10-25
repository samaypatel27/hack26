"use client";

import Link from "next/link";
import { SiteHeader } from "@/components/SiteHeader";
import { DEMO_SPEEDS, useDemoRun } from "@/lib/board-room/useDemoRun";
import { BoardRoom } from "./BoardRoom";

/** /board-room/demo: replays the recorded run in lib/board-room/demo-run.json. No backend needed. */
export function DemoBoardRoom() {
  const { state, speed, setSpeed, restart, skipToEnd, progress, done } = useDemoRun(1);

  const banner = (
    <div className="mt-4 flex flex-col gap-2 rounded-[2px] border border-dashed border-rule-strong bg-card/70 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
      <p className="text-[13.5px] leading-snug text-ink-soft">
        <span className="mr-2 rounded-[2px] bg-ink px-1.5 py-px font-mono text-[10.5px] font-semibold uppercase tracking-wider text-paper">demo</span>
        A recorded team run on the Prisma-in-Alpine-Docker case, replayed in real time. No server needed.{" "}
        <Link href="/board-room" className="whitespace-nowrap text-ink underline underline-offset-4">
          Open a live run
        </Link>
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <div className="hidden h-1 w-24 overflow-hidden rounded-full bg-paper-deep sm:block" aria-hidden="true">
          <div className="h-full bg-rescue" style={{ width: `${Math.round(progress * 100)}%` }} />
        </div>
        <div className="flex" role="group" aria-label="Playback speed">
          {DEMO_SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              aria-pressed={speed === s}
              onClick={() => setSpeed(s)}
              className={`btn btn-sm -ml-px first:ml-0 ${speed === s ? "border-ink bg-paper-deep" : ""}`}
            >
              {s}×
            </button>
          ))}
        </div>
        <button type="button" className="btn btn-sm" onClick={restart}>
          ↺ replay
        </button>
        <button type="button" className="btn btn-sm" onClick={skipToEnd} disabled={done}>
          skip to report
        </button>
      </div>
    </div>
  );

  return (
    <>
      <SiteHeader />
      <BoardRoom state={state} banner={banner} mode="demo" />
    </>
  );
}
