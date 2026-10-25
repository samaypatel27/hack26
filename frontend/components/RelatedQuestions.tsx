"use client";

import Link from "next/link";
import { fetchRelatedPosts } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

export function RelatedQuestions({ postId }: { postId: number }) {
  const related = usePoll(`related:${postId}`, () => fetchRelatedPosts(postId));
  const posts = related.loading ? null : related.data;

  return (
    <section className="min-w-0 p-4" aria-labelledby="related-questions-heading">
      <h2 id="related-questions-heading" className="font-mono text-[10.5px] font-medium uppercase tracking-[0.12em] text-faint">Related questions</h2>
      <p className="mt-1 text-[12px] leading-snug text-faint">Based on shared tags.</p>
      {Boolean(related.error) && (
        <p role="status" className="mt-2 text-[12px] leading-snug text-ink-soft">
          {posts ? "Couldn’t refresh related questions. Showing the last results." : "Couldn’t load related questions."} Retrying shortly.
        </p>
      )}
      {!posts ? (
        !related.error && <p role="status" aria-busy="true" className="mt-3 text-[13px] italic text-faint">Finding related questions…</p>
      ) : posts.length === 0 ? (
        <p className="mt-3 text-[13px] italic leading-snug text-ink-soft">No other questions share these tags yet.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {posts.map((post) => (
            <li key={post.id}>
              <Link href={`/posts/${post.id}`} className="block min-w-0 rounded border border-rule bg-paper/60 px-3 py-2.5 transition-colors hover:border-rule-strong hover:bg-paper-deep focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
                <span className={`font-mono text-[10px] ${post.is_open ? "text-accent" : "text-ink-soft"}`}>{post.is_open ? "Open question" : "Solved"}</span>
                <span className="mt-1 block break-words text-[14px] font-semibold leading-snug">{post.problem || post.headline}</span>
                <span className="mt-1.5 block break-words font-mono text-[10.5px] leading-snug text-faint">Shared tags: {post.shared_tags.join(", ")}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
