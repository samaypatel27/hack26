"""Struggle signals: is the agent struggling? (docs/struggle-signals.md)

Pure functions. They read the action history the notebook keeps and return the signals that
fired. No files, no network, standard library only, so they are easy to test.

History is a list of actions, oldest first:
  {"kind": "run",  "t": 1729.0, "cmd": "npm run build", "key": "npm run build",
   "ok": False, "err_fp": "d5b1a4b9", "out_fp": "77aa01c3"}
  {"kind": "edit", "t": 1730.0, "path": "src/db.ts"}
  {"kind": "read", "t": 1731.0, "path": "src/db.ts"}        # file reads and code searches
  {"kind": "user", "t": 1732.0}                              # the user sent a message
  {"kind": "rewind", "t": 1733.0, "to": "last_good"}         # rewind.py reset the files (docs/rewind.md)

A rewind resets the counts like progress does: the attempts before it were made on files that no
longer exist, so signals 1-3 and the judge only look at what happened after the last rewind.

The notebook builds "run" records with command_key() and fingerprint() from this file.

Entry points (one per moment the hook runs):
  check_after_run(history)               after every command          -> signals 1, 2, 3
  check_user_message(prompt, history)    when the user sends a message -> signal 6
  check_stop(history, last_reply)        when the agent tries to stop  -> signals 4, 5
  judge_due(history, now, last_judge)    should we ask the AI judge?
  is_struggling(signals)                 strong >= 1 or medium >= 2
"""

import hashlib
import re
import shlex
from dataclasses import dataclass
from typing import Any

# Starting thresholds; tune on the trap tasks.
SAME_ERROR_RUNS = 3          # signal 1
WHACK_A_MOLE_RUNS = 4        # signal 2
WHACK_A_MOLE_ERRORS = 3
SAME_OUTPUT_CYCLES = 3       # signal 3
JUDGE_ACTIONS = 20           # AI judge: actions without progress
JUDGE_SECONDS = 600          # AI judge: time without progress
JUDGE_EVERY = 10             # AI judge: at most once per this many actions

# Commands whose failure is normal (grep with no matches exits 1). Never counted as struggle.
PROBE_COMMANDS = {
    "grep", "rg", "find", "ls", "dir", "cat", "head", "tail", "which", "where", "type",
    "test", "[", "diff", "echo", "pwd", "git status", "git diff", "git log", "git show",
    "cd", "wc", "sort", "less", "more", "file", "stat", "tree", "printf", "true", "sleep",
    "findstr", "where.exe", "write-output", "write-host", "set-location", "select-string", "select-object",
    "test-path", "format-table", "format-list", "measure-object", "out-string", "sl", "gci", "cls", "clear",
}

PUSHBACK_PHRASES = [
    r"still (broken|failing|fails|not working|doesn'?t work|the same|erroring|crashing)",
    r"that'?s (wrong|not right|incorrect)",
    r"that is (wrong|not right|incorrect)",
    r"(does ?n'?t|does not|did ?n'?t|did not) (work|fix)",
    r"not (working|fixed)",
    r"same (error|issue|problem|thing)",
    r"you already tried",
    r"wrong again",
]
PUSHBACK_RE = re.compile(r"\b(" + "|".join(PUSHBACK_PHRASES) + r")\b")

SUCCESS_PHRASES = [
    r"(is|are|has been|have been|now) (fixed|resolved|working)",
    r"i'?ve (fixed|resolved)",
    r"should (now )?work( now)?",
    r"works now",
    r"this (fixes|resolves|solves)",
    r"(problem|issue|error|bug) is (solved|gone|fixed|resolved)",
    r"all set",
]
SUCCESS_RE = re.compile(r"\b(" + "|".join(SUCCESS_PHRASES) + r")\b")

ERROR_LINE_RE = re.compile(
    r"error|exception|traceback|failed|failure|fatal|not found|no such file|cannot|can't|"
    r"undefined|denied|refused|missing|invalid|unable",
    re.IGNORECASE,
)


@dataclass
class Signal:
    name: str       # e.g. "same_error_loop"
    strength: str   # "strong" | "medium" | "weak"
    reason: str     # one plain sentence, used in messages and logs
    key: str = ""   # the command it concerns, if any


