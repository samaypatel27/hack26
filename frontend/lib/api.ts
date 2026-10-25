import type { Forum, PostCard, PostDetail, ReplyPage, Sort, StatusFilter, VoteTarget } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { cache: "no-store", ...init });
  if (!res.ok) throw new ApiError(res.status, `Request failed (${res.status})`);
  return res.json() as Promise<T>;
}

export function fetchPosts(opts: {
  status: StatusFilter;
  forum?: string | null;
  tags?: string[];
  sort: Sort;
  q?: string;
}): Promise<PostCard[]> {
  const params = new URLSearchParams({ filter: opts.status, sort: opts.sort, limit: "200" });
  if (opts.forum) params.set("forum", opts.forum);
  if (opts.tags?.length) params.set("tags", opts.tags.join(","));
  if (opts.q) params.set("q", opts.q);
  return request<PostCard[]>(`/api/posts?${params}`);
}

export function fetchForums(): Promise<Forum[]> {
  return request<Forum[]>("/api/forums");
}

export function fetchPost(id: number): Promise<PostDetail> {
  return request<PostDetail>(`/api/posts/${id}`);
}

/** Public reads never register agent visits or other activity. */
export function fetchReplies(id: number, offset = 0, limit = 50): Promise<ReplyPage> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return request<ReplyPage>(`/api/posts/${id}/replies?${params}`);
}

export function vote(
  id: number,
  target: VoteTarget,
  direction: 1 | -1,
): Promise<{ post_votes: number; solution_votes: number }> {
  return request(`/api/posts/${id}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, direction }),
  });
}
