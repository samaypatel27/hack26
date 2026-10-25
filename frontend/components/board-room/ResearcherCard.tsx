"use client";

import { useEffect, useRef } from "react";
import { chipLabel, pct, SEAT_INFO, type SeatView } from "@/lib/board-room/derive";
import { card, ConfidenceBar, ExternalLink, label, ModelTag, Pill, riseIn, washIn } from "./parts";

const KIND_LABEL = { search: "search", read: "read", switch: "switch", error: "error", note: "note" } as const;
const MAX_CHIPS = 9;

function phasePill(v: SeatView) {
  switch (v.phase) {
    case "waiting":
      return <Pill tone="idle">waiting</Pill>;
    case "working": {
      const last = v.feed[v.feed.length - 1];
      return <Pill tone="live">{last?.kind === "read" ? "reading" : last?.kind === "search" ? "searching" : "starting"}</Pill>;
    }
    case "finished":
      return <Pill tone="done">done{v.elapsedS !== null ? ` · ${v.elapsedS.toFixed(1)}s` : ""}</Pill>;
    default:
      return <Pill tone="bad">{v.phase}</Pill>;
  }
}

export function ResearcherCard({ view, index }: { view: SeatView; index: number }) {
  const info = SEAT_INFO[view.seat];
  const feedRef = useRef<HTMLOListElement>(null);
  const top = view.findings[0];

  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [view.feed.length]);

  return (
    <article className={`${card} flex flex-col`} aria-label={`${info.title} researcher`}>
      <header className="border-b border-rule px-4 pb-3 pt-3.5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className={label}>
              <span className="text-accent">0{index + 1}</span> · researcher
            </p>
            <h3 className="mt-0.5 text-[19px] font-bold leading-tight tracking-tight">{info.title}</h3>
          </div>
          {phasePill(view)}
        </div>
        <ModelTag id={view.model} className="mt-1.5" />
        <p className="mt-0.5 text-[13px] italic leading-snug text-ink-soft">{info.scope}</p>
      </header>

      <div className="flex flex-1 flex-col px-4 py-3">
        <p className="font-mono text-[11.5px] text-faint">
          <span className="text-ink">{view.searches}</span> {view.searches === 1 ? "search" : "searches"} ·{" "}
          <span className="text-ink">{view.pagesRead}</span> {view.pagesRead === 1 ? "page" : "pages"} read ·{" "}
          <span className="text-ink">{view.sources.length}</span> {view.sources.length === 1 ? "source" : "sources"}
        </p>

        <ol
          ref={feedRef}
          className="mt-2 h-36 space-y-0.5 overflow-y-auto rounded-[2px] border border-rule bg-paper/60 p-1.5 sm:h-40"
          aria-live="polite"
          aria-label="Activity"
        >
          {view.feed.length === 0 && (
            <li className="px-1.5 py-1 font-mono text-[11.5px] italic text-faint">
              {view.phase === "waiting" ? "not started yet" : "thinking about where to look…"}
            </li>
          )}
          {view.feed.map((item) => (
            <li key={item.id} style={washIn} className="flex gap-2 rounded-[2px] px-1.5 py-0.5 text-[12.5px] leading-snug">
              <span
                className={`w-12 shrink-0 font-mono text-[10.5px] uppercase tracking-wider ${
                  item.kind === "error" || item.kind === "switch" ? "text-accent" : "text-faint"
                } pt-[2px]`}
              >
                {KIND_LABEL[item.kind]}
              </span>
              {item.url ? (
                <ExternalLink href={item.url} className="min-w-0 break-all font-mono text-[11.5px] text-ink-soft hover:text-ink hover:underline">
                  {item.text}
                </ExternalLink>
              ) : (
                <span className="min-w-0 break-words text-ink">{item.kind === "search" && !/^["“]/.test(item.text) ? `“${item.text}”` : item.text}</span>
              )}
            </li>
          ))}
        </ol>

        {view.sources.length > 0 && (
          <ul className="mt-2.5 flex flex-wrap gap-1.5" aria-label="Sources found">
            {view.sources.slice(0, MAX_CHIPS).map((s) => (
              <li key={s.url} style={riseIn}>
                <ExternalLink
                  href={s.url}
                  title={s.title ? `${s.title}\n${s.url}` : s.url}
                  className="inline-block max-w-full truncate sm:max-w-[15rem] rounded-[2px] border border-rule-strong bg-card px-1.5 py-px font-mono text-[11px] text-ink-soft transition hover:border-ink hover:text-ink"
                >
                  {chipLabel(s.url)}
                </ExternalLink>
              </li>
            ))}
            {view.sources.length > MAX_CHIPS && (
              <li className="px-1 py-px font-mono text-[11px] text-faint">+{view.sources.length - MAX_CHIPS} more</li>
            )}
          </ul>
        )}

        <div className="mt-auto pt-3">
          {view.phase === "finished" && (
            <div style={riseIn} className="rounded-[2px] border-l-[3px] border-rescue bg-accent-soft/40 px-3 py-2.5">
              {top ? (
                <>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className={label}>top finding</p>
                    <ConfidenceBar value={top.confidence ?? 0} />
                  </div>
                  <p className="mt-1 line-clamp-4 text-[14px] leading-snug" title={top.cause}>
                    {top.cause}
                  </p>
                  <p className="mt-1.5 font-mono text-[11px] text-faint">
                    {view.findings.length} {view.findings.length === 1 ? "finding" : "findings"}
                    {view.findings.length > 1 && ` (next: ${pct(view.findings[1].confidence ?? 0)})`}
                    {view.costUsd !== null && ` · $${view.costUsd.toFixed(3)}`}
                  </p>
                </>
              ) : (
                <p className="text-[14px] italic text-ink-soft">Finished with no findings.</p>
              )}
            </div>
          )}
          {(view.phase === "failed" || view.phase === "timeout" || view.phase === "skipped") && (
            <div style={riseIn} className="rounded-[2px] border-l-[3px] border-accent bg-paper px-3 py-2.5">
              <p className={label}>{view.phase === "timeout" ? "out of time" : view.phase}</p>
              <p className="mt-1 break-words text-[13.5px] leading-snug text-ink-soft">{view.error}</p>
              <p className="mt-1 text-[12.5px] italic text-faint">The chair works with the other researchers.</p>
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
