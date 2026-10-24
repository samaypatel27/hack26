import type { PostCard, PostDetail, Step } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return res.json() as Promise<T>;
}

export function fetchPosts(limit = 200): Promise<PostCard[]> {
  return getJson<PostCard[]>(`/api/posts?limit=${limit}`);
}

export function fetchPost(id: number): Promise<PostDetail> {
  return getJson<PostDetail>(`/api/posts/${id}`);
}

/**
 * The journey steps for a post. The backend only stores the end state today, so this returns just
 * the final step.
 * TODO(backend): expose the recorded steps per post (e.g. `steps: [{id, at}]` from an `episodes`
 * table: spotted_at, checked_at, specialists_started_at, resolved_at) and return them here.
 */
export function stepsFor(post: PostCard): Step[] {
  return [{ id: post.is_open ? "open" : "confirmed" }];
}
