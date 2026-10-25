import { Suspense } from "react";
import { PostsPage } from "@/components/PostsPage";
import { SiteHeader } from "@/components/SiteHeader";

export default function ForumsPage() {
  return (
    <Suspense
      fallback={<SiteHeader active="forums" />}
    >
      <PostsPage />
    </Suspense>
  );
}
