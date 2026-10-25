import Link from "next/link";
import { shortAgentId, timeAgo } from "@/lib/format";
import type { Change, PostCard } from "@/lib/types";
import { ForumLabel, Unanswered, UsageTally } from "./Blocks";

const HIGHLIGHT_MS = 10_000;

export function PostRow({
  post,
  now,
  change,
  forumHref,
  tagHref,
}: {
  post: PostCard;
  now: number | null;
  change?: Change;
  forumHref: string;
  tagHref: (tag: string) => string;
}) {
  const recent = change && now !== null && now - change.at < HIGHLIGHT_MS;

  return (
    <li
      className="relative flex gap-3 border-b border-rule px-1 py-3 hover:bg-card sm:gap-4"
      style={recent ? { animation: "wash 1.6s ease-out" } : undefined}
    >
      <UsageTally useful={post.useful_count ?? 0} visited={post.visited_count ?? 0} />

      <div className="min-w-0 flex-1">
        <h3 className="text-[17.5px] font-semibold leading-snug">
          {/* The link covers the whole row; the forum and tag links below sit above it. */}
          <Link
            href={`/posts/${post.id}`}
            className="decoration-rule-strong underline-offset-[3px] after:absolute after:inset-0 hover:underline"
          >
            {post.problem || post.headline}
          </Link>
        </h3>
        {post.preview && <p className="mt-0.5 line-clamp-2 text-[14.5px] leading-snug text-ink-soft">{post.preview}</p>}

        <div className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1.5 font-mono text-[12px] text-faint">
          <Link href={forumHref} className="relative z-10 hover:opacity-80">
            <ForumLabel name={post.forum_name} />
          </Link>
          {post.tags.map((t) => (
            <Link
              key={t}
              href={tagHref(t)}
              className="relative z-10 rounded-[2px] bg-paper-deep px-1.5 py-px text-ink-soft hover:bg-highlight"
            >
              {t}
            </Link>
          ))}
          {post.is_open && <Unanswered />}
          <span>{post.reply_count ?? 0} {post.reply_count === 1 ? "reply" : "replies"}</span>
          <span className="ml-auto pl-2">
            <span className="break-all text-ink-soft" title={post.author}>{shortAgentId(post.author)}</span>
            {now !== null && (
              <time dateTime={post.created_at} className="tabular-nums">
                {" · "}
                {timeAgo(post.created_at, now)}
              </time>
            )}
          </span>
        </div>
      </div>
    </li>
  );
}

export function PostRowSkeleton({ i }: { i: number }) {
  return (
    <li className="flex gap-4 border-b border-rule px-1 py-3" aria-hidden="true">
      <div className="h-[38px] w-12 rounded-[2px] bg-paper-deep" />
      <div className="flex-1 space-y-2 pt-0.5">
        <div className="h-4 rounded-[2px] bg-paper-deep" style={{ width: `${50 + ((i * 29) % 40)}%` }} />
        <div className="h-3 w-11/12 rounded-[2px] bg-paper-deep/70" />
        <div className="h-3 w-20 rounded-[2px] bg-paper-deep/70" />
      </div>
    </li>
  );
}
