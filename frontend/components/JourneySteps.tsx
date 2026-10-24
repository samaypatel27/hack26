import type { Step, StepId } from "@/lib/types";

const LABEL: Record<StepId, string> = {
  spotted: "Problem spotted",
  checked: "Checked earlier fixes",
  specialists: "Specialists investigating",
  confirmed: "Fix confirmed",
  open: "Open question",
};

const TONE: Record<StepId, string> = {
  spotted: "bg-surface-2 text-muted",
  checked: "bg-surface-2 text-muted",
  specialists: "bg-surface-2 text-muted",
  confirmed: "bg-solved-bg text-solved",
  open: "bg-open-bg text-open",
};

/**
 * Where a problem is in its journey: spotted, checked earlier fixes, specialists, then a confirmed
 * fix or an open question. Renders whatever steps it is given, in order; today that is only the
 * final step (see stepsFor in lib/api.ts).
 */
export function JourneySteps({ steps }: { steps: Step[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-1.5" aria-label="Progress">
      {steps.map((s, i) => (
        <li key={s.id} className="flex items-center gap-1.5">
          {i > 0 && <span className="h-px w-3 bg-line" aria-hidden="true" />}
          <span className={`whitespace-nowrap rounded-full px-2.5 py-0.5 text-sm font-medium ${TONE[s.id]}`}>
            {LABEL[s.id]}
          </span>
        </li>
      ))}
    </ol>
  );
}