# ---------------------------------------------------------------------------
# Helpers the notebook uses to record runs
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """One simple command inside a compound shell command."""
    text: str          # the command itself, e.g. `python -m unittest 2>&1`
    sep: str           # what follows it: "\n", ";", "&&", "||", "|" or "" (the end)
    piped: bool        # its output goes into a pipe (`pytest | tail`)


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
HERESTRING_START_RE = re.compile(r"@(['\"])\s*$")            # PowerShell: @' ... '@
ENV_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT_WORD_RE = re.compile(r"^(\d*>>?|&>|\d*>&\d+|<<?-?|<)(.*)$")

# Test and build runners: in a compound command, the run is about this part (`cd x && pytest; git status`).
CHECK_RE = re.compile(
    r"^(?:&\s*)?(?:"
    r"(?:python[\d.]*|py|python3)(?:\.exe)?\s+(?:-\S+\s+)*-m\s+(?:unittest|pytest|py\.test|tox|nox|mypy|doctest)\b"
    r"|(?:python[\d.]*|py)(?:\.exe)?\s+(?:-\S+\s+)*\S*(?:manage\.py\s+test|setup\.py\s+test|runtests\.py|test\w*\.py)\b"
    r"|(?:pytest|py\.test|tox|nox|mypy|jest|vitest|mocha|tsc|rspec|phpunit|ctest|gradle|mvn|make)(?:\.exe|\.cmd)?\b"
    r"|\./gradlew\b|\./mvnw\b"
    r"|(?:npm|pnpm|yarn|bun)(?:\.cmd)?\s+(?:test|t|run|run-script|build)\b"
    r"|node\s+(?:\S+\s+)*--test\b|npx\s+\S+|deno\s+test\b"
    r"|cargo\s+(?:test|build|check|run|clippy)\b|go\s+(?:test|build|vet)\b"
    r"|dotnet\s+(?:test|build)\b|cmake\s+--build\b|bundle\s+exec\s+rspec\b"
    r"|docker[\s-]+compose\s+(?:up|build|run)\b|docker\s+(?:build|run)\b"
    r")",
    re.IGNORECASE,
)


def split_heredocs(cmd: str) -> tuple[str, list[str]]:
    """(the command without heredoc / here-string bodies, the bodies). `python - <<'EOF' ... EOF` keeps
    `python - <<'EOF'`; the script inside is returned separately (notebook.py reads it for file writes)."""
    kept: list[str] = []
    bodies: list[str] = []
    lines = (cmd or "").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        ends = [m.group(2) for m in HEREDOC_RE.finditer(line) if not line[max(0, m.start() - 1):m.start()] == "<"]
        if HERESTRING_START_RE.search(line):
            ends.append("'@" if line.rstrip().endswith("'") else '"@')
        for end in ends:
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != end and not (end in ("'@", '"@')
                                                                       and lines[i].lstrip().startswith(end)):
                body.append(lines[i])
                i += 1
            if i < len(lines):
                tail = lines[i].lstrip()[len(end):] if end in ("'@", '"@') else ""
                if tail.strip():
                    kept.append(tail)   # `'@ | Set-Content x`: the rest of the closing line is still a command
            i += 1   # the end marker
            bodies.append("\n".join(body))
    return "\n".join(kept), bodies


def split_command(cmd: str) -> list[Segment]:
    """The simple commands in a compound one, in order. Splits on newlines, `;`, `&&`, `||` and `|`, outside
    quotes; heredoc bodies are skipped. Empty parts are dropped."""
    text, _ = split_heredocs(cmd)
    segments: list[Segment] = []
    current: list[str] = []
    quote = ""
    i = 0

    def flush(sep: str) -> None:
        part = "".join(current).strip()
        current.clear()
        if part:
            segments.append(Segment(part, sep, sep == "|"))
        elif segments and sep:
            segments[-1].sep = sep if segments[-1].sep in ("", "\n") else segments[-1].sep

    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < len(text):
                current.append(ch)
                i += 1
                ch = text[i]
            current.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "\\" and i + 1 < len(text) and text[i + 1] == "\n":
            i += 2   # a continued line
            current.append(" ")
            continue
        elif ch == "\n" or ch == ";":
            flush(ch)
            i += 1
            continue
        elif text.startswith("&&", i) or text.startswith("||", i):
            flush(text[i:i + 2])
            i += 2
            continue
        elif ch == "|" and not (i > 0 and text[i - 1] == ">"):
            flush("|")
            i += 1
            continue
        current.append(ch)
        i += 1
    flush("")
    return segments


