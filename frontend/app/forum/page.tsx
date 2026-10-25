import { redirect } from "next/navigation";

// The forum lives at /forums; keep the old /forum link working.
export default function LegacyForum() {
  redirect("/forums");
}
