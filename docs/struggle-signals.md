# Struggle signals: how the hook tells an agent is struggling

Owner: hook team · Branch: `hook-engine` · Last updated: 2026-10-24

> **For coding agents:** this file defines what counts as "struggling" for the hook. Implement the **v1** signals and
> the **AI judge** first. Everything in "Future signals" is for later and should not be built unless asked.

---

## The rule: judge what the agent does, not what it says

An agent can sound confident while it is going in circles. We cannot read its reasoning: Claude Code's conversation
log records thinking blocks without their text. We checked a real session log on 2026-10-24: 59 thinking blocks, 0
with readable text. Even readable reasoning can be confidently wrong.

So we detect struggle from **behavior** (actions and their results) and from **claims that do not match results**.
Words count only as supporting evidence.

---

## What the hook can see

| Source | What it contains | Where it comes from |
| --- | --- | --- |
| Actions | Every tool call: commands, file edits, file reads, code searches. Each with its input and result (passed or failed, output) | `PostToolUse` and `PostToolUseFailure` events (all tools, not just Bash) |
| User messages | The text the user types | `UserPromptSubmit` event (`prompt` field) |
| Agent replies | What the agent writes in chat | The conversation log, whose path comes with every event (`transcript_path`) |
| Finishing | The moment the agent tries to end its turn | `Stop` event (includes `stop_hook_active`) |
| Thinking | Not readable | Logged with empty text |

---

## How signals combine into "struggling"

| Strength | Rule |
| --- | --- |
| **Strong** | Any one strong signal alone = struggling |
| **Medium** | Two medium signals together = struggling (future signals only) |
| **Weak** | Never on its own; only adds weight to a medium or strong signal |
| **Unclear** | Some evidence but nothing above fires → ask the **AI judge** |

**Progress resets the counts:**
- the goal command passes;
- the number of failing tests drops;
- a new error appears further along (a different stage of the build or test run).

**Not struggling (avoid false alarms):**
- **Waiting:** polling a server that is still starting ("connection refused" a few times) does not count until a
  time limit passes.
- **Test-first work:** a test failing before its code exists counts only if it keeps failing the same way after
  edits.
- **Exploring at the start:** lots of reading in the first minutes of a task is normal.

---

## v1 signals (build these)

| # | Signal | Exact rule (starting thresholds, tune on trap tasks) | Data needed | Strength | Catches |
| --- | --- | --- | --- | --- | --- |
| 1 | **Same error loop** | The same error fingerprint fails **3 times** in a session, with no pass of that command in between | Command results (`PostToolUseFailure`) | Strong | The classic loop |
| 2 | **Whack-a-mole** | The same command (normalized: flags dropped, first 3 words) fails **4+ times** with **3+ different** error fingerprints | Command results | Strong | Each fix breaking something new |
| 3 | **Edit, run, same output** | **3 cycles in a row** where the agent edits a file and then reruns the same command, and the output fingerprint is unchanged (passing or failing) | Edits (`PostToolUse` on Edit/Write) + command output | Strong | Logic bugs with no crash: the code changes but the result does not |
| 4 | **Says done, isn't** | At `Stop`, the last run of the tracked command failed | `Stop` + action history | Strong | Confidently wrong agents claiming false success |
| 5 | **Unchecked success claim** | At `Stop`, the agent's last reply claims success ("fixed", "should work now", "this resolves", "done") but **no command ran after its last edit** | `Stop` + last reply from the conversation log + action history | Strong | Confidently wrong agents claiming success they never verified |
| 6 | **User pushback** | The user's message matches a pushback phrase ("still broken", "still failing", "that's wrong", "doesn't work", "same error", "you already tried that") **or** contains an error whose fingerprint we already saw this session | `UserPromptSubmit` | Strong | The user noticing what the agent did not |

**Notes for building:**
- Signals 4 and 5 fire on `Stop`. Block the stop **once** per episode (track `stop_blocked`), and always allow it when
  `stop_hook_active` is true, or the agent can loop forever.
