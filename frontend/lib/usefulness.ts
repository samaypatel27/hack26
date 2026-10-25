/** Fractional stars from explicit agent activity; no visits means no rating. */
export function usefulnessRating(useful: number, visited: number): number | null {
  if (!Number.isFinite(useful) || !Number.isFinite(visited) || visited <= 0) return null;
  return Math.min(5, Math.max(0, (useful / visited) * 5));
}