def words_of(segment: str) -> list[str]:
    """The words of a simple command without env assignments, redirections and a leading call operator."""
    try:
        raw = shlex.split(segment.replace("\\", "/"), posix=True)   # Windows paths: keep them whole
    except ValueError:
        raw = segment.split()
    words: list[str] = []
    skip_next = False
    for w in raw:
        if skip_next:
            skip_next = False
            continue
        if not words and (ENV_WORD_RE.match(w) or w == "&"):
            continue
        redirect = REDIRECT_WORD_RE.match(w)
        if redirect:
            skip_next = not redirect.group(2)   # `> file`: the file is the next word
            continue
        words.append(w)
    return words


def is_check(segment: str) -> bool:
    """A test or build runner (`python -m unittest`, `pytest`, `npm test`, `cargo build`, ...)."""
    return bool(CHECK_RE.match(" ".join(words_of(segment))))


def check_index(segments: list[Segment]) -> int | None:
    """Index of the last check command in a compound command, or None."""
    found = None
    for n, seg in enumerate(segments):
        if is_check(seg.text):
            found = n
    return found


def _segment_key(segment: str) -> str:
    words = [w for w in words_of(segment) if not w.startswith("-")]
    return " ".join(words[:3])


def command_key(cmd: str) -> str:
    """Normalize a command so reruns with different flags count as the same command.

    In a compound command, the key is the check (test/build runner) it contains, so
    `cd app && NODE_ENV=test npm run build --verbose | tail` -> `npm run build`, and
    `python - <<'EOF' (patch) EOF` + `python -m unittest 2>&1 | tail -20; git status` -> `python unittest`.
    Without a check, the last command that is not a probe (git status, ls, cat, ...), else the last one.
    """
    segments = split_command(cmd)
    if not segments:
        return ""
    idx = check_index(segments)
    if idx is None:
        heads = [s for n, s in enumerate(segments) if n == 0 or not segments[n - 1].piped]   # not `| tail`
        keys = [_segment_key(s.text) for s in heads]
        real = [k for k in keys if k and not _is_probe(k)]
        return (real or [k for k in keys if k] or [""])[-1]
    return _segment_key(segments[idx].text)


def _normalize(text: str, keep_numbers: bool = False) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)                  # terminal colors
    text = re.sub(r"(?m)^exit code -?\d+\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[A-Za-z]:[\\/][^\s'\"]*", "<path>", text)          # C:\Users\...
    text = re.sub(r"(?<![\w.])/[^\s'\":]+", "<path>", text)            # /home/... /app/...
    text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*", "<time>", text)
    text = re.sub(r"\d+(\.\d+)?\s*(ms|s|sec|secs|seconds)\b", "<duration>", text)
    if not keep_numbers:
        text = re.sub(r"\d+(\.\d+)?", "<n>", text)                     # line numbers, sizes
    return re.sub(r"\s+", " ", text).strip().lower()


