# Rewind: rolling a struggling agent back to a clean state

Owner: hook team · Branch: `hook-engine` · Status: planned for v1.5 (after the core loop works) · Last updated: 2026-10-24

> **For coding agents:** this is the design for the rewind feature. Build it only after the v1 struggle signals in
> [struggle-signals.md](struggle-signals.md) work end to end. Never make rewinding automatic.

Background on where this idea came from: [mentor-feedback.md](mentor-feedback.md).

---

## The idea in one paragraph

When an agent struggles, it does not just waste time. It leaves a mess: half-fixes stacked on top of each other.
Our hook already knows when the agent is going in circles. So it can also offer to **roll the project's files back**
to a clean point, like the last time the build passed, and hand the agent a short note on what was tried and why it
failed. The agent then starts over from a clean state with a fresh approach.

**Pitch line:** "An undo button that knows when to press itself, and where to stop."

---

## What already exists, and what is new

Claude Code has a built-in rewind (`/rewind`, or pressing Esc twice) that restores the conversation and/or code to an
earlier message. Its limits:

| Built-in `/rewind` | Ours |
| --- | --- |
| Only the human can trigger it | The hook detects when it is needed and offers it |
| The human has to pick the point | We offer named points: "last time the build passed", "when this problem started" |
| Tracks edits made by Claude's file-editing tool only | Snapshots the whole working tree with git, so changes made by terminal commands are covered too |
| Restores the conversation too | Restores files only, then gives the agent a "clean slate" note (the human can still use `/rewind` for the conversation) |

**What's new is knowing when to rewind and where to rewind to.**

---

## How it works

```
Goal command passes ──▶ hook saves checkpoint "last_good"
        │
First failure of a stuck episode ──▶ hook saves checkpoint "episode_start"
        │
Struggle signal 2, 3 or the judge fires, and 3+ edits since a checkpoint
        │
        ▼
Hook sends REWIND_OFFER (both points, times, edits each would undo, what already failed)
        │
        ▼
Agent asks the human ──▶ human says yes ──▶ agent calls the rewind tool
        │
        ▼
Tool saves the current state as "before_rewind" (so it can be undone), restores the files,
and returns the clean slate note ──▶ agent tries a different approach
```

### 1. Checkpoints (hook)

| Checkpoint | Saved when | What rewinding to it undoes |
| --- | --- | --- |
| `last_good` | The goal command (or the test command) passes | Everything since the project last worked |
| `episode_start` | The first failure of a new stuck episode | Only the back-and-forth since the problem appeared; keeps earlier work |
| `before_rewind` | Right before any rewind | The rewind itself (this is the undo) |

**Snapshot method:** git plumbing with a temporary index, so the user's branch, staged files and history are never
touched. New untracked files are included, and ignored files (`node_modules`, `.env`) are skipped.

```bash
# 1. Build a tree of the current working directory in a throwaway index
GIT_INDEX_FILE=<temp file> git read-tree HEAD
GIT_INDEX_FILE=<temp file> git add -A          # respects .gitignore
tree=$(GIT_INDEX_FILE=<temp file> git write-tree)

# 2. Wrap it in a commit that is not on any branch
commit=$(git commit-tree "$tree" -p HEAD -m "agentoverflow checkpoint: last_good")

# 3. Keep it reachable under a hidden ref
git update-ref refs/agentoverflow/checkpoints/<session_id>/<n> "$commit"
```

Rules:
- Skip the snapshot if the tree hash equals the previous checkpoint's (nothing changed).
- Skip it if git takes longer than about 2 seconds (very large repos), or if the project is not a git repo.
- Keep at most 20 checkpoints per session; delete the oldest refs.
- Record each checkpoint in the hook's per-session notes: `{id, ref, label, time, edits_since}`.

### 2. When the hook offers a rewind (brain)

All of these must be true:
- One of these fired: **whack-a-mole** (signal 2), **edit, run, same output** (signal 3), or the **AI judge** said
  stuck;
- at least **3 edits** since the checkpoint we would offer (otherwise there is nothing worth undoing);
- no rewind has been offered yet in this episode.

**Place on the help ladder:** forum fix → **rewind + fresh approach** → specialist team → Open Question.

Example message:

> [AgentOverflow: REWIND_OFFER] You've made 9 edits since the build last passed (7:42 PM) and it still fails. The
> edits may be stacking on top of each other. Options: rewind to **last time it passed** (undoes 9 edits in 4 files)
> or **when this problem started** (undoes 6 edits in 2 files). Already tried and failed: (1) …, (2) …
> Ask the user before rewinding.

### 3. The rewind tool

A small local tool, `rewind(checkpoint)`, where `checkpoint` is `"last_good"`, `"episode_start"`,
`"before_rewind"`, or a checkpoint id.

Steps:
1. Save the current state as `before_rewind`.
2. Restore the working tree from the checkpoint without touching the index:
   `git restore --source=<commit> --worktree -- .`
3. Delete files that were created after the checkpoint (present now, absent in the checkpoint):
   `git diff --name-only --diff-filter=A <checkpoint> <before_rewind>`
4. Return the **clean slate note**: which point it restored to, what was undone, the attempts that failed and why
   (from the hook's notes), and "start from here with a different approach. To undo, rewind to `before_rewind`."

Checkpoint logic lives in one shared module (`plugin/hook/checkpoints.py`) used by both the hook (saving) and the
tool (restoring).

### 4. Skill instructions (SKILL.md)

A `REWIND_OFFER` section:
- Show the user both options and what each would undo, and ask before rewinding. Never rewind without a yes.
- After rewinding, read the clean slate note and do not repeat the listed attempts.
- If the user says no, continue to the next rung of the help ladder.

### 5. Bonus: specialists start from the checkpoint

`run_specialists` currently creates its worktrees from the last commit, so it misses uncommitted work. It can create
them from the `episode_start` checkpoint instead (`git worktree add <path> <checkpoint commit>`), so every specialist
starts from the real, relevant state.

---

## Safety rules

| Risk | Rule |
| --- | --- |
| Throwing away good work mixed in with bad | Never automatic. The agent proposes, the human confirms |
| Not being able to undo | Always save `before_rewind` first |
| Side effects outside files (installed packages, database changes, Docker images) | The offer and the note say clearly: files only |
| Ignored files (`.env`, `node_modules`) | Never touched by snapshots or restores |
| Touching the user's git state | Hidden refs only; never change the branch, the index or history |

---

## Demo beat

The agent goes back and forth on a trap task → the hook spots it → REWIND_OFFER appears → we say yes → 9 bad edits
roll back → the agent reads what already failed, tries a new approach, and the build passes.

---

## Open decisions

- [ ] Does the `rewind` tool go in `mcp/server.py` (coordinate with the MCP owner), or does the agent run a small
      script through Bash?
- [ ] Which command counts as the "goal command" for `last_good`: the tracked failing command only, or any test
      command?
- [ ] Final trigger numbers (3 edits, one offer per episode)
