"use client";

import { useMemo, useState } from "react";
import { fetchPost, stepsFor } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import type { Change, Filter, PostCard, PostDetail } from "@/lib/types";
import { JourneySteps } from "./JourneySteps";

const TABS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "solved", label: "Solved" },
  { id: "open", label: "Open questions" },
];

const HIGHLIGHT_MS = 10_000;

function Details({ detail }: { detail: PostDetail }) {
  return (
    <div className="mt-4 space-y-4 border-t border-line pt-4">
      <div>
        <h4 className="text-sm font-semibold">The problem</h4>
        <p className="mt-1 whitespace-pre-wrap break-words text-[15px] leading-relaxed text-muted">{detail.description}</p>
      </div>
      <div>
        <h4 className="text-sm font-semibold">The fix</h4>
        {detail.solution ? (
          <p className="mt-1 whitespace-pre-wrap break-words text-[15px] leading-relaxed">{detail.solution}</p>
        ) : (
          <p className="mt-1 text-[15px] text-muted">No confirmed fix yet.</p>
        )}
      </div>
    </div>
  );
}

function Card({ post, now, change }: { post: PostCard; now: number | null; change?: Change }) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<PostDetail | null>(null);
  const [failed, setFailed] = useState(false);

  const recent = change && now !== null && now - change.at < HIGHLIGHT_MS ? change.kind : null;

  async function toggle() {
    const next = !expanded;
    setExpanded(next);
    // Refetch while the post is open so a newly added fix shows up.
    if (next && (!detail || detail.is_open !== post.is_open)) {
      setFailed(false);
      try {
        setDetail(await fetchPost(post.id));
      } catch {
        setFailed(true);
      }
    }
  }

  const panelId = `post-${post.id}-details`;

  return (
    <li
      className={`rounded-lg border bg-surface transition-[box-shadow,border-color] duration-700 ${
        recent === "solved"
          ? "border-solved shadow-[0_0_0_3px_var(--solved-bg)]"
          : recent === "new"
            ? "border-accent"
            : "border-line"
      }`}
      style={recent === "new" ? { animation: "card-in 0.35s ease-out" } : undefined}
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={expanded}
        aria-controls={panelId}
        className="w-full rounded-lg p-4 text-left focus-visible:outline-2 focus-visible:outline-accent sm:p-5"
      >
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <h3 className="min-w-0 flex-1 text-base font-semibold leading-snug sm:text-lg">{post.headline}</h3>
          <div className="flex items-center gap-2">
            {recent === "solved" && <span className="text-sm font-semibold text-solved">Just solved</span>}
            {recent === "new" && <span className="text-sm font-semibold text-accent">New</span>}
            <JourneySteps steps={stepsFor(post)} />
          </div>
        </div>
        <p className="mt-2 text-[15px] leading-relaxed text-muted">{post.value}</p>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap gap-1.5">
            {post.tags.map((t) => (
              <span key={t} className="rounded bg-surface-2 px-2 py-0.5 font-mono text-xs text-muted">
                {t}
              </span>
            ))}
          </div>
          <div className="flex items-center gap-3 text-sm text-muted tabular-nums">
            <span>#{post.id}</span>
            {now !== null && <time dateTime={post.created_at}>{timeAgo(post.created_at, now)}</time>}
            <span aria-hidden="true">{expanded ? "▴" : "▾"}</span>
          </div>
        </div>
      </button>
      {expanded && (
        <div id={panelId} className="px-4 pb-4 sm:px-5 sm:pb-5">
          {detail ? (
            <Details detail={detail} />
          ) : failed ? (
            <p className="mt-2 border-t border-line pt-4 text-sm text-danger">Couldn&apos;t load this post. Try again.</p>
          ) : (
            <div className="mt-2 h-16 animate-pulse rounded bg-surface-2" />
          )}
        </div>
      )}
    </li>
  );
}

export function PostFeed({
  posts,
  now,
  changes,
}: {
  posts: PostCard[] | null;
  now: number | null;
  changes: Record<number, Change>;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");

  const counts = useMemo(() => {
    const open = posts?.filter((p) => p.is_open).length ?? 0;
    const total = posts?.length ?? 0;
    return { all: total, open, solved: total - open };
  }, [posts]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (posts ?? []).filter((p) => {
      if (filter === "solved" && p.is_open) return false;
      if (filter === "open" && !p.is_open) return false;
      if (!q) return true;
      return (
        p.headline.toLowerCase().includes(q) ||
        p.value.toLowerCase().includes(q) ||
        p.tags.some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [posts, filter, query]);

  return (
    <section aria-label="Problems">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="tablist" aria-label="Filter problems" className="flex gap-1 rounded-lg border border-line bg-surface p-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={filter === t.id}
              onClick={() => setFilter(t.id)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-accent ${
                filter === t.id ? "bg-surface-2 text-text" : "text-muted hover:text-text"
              }`}
            >
              {t.label} <span className="ml-1 tabular-nums text-muted">{counts[t.id]}</span>
            </button>
          ))}
        </div>
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search problems or paste an error message…"
          aria-label="Search problems"
          className="w-full rounded-lg border border-line bg-surface px-3 py-2 text-[15px] placeholder:text-muted focus:outline-2 focus:outline-accent sm:w-96"
        />
      </div>

      <div className="mt-3">
        {posts === null ? (
          <ul className="space-y-3" aria-busy="true">
            {[0, 1, 2].map((i) => (
              <li key={i} className="h-32 animate-pulse rounded-lg border border-line bg-surface-2" />
            ))}
          </ul>
        ) : visible.length === 0 ? (
          <div className="rounded-lg border border-dashed border-line bg-surface px-4 py-14 text-center text-[15px] text-muted">
            {posts.length === 0 ? "No problems posted yet." : "No problems match your search."}
          </div>
        ) : (
          <ul className="space-y-3">
            {visible.map((p) => (
              <Card key={p.id} post={p} now={now} change={changes[p.id]} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
