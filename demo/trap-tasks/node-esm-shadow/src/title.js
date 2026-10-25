const SMALL_WORDS = new Set(["a", "an", "and", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to"]);

/**
 * Title-case a heading, keeping small words lowercase: "the art of war" -> "The Art of War".
 */
export function titleCase(text) {
  return String(text)
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .map((word, i) => (i > 0 && SMALL_WORDS.has(word) ? word : word[0].toUpperCase() + word.slice(1)))
    .join(" ");
}
