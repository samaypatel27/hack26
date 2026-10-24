import { timeAgo } from "@/lib/format";
import type { PostCard } from "@/lib/types";

function Kpi({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-line bg-surface px-4 py-3">
      <dt className="text-sm font-medium text-muted">{label}</dt>
      <dd className={`mt-1 text-3xl font-semibold tabular-nums tracking-tight ${tone ?? ""}`}>{value}</dd>
      {hint && <p className="mt-0.5 truncate text-sm text-muted">{hint}</p>}
    </div>
  );
}

export function KpiStrip({ posts, now }: { posts: PostCard[] | null; now: number | null }) {
  if (!posts) {
    return (
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-busy="true">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-[100px] animate-pulse rounded-lg border border-line bg-surface-2" />
        ))}
      </div>
    );
  }

  const total = posts.length;
  const solved = posts.filter((p) => !p.is_open).length;
  const open = total - solved;
  const share = total === 0 ? "—" : `${Math.round((solved / total) * 100)}%`;
  const latest = posts[0];

  return (
    <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Kpi label="Solved" value={String(solved)} hint="Problems with a confirmed fix" tone="text-solved" />
      <Kpi label="Open questions" value={String(open)} hint="Nobody has solved these yet" tone="text-open" />
      <Kpi label="Share solved" value={share} hint={`${solved} of ${total} problems`} />
      <Kpi
        label="Latest activity"
        value={latest && now ? timeAgo(latest.created_at, now) : "—"}
        hint={latest ? latest.headline : "Nothing posted yet"}
      />
    </dl>
  );
}
