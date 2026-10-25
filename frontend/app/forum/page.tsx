"use client";

import { Header } from "@/components/Header";
import { KpiStrip } from "@/components/KpiStrip";
import { PostFeed } from "@/components/PostFeed";
import { OpenQuestions, TopTags } from "@/components/SidePanels";
import { usePosts } from "@/lib/usePosts";

export default function Home() {
  const { posts, connection, changes, now, updatedAt } = usePosts();

  return (
    <>
      <Header connection={connection} updatedAt={updatedAt} now={now} />
      <main className="mx-auto w-full max-w-7xl flex-1 space-y-4 px-4 py-6 sm:px-6">
        {connection === "offline" && posts === null && (
          <div role="alert" className="rounded-lg border border-line bg-surface px-4 py-3 text-[15px] text-danger">
            Can&apos;t reach the forum. This page will keep retrying.
          </div>
        )}

        <KpiStrip posts={posts} now={now} />

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
          <PostFeed posts={posts} now={now} changes={changes} />
          {posts && (
            <aside className="space-y-4">
              <OpenQuestions posts={posts} now={now} />
              <TopTags posts={posts} />
            </aside>
          )}
        </div>
      </main>
    </>
  );
}
