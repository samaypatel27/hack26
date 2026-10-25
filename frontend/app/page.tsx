import { SiteHeader } from "@/components/SiteHeader";

/** Home is intentionally empty for now. The posts live under /forums. */
export default function Home() {
  return (
    <div className="mx-auto w-full max-w-[1120px] px-4 sm:px-6">
      <SiteHeader active="home" />
      <main />
    </div>
  );
}
