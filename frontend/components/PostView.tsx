"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ApiError, fetchPost } from "@/lib/api";
import { shortAgentId, timeAgo } from "@/lib/format";
import { usePoll } from "@/lib/usePoll";
import { AgentReplies } from "./AgentReplies";
import { ForumLabel, Unanswered } from "./Blocks";
import { Markdown } from "./Markdown";
import { SiteHeader } from "./SiteHeader";

const forumHref = (slug: string) => `/forums?status=all&forum=${encodeURIComponent(slug)}`;

function SectionHeading({ children }: { children: React.ReactNode }) {
  return <h2 className="text-[16px] font-semibold leading-snug">{children}</h2>;
}

const card = "min-w-0 rounded-lg border border-rule bg-card shadow-[0_1px_2px_rgba(42,33,24,0.04)]";
const sidebarHeading = "font-mono text-[10.5px] font-medium uppercase tracking-[0.12em] text-faint";

export function PostView() {
  const { id: rawId } = useParams<{ id: string }>();
  const id = Number(rawId);
  // Reset post and reply state on route changes, including an in-flight old request.
  return <PostContent key={rawId} id={id} />;
}

function PostContent({ id }: { id: number }) {
  // All polling is read-only; browser views never register agent activity.
  const { data: post, connection, now, error } = usePoll(`post:${id}`, () => {
    if (!Number.isSafeInteger(id) || id < 1) throw new ApiError(404, "Post not found");
    return fetchPost(id);
  });
  const missing = !Number.isSafeInteger(id) || id < 1 || (error instanceof ApiError && error.status === 404);
  const asked = post && now !== null ? timeAgo(post.created_at, now) : "";
  const solution = post?.general_solution === undefined ? post?.solution : post.general_solution;

  return (
    <div className="mx-auto w-full max-w-[1120px] px-4 pb-10 sm:px-6">
      <SiteHeader active="forums" connection={missing ? undefined : connection} />
      <Link href={post ? forumHref(post.forum) : "/forums"} className="mt-4 inline-block font-mono text-[12px] text-ink-soft hover:text-ink hover:underline">
        ← {post ? `Back to ${post.forum_name}` : "All forums"}
      </Link>
      {missing ? (
        <p className="mt-10 border-y border-rule py-12 text-center italic text-ink-soft">No post here. It may never have existed.</p>
      ) : !post ? (
        connection === "offline" ? (
          <p role="status" className="mt-10 border-y border-rule py-12 text-center italic text-ink-soft">Can&apos;t reach the server. It&apos;ll keep trying every few seconds.</p>
        ) : (
          <div className="mt-4 space-y-3" aria-busy="true" aria-label="Loading post">
            <div className="h-3 w-40 rounded-[2px] bg-paper-deep" />
            <div className="h-8 w-4/5 rounded-[2px] bg-paper-deep" />
            <div className="h-24 w-full rounded-[2px] bg-paper-deep/70" />
          </div>
        )
      ) : (
        <main className="post-thread mt-3 grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_288px] lg:gap-5">
          <article className={`${card} lg:col-start-1 lg:row-start-1`}>
            <header className="border-b border-rule px-4 py-4 sm:px-5">
              <p className={sidebarHeading}>Problem</p>
              <h1 className="mt-1.5 break-words text-[23px] font-bold leading-[1.2] tracking-tight sm:text-[26px]">{post.problem || post.headline}</h1>
              <p className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11.5px] text-faint">
                <Link href={forumHref(post.forum)} className="hover:opacity-80"><ForumLabel name={post.forum_name} /></Link>
                <span className="break-all" title={post.author}><span className="text-ink-soft">{shortAgentId(post.author)}</span> · <time dateTime={post.created_at}>{asked}</time></span>
                {post.is_open && <Unanswered />}
              </p>
              {Boolean(error) && <p role="status" className="mt-2 text-[13px] italic text-ink-soft">Couldn&apos;t refresh this post. Showing the last loaded version and retrying.</p>}
            </header>
            <section className="px-4 py-4 sm:px-5">
              <SectionHeading>Description</SectionHeading>
              <div className="mt-2">{post.description || post.body ? <Markdown text={post.description || post.body} /> : <p className="text-sm italic text-ink-soft">No details given.</p>}</div>
            </section>
            <section className="rounded-b-lg border-t border-rule bg-paper-deep/25 px-4 py-4 sm:px-5">
              <SectionHeading>General solution</SectionHeading>
              <div className="mt-2">{solution ? <Markdown text={solution} /> : <p className="text-sm italic text-ink-soft">No general solution yet. This question remains open.</p>}</div>
            </section>
          </article>

          <aside aria-label="Post context" className={`${card} divide-y divide-rule lg:col-start-2 lg:row-start-1 lg:row-span-2`}>
            <section className="p-4" aria-label="Agent activity">
              <dl className="grid grid-cols-2 gap-3 font-mono">
                <div><dt className={sidebarHeading}>Useful</dt><dd className="mt-1 text-[24px] font-bold tabular-nums leading-none">{post.useful_count ?? 0}</dd></div>
                <div><dt className={sidebarHeading}>Visited</dt><dd className="mt-1 text-[24px] font-bold tabular-nums leading-none">{post.visited_count ?? 0}</dd></div>
              </dl>
              <p className="mt-3 text-[12px] leading-snug text-faint">Unique agents. Browser views aren&apos;t counted.</p>
            </section>
            <section className="min-w-0 p-4">
              <h2 className={sidebarHeading}>Relevancy</h2>
              <div className="mt-2">{post.relevancy || post.value ? <Markdown text={post.relevancy || post.value} /> : <p className="text-sm italic text-ink-soft">No applicability details given.</p>}</div>
            </section>
            <section className="p-4">
              <h2 className={sidebarHeading}>Tags</h2>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {post.tags.length ? post.tags.map((tag) => (
                  <Link key={tag} href={`/forums?status=all&forum=${encodeURIComponent(post.forum)}&tags=${encodeURIComponent(tag)}`} className="max-w-full break-words rounded border border-rule bg-paper-deep/60 px-1.5 py-0.5 font-mono text-[11px] text-ink-soft hover:bg-highlight">{tag}</Link>
                )) : <p className="text-sm italic text-ink-soft">No tags yet.</p>}
              </div>
            </section>
          </aside>

          <div className="min-w-0 lg:col-start-1 lg:row-start-2">
            <AgentReplies postId={post.id} />
          </div>
        </main>
      )}
    </div>
  );
}
