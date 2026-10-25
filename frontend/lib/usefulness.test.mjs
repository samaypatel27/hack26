import assert from "node:assert/strict";
import test from "node:test";
import { usefulnessRating } from "./usefulness.ts";

test("no visits is unrated rather than zero stars", () => {
  assert.equal(usefulnessRating(0, 0), null);
});

test("visits without useful marks is a real zero-star rating", () => {
  assert.equal(usefulnessRating(0, 8), 0);
});

test("all visiting agents finding the post useful gives five stars", () => {
  assert.equal(usefulnessRating(8, 8), 5);
});

test("partial usefulness retains precision for fractional star fills", () => {
  assert.equal(usefulnessRating(3, 8), 1.875);
});

test("inconsistent counts cannot render outside the five-star range", () => {
  assert.equal(usefulnessRating(12, 8), 5);
  assert.equal(usefulnessRating(-1, 8), 0);
});

test("unavailable or invalid counts stay unrated", () => {
  assert.equal(usefulnessRating(2, -1), null);
  assert.equal(usefulnessRating(2, Number.NaN), null);
  assert.equal(usefulnessRating(Number.NaN, 8), null);
  assert.equal(usefulnessRating(2, Number.POSITIVE_INFINITY), null);
});
