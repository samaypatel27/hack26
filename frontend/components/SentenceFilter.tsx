"use client";

import { useEffect, useState } from "react";
import type { Forum, StatusFilter } from "@/lib/types";

type Which = "status" | "forum" | "tags";

export type FilterChange = { status?: StatusFilter; forum?: string | null; tags?: string[] };

const STATUS_OPTIONS: { id: StatusFilter; label: string; hint: string }[] = [
  { id: "all", label: "everything", hint: "Solved and unsolved" },
  { id: "solved", label: "solved", hint: "Has a fix" },
  { id: "open", label: "unsolved", hint: "Nobody has a fix yet" },
];

function listTags(tags: string[]): string {
  if (tags.length === 0) return "any tag";
  if (tags.length === 1) return tags[0];
  return `${tags.slice(0, -1).join(", ")} and ${tags[tags.length - 1]}`;
}

/** The connecting words of the sentence: lighter than the blanks, so the blanks read as inputs. */
function Word({ children }: { children: React.ReactNode }) {
  return <span className="font-normal italic text-ink-soft">{children}</span>;
}

function Blank({
  open,
  filled,
  onClick,
  children,
  label,
}: {
  open: boolean;
  filled: boolean;
  onClick: () => void;
  children: React.ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-haspopup="listbox"
      aria-expanded={open}
      aria-label={label}
      className={`rounded-[2px] border-b-[3px] border-dotted px-1 text-left font-bold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink ${
        filled ? "border-accent text-accent" : "border-rule-strong italic font-normal text-faint"
      } ${open ? "bg-highlight" : "hover:bg-highlight/60"}`}
    >
      {children}
      <span className="ml-1 align-middle font-mono text-[11px] font-normal not-italic text-faint" aria-hidden="true">
        {open ? "▴" : "▾"}
      </span>
    </button>
  );
}

/** A dropdown on laptops; a sheet that slides up from the bottom on phones. */
function Menu({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <>
      <div className="fixed inset-0 z-20 bg-ink/30 md:hidden" onClick={onClose} aria-hidden="true" />
      <div className="fixed inset-x-0 bottom-0 z-30 max-h-[72vh] overflow-y-auto border-t-2 border-ink bg-card pb-6 text-[17px] font-normal not-italic md:absolute md:inset-x-auto md:bottom-auto md:left-0 md:top-full md:mt-1.5 md:max-h-80 md:w-72 md:border-2 md:pb-0 md:text-[15.5px]">
        <div className="flex items-center justify-between border-b border-rule px-3 py-2 md:hidden">
          <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-faint">{title}</span>
          <button type="button" onClick={onClose} className="font-mono text-sm font-medium text-accent">
            Done
          </button>
        </div>
        {children}
      </div>
    </>
  );
}

function Option({
  selected,
  onClick,
  children,
  aside,
  marker,
}: {
  selected: boolean;
  onClick: () => void;
  children: React.ReactNode;
  aside?: React.ReactNode;
  marker?: string;
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      onClick={onClick}
      className={`flex w-full items-baseline gap-2 border-l-[3px] px-3 py-2.5 text-left leading-snug hover:bg-paper-deep md:py-1.5 ${
        selected ? "border-accent font-semibold" : "border-transparent"
      }`}
    >
      {marker && <span className="font-mono text-[13px] text-ink-soft">{marker}</span>}
      <span className="flex-1">{children}</span>
      {aside && <span className="font-mono text-[12px] text-faint">{aside}</span>}
    </button>
  );
}

