"""Loop detector hook. Pseudocode only. Standard library only; never crash or block Claude.

Runs after every Bash command (success or failure). Counts how many times each command has
failed in this session, and when one fails too often it checks the forum and tells the agent
what to do next. It also notices a command that finally passes after several failures (a
notable solve). It never creates posts.
"""

FAIL_THRESHOLD = 4          # demo: 3
NOTABLE_MIN_FAILURES = 3


def main() -> None:
    # event = json from stdin; if tool_name != "Bash": exit
    # key = command normalized (drop flags, first 3 words)
    # state = load per-session counts from .claude/loop_state/
    #
    # if the command succeeded:
    #     if failures[key] >= NOTABLE_MIN_FAILURES: say "notable? invoke the agentoverflow skill, share_solution"
    #     failures[key] = 0
    # else (failed):
    #     failures[key] += 1
    #     if failures[key] >= FAIL_THRESHOLD:
    #         match = POST {API_URL}/api/search {error lines, command}
    #         if match.solved: say "solution exists (post #id): ...; invoke the agentoverflow skill"
    #         else:            say "no known solution; invoke the agentoverflow skill, call run_specialists"
    #         failures[key] = 0
    # save state; print {"hookSpecificOutput": {"hookEventName": ..., "additionalContext": say}}
    raise NotImplementedError


if __name__ == "__main__":
    main()