def fingerprint(text: str | None, errors_only: bool = True) -> str | None:
    """Turn error output into a short ID that is the same on every laptop.

    errors_only=True keeps the last few error-looking lines and drops numbers (line numbers
    differ between laptops). errors_only=False uses the whole output and keeps numbers, because
    for "did the output change?" a number like `total = 41` is the result itself.
    """
    if not text or not text.strip():
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if errors_only:
        error_lines = [ln for ln in lines if ERROR_LINE_RE.search(ln)]
        lines = error_lines[-5:] if error_lines else lines[-20:]
    else:
        lines = lines[-50:]
    normalized = _normalize("\n".join(lines), keep_numbers=not errors_only)
    if not normalized:
        return None
    return hashlib.sha1(normalized.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Small history helpers
# ---------------------------------------------------------------------------

def _is_probe(key: str) -> bool:
    words = key.lower().split()
    return (not words or words[0] in PROBE_COMMANDS or " ".join(words[:2]) in PROBE_COMMANDS
            or words[0].startswith("get-"))


def _runs(history: list[dict[str, Any]], key: str | None = None) -> list[tuple[int, dict[str, Any]]]:
    """(index, run) for real (non-probe) runs, optionally only for one command."""
    return [
        (i, a) for i, a in enumerate(history)
        if a.get("kind") == "run" and not _is_probe(a.get("key", ""))
        and (key is None or a.get("key") == key)
    ]


def _since_last_pass(history: list[dict[str, Any]], key: str) -> list[tuple[int, dict[str, Any]]]:
    """Runs of `key` after its most recent passing run (progress resets the count)."""
    runs = _runs(history, key)
    for pos in range(len(runs) - 1, -1, -1):
        if runs[pos][1].get("ok"):
            return runs[pos + 1:]
    return runs


def _edited_paths(history: list[dict[str, Any]], start: int, end: int) -> set[str]:
    return {a.get("path", "") for a in history[start + 1:end] if a.get("kind") == "edit"}


def _since_rewind(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The actions after the last rewind (all of them if there was none)."""
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("kind") == "rewind":
            return history[i + 1:]
    return history


def _current_turn_start(history: list[dict[str, Any]]) -> int:
    """Index of the last user message, or -1 if there is none."""
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("kind") == "user":
            return i
    return -1


# ---------------------------------------------------------------------------
# Signals 1-3: after every command
# ---------------------------------------------------------------------------

def check_after_run(history: list[dict[str, Any]]) -> list[Signal]:
    """Signals 1-3 for the command that just ran (the last run in the history)."""
    history = _since_rewind(history)
    runs = _runs(history)
    if not runs:
        return []
    key = runs[-1][1]["key"]
    signals = []
    for check in (_same_error_loop, _whack_a_mole, _edit_run_same_output):
        signal = check(history, key)
        if signal:
            signals.append(signal)
    return signals


def _same_error_loop(history: list[dict[str, Any]], key: str) -> Signal | None:
    """Signal 1: the same error, 3 times, with no pass of the command in between."""
    failures = [r for _, r in _since_last_pass(history, key) if not r.get("ok")]
    if not failures or not failures[-1].get("err_fp"):
        return None
    latest = failures[-1]["err_fp"]
    count = sum(1 for r in failures if r.get("err_fp") == latest)
    if count >= SAME_ERROR_RUNS:
        return Signal("same_error_loop", "strong", f"`{key}` failed {count} times with the same error.", key)
    return None


def _whack_a_mole(history: list[dict[str, Any]], key: str) -> Signal | None:
    """Signal 2: the same command fails 4+ times with 3+ different errors."""
    failures = [r for _, r in _since_last_pass(history, key) if not r.get("ok")]
    distinct = {r.get("err_fp") for r in failures if r.get("err_fp")}
    if len(failures) >= WHACK_A_MOLE_RUNS and len(distinct) >= WHACK_A_MOLE_ERRORS:
        return Signal(
            "whack_a_mole", "strong",
            f"`{key}` failed {len(failures)} times with {len(distinct)} different errors: each fix breaks something new.",
            key,
        )
    return None


def _edit_run_same_output(history: list[dict[str, Any]], key: str) -> Signal | None:
    """Signal 3: three edit -> rerun cycles in a row where the output did not change.

    A cycle counts when the agent edited something between two runs of the same command and
    the output fingerprint is identical. To avoid flagging normal work (editing unrelated files
    while the tests keep passing), a cycle only counts if the run failed, or if the edits touched
    a file that was also edited in the previous cycle (working on the same spot).
    Reruns with no edit in between are skipped: they are retries, which signal 1 covers.
    """
    runs = _runs(history, key)
    cycles = []   # oldest first: (output unchanged?, run failed?, files edited in between)
    for (i_prev, prev), (i_cur, cur) in zip(runs, runs[1:]):
        edits = _edited_paths(history, i_prev, i_cur)
        if edits:
            same = bool(cur.get("out_fp")) and cur.get("out_fp") == prev.get("out_fp")
            cycles.append((same, not cur.get("ok"), edits))

    streak = 0
    for pos in range(len(cycles) - 1, -1, -1):
        same, failed, edits = cycles[pos]
        if not same:
            break
        neighbors = [cycles[p][2] for p in (pos - 1, pos + 1) if 0 <= p < len(cycles)]
        same_spot = any(edits & other for other in neighbors)
        if not (failed or same_spot):
            break
        streak += 1

    if streak >= SAME_OUTPUT_CYCLES:
        return Signal(
            "edit_run_same_output", "strong",
            f"The code was changed {streak} times but `{key}` gives exactly the same result each time.",
            key,
        )
    return None


# ---------------------------------------------------------------------------
# Signal 6: when the user sends a message
# ---------------------------------------------------------------------------

def check_user_message(prompt: str, history: list[dict[str, Any]]) -> list[Signal]:
    """Signal 6: the user pushes back, or pastes an error we already saw this session."""
    text = prompt.lower().replace("\u2019", "'")
    match = PUSHBACK_RE.search(text)
    if match:
        return [Signal("user_pushback", "strong", f'The user said "{match.group(0)}".')]

    pasted = fingerprint(prompt)
    seen = {a.get("err_fp") for a in history if a.get("kind") == "run" and not a.get("ok")}
    if pasted and ERROR_LINE_RE.search(prompt) and pasted in seen:
        return [Signal("user_pushback", "strong", "The user pasted an error the agent already hit this session.")]
    return []


# ---------------------------------------------------------------------------
# Signals 4-5: when the agent tries to stop
# ---------------------------------------------------------------------------

def _is_check_run(run: dict[str, Any]) -> bool:
    if "check" in run:
        return bool(run["check"])
    return check_index(split_command(run.get("cmd", ""))) is not None


def _superseded(failed: dict[str, Any], turn_runs: list[dict[str, Any]]) -> bool:
    """A failed run that a later passing check run answers: the same runner on the same or a wider target
    (`python unittest` passing covers `python unittest tests.test_store`), or, for a failed command that is not a
    check at all (a patch script, a debug snippet), any later passing check."""
    later = turn_runs[turn_runs.index(failed) + 1:] if failed in turn_runs else []
    key = failed.get("key", "")
    for run in later:
        if not run.get("ok") or not _is_check_run(run):
            continue
        other = run.get("key", "")
        if not _is_check_run(failed) or key == other or key.startswith(other + " "):
            return True
    return False


def check_stop(history: list[dict[str, Any]], last_reply: str = "") -> list[Signal]:
    """Signals 4 and 5, looking only at the current turn (since the last user message)."""
    start = _current_turn_start(history)
    turn = history[start + 1:]
    signals = []

    # Signal 4: says done, isn't. Some command failed this turn and its last run still fails.
    turn_runs = [a for a in turn if a.get("kind") == "run" and not _is_probe(a.get("key", ""))]
    last_by_key: dict[str, dict[str, Any]] = {}
    for run in turn_runs:
        last_by_key[run["key"]] = run
    still_failing = [k for k, r in last_by_key.items() if not r.get("ok") and not _superseded(r, turn_runs)]
    if still_failing:
        key = still_failing[-1]
        signals.append(Signal("says_done_isnt", "strong", f"The agent is finishing, but `{key}` still fails.", key))

    # Signal 5: unchecked success claim. Something failed this turn, the agent edited code,
    # claims success, and ran nothing after its last edit.
    reply = last_reply.lower().replace("\u2019", "'")
    claim = SUCCESS_RE.search(reply)
    had_failure = any(not r.get("ok") for r in turn_runs)
    last_edit = max((i for i, a in enumerate(turn) if a.get("kind") == "edit"), default=-1)
    last_run = max((i for i, a in enumerate(turn) if a.get("kind") == "run"
                    and not _is_probe(a.get("key", ""))), default=-1)
    if claim and had_failure and last_edit > last_run:
        signals.append(Signal(
            "unchecked_success_claim", "strong",
            f'The agent says "{claim.group(0)}" but ran nothing to check it after its last edit.',
        ))
    return signals


# ---------------------------------------------------------------------------
# The AI judge and the final decision
# ---------------------------------------------------------------------------

def judge_due(history: list[dict[str, Any]], now: float, last_judge: int | None = None) -> bool:
    """Ask the AI judge when something failed, nothing has progressed for a while, and no
    strong rule fired. `last_judge` is the history length when the judge last ran."""
    if last_judge is not None and len(history) - last_judge < JUDGE_EVERY:
        return False
    # Last progress: the most recent passing run of a command that had failed before it, or a rewind.
    progress = len(history) - len(_since_rewind(history)) - 1
    failed_keys: set[str] = set()
    for i, a in _runs(history):
        if not a.get("ok"):
            failed_keys.add(a["key"])
        elif a["key"] in failed_keys:
            progress = max(progress, i)
            failed_keys.discard(a["key"])
    since = history[progress + 1:]
    if not any(a.get("kind") == "run" and not a.get("ok") and not _is_probe(a.get("key", "")) for a in since):
        return False   # no trouble since the last progress: normal long work, not struggle
    started = since[0].get("t", now) if since else now
    return len(since) >= JUDGE_ACTIONS or now - started >= JUDGE_SECONDS


def is_struggling(signals: list[Signal]) -> bool:
    """One strong signal, or two medium ones. Weak signals never decide on their own."""
    strong = sum(1 for s in signals if s.strength == "strong")
    medium = sum(1 for s in signals if s.strength == "medium")
    return strong >= 1 or medium >= 2