export function SentenceFilter({
  status,
  started,
  forum,
  tags,
  forums,
  onChange,
  onReset,
  large = false,
}: {
  /** Bigger type for the centered intro on /forums; the size change animates. */
  large?: boolean;
  status: StatusFilter;
  /** True once a status was picked, which is what brings in the forum blank. */
  started: boolean;
  forum: string | null;
  tags: string[];
  forums: Forum[] | null;
  onChange: (change: FilterChange) => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState<Which | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!(e.target as Element).closest("[data-blank]")) setOpen(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = (which: Which) => setOpen((o) => (o === which ? null : which));
  const current = forums?.find((f) => f.slug === forum) ?? null;
  const showForum = started || forum !== null;
  const showTags = forum !== null;
  const dirty = started || forum !== null || tags.length > 0;

  function pickForum(slug: string) {
    const next = forums?.find((f) => f.slug === slug);
    // Keep the tags that also exist in the new forum; tags can belong to more than one forum.
    onChange({ forum: slug, tags: next ? tags.filter((t) => next.tags.includes(t)) : [] });
    setOpen(null);
  }

  function toggleTag(tag: string) {
    onChange({ tags: tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag] });
  }

  return (
    <div
      className={`font-semibold leading-[1.75] tracking-tight transition-[font-size] duration-[600ms] ease-[cubic-bezier(0.2,0.7,0.2,1)] ${
        large ? "text-[34px] sm:text-[48px]" : "text-[25px] sm:text-[32px]"
      }`}
      aria-label="Filter"
    >
      <Word>Show me</Word>{" "}
      <span className="relative inline-block" data-blank>
        <Blank open={open === "status"} filled onClick={() => toggle("status")} label="Status">
          {STATUS_OPTIONS.find((s) => s.id === status)?.label}
        </Blank>
        {open === "status" && (
          <Menu title="Show me" onClose={() => setOpen(null)}>
            <div role="listbox" aria-label="Status">
              {STATUS_OPTIONS.map((s) => (
                <Option
                  key={s.id}
                  selected={status === s.id}
                  aside={s.hint}
                  onClick={() => {
                    onChange({ status: s.id });
                    setOpen(null);
                  }}
                >
                  {s.label}
                </Option>
              ))}
            </div>
          </Menu>
        )}
      </span>
      {status !== "all" && (
        <>
          {" "}
          <Word>problems</Word>
        </>
      )}

      {showForum && (
        <>
          {" "}
          <Word>in</Word>{" "}
          <span className="relative inline-block" data-blank>
            <Blank open={open === "forum"} filled={forum !== null} onClick={() => toggle("forum")} label="Forum">
              {current?.name ?? forum ?? "pick a forum"}
            </Blank>
            {open === "forum" && (
              <Menu title="In which forum" onClose={() => setOpen(null)}>
                <div role="listbox" aria-label="Forum">
                  {forums === null ? (
                    <p className="px-3 py-3 italic text-ink-soft">Loading…</p>
                  ) : (
                    forums.map((f) => (
                      <Option key={f.slug} selected={forum === f.slug} aside={f.post_count} onClick={() => pickForum(f.slug)}>
                        {f.name}
                      </Option>
                    ))
                  )}
                  {forum && (
                    <button
                      type="button"
                      onClick={() => {
                        onChange({ forum: null, tags: [] });
                        setOpen(null);
                      }}
                      className="w-full border-t border-rule px-3 py-2.5 text-left font-mono text-[13px] text-accent hover:bg-paper-deep md:py-1.5"
                    >
                      Remove this
                    </button>
                  )}
                </div>
              </Menu>
            )}
          </span>
        </>
      )}

      {showTags && (
        <>
          {" "}
          <Word>about</Word>{" "}
          <span className="relative inline-block" data-blank>
            <Blank open={open === "tags"} filled={tags.length > 0} onClick={() => toggle("tags")} label="Tags">
              {listTags(tags)}
            </Blank>
            {open === "tags" && (
              <Menu title={`Tags in ${current?.name ?? forum}`} onClose={() => setOpen(null)}>
                <div role="listbox" aria-label="Tags" aria-multiselectable="true">
                  <p className="border-b border-rule px-3 py-1.5 font-mono text-[11px] text-faint">
                    Posts with all the tags you tick
                  </p>
                  {current === null ? (
                    <p className="px-3 py-3 italic text-ink-soft">Loading…</p>
                  ) : (
                    current.tags.map((t) => (
                      <Option
                        key={t}
                        selected={tags.includes(t)}
                        marker={tags.includes(t) ? "[x]" : "[ ]"}
                        onClick={() => toggleTag(t)}
                      >
                        <span className="font-mono text-[14px]">{t}</span>
                      </Option>
                    ))
                  )}
                  {tags.length > 0 && (
                    <button
                      type="button"
                      onClick={() => onChange({ tags: [] })}
                      className="w-full border-t border-rule px-3 py-2.5 text-left font-mono text-[13px] text-accent hover:bg-paper-deep md:py-1.5"
                    >
                      Remove this
                    </button>
                  )}
                </div>
              </Menu>
            )}
          </span>
        </>
      )}

      {dirty && (
        <button
          type="button"
          onClick={() => {
            setOpen(null);
            onReset();
          }}
          className="ml-3 align-middle font-mono text-[12px] font-normal text-faint underline underline-offset-4 hover:text-ink"
        >
          start over
        </button>
      )}
    </div>
  );
}
