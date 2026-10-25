import { Suspense } from "react";
import { PostView } from "@/components/PostView";
import { SiteHeader } from "@/components/SiteHeader";

export default function PostPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto w-full max-w-[860px] px-4 sm:px-6">
          <SiteHeader active="forums" />
        </div>
      }
    >
      <PostView />
    </Suspense>
  );
}
