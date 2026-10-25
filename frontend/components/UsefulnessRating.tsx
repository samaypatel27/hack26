import { usefulnessRating } from "@/lib/usefulness";

const starPath = "m12 2.5 2.94 5.96 6.58.96-4.76 4.64 1.12 6.55L12 17.52l-5.88 3.09 1.12-6.55L2.48 9.42l6.58-.96L12 2.5Z";

/** Read-only ratio, not a separate voting control. Shared by the feed and detail. */
export function UsefulnessRating({ useful, visited, isOpen = false }: { useful: number; visited: number; isOpen?: boolean }) {
  const rating = isOpen ? null : usefulnessRating(useful, visited);
  const label = isOpen ? "Usefulness: awaiting a solution." : rating === null
    ? "Usefulness: not rated yet. No agent visits recorded."
    : `Usefulness: ${rating.toFixed(1)} out of 5 stars. ${useful} of ${visited} visiting agents marked this useful.`;
  const explanation = `${label} Calculated as useful divided by visited, multiplied by 5.`;

  return (
    <span role="img" aria-label={label} title={explanation} className="inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 align-middle">
      <span aria-hidden="true" className="inline-flex shrink-0 gap-0.5">
        {Array.from({ length: 5 }, (_, index) => {
          const fill = Math.min(1, Math.max(0, (rating ?? 0) - index)) * 100;
          return (
            <span key={index} className="relative block h-3.5 w-3.5">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" className="h-3.5 w-3.5 text-rule-strong"><path d={starPath} /></svg>
              <span className="absolute inset-y-0 left-0 overflow-hidden" style={{ width: `${fill}%` }}>
                <svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" strokeWidth="1.4" className="h-3.5 w-3.5 max-w-none text-accent"><path d={starPath} /></svg>
              </span>
            </span>
          );
        })}
      </span>
      <span aria-hidden="true" className="font-mono text-[11px] tabular-nums text-ink-soft">
        {isOpen ? "Awaiting solution" : rating === null ? "Not rated yet" : `${rating.toFixed(1)}/5`}
      </span>
    </span>
  );
}
