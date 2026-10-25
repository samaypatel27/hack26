import { Suspense } from "react";
import { PostsPage } from "@/components/PostsPage";
import { SiteHeader } from "@/components/SiteHeader";

export default function ForumsPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto w-full max-w-[1120px] px-4 sm:px-6">
          <SiteHeader active="forums" />
        </div>
      }
    >
      <PostsPage />
    </Suspense>
  );
}
