# Rewind: rolling a struggling agent back to a clean state

Owner: hook team · Branch: `hook-engine` · Status: **built** (v1.5; tested, see "As built" at the end) · Last updated: 2026-10-24

> **For coding agents:** this is the design for the rewind feature, and it is built: `plugin/hook/checkpoints.py`
> (snapshots), `brain.py` (when to save and when to offer), `messages.py` (`REWIND_OFFER`, `CLEAN_SLATE`) and
> `plugin/hook/rewind.py` (the command the agent runs). The section **"As built"** at the end lists where the code
> differs from the design below. Never make rewinding automatic.

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
Agent asks the human ──▶ human says yes ──▶ agent runs the rewind command from the message (Bash)
        │
        ▼
rewind.py saves the current state as "before_rewind" (so it can be undone), restores the files,
and prints the clean slate note ──▶ agent tries a different approach
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
commit=$(git commit-tree "$tree" -p HEAD -m "breakpoint checkpoint: last_good")

# 3. Keep it reachable under a hidden ref
git update-ref refs/breakpoint/checkpoints/<session_id>/<n> "$commit"
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

> [Breakpoint: REWIND_OFFER] You've made 9 edits since the build last passed (7:42 PM) and it still fails. The
> edits may be stacking on top of each other. Options: rewind to **last time it passed** (undoes 9 edits in 4 files)
> or **when this problem started** (undoes 6 edits in 2 files). Already tried and failed: (1) …, (2) …
> Ask the user before rewinding.

### 3. The rewind command

