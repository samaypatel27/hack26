# Mentor feedback log

Ideas mentors gave us during HackOHIO 2026, what we found when we looked into each one, and what we decided. Newest
first.

---

## Rewind a struggling agent to before the problem

**Date:** 2026-10-24 (Saturday evening) · **From:** a HackOHIO mentor · **Status:** planned for v1.5

### The suggestion

After we explained how our hook detects a struggling agent, a mentor asked: if an agent is going back and forth on a
problem, could we **rewind it to where it was before the issue started**?

### What we found when we looked into it

- **Claude Code already has a rewind** (`/rewind`, or Esc twice). It restores the conversation and/or code to an
  earlier message, so "being able to rewind" alone would not be new.
- **Its limits are exactly where our project helps:**
  - only the human can trigger it, and they have to notice the problem themselves;
  - they have to pick the point to go back to;
  - it only tracks edits made by Claude's file-editing tool, not changes made through terminal commands.
- **The idea fits what we already built.** Our hook already detects going in circles (whack-a-mole, edits that don't
  change the result), so it also knows *when* a rewind would help and *which* point is clean (the last time the
  build passed).
- **A struggling agent leaves a mess,** not just lost time: half-fixes layered on top of each other. Rolling back
  and listing what already failed is how an experienced developer would recover.

### What we decided

- **Build it, as v1.5:** right after the core loop (detect, check the forum, call specialists) works end to end.
- **Our angle:** an undo button that knows *when* to press itself and *where* to stop.
  - The hook saves quiet git checkpoints ("last time it passed", "when this problem started").
  - It offers a rewind when it sees going in circles.
  - A small tool restores the files and gives the agent a "clean slate" note of what already failed.
- **Safety:**
  - never automatic: the human confirms;
  - every rewind can be undone;
  - files only, never the user's git history.
- **Bonus we found while designing it:** the specialist team can start from the "when this problem started"
  checkpoint instead of the last commit, so specialists see the real, uncommitted state of the project.

### How it changed our design

- A new rung on the help ladder: forum fix → **rewind + fresh approach** → specialist team → Open Question.
- A new message type for the hook (`REWIND_OFFER`) and a new section in the skill.
- A shared checkpoint module used by the hook and a new `rewind` tool.

Full design: [rewind.md](rewind.md).

---

## Use multiple engines for the backup team

**Date:** 2026-10-24 (Saturday) · **From:** a HackOHIO mentor · **Status:** partly adopted

### The suggestion

The backup team should not be one model wearing different labels. Use **multiple engines**, so different team
members catch different things.

### What we decided

- In the server-side design, each team member uses a different model (Claude, GPT, Gemini) **and** a different source
  (docs, GitHub issues, Stack Overflow), and a chair combines their findings. Andrej Karpathy's "LLM Council"
  popularized this pattern.
- In the local `run_specialists` design, specialists differ by strategy (environment, config, docs,
  assumption-challenger) and can run on different Claude models (`--model`).
- Which team design ships in the demo is still an open decision in [../PROJECT_PLAN.md](../PROJECT_PLAN.md).
