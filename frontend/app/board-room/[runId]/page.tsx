import type { Metadata } from "next";
import { Suspense } from "react";
import { LiveBoardRoom } from "@/components/board-room/LiveBoardRoom";
import { SiteHeader } from "@/components/SiteHeader";

export const metadata: Metadata = {
  title: "Board room · Breakpoint",
  description: "A specialist-team run, live.",
};

export default function BoardRoomRunPage() {
  return (
    <Suspense fallback={<SiteHeader />}>
      <LiveBoardRoom />
    </Suspense>
  );
}
