"use client";

import { useMemo } from "react";
import {
  CATEGORY_LABEL,
  costSoFar,
  deriveChair,
  deriveSeat,
  formatElapsed,
  GATE_MODEL,
  SEATS,
} from "@/lib/board-room/derive";
import type { BoardState, TeamEvent } from "@/lib/board-room/types";
import { ChairPanel } from "./ChairPanel";
import { label, ModelTag, Pill } from "./parts";
import { ResearcherCard } from "./ResearcherCard";

const TARGET_S = 60;
const HARD_STOP_S = 120;

function stamp(e: TeamEvent | undefined): number | null {
  const raw = e?.t ?? e?.created_at;
  const ms = raw ? Date.parse(raw) : NaN;
  return Number.isFinite(ms) ? ms : null;
}

function StatusBadge({ state }: { state: BoardState }) {
  const word = state.message.split(/[:\s]/)[0] || "TEAM_WORKING";
  const map = {
    working: { tone: "live" as const, text: "Team working" },
    report: { tone: "done" as const, text: "Report ready" },
    no_answer: { tone: "idle" as const, text: "No confident answer" },
    failed: { tone: "bad" as const, text: "Run failed" },
  };
  const s = map[state.status] ?? map.working;
  return (
    <div className="flex flex-col items-start gap-1 sm:items-end">
      <Pill tone={s.tone}>
        <span className="text-[12.5px] font-semibold">{s.text}</span>
      </Pill>
      <span className="font-mono text-[11px] text-faint">{word}</span>
    </div>
  );
}

