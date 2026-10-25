"""Mailer: how the backend sends an email to a human (docs/accounts.md).

A "provider" is the service that actually delivers the email. There are two:

  LogMailer     the default. Sends nothing; writes one log line per email (address masked). Safe for
                local runs and demos.
  ResendMailer  used only when RESEND_API_KEY is set. Sends through Resend's HTTP API
                (https://resend.com/docs/api-reference/emails/send-email):
                  POST https://api.resend.com/emails
                  Authorization: Bearer <RESEND_API_KEY>, Content-Type: application/json,
                  User-Agent: <required by Resend, https://resend.com/docs/api-reference/introduction>,
                  Idempotency-Key: <optional, unique per email, kept 24 hours>
                  {"from", "to": [..], "subject", "text", "html"} -> {"id"}

Settings (env):
  EMAIL_PROVIDER  "log" or "resend". Empty: "resend" if RESEND_API_KEY is set, else "log".
  RESEND_API_KEY  the Resend key. Never logged.
  EMAIL_FROM      sender, default "Breakpoint <onboarding@resend.dev>" (Resend's example sender; use an
                  address on a domain you verified in Resend for real users).

Standard library only (urllib), so it adds no dependency. send() never raises: it returns True if the
provider accepted the email.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("accounts.mailer")

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Breakpoint <onboarding@resend.dev>"
USER_AGENT = "breakpoint-backend/0.1"
TIMEOUT = 5.0


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str | None = None
    idempotency_key: str | None = None   # same key = same email; the provider sends it once


class Mailer(Protocol):
    name: str

    def send(self, email: Email) -> bool: ...


def mask(address: str) -> str:
    """"harsh@osu.edu" -> "h***@osu.edu". Logs never get the full address."""
    local, _, domain = (address or "").rpartition("@")
    return f"{local[:1]}***@{domain}" if local else "***"


class LogMailer:
    """Sends nothing. Logs who would get what, so a demo shows the email without a provider."""
    name = "log"

    def __init__(self) -> None:
        self.sent: list[Email] = []   # kept for tests and the demo

    def send(self, email: Email) -> bool:
        self.sent.append(email)
        log.warning("EMAIL (log only, not sent) to=%s subject=%r\n%s", mask(email.to), email.subject, email.text)
        return True


class ResendMailer:
    name = "resend"

    def __init__(self, api_key: str, sender: str = DEFAULT_FROM) -> None:
        self.api_key, self.sender = api_key, sender

    def send(self, email: Email) -> bool:
        body = {"from": self.sender, "to": [email.to], "subject": email.subject, "text": email.text}
        if email.html:
            body["html"] = email.html
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if email.idempotency_key:
            headers["Idempotency-Key"] = email.idempotency_key[:256]
        request = urllib.request.Request(RESEND_URL, data=json.dumps(body).encode("utf-8"),
                                         method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
                ok = 200 <= resp.status < 300
                if ok:
                    log.info("email sent via resend to=%s", mask(email.to))
                return ok
        except urllib.error.HTTPError as e:
            log.warning("resend refused the email to=%s: HTTP %s %s", mask(email.to), e.code,
                        e.read()[:300].decode("utf-8", "replace"))
            e.close()
            return False
        except Exception as e:  # noqa: BLE001  network problems must never break a request
            log.warning("resend unreachable for email to=%s: %s", mask(email.to), type(e).__name__)
            return False


def get_mailer() -> Mailer:
    """The provider the settings ask for. Falls back to logging when Resend has no key."""
    choice = os.environ.get("EMAIL_PROVIDER", "").strip().lower()
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if choice == "log":
        return LogMailer()
    if key and choice in ("", "resend"):
        return ResendMailer(key, os.environ.get("EMAIL_FROM", "").strip() or DEFAULT_FROM)
    if choice == "resend":
        log.warning("EMAIL_PROVIDER=resend but RESEND_API_KEY is empty; emails are only logged")
    return LogMailer()
