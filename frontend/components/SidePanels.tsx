import { countTags, timeAgo } from "@/lib/format";
import type { PostCard } from "@/lib/types";

export function OpenQuestions({ posts, now }: { posts: PostCard[]; now: number | null }) {
  const open = posts.filter((p) => p.is_open).slice(0, 6);

  return (
    <section className="rounded-lg border border-line bg-surface p-4 sm:p-5" aria-label="Waiting for a fix">
      <h2 className="text-base font-semibold">Waiting for a fix</h2>
      <p className="mt-0.5 text-sm text-muted">Open questions nobody has solved yet</p>
      {open.length === 0 ? (
        <p className="mt-4 text-[15px] text-muted">No open questions.</p>
      ) : (
        <ul className="mt-3 divide-y divide-line">
          {open.map((p) => (
            <li key={p.id} className="py-3 first:pt-0 last:pb-0">
              <p className="text-[15px] leading-snug">{p.headline}</p>
              <p className="mt-1 text-sm text-muted tabular-nums">
                #{p.id}
                {now !== null && <> · {timeAgo(p.created_at, now)}</>}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function TopTags({ posts }: { posts: PostCard[] }) {
  const tags = countTags(posts, 8);
  const max = tags[0]?.count ?? 1;

  return (
    <section className="rounded-lg border border-line bg-surface p-4 sm:p-5" aria-label="Common tags">
      <h2 className="text-base font-semibold">Common tags</h2>
      {tags.length === 0 ? (
        <p className="mt-3 text-[15px] text-muted">No tags yet.</p>
      ) : (
        <ul className="mt-3 space-y-2.5">
          {tags.map(({ tag, count }) => (
            <li key={tag} className="flex items-center gap-3">
              <span className="w-28 shrink-0 truncate font-mono text-sm text-muted">{tag}</span>
              <span className="h-2 flex-1 overflow-hidden rounded-full bg-surface-2" aria-hidden="true">
                <span className="block h-full rounded-full bg-accent" style={{ width: `${(count / max) * 100}%` }} />
              </span>
              <span className="w-6 text-right text-sm tabular-nums text-muted">{count}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