function firstLine(text: string | undefined, max = 220): string {
  const line = (text ?? "").split("\n").find((l) => l.trim()) ?? "";
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function Stages({ state, researchersDone, chairPhase }: { state: BoardState; researchersDone: number; chairPhase: string }) {
  const finished = state.status !== "working";
  const steps = [
    { name: state.source === "rerun" ? "Re-run" : "Gate", done: true, live: false },
    { name: `Researchers ${researchersDone}/3`, done: researchersDone === 3 || chairPhase !== "waiting" || finished, live: !finished && researchersDone < 3 && chairPhase === "waiting" },
    { name: "Chair", done: finished || chairPhase === "done", live: !finished && chairPhase === "thinking" },
    { name: "Report", done: finished, live: false },
  ];
  return (
    <ol className="flex shrink-0 flex-wrap items-center gap-x-1.5 gap-y-1 font-mono text-[11.5px] lg:flex-nowrap" aria-label="Progress">
      {steps.map((s, i) => (
        <li key={s.name} className="flex items-center gap-1.5 whitespace-nowrap">
          {i > 0 && <span className="text-rule-strong">→</span>}
          <span className={s.live ? "font-semibold text-accent" : s.done ? "text-ink" : "text-faint"}>
            {s.done && !s.live ? "✓ " : s.live ? "● " : "○ "}
            {s.name}
          </span>
        </li>
      ))}
    </ol>
  );
}

/** The whole board: header, gate line, three researcher cards, the chair, and a footer with time and cost. */
export function BoardRoom({ state, banner, mode = "live" }: { state: BoardState; banner?: React.ReactNode; mode?: "live" | "demo" }) {
  const { events } = state;
  const seats = useMemo(() => SEATS.map((s) => deriveSeat(s, events, state)), [events, state]);
  const chair = useMemo(() => deriveChair(events, state), [events, state]);
  const cost = costSoFar(state, seats, chair);

  const start = stamp(events.find((e) => e.member === "runner" && e.type === "started"));
  const chairStart = stamp(events.find((e) => e.member === "chair" && e.type === "chair_started"));
  const chairSinceS = start !== null && chairStart !== null ? Math.max(0, state.elapsedS - (chairStart - start) / 1000) : null;
  const researchersDone = seats.filter((s) => s.phase !== "waiting" && s.phase !== "working").length;
  const category = state.report?.category ?? seats.find((s) => s.category)?.category ?? null;
  const runnerModels = events.find((e) => e.member === "runner" && e.type === "started")?.data?.models as Record<string, string> | undefined;
  const gateModel = runnerModels?.gate ?? GATE_MODEL;
  const cf = state.caseFile;
  const working = state.status === "working";
  const over = state.elapsedS > TARGET_S && working;

  return (
    <>
      <div className="mx-auto w-full max-w-[1200px] px-4 pb-6 sm:px-6">
        {banner}

        {/* Header: the problem and the run's status */}
        <section className="mt-5 flex flex-col gap-4 border-b-2 border-ink pb-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 flex-1">
            <p className={label}>
              <span className="text-accent">board room</span> · specialist team · <span className="normal-case tracking-normal">{state.runId}</span>
            </p>
            <h1 className="mt-1 text-[24px] font-bold leading-tight tracking-tight sm:text-[30px]">
              {cf?.goal ? firstLine(cf.goal, 180) : "A stuck agent asked the team for help"}
            </h1>
            {cf?.symptom && (
              <p className="mt-2 max-w-[900px] break-words rounded-[2px] border border-rule border-l-[3px] border-l-accent bg-card px-3 py-1.5 font-mono text-[12.5px] leading-snug text-ink">
                {firstLine(cf.symptom)}
              </p>
            )}
            <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[11.5px] text-ink-soft">
              {category && <span>category: <span className="text-ink">{CATEGORY_LABEL[category] ?? category}</span></span>}
              {cf?.tried && cf.tried.length > 0 && <span>agent tried first: <span className="text-ink">{cf.tried.length} fixes</span></span>}
              {cf?.check_command && <span>check: <code className="text-ink">{cf.check_command}</code></span>}
            </div>
            {cf?.environment && <p className="mt-1 line-clamp-2 text-[13px] italic text-faint">{cf.environment}</p>}
          </div>
          <StatusBadge state={state} />
        </section>

        {/* Gate */}
        <div className="flex flex-col gap-2 border-b border-rule py-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-[14px]">
            <span className={label}>
              <span className="text-accent">00</span> · gate
            </span>
            <ModelTag id={gateModel} />
            <span className="text-ink-soft">
              {state.source === "rerun"
                ? `Cold-case re-run${state.openQuestionId ? ` of Open Question #${state.openQuestionId}` : ""}: no gate, straight to the team.`
                : "No solved post on the forum matched, and the problem looks external, so it sent the case to the team."}
            </span>
          </p>
          <Stages state={state} researchersDone={researchersDone} chairPhase={chair.phase} />
        </div>

        {/* Researchers */}
        <div className="mt-4 grid gap-4 lg:grid-cols-3">
          {seats.map((v, i) => (
            <ResearcherCard key={v.seat} view={v} index={i} />
          ))}
        </div>

        <ChairPanel state={state} chair={chair} seats={seats} chairSinceS={chairSinceS} />
      </div>

      {/* Footer: time and cost, always in view */}
      <footer className="sticky bottom-0 z-10 border-t border-rule-strong bg-paper/95 backdrop-blur supports-[backdrop-filter]:bg-paper/85">
        <div className="mx-auto flex w-full max-w-[1200px] flex-wrap items-center justify-between gap-x-6 gap-y-1 px-4 py-2.5 font-mono text-[12px] sm:px-6">
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
            <span>
              <span className="text-faint">elapsed </span>
              <span className={`text-[15px] font-semibold tabular-nums ${over ? "text-accent" : "text-ink"}`}>{formatElapsed(state.elapsedS)}</span>
              <span className="text-faint"> / target {TARGET_S}s · stop {HARD_STOP_S}s</span>
            </span>
            <span>
              <span className="text-faint">{cost.final ? "cost " : "cost so far "}</span>
              <span className="text-[15px] font-semibold tabular-nums text-ink">${cost.usd.toFixed(3)}</span>
            </span>
            <span className="hidden text-faint md:inline">
              {seats.reduce((n, s) => n + s.searches, 0)} searches · {seats.reduce((n, s) => n + s.pagesRead, 0)} pages · {seats.reduce((n, s) => n + s.sources.length, 0)} sources
            </span>
          </div>
          <span className="text-faint" role="status" aria-live="polite">
            {state.connection === "offline"
              ? <span className="text-accent">can&apos;t reach the server, retrying</span>
              : mode === "demo"
                ? working
                  ? "replaying a recorded run"
                  : "replay finished"
                : working
                  ? "live · updates every 1.5s"
                  : "finished · no more updates"}
          </span>
        </div>
      </footer>
    </>
  );
}
