"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ApiError, fetchPost } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { usePoll } from "@/lib/usePoll";
import { ForumLabel, Unanswered } from "./Blocks";
import { Markdown } from "./Markdown";
import { SiteHeader } from "./SiteHeader";
import { VoteColumn } from "./VoteColumn";

const forumHref = (slug: string) => `/forums?status=all&forum=${encodeURIComponent(slug)}`;

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <p className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-faint">{children}</p>;
}

export function PostView() {
  const { id: rawId } = useParams<{ id: string }>();
  const id = Number(rawId);
  // Polls, so a solution added while you're reading shows up without a refresh.
  const { data: post, connection, now, error } = usePoll(`post:${id}`, () => fetchPost(id));

  const missing = !Number.isInteger(id) || (error instanceof ApiError && error.status === 404);
  const asked = post && now !== null ? timeAgo(post.created_at, now) : "";

  return (
    <div className="mx-auto w-full max-w-[860px] px-4 pb-16 sm:px-6">
      <SiteHeader active="forums" connection={missing ? undefined : connection} />

      <Link
        href={post ? forumHref(post.forum) : "/forums"}
        className="mt-5 inline-block font-mono text-[12.5px] text-ink-soft hover:text-ink hover:underline"
      >
        ← {post ? `Back to ${post.forum_name}` : "All forums"}
      </Link>

      {missing ? (
        <p className="mt-10 border-y border-rule py-12 text-center italic text-ink-soft">
          No post here. It may never have existed.
        </p>
      ) : !post ? (
        connection === "offline" ? (
          <p className="mt-10 border-y border-rule py-12 text-center italic text-ink-soft">
            Can&apos;t reach the server. It&apos;ll keep trying every few seconds.
          </p>
        ) : (
          <div className="mt-4 space-y-3" aria-busy="true" aria-label="Loading post">
            <div className="h-3 w-40 rounded-[2px] bg-paper-deep" />
            <div className="h-8 w-4/5 rounded-[2px] bg-paper-deep" />
            <div className="h-24 w-full rounded-[2px] bg-paper-deep/70" />
          </div>
        )
      ) : (
        <article className="mt-3">
          <h1 className="text-[28px] font-bold leading-[1.2] tracking-tight sm:text-[32px]">{post.headline}</h1>
          <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[12.5px] text-faint">
            <Link href={forumHref(post.forum)} className="hover:opacity-80">
              <ForumLabel name={post.forum_name} />
            </Link>
            <span>
              <span className="text-ink-soft">{post.author}</span> · {asked}
            </span>
            {post.is_open && <Unanswered />}
          </p>

          {/* The question */}
          <div className="mt-4 flex gap-3 border-t-2 border-ink pt-5 sm:gap-5">
            <VoteColumn postId={post.id} target="post" count={post.post_votes} />
            <div className="min-w-0 flex-1">
              {post.body ? <Markdown text={post.body} /> : <p className="italic text-ink-soft">No details given.</p>}
            </div>
          </div>

          {/* Why it matters */}
          <section className="mt-6 border-l-[3px] border-ink bg-paper-deep/60 px-4 py-3">
            <SectionLabel>Why this matters</SectionLabel>
            <p className="mt-1 text-[16.5px] italic leading-relaxed">{post.value}</p>
          </section>

          {/* The solution */}
          <section className="mt-9">
            <h2 className="border-b border-ink pb-1.5 text-[22px] font-bold tracking-tight">Solution</h2>
            {post.solution ? (
              <div className="mt-5 flex gap-3 sm:gap-5">
                <VoteColumn postId={post.id} target="solution" count={post.solution_votes} />
                <div className="min-w-0 flex-1">
                  <Markdown text={post.solution} />
                </div>
              </div>
            ) : (
              <p className="mt-4 italic text-ink-soft">
                Nobody has solved this yet. When someone does, the fix shows up here.
              </p>
            )}
          </section>

          {post.tags.length > 0 && (
            <footer className="mt-9 flex flex-wrap items-center gap-1.5 border-t border-rule pt-3">
              <span className="mr-1 font-mono text-[11px] uppercase tracking-[0.14em] text-faint">Tagged</span>
              {post.tags.map((t) => (
                <Link
                  key={t}
                  href={`/forums?status=all&forum=${encodeURIComponent(post.forum)}&tags=${encodeURIComponent(t)}`}
                  className="rounded-[2px] bg-paper-deep px-1.5 py-px font-mono text-[12px] text-ink-soft hover:bg-highlight"
                >
                  {t}
                </Link>
              ))}
            </footer>
          )}
        </article>
      )}
    </div>
  );
}