A small local script, `plugin/hook/rewind.py <checkpoint>`, that the agent runs through Bash, where `checkpoint` is
`last_good`, `episode_start`, `before_rewind`, or a checkpoint id. (Built as a script, not an MCP tool: see "Open
decisions".)

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

### 5. Bonus: test copies start from the checkpoint

The local tester ([local-tester.md](local-tester.md)) makes its copies from the "when this problem started"
checkpoint, so every test starts from the real, relevant state, uncommitted work included. (The old
`run_specialists` tool, which worked from the last commit, was removed on 2026-10-25.)

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

- [x] Tool in `mcp/server.py`, or a script run through Bash? **A script** (`plugin/hook/rewind.py`). The MCP server
      has a "no edit or delete tools" rule, and Claude Code's Bash permission prompt doubles as a second "yes".
- [x] Which command counts as the "goal command" for `last_good`? **During an episode: only the episode's command.
      Otherwise: the last episode's command, any command whose previous run failed, or a test/build command**
      (`pytest`, `npm test`, `npm run build`, `jest`, `tsc`, `cargo`, ...; `GOAL_COMMAND_RE` in `brain.py`).
- [ ] Final trigger numbers: 3 edits and one offer per episode for now (`REWIND_MIN_EDITS` in `brain.py`). Tune on
      the trap tasks.
- [ ] Bonus (section 5, specialists start from `episode_start`): not built. It needs a change in
      `plugin/mcp/server.py`, which the MCP owner holds.

---

## As built

### Exactly when things happen

| Moment | What the hook does |
| --- | --- |
| A goal command passes (rule above) | Saves `last_good` |
| A command fails for the first time since it last passed, and no episode is open | Saves `episode_start`. Episodes open lazily (at the 3rd or 4th failure), so we snapshot every first failure; the one at the episode's first failure becomes its `episode_start` |
| A nudge would go to triage or the team, and all of: the signals include whack-a-mole, edit-run-same-output or the AI judge; 3+ edits since the offered checkpoint; git shows at least one file would change; no offer yet this episode | Sends `REWIND_OFFER` instead, once. The next nudge carries on up the ladder |
| The agent runs `rewind.py` | Hook ignores that command (it is not work on the problem) |

The ladder is therefore: forum → **rewind offer** → triage (`STEP_BACK`) → team → Open Question. If a forum fix was
suggested and failed, the offer can still come on a later nudge, before the team.

### The command and the note

The offer contains one ready-to-run line per option, with absolute paths filled in:

```bash
python "C:/.../plugin/hook/rewind.py" --project "C:/path/to/project" --session <session id> last_good
python "C:/.../plugin/hook/rewind.py" --project "C:/path/to/project" --session <session id> episode_start
python "C:/.../plugin/hook/rewind.py" --project "C:/path/to/project" --session <session id> before_rewind   # undo
python "C:/.../plugin/hook/rewind.py" --project "C:/path/to/project" --session <session id> --list
```

It accepts `last_good`, `episode_start`, `before_rewind` or a checkpoint id from `--list`. It exits 1 with a message
on stderr if the checkpoint does not exist or git fails. It prints the clean slate note, tagged
`[Breakpoint: CLEAN_SLATE]`: the point it went back to, files restored, brought back and deleted, "files only",
the attempts that already failed, "start from here with a different approach", and the undo command.

**What it changes in the hook's notes:** it adds a `{"kind": "rewind"}` record to the history, marks the episode
`rewound` (no second offer), adds "rewound the files to last_good" to the episode's tried list, and starts the usual
3-action pause before the next nudge. `struggles.py` counts signals 1-3 and the judge only after the last rewind
record, because the earlier attempts were made on files that no longer exist; without this the very next failure
would fire whack-a-mole again. The episode itself stays open (the problem is not solved yet): its ladder position,
tried list and failure count carry on, so a later pass still gets `NOTABLE_SOLVE`.

### Differences from the design above

- **Snapshot speed:** the throwaway index starts as a *copy of the real index* instead of `git read-tree HEAD`, so
  git can skip unchanged files using its timestamp cache. The result is the same tree. `read-tree HEAD` is the
  fallback when there is no index file, and an empty index when there are no commits.
- **`.claude/` is left out** of snapshots and restores. Claude Code writes `settings.local.json` there when the user
  approves a command, and the hook keeps its notes there; a rewind must not roll those back.
- **Duplicate snapshots are shared:** when the files have not changed since the previous checkpoint, no new ref is
  made and the new name points at the same snapshot. The notes record `{id, ref, label, time, history_idx, key, cmd}`
  and edit counts are computed from the history (instead of storing `edits_since`).
- **The repo's top folder is what gets snapshotted**, even if the project folder is a subfolder of a bigger repo.
- Snapshot commits use a fixed identity (`Breakpoint <breakpoint@localhost>`) and `--no-gpg-sign`, so they
  work without `user.name` and never stop for a signing prompt. They are never on a branch.
- Timeouts: 2 seconds per git call from the hook (skip silently), 60 seconds per git call in `rewind.py` (the user
  is waiting for it, and the undo point must be saved before any file changes).

### Try it

```bash
python -m unittest discover -s plugin/hook/tests                                     # all hook tests
python plugin/hook/dev/replay.py plugin/hook/dev/scenarios/rewind_offer.jsonl         # whole flow, real git repo
```

The scenario builds a small git project, passes the tests (`last_good`), goes through four edit → fail rounds with
different errors (`REWIND_OFFER`), runs `rewind.py last_good` as the agent would after a yes (`CLEAN_SLATE`), then
passes with a new approach (`NOTABLE_SOLVE`, whose tried list includes the rewind).

### Known limits

- Needs git 2.23+ (`git restore`). Not a git repo, or git slower than 2 seconds: no checkpoints and no offer.
- Rewinding to `last_good` saved for a *different* command than the failing one is possible (the message names
  the command that passed).
- A checkpoint saved before the hook's 200-action history window still works, but its edit count only covers the
  actions still in the history.
- Hidden refs are kept until pruned (20 per session). They are local only (never pushed by a normal `git push`);
  `git for-each-ref refs/breakpoint` lists them and `git update-ref -d` removes one. Checkpoints saved before the
  rename to Breakpoint live under `refs/agentoverflow/checkpoints/`; they are still listed and can still be rewound to.
