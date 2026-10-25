"use client";

import { hostOf, pct, SEAT_INFO, shortUrl, splitCaution, type ChairView, type SeatView } from "@/lib/board-room/derive";
import type { BoardState, Hypothesis, Seat } from "@/lib/board-room/types";
import { card, ConfidenceBar, CopyButton, Dot, ExternalLink, label, ModelTag, Pill, riseIn } from "./parts";

const SOURCE_LABEL: Record<string, string> = { docs: "docs", github: "github", community: "web", stackoverflow: "stack overflow", web: "web" };

function seatTitle(seat: string): string {
  return SEAT_INFO[seat as Seat]?.title ?? seat;
}

function AgreedBy({ seats }: { seats: string[] }) {
  if (!seats.length) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span className="font-mono text-[11px] text-faint">{seats.length >= 3 ? "all three found it:" : "found by"}</span>
      {seats.map((s) => (
        <span key={s} className="rounded-[2px] border border-ink bg-card px-1.5 py-px font-mono text-[11px] text-ink">
          {seatTitle(s)}
        </span>
      ))}
    </span>
  );
}

function HypothesisBody({ h }: { h: Hypothesis }) {
  return (
    <div className="space-y-4 pt-3">
      {h.evidence.length > 0 && (
        <div>
          <p className={label}>evidence</p>
          <ul className="mt-1.5 space-y-2">
            {h.evidence.map((ev, i) => (
              <li key={`${ev.url}-${i}`} className="flex gap-2 text-[14px] leading-snug">
                <span className="mt-[2px] h-fit shrink-0 rounded-[2px] bg-paper-deep px-1.5 py-px font-mono text-[10.5px] uppercase tracking-wider text-ink-soft">
                  {SOURCE_LABEL[ev.source ?? ""] ?? ev.source ?? hostOf(ev.url)}
                </span>
                <span className="min-w-0">
                  <ExternalLink href={ev.url} className="break-all font-mono text-[12px] text-ink underline decoration-rule-strong underline-offset-2 hover:decoration-ink">
                    {shortUrl(ev.url)}
                  </ExternalLink>
                  {ev.seen === false && <span className="ml-1.5 font-mono text-[11px] text-accent">(unconfirmed link)</span>}
                  {ev.quote && <span className="mt-0.5 block italic text-ink-soft">“{ev.quote}”</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {h.steps.length > 0 && (
        <div>
          <p className={label}>steps</p>
          <ol className="mt-1.5 list-decimal space-y-1 pl-5 text-[14.5px] leading-snug marker:font-mono marker:text-[12px] marker:text-faint">
            {h.steps.map((raw, i) => {
              const { caution, text } = splitCaution(raw);
              return (
                <li key={i} className="pl-1">
                  {caution && (
                    <span className="mr-1.5 rounded-[2px] border border-accent px-1 py-px font-mono text-[10.5px] text-accent">caution: {caution}</span>
                  )}
                  {text}
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {(h.check?.command || h.check?.description) && (
        <div>
          <p className={label}>check</p>
          {h.check.command && (
            <div className="mt-1.5 flex items-center gap-2 rounded-[2px] border border-rule border-l-[3px] border-l-rule-strong bg-[#dfeaec] px-3 py-2">
              <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre font-mono text-[13px] text-ink">
                <span className="select-none text-faint">$ </span>
                {h.check.command}
              </code>
              <CopyButton text={h.check.command} />
            </div>
          )}
          {h.check.description && <p className="mt-1 text-[13.5px] italic text-ink-soft">Passes when: {h.check.description}</p>}
        </div>
      )}
    </div>
  );
}

function HypothesisCard({ h, minConfidence, open }: { h: Hypothesis; minConfidence: number; open: boolean }) {
  const head = (
    <div className="flex gap-3 sm:gap-4">
      <span className={`shrink-0 font-mono text-[28px] font-semibold leading-none sm:text-[34px] ${h.rank === 1 ? "text-accent" : "text-rule-strong"}`}>
        {h.rank}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[16.5px] font-semibold leading-snug sm:text-[17.5px]">{h.cause}</p>
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5">
          <ConfidenceBar value={h.confidence} bar={minConfidence} />
          <AgreedBy seats={h.agreed_by} />
        </div>
      </div>
    </div>
  );
  return (
    <li style={{ ...riseIn, animationDelay: `${(h.rank - 1) * 120}ms` }} className="border-t border-rule first:border-t-0">
      <details open={open} className="group py-4 [&_summary::-webkit-details-marker]:hidden">
        <summary className="cursor-pointer list-none">
          {head}
          <span className="mt-2 block pl-10 font-mono text-[11.5px] text-faint group-open:hidden sm:pl-12">show evidence, steps and check ▾</span>
        </summary>
        <div className="pl-10 sm:pl-12">
          <HypothesisBody h={h} />
        </div>
      </details>
    </li>
  );
}

function Thinking({ chair, seats, sinceS }: { chair: ChairView; seats: SeatView[]; sinceS: number | null }) {
  const done = seats.filter((s) => s.phase === "finished").length;
  return (
    <div style={riseIn} className="py-5">
      <p className="flex items-center gap-2 text-[16px]">
        <Dot tone="live" />
        <span>
          Reading {done} researcher {done === 1 ? "report" : "reports"}, merging the same causes and writing exact steps
          <span className="inline-block w-6 animate-pulse">…</span>
        </span>
      </p>
      <p className="mt-1 pl-4 font-mono text-[11.5px] text-faint">
        {chair.text}
        {sinceS !== null && ` · thinking for ${Math.floor(sinceS)}s`}
      </p>
      <div className="mt-4 space-y-3" aria-hidden="true">
        {[0.86, 0.6, 0.35].map((w, i) => (
          <div key={i} className="flex gap-4" style={{ opacity: 1 - i * 0.25 }}>
            <div className="h-7 w-5 animate-pulse rounded-[2px] bg-paper-deep" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3.5 animate-pulse rounded-[2px] bg-paper-deep" style={{ width: `${w * 100}%`, animationDelay: `${i * 150}ms` }} />
              <div className="h-2 w-36 animate-pulse rounded-full bg-paper-deep" style={{ animationDelay: `${i * 150}ms` }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function ChairPanel({
  state,
  chair,
  seats,
  chairSinceS,
}: {
  state: BoardState;
  chair: ChairView;
  seats: SeatView[];
  chairSinceS: number | null;
}) {
  const rep = state.report;
  const minConfidence = rep?.min_confidence ?? 0.4;
  const hyps = rep?.hypotheses ?? [];
  const removed = (rep?.flagged ?? []).filter((f) => f.action === "removed");
  const finished = seats.filter((s) => s.phase !== "waiting" && s.phase !== "working").length;

  let pill;
  if (state.status === "working") {
    pill = chair.phase === "thinking" ? <Pill tone="live">thinking</Pill> : chair.phase === "done" ? <Pill tone="done">done</Pill> : <Pill tone="idle">waiting</Pill>;
  } else if (chair.phase === "failed" || rep?.chair_fallback) {
    pill = <Pill tone="bad">{chair.phase === "failed" ? "failed: fallback merge" : "skipped: fallback merge"}</Pill>;
  } else {
    pill = <Pill tone="done">done{chair.elapsedS !== null ? ` · ${chair.elapsedS.toFixed(1)}s` : ""}</Pill>;
  }

  return (
    <section className={`${card} mt-4`} aria-label="Chair">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-rule px-4 py-3.5 sm:px-5">
        <div className="min-w-0">
          <p className={label}>
            <span className="text-accent">04</span> · chair
          </p>
          <h2 className="mt-0.5 text-[21px] font-bold leading-tight tracking-tight">The ranked report</h2>
          <ModelTag id={chair.model} className="mt-1" />
          <p className="mt-0.5 text-[13px] italic text-ink-soft">Reads only the researchers&apos; findings (it doesn&apos;t browse), merges matching causes, ranks at most three.</p>
        </div>
        {pill}
      </header>

      <div className="px-4 sm:px-5">
        {state.status === "working" && chair.phase !== "thinking" && (
          <p className="flex items-center gap-2 py-6 text-[15px] italic text-ink-soft">
            <Dot tone="idle" />
            Waiting for the researchers: {finished} of 3 done. The chair starts when all three are done or out of time.
          </p>
        )}
        {state.status === "working" && chair.phase === "thinking" && <Thinking chair={chair} seats={seats} sinceS={chairSinceS} />}

        {state.status === "failed" && (
          <div style={riseIn} className="py-5">
            <p className="text-[16px] font-semibold">The run could not finish.</p>
            <p className="mt-1 break-words font-mono text-[12.5px] text-accent">{state.error ?? rep?.error ?? "unknown error"}</p>
            <p className="mt-2 text-[14px] italic text-ink-soft">The agent keeps debugging on its own and can ask again later, or post an Open Question.</p>
          </div>
        )}

        {(state.status === "report" || state.status === "no_answer") && (
          <div style={riseIn}>
            {state.status === "no_answer" && (
              <p className="border-b border-rule py-4 text-[15px]">
                <strong>No confident answer.</strong>{" "}
                <span className="text-ink-soft">
                  No cause reached {pct(minConfidence)}.{hyps.length ? " Low-confidence guesses, if the agent wants to try them:" : ""}
                </span>
              </p>
            )}
            {hyps.length > 0 ? (
              <ol>
                {hyps.map((h) => (
                  <HypothesisCard key={h.rank} h={h} minConfidence={minConfidence} open={h.rank === 1} />
                ))}
              </ol>
            ) : (
              state.status === "report" && <p className="py-5 italic text-ink-soft">The report has no hypotheses.</p>
            )}

            {(rep?.environment_check || rep?.if_none_work || removed.length > 0 || (rep?.notes ?? []).length > 0) && (
              <dl className="grid gap-3 border-t border-rule py-4 text-[14px] leading-snug sm:grid-cols-2">
                {rep?.environment_check && (
                  <div>
                    <dt className={label}>setup check</dt>
                    <dd className="mt-1 text-ink-soft">{rep.environment_check}</dd>
                  </div>
                )}
                {rep?.if_none_work && (
                  <div>
                    <dt className={label}>if none work</dt>
                    <dd className="mt-1 text-ink-soft">{rep.if_none_work}</dd>
                  </div>
                )}
                {removed.length > 0 && (
                  <div>
                    <dt className={label}>safety filter</dt>
                    <dd className="mt-1 text-ink-soft">
                      Removed {removed.length} step{removed.length === 1 ? "" : "s"}: {[...new Set(removed.map((f) => f.reason))].join("; ")}.
                    </dd>
                  </div>
                )}
                {(rep?.notes ?? []).length > 0 && (
                  <div>
                    <dt className={label}>notes</dt>
                    <dd className="mt-1 space-y-0.5 text-ink-soft">
                      {rep!.notes!.map((n, i) => (
                        <p key={i}>{n}</p>
                      ))}
                    </dd>
                  </div>
                )}
              </dl>
            )}
            <p className="border-t border-rule py-3 text-[13px] italic text-faint">
              Possible answer: untested suggestions from web research. The stuck agent tries them one at a time on the real project; it only
              counts as solved once the check passes.
            </p>
          </div>
        )}
      </div>

      {state.message && (
        <details className="border-t border-rule px-4 py-3 sm:px-5">
          <summary className="cursor-pointer font-mono text-[12px] text-ink-soft hover:text-ink">
            What the agent reads from check_help <span className="text-faint">({state.message.split(/[:\s]/)[0]})</span>
          </summary>
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-[2px] border border-rule bg-paper p-3 font-mono text-[12px] leading-relaxed text-ink">
            {state.message}
          </pre>
        </details>
      )}
    </section>
  );
}
