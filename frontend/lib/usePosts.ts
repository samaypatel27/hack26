"use client";

import { useEffect, useRef, useState } from "react";
import { fetchPosts } from "./api";
import type { Change, ConnectionState, PostCard } from "./types";

const POLL_MS = 5000;

export function usePosts() {
  const [posts, setPosts] = useState<PostCard[] | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [changes, setChanges] = useState<Record<number, Change>>({});
  // Clock values are only set from callbacks, never during render, so prerendering stays stable.
  const [now, setNow] = useState<number | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const previous = useRef<Map<number, boolean> | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const data = await fetchPosts();
        if (cancelled) return;
        const stamp = Date.now();

        const prev = previous.current;
        if (prev) {
          const fresh: Record<number, Change> = {};
          for (const p of data) {
            if (!prev.has(p.id)) fresh[p.id] = { kind: "new", at: stamp };
            else if (prev.get(p.id) && !p.is_open) fresh[p.id] = { kind: "solved", at: stamp };
          }
          if (Object.keys(fresh).length) setChanges((c) => ({ ...c, ...fresh }));
        }
        previous.current = new Map(data.map((p) => [p.id, p.is_open]));

        setPosts(data);
        setConnection("live");
        setUpdatedAt(stamp);
        setNow(stamp);
      } catch {
        // Keep showing the last data we had; just mark the connection as offline.
        if (!cancelled) {
          setConnection("offline");
          setNow(Date.now());
        }
      }
    }

    load();
    const poll = setInterval(load, POLL_MS);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      cancelled = true;
      clearInterval(poll);
      clearInterval(tick);
    };
  }, []);

  return { posts, connection, changes, now, updatedAt };
}
