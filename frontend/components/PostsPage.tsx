"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
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

  // Until a status is picked or a search is submitted, only the sentence and search bar show, centered.
  const [searched, setSearched] = useState(false);
  const revealed = view.status !== null || searched;

  // FLIP: remember where the two controls sit before the reveal, then slide them from there into place.
  const sentenceRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLFormElement>(null);
  const before = useRef<{ sentence: DOMRect; search: DOMRect } | null>(null);
  const captureBefore = () => {
    if (revealed || !sentenceRef.current || !searchRef.current) return;
    before.current = { sentence: sentenceRef.current.getBoundingClientRect(), search: searchRef.current.getBoundingClientRect() };
  };
  useLayoutEffect(() => {
    const first = before.current;
    before.current = null;
    if (!revealed || !first || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const pairs = [
      [sentenceRef.current, first.sentence],
      [searchRef.current, first.search],
    ] as const;
    for (const [el, from] of pairs) {
      if (!el) continue;
      const to = el.getBoundingClientRect();
      el.animate([{ transform: `translate(${from.left - to.left}px, ${from.top - to.top}px)` }, { transform: "none" }], {
        duration: 600,
        easing: "cubic-bezier(0.2, 0.7, 0.2, 1)",
      });
    }
  }, [revealed]);

  const go = (href: string) => router.push(href, { scroll: false });
  const onFilterChange = (c: FilterChange) => {
    captureBefore();
    go(hrefFor({ status: c.status ?? view.status ?? "all", ...("forum" in c ? { forum: c.forum } : {}), ...("tags" in c ? { tags: c.tags } : {}) }));
  };
  const onSearch = (e: React.FormEvent) => {
    e.preventDefault();
    captureBefore();
    setQ(query.trim());
    setSearched(true);
  };

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
    <>
      <SiteHeader active="forums" connection={posts.connection} />
      <div className="mx-auto w-full max-w-[1120px] px-4 pb-16 sm:px-6">
        <main className={revealed ? "mt-8" : "flex min-h-[calc(100svh-8rem)] flex-col justify-center pb-16"}>
          <div ref={sentenceRef} className={`w-fit max-w-full ${revealed ? "" : "mx-auto text-center"}`}>
            <SentenceFilter
              large={!revealed}
              status={status}
              started={view.status !== null}
              forum={view.forum}
              tags={view.tags}
              forums={forums.data}
              onChange={onFilterChange}
              onReset={() => go(pathname)}
            />
          </div>
          {revealed && current?.description && <p className="forums-reveal mt-1 italic text-ink-soft">{current.description}</p>}

          <form
            ref={searchRef}
            onSubmit={onSearch}
            role="search"
            className={`mx-auto flex w-full transition-[max-width,margin] duration-[600ms] ease-[cubic-bezier(0.2,0.7,0.2,1)] ${
              revealed ? "mt-6 max-w-full" : "mt-8 max-w-[44rem]"
            }`}
          >
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search these, or paste the error you're staring at"
              aria-label="Search posts"
              className={`min-w-0 flex-1 rounded-l-[2px] border border-r-0 border-rule-strong bg-card px-3 font-mono text-ink outline-none transition-[padding,font-size] duration-[600ms] ease-[cubic-bezier(0.2,0.7,0.2,1)] placeholder:text-faint focus:border-ink ${
                revealed ? "py-2 text-[13.5px]" : "py-3.5 text-[16px]"
              }`}
            />
            <button
              type="submit"
              className="rounded-r-[2px] border border-[#ff4f00] bg-[#ff4f00] px-4 font-mono text-[13px] font-semibold text-ink transition hover:border-[#ff6a26] hover:bg-[#ff6a26] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink"
            >
              Enter ↵
            </button>
          </form>

          {revealed && (
            <div className="forums-reveal">
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
                        title={s.id === "top" ? "Most useful, then most visited, then newest" : "Newest posts first"}
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
            </div>
          )}
        </main>
      </div>
    </>
  );
}
