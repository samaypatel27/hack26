/** Explicit agent activity, displayed without browser write controls. */
export function UsageTally({ useful, visited }: { useful: number; visited: number }) {
  return (
    <div className="w-14 shrink-0 space-y-2 pt-0.5 text-center leading-none">
      <div>
        <div className="text-[19px] font-bold tabular-nums">{useful}</div>
        <div className="mt-1 font-mono text-[10.5px] text-faint">useful</div>
      </div>
      <div className="font-mono text-[10.5px] leading-snug text-faint"><span className="tabular-nums">{visited}</span> visited</div>
    </div>
  );
}

/** Only unanswered posts say anything about status; a fixed post is the normal case. */
export function Unanswered() {
  return <span className="font-mono text-[12px] font-medium text-accent">no fix yet</span>;
}

export function ForumLabel({ name }: { name: string }) {
  return (
    <span className="rounded-[2px] border border-rule-strong bg-card px-1.5 py-px font-mono text-[11.5px] text-ink-soft">
      {name}
    </span>
  );
}