- Signal 3 needs an "output fingerprint": the same normalization as error fingerprints, applied to the command's
  output.
- Signal 6's phrase list lives in one place in the code so the team can extend it.

---

## The AI judge (v1)

A small, cheap model reads recent activity and decides whether the agent is stuck. It covers the gray zone the rules
miss, especially logic struggles that produce no errors.

| Item | Decision |
| --- | --- |
| Model | A small, fast model, for example Claude Haiku 5.5 (`claude-haiku-5-5`) |
| When it runs | No strong signal fired, but **20+ actions** or **10+ minutes** have passed since the last progress. At most **once every 10 actions** per session |
| What it reads | The last ~30 actions (tool, input summary, passed or failed, short output) and the agent's last ~5 replies, from the conversation log. Secrets scrubbed first |
| Question | "Is this coding agent stuck or going in circles? Consider repeated attempts, edits that don't change results, and claims that don't match results." |
| Answer format | JSON: `{"stuck": true or false, "reason": "<one sentence>", "confidence": 0.0 to 1.0}` |
| Counts as | Strong signal when `stuck` is true and `confidence >= 0.7` |
| Where it runs | Through the backend (proposed endpoint `POST /v1/judge`), so the API key stays on the server and the hook stays fast. Fail open: no answer within ~5 seconds = no signal |

The hook only collects and sends the recent activity. The backend owns the model call.

---

## What happens after a signal fires

The hook moves the stuck episode up the help ladder one step at a time:

1. **Forum fix:** search the forum and hand over a known fix.
2. **Rewind + fresh approach (v1.5):** if the signal was whack-a-mole, edit-run-same-output or the judge, and there
   are 3+ edits to undo, offer to roll the files back to a clean checkpoint. See [rewind.md](rewind.md).
3. **Specialist team:** run `run_specialists`.
4. **Open Question:** post it so a future agent or model can solve it.

---

## Future signals (not in v1)

| Signal | Rule idea | Data needed | Strength |
| --- | --- | --- | --- |
| Tests not improving | The failing test count (read from "5 failed" in pytest or jest output) does not drop over 3+ runs | Command output | Medium |
| Editing the same spot | The same file edited 5+ times without the goal command passing | Edits | Medium |
| Going in circles | After an edit, a file returns to a version it had earlier (same content hash) | Edits | Strong |
| Lost or wandering | 15+ reads or searches in a row with no edit and no successful run | Reads, searches | Medium |
| Repeating itself | A new reply closely matches an earlier one (word overlap) | Conversation log | Medium |
| Approach switching | Phrases like "let me try a different approach", "that didn't work", "actually", apologies, counted per stretch of work | Conversation log | Weak |
| Long time, no progress | 10+ minutes or 25+ actions since the last progress | Action history | Medium (in v1 it only triggers the judge) |

### More ideas, not yet validated

- **Reinstall loops:** deleting and reinstalling dependencies again and again (`rm -rf node_modules && npm install`,
  recreating a virtualenv).
- **Reverting its own work:** `git checkout --`, `git restore` or `git stash` on files it just edited.
- **Debug-print flood:** adding more and more print or log statements to the same area.
- **Scope creep:** edits spreading into many files unrelated to the error.
- **Disabling checks:** skipping tests, `|| true`, `--no-verify`, `@ts-ignore`. Already used as a fake-fix check
  before posting; could also count as struggle.
- **Context compaction mid-task:** the conversation getting long enough to be compacted (`PreCompact` event) while
  the goal command still fails.
- **Cost burn:** token or cost use climbing fast with no progress.

---

## Open decisions

- [ ] Final thresholds for each v1 signal, after testing on the trap tasks
- [ ] The pushback and success-claim phrase lists
- [ ] Judge trigger numbers (20 actions / 10 minutes / once per 10 actions)
- [ ] Backend team confirms `POST /v1/judge`, or we call the model from the hook directly
