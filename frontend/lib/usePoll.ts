"use client";

import { useEffect, useRef, useState } from "react";
import type { ConnectionState } from "./types";

const POLL_MS = 5000;

/**
 * Fetch now and every 5 seconds while `key` stays the same. Keeps the last good data when a
 * request fails or while a new key loads (`loading` says which). `now` ticks every second for
 * relative times and is only set from callbacks, so nothing time-dependent renders during
 * prerendering.
 */
export function usePoll<T>(key: string, fetcher: () => Promise<T>, onData?: (data: T, sameKey: boolean) => void) {
  const [data, setData] = useState<T | null>(null);
  const [dataKey, setDataKey] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [now, setNow] = useState<number | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const latest = useRef({ fetcher, onData });
  const loadedKey = useRef<string | null>(null);

  useEffect(() => {
    latest.current = { fetcher, onData };
  });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const result = await latest.current.fetcher();
        if (cancelled) return;
        const stamp = Date.now();
        latest.current.onData?.(result, loadedKey.current === key);
        loadedKey.current = key;
        setData(result);
        setDataKey(key);
        setError(null);
        setConnection("live");
        setUpdatedAt(stamp);
        setNow(stamp);
      } catch (e) {
        if (cancelled) return;
        setError(e);
        setConnection("offline");
        setNow(Date.now());
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
  }, [key]);

  return { data, connection, now, updatedAt, error, loading: dataKey !== key };
}
