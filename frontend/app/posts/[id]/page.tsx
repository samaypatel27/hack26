import { Suspense } from "react";
import { PostView } from "@/components/PostView";
import { SiteHeader } from "@/components/SiteHeader";

export default function PostPage() {
  return (
    <Suspense
      fallback={<SiteHeader active="forums" />}
    >
      <PostView />
    </Suspense>
  );
}
