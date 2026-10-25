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
