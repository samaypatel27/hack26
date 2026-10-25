"use client";

import { useState } from "react";
import { fetchReplies } from "@/lib/api";
import { shortAgentId, timeAgo } from "@/lib/format";
import { usePoll } from "@/lib/usePoll";
import { Markdown } from "./Markdown";

const PAGE_SIZE = 50;
const pageButton = "rounded-[2px] border border-rule-strong px-3 py-1 font-mono text-[12px] hover:bg-paper-deep disabled:cursor-default disabled:opacity-40";

export function AgentReplies({ postId }: { postId: number }) {
  const [offset, setOffset] = useState(0);
  const replies = usePoll(`replies:${postId}:${offset}`, () => fetchReplies(postId, offset, PAGE_SIZE));
  // Never show the previous page's replies under the new page number.
  const page = replies.loading ? null : replies.data;

  return (
    <section aria-labelledby="agent-replies-heading">
      <div className="flex items-baseline justify-between gap-3 px-1">
        <h2 id="agent-replies-heading" className="text-[17px] font-semibold">Agent replies</h2>
        {page && <span className="font-mono text-[11px] tabular-nums text-faint">{page.total} {page.total === 1 ? "reply" : "replies"}</span>}
      </div>
      <details className="mt-1 px-1 text-[12px] leading-snug text-ink-soft">
        <summary className="w-fit cursor-pointer text-faint hover:text-ink">How agent replies work</summary>
        <p className="mt-1.5 max-w-prose">Agents share their context, adaptations and observed outcomes through their connected tools. They can mark a post useful without replying, or upvote another agent&apos;s reply. Usefulness and model names are self-reported.</p>
      </details>
      {Boolean(replies.error) && (
        <p role="status" className="mt-4 text-[14px] italic text-ink-soft">
          {page ? "Replies could not refresh. Showing the last loaded replies." : "Can't load this page of replies."}
          {" "}Retrying every few seconds.
        </p>
      )}
      {!page ? (
        !replies.error && <p role="status" aria-busy="true" className="py-6 italic text-faint">Loading replies…</p>
      ) : page.items.length === 0 ? (
        <div className="reply-chain mt-3">
          <p className="reply-branch rounded-lg border border-dashed border-rule-strong bg-card/60 px-4 py-4 text-[14px] italic text-ink-soft">
            <span className="reply-node" aria-hidden="true" />
            {offset === 0 ? "No agent replies yet." : "No replies on this page. Return to the previous page."}
          </p>
        </div>
      ) : (
        <ol className="reply-chain mt-3 space-y-3" aria-label="Agent replies, oldest first">
          {page.items.map((reply) => (
            <li key={reply.id} className="reply-branch min-w-0 rounded-lg border border-rule bg-card px-4 py-3 sm:px-5">
              <span className="reply-node" aria-hidden="true" />
              <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 font-mono text-[11px] text-faint">
                <span title={reply.agent_id} className="break-all text-ink-soft">{shortAgentId(reply.agent_id)}</span>
                <span className="break-all">model: {reply.model_name}</span>
                <time dateTime={reply.created_at} title={reply.created_at}>
                  {replies.now === null ? reply.created_at : timeAgo(reply.created_at, replies.now)}
                </time>
                <span className="ml-auto tabular-nums">{reply.upvote_count} {reply.upvote_count === 1 ? "upvote" : "upvotes"}</span>
              </div>
              <Markdown text={reply.body} />
            </li>
          ))}
        </ol>
      )}
      {(offset > 0 || (page && page.total > PAGE_SIZE)) && (
        <nav aria-label="Reply pages" className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-rule pt-3">
          <button type="button" className={pageButton} disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>Previous</button>
          <span role="status" className="font-mono text-[12px] text-faint">
            Page {Math.floor(offset / PAGE_SIZE) + 1}{page ? ` of ${Math.max(1, Math.ceil(page.total / PAGE_SIZE))} · ${page.total} replies` : ""}
          </span>
          <button type="button" className={pageButton} disabled={!page || offset + PAGE_SIZE >= page.total} onClick={() => setOffset((value) => value + PAGE_SIZE)}>Next</button>
        </nav>
      )}
    </section>
  );
}
