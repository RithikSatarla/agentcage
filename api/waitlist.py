"""Waiting-list endpoint, deployed as a Vercel serverless function.

The one rule this file exists to enforce: **never tell someone they are on the list
unless their address actually went somewhere.** A form that posts into the void and
shows a tick is worse than no form, because the signups are gone and nobody finds out
for weeks.

So there is no silent success path. The address is forwarded to whichever destination
is configured, and if none is configured the endpoint answers 503 and the page tells
the visitor to email instead. Configure exactly one of:

    WAITLIST_WEBHOOK_URL   any endpoint that accepts a JSON POST. A Zapier or Make
                           hook, a Google Apps Script, a Buttondown or ConvertKit
                           subscribe URL, a Slack incoming webhook.
    RESEND_API_KEY         plus WAITLIST_TO and WAITLIST_FROM, to have each signup
                           emailed to you through Resend.

Set them in the Vercel dashboard under Settings, Environment Variables, then redeploy.
Standard library only, so there is nothing to install and no build step.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

# Deliberately permissive. Address validation beyond "looks like an address" rejects
# real people (long TLDs, plus-addressing, unicode domains) to catch typos it cannot
# actually catch. The destination service does the real verification.
EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

MAX_BODY = 4096          # a signup is a few hundred bytes; anything larger is noise
TIMEOUT = 8              # seconds, comfortably inside the function's own limit


class Rejected(Exception):
    """Bad input from the client. Carries the status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _forward(email: str, source: str) -> str:
    """Send the address to whichever destination is configured.

    Returns the name of the destination used. Raises if none is configured, or if the
    configured one refuses, so the caller can report the failure rather than swallow it.
    """
    webhook = os.environ.get("WAITLIST_WEBHOOK_URL", "").strip()
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()

    if webhook:
        payload = json.dumps({"email": email, "source": source}).encode("utf-8")
        request = urllib.request.Request(
            webhook, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "agentcage-waitlist"},
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status >= 300:
                raise Rejected("the waiting list service refused the signup", 502)
        return "webhook"

    if resend_key:
        to = os.environ.get("WAITLIST_TO", "").strip()
        sender = os.environ.get("WAITLIST_FROM", "").strip()
        if not to or not sender:
            raise Rejected("signup is not fully configured", 503)
        payload = json.dumps({
            "from": sender,
            "to": [to],
            "subject": f"AgentCage waiting list: {email}",
            "text": f"{email}\nsource: {source}\n",
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://api.resend.com/emails", data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {resend_key}"},
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status >= 300:
                raise Rejected("the mail service refused the signup", 502)
        return "resend"

    # Nothing configured. This is the case that must never look like success.
    raise Rejected("signup is not connected yet", 503)


def _read(handler: "BaseHTTPRequestHandler") -> dict:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        raise Rejected("malformed request")
    if length <= 0:
        raise Rejected("empty request")
    if length > MAX_BODY:
        raise Rejected("request too large", 413)
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Rejected("body was not valid JSON")


class handler(BaseHTTPRequestHandler):        # noqa: N801 - Vercel requires this name

    def _send(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:                # noqa: N802 - http.server naming
        try:
            data = _read(self)
            if not isinstance(data, dict):
                raise Rejected("body was not an object")

            # Honeypot: a field styled out of sight that only a bot fills in. Answered
            # 200 so the bot has nothing to learn, but nothing is forwarded.
            if (data.get("company") or "").strip():
                self._send(200, {"ok": True})
                return

            email = (data.get("email") or "").strip()
            if not email:
                raise Rejected("enter an email address")
            if len(email) > 254 or not EMAIL.match(email):
                raise Rejected("that does not look like an email address")

            source = (data.get("source") or "website")[:64]
            destination = _forward(email, source)
            self._send(200, {"ok": True, "destination": destination})

        except Rejected as exc:
            self._send(exc.status, {"ok": False, "error": exc.message})
        except (urllib.error.URLError, TimeoutError):
            self._send(502, {"ok": False,
                             "error": "could not reach the waiting list service"})
        except Exception:                     # noqa: BLE001 - never 200 on an unknown
            self._send(500, {"ok": False, "error": "something went wrong"})

    def do_GET(self) -> None:                 # noqa: N802
        """Report whether a destination is configured, without revealing which."""
        configured = bool(os.environ.get("WAITLIST_WEBHOOK_URL", "").strip()
                          or os.environ.get("RESEND_API_KEY", "").strip())
        self._send(200, {"configured": configured})
