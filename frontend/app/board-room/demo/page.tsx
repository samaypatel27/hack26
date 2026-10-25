import type { Metadata } from "next";
import { DemoBoardRoom } from "@/components/board-room/DemoBoardRoom";

export const metadata: Metadata = {
  title: "Board room demo · Breakpoint",
  description: "A recorded specialist-team run, replayed.",
};

export default function BoardRoomDemoPage() {
  return <DemoBoardRoom />;
}
