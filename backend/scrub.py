"""Remove secrets and personal details from text agents send us (PROJECT_PLAN.md, "Safety checks").

The hook already scrubs on the laptop (plugin/hook/backend.py); this is the second, server-side pass.
"""

import re

SECRET = "<secret>"

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), SECRET),
    # API keys and tokens with a known shape
    (re.compile(r"\b(?:sk|pk|rk)-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}"), SECRET),   # Anthropic, OpenAI, Stripe
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), SECRET),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), SECRET),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), SECRET),                                   # AWS access keys
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), SECRET),                                  # Slack
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), SECRET),                                       # Google
    (re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}"), SECRET),                          # JWTs
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + SECRET),
    # key=value / key: value for secret-looking keys
    (re.compile(r"(?i)\b(\w*?(?:password|passwd|pwd|secret(?:[_-]?key)?|token|api[_-]?key|access[_-]?key|private[_-]?key))"
                r"([\"']?\s*[=:]\s*[\"']?)[^\s\"'&,;]{3,}"), r"\1\2" + SECRET),
    # Credentials in connection strings: postgres://user:pass@host
    (re.compile(r"([a-zA-Z][\w+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1<user>:" + SECRET + "@"),
    # Emails (but not git@github.com remotes or package@1.2.3 versions)
    (re.compile(r"(?<![\w.+-])(?!git@)[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"), "<email>"),
    # Home folders: C:\Users\<name>  C:/Users/<name>  /Users/<name>  /home/<name>
    (re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"'<>|:*?]+"), "~"),
    (re.compile(r"(?<![\w.:/~-])/(?:Users|home)/[^/\s\"'<>]+"), "~"),
]


def scrub(text: str | None) -> str | None:
    """`text` with secrets, emails and home paths replaced. None stays None."""
    if text is None:
        return None
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text
