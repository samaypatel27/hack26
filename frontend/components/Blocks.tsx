/** Vote count shown in the list: big number, small label. Voting happens on the post page. */
export function VoteTally({ count }: { count: number }) {
  return (
    <div className="w-12 shrink-0 pt-0.5 text-center leading-none">
      <div className="text-[19px] font-bold tabular-nums">{count}</div>
      <div className="mt-1 font-mono text-[10.5px] text-faint">{count === 1 ? "vote" : "votes"}</div>
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
