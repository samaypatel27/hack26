/**
 * Turn any text into a URL slug: "Crème Brûlée!" -> "creme-brulee".
 */
export function slugify(text, { separator = "-" } = {}) {
  return String(text)
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "") // drop accents
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
    .join(separator);
}
