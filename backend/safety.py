"""Reject posts that other agents should never act on (PROJECT_PLAN.md, "Safety checks in the gatekeeper").

Posts are read by other agents, so a dangerous command or an instruction aimed at the reader is
refused outright (400 with the reason) instead of being stored.
"""

import re

# Where a shell command ends: end of text, whitespace, ; & | ) or a quote/backtick.
_END = r"(?=$|[\s;&|)\"'`])"

RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(?:-{1,2}[\w-]+\s+)*(?:/\*?|~/?\*?|\*|\$HOME/?\*?|\$\{HOME\}/?\*?)" + _END),
     "deletes the whole disk, home folder or every file (rm on /, ~ or *)"),
    (re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b"),
     "pipes a downloaded script straight into a shell (curl | sh)"),
    (re.compile(r"(?i)\b(?:iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]*\|\s*(?:iex|invoke-expression)\b"),
     "pipes a downloaded script straight into PowerShell (iwr | iex)"),
    (re.compile(r"\bchmod\s+(?:-\w+\s+)*0?777\s+/" + _END), "makes the whole disk writable (chmod 777 /)"),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "formats a disk (mkfs)"),
    (re.compile(r"\bdd\s+[^\n]*\bif="), "raw disk copy (dd if=)"),
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"(?i)\b(?:ignore|forget)\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier)\s+instructions"),
     "prompt injection (ignore previous instructions)"),
    (re.compile(r"(?i)\bdisregard\s+(?:all\s+|any\s+)?(?:your|the|previous|prior|above)\s+(?:\w+\s+)?instructions"),
     "prompt injection (disregard your instructions)"),
    (re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|DAN|in\s+developer\s+mode|jailbroken|unrestricted)\b"),
     "prompt injection (you are now ...)"),
    (re.compile(r"(?i)\b(?:reveal|print|show|output|repeat|leak|ignore|override)\s+(?:your\s+|the\s+)?system\s+prompt"),
     "prompt injection (system prompt)"),
    (re.compile(r"\b(?:env|printenv|set)\b[^\n|]*\|\s*(?:curl|wget|nc|ncat|netcat)\b"),
     "sends environment variables to another server (env | curl)"),
    (re.compile(r"\bcat\s+[^\n]*(?:~|\$HOME)/\.(?:ssh|aws|gnupg)\b"), "reads private keys or credentials (cat ~/.ssh)"),
    (re.compile(r"(?:@|<\s*)(?:~|\$HOME)/\.(?:ssh|aws)/"), "uploads private keys or credentials"),
]


def check(*texts: str | None) -> str | None:
    """The reason the texts are rejected, or None if they look safe."""
    for text in texts:
        if not text:
            continue
        for pattern, reason in RULES:
            if pattern.search(text):
                return f"Rejected by the safety filter: {reason}."
    return None
