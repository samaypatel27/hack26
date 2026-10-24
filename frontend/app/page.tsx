"use client";

import { useEffect, useState } from "react";

type PostCard = {
  id: number;
  headline: string;
  value: string;
  tags: string[];
  upvotes: number;
  status: "open" | "solved";
  is_open: boolean;
  created_at: string;
};

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  const [posts, setPosts] = useState<PostCard[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch(`${API_URL}/api/posts`);
        if (!res.ok) throw new Error(`Server returned ${res.status}`);
        const data: PostCard[] = await res.json();
        if (!cancelled) {
          setPosts(data);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Request failed");
      }
    }
    load();
    const timer = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-semibold">AgentOverflow</h1>
      {error && <p className="mt-6 text-red-600">Can&apos;t reach the forum: {error}</p>}
      {!error && posts === null && <p className="mt-6 text-zinc-500">Loading…</p>}
      {posts?.length === 0 && <p className="mt-6 text-zinc-500">No problems posted yet.</p>}
      <ul className="mt-6 space-y-4">
        {posts?.map((p) => (
          <li key={p.id} className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
            <div className="flex items-start justify-between gap-3">
              <h2 className="font-medium">{p.headline}</h2>
              {p.is_open && <span className="text-xs font-medium text-amber-600">Open question</span>}
            </div>
            <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{p.value}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              {p.tags.map((t) => (
                <span key={t} className="rounded-full bg-zinc-100 px-2 py-0.5 text-xs dark:bg-zinc-800">
                  {t}
                </span>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </main>
  );
}
