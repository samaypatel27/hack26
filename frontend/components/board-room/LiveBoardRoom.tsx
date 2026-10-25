"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { SiteHeader } from "@/components/SiteHeader";
import { cleanRunId } from "@/lib/board-room/api";
import { useLiveRun } from "@/lib/board-room/useLiveRun";
import { BoardRoom } from "./BoardRoom";
import { RunIdForm } from "./RunIdForm";

export function LiveBoardRoom() {
  const { runId: raw } = useParams<{ runId: string }>();
  const runId = cleanRunId(decodeURIComponent(raw ?? ""));
  // A new id gets fresh state (and stops the old poller).
  return <LiveRun key={runId} runId={runId} />;
}

function LiveRun({ runId }: { runId: string }) {
  const state = useLiveRun(runId);
  const back = (
    <Link href="/board-room" className="mt-4 inline-block font-mono text-[12px] text-ink-soft hover:text-ink hover:underline">
      ← Board room
    </Link>
  );

  if (state.connection === "missing") {
    return (
      <>
        <SiteHeader />
        <div className="mx-auto w-full max-w-[760px] px-4 pb-12 sm:px-6">
          {back}
          <div className="mt-8 border-y border-rule py-10">
            <h1 className="text-[24px] font-bold leading-tight">No team run called {runId}</h1>
            <p className="mt-2 text-[15.5px] text-ink-soft">
              Check the id the agent got (<code className="font-mono text-[13px]">TEAM_STARTED: tr_…</code>). Team runs are kept in the server&apos;s
              memory, so they disappear when the server restarts.
            </p>
            <div className="mt-6">
              <RunIdForm initial={runId} />
            </div>
            <p className="mt-6 font-mono text-[12.5px]">
              <Link href="/board-room/demo" className="text-accent underline underline-offset-4">
                Watch the demo replay instead →
              </Link>
            </p>
          </div>
        </div>
      </>
    );
  }

  if (state.connection !== "live" && state.events.length === 0 && !state.report) {
    return (
      <>
        <SiteHeader connection={state.connection === "offline" ? "offline" : undefined} />
        <div className="mx-auto w-full max-w-[1200px] px-4 pb-12 sm:px-6">
          {back}
          {state.connection === "offline" ? (
            <p role="status" className="mt-10 border-y border-rule py-12 text-center italic text-ink-soft">
              Can&apos;t reach the server. Trying again every few seconds.{" "}
              <Link href="/board-room/demo" className="not-italic text-accent underline underline-offset-4">
                Demo replay
              </Link>
            </p>
          ) : (
            <div className="mt-6 space-y-3" aria-busy="true" aria-label="Loading team run">
              <div className="h-3 w-48 rounded-[2px] bg-paper-deep" />
              <div className="h-8 w-3/4 rounded-[2px] bg-paper-deep" />
              <div className="grid gap-4 pt-4 lg:grid-cols-3">
                {[0, 1, 2].map((i) => (
                  <div key={i} className="h-72 animate-pulse rounded-lg bg-paper-deep/70" />
                ))}
              </div>
            </div>
          )}
        </div>
      </>
    );
  }

  return (
    <>
      <SiteHeader connection={state.connection === "offline" ? "offline" : undefined} />
      <BoardRoom state={state} banner={back} mode="live" />
    </>
  );
}
