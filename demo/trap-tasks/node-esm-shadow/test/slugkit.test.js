import { test } from "node:test";
import assert from "node:assert/strict";
import { slugify, titleCase } from "../src/index.js";

test("slugify makes URL-safe slugs", () => {
  assert.equal(slugify("Hello World"), "hello-world");
  assert.equal(slugify("  Crème Brûlée!  "), "creme-brulee");
  assert.equal(slugify("Q3 report: final (v2)"), "q3-report-final-v2");
  assert.equal(slugify("a b", { separator: "_" }), "a_b");
});

test("titleCase keeps small words lowercase", () => {
  assert.equal(titleCase("the art of war"), "The Art of War");
  assert.equal(titleCase("HARRY POTTER AND THE GOBLET OF FIRE"), "Harry Potter and the Goblet of Fire");
});
