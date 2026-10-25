"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchForums, fetchPosts } from "@/lib/api";
import { plural } from "@/lib/format";
import type { Change, PostCard, Sort, StatusFilter } from "@/lib/types";
import { usePoll } from "@/lib/usePoll";
import { PostRow, PostRowSkeleton } from "./PostRow";
import { SentenceFilter, type FilterChange } from "./SentenceFilter";
import { SiteHeader } from "./SiteHeader";

const SORTS: { id: Sort; label: string }[] = [
  { id: "top", label: "Top" },
  { id: "newest", label: "Newest" },
];

type View = { status: StatusFilter | null; forum: string | null; tags: string[]; sort: Sort };

export function PostsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  // The whole filter lives in the URL, so a view can be shared and the back button works.
  const statusParam = params.get("status");
  const view: View = {
    status: statusParam === "solved" || statusParam === "open" || statusParam === "all" ? statusParam : null,
    forum: params.get("forum"),
    tags: useMemo(() => (params.get("tags") ?? "").split(",").filter(Boolean), [params]),
    sort: params.get("sort") === "top" ? "top" : "newest",
  };
  const status: StatusFilter = view.status ?? "all";

  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 300);
    return () => clearTimeout(t);
  }, [query]);

  const hrefFor = useCallback(
    (next: Partial<View>) => {
      const v = { ...view, ...next };
      const p = new URLSearchParams();
      if (v.status) p.set("status", v.status);
      if (v.forum) p.set("forum", v.forum);
      if (v.forum && v.tags.length) p.set("tags", v.tags.join(","));
      if (v.sort === "top") p.set("sort", "top");
      const qs = p.toString();
      return qs ? `${pathname}?${qs}` : pathname;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [view.status, view.forum, view.tags, view.sort, pathname],
  );

  const go = (href: string) => router.push(href, { scroll: false });
  const onFilterChange = (c: FilterChange) =>
    go(hrefFor({ status: c.status ?? view.status ?? "all", ...("forum" in c ? { forum: c.forum } : {}), ...("tags" in c ? { tags: c.tags } : {}) }));

  // Highlight posts that appear, or flip from open to solved, between two polls of the same view.
  const [changes, setChanges] = useState<Record<number, Change>>({});
  const previous = useRef<Map<number, boolean> | null>(null);
  const onPosts = useCallback((data: PostCard[], sameKey: boolean) => {
    const prev = sameKey ? previous.current : null;
    if (prev) {
      const at = Date.now();
      const fresh: Record<number, Change> = {};
      for (const p of data) {
        if (!prev.has(p.id)) fresh[p.id] = { kind: "new", at };
        else if (prev.get(p.id) && !p.is_open) fresh[p.id] = { kind: "solved", at };
      }
      if (Object.keys(fresh).length) setChanges((c) => ({ ...c, ...fresh }));
    }
    previous.current = new Map(data.map((p) => [p.id, p.is_open]));
  }, []);

  const key = `${status}|${view.forum}|${view.tags.join(",")}|${view.sort}|${q}`;
  const posts = usePoll(key, () => fetchPosts({ status, forum: view.forum, tags: view.tags, sort: view.sort, q }), onPosts);
  const forums = usePoll("forums", fetchForums);

  const list = posts.data;
  const current = view.forum ? forums.data?.find((f) => f.slug === view.forum) : null;

  let note: string | null = null;
  if (list === null) {
    if (posts.connection === "offline") note = "Can't reach the server. It'll keep trying every few seconds.";
  } else if (list.length === 0 && !posts.loading) {
    if (q) note = `Nothing matches "${q}" here. Try the exact error text.`;
    else if (view.forum || view.tags.length || status !== "all") note = "Nothing like that yet. Quiet week.";
    else note = "Nothing's broken. Yet.";
  }

  return (
    <div className="mx-auto w-full max-w-[1120px] px-4 pb-16 sm:px-6">
      <SiteHeader active="forums" connection={posts.connection} />

      <main className="mt-8">
        <SentenceFilter
          status={status}
          started={view.status !== null}
          forum={view.forum}
          tags={view.tags}
          forums={forums.data}
          onChange={onFilterChange}
          onReset={() => go(pathname)}
        />
        {current?.description && <p className="mt-1 italic text-ink-soft">{current.description}</p>}

        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search these, or paste the error you're staring at"
          aria-label="Search posts"
          className="mt-6 w-full rounded-[2px] border border-rule-strong bg-card px-3 py-2 font-mono text-[13.5px] text-ink outline-none placeholder:text-faint focus:border-ink"
        />

        <div className="mt-4 flex items-end justify-between gap-3 border-b border-ink pb-1.5">
          <nav aria-label="Sort posts" className="flex gap-5 text-[15px]">
            {SORTS.map((s) => {
              const active = view.sort === s.id;
              return (
                <Link
                  key={s.id}
                  href={hrefFor({ sort: s.id })}
                  scroll={false}
                  aria-current={active ? "true" : undefined}
                  className={`underline-offset-[7px] ${
                    active ? "font-semibold text-accent underline decoration-2" : "text-ink-soft hover:text-ink"
                  }`}
                >
                  {s.label}
                </Link>
              );
            })}
          </nav>
          {list && (
            <span className="font-mono text-[12px] tabular-nums text-faint">
              {plural(list.length, "post")}
              {q && " found"}
            </span>
          )}
        </div>

        {note ? (
          <p className="border-b border-rule py-12 text-center italic text-ink-soft">{note}</p>
        ) : list === null ? (
          <ul aria-busy="true" aria-label="Loading posts">
            {Array.from({ length: 7 }, (_, i) => (
              <PostRowSkeleton key={i} i={i} />
            ))}
          </ul>
        ) : (
          // While a new filter loads, the previous results stay, slightly dimmed, so nothing flashes.
          <ul className={posts.loading ? "opacity-50 transition-opacity" : "transition-opacity"} aria-busy={posts.loading}>
            {list.map((p) => (
              <PostRow
                key={p.id}
                post={p}
                now={posts.now}
                change={changes[p.id]}
                forumHref={hrefFor({ status: view.status ?? "all", forum: p.forum, tags: [] })}
                tagHref={(t) => hrefFor({ status: view.status ?? "all", forum: p.forum, tags: [t] })}
              />
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
