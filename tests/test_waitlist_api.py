"""The waiting-list endpoint must never claim a signup it did not deliver.

That is the only property here worth protecting. A form that shows a tick while the
address goes nowhere loses real signups and nobody notices for weeks, so the case with
nothing configured is tested first and hardest.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

waitlist = pytest.importorskip("waitlist")


class FakeHandler(waitlist.handler):
    """The real handler with its socket replaced by buffers.

    Subclassed rather than duck-typed so the code under test is the code that ships:
    do_POST, _send and the validation all come from the module, and only the transport
    is substituted. BaseHTTPRequestHandler.__init__ is skipped deliberately, since it
    would try to read from a connection that does not exist.
    """

    def __init__(self, body: bytes, headers: dict | None = None):  # noqa: D107
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body))}
        self.headers.update(headers or {})
        self.status = None
        self.sent_headers = {}

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    @property
    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def post(body: dict, monkeypatch, env: dict | None = None):
    """Drive do_POST with a fake handler and a controlled environment."""
    for key in ("WAITLIST_WEBHOOK_URL", "RESEND_API_KEY", "WAITLIST_TO",
                "WAITLIST_FROM"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    raw = json.dumps(body).encode("utf-8")
    fake = FakeHandler(raw)
    fake.do_POST()
    return fake


# --- the invariant -----------------------------------------------------------

def test_unconfigured_endpoint_refuses_instead_of_faking_success(monkeypatch):
    """With no destination set, a signup must fail loudly. This is the whole point."""
    fake = post({"email": "someone@example.com"}, monkeypatch)
    assert fake.status == 503
    assert fake.payload["ok"] is False
    assert "not connected" in fake.payload["error"]


def test_a_failing_destination_is_reported_not_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise waitlist.urllib.error.URLError("no route to host")

    monkeypatch.setattr(waitlist.urllib.request, "urlopen", boom)
    fake = post({"email": "someone@example.com"}, monkeypatch,
                {"WAITLIST_WEBHOOK_URL": "https://hook.example.invalid/x"})
    assert fake.status == 502
    assert fake.payload["ok"] is False


def test_a_configured_webhook_receives_the_address(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def capture(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr(waitlist.urllib.request, "urlopen", capture)
    fake = post({"email": "someone@example.com", "source": "landing"}, monkeypatch,
                {"WAITLIST_WEBHOOK_URL": "https://hook.example.invalid/x"})

    assert fake.status == 200
    assert fake.payload["ok"] is True
    assert seen["body"] == {"email": "someone@example.com", "source": "landing"}


# --- input handling ----------------------------------------------------------

@pytest.mark.parametrize("address", ["", "   ", "nope", "no@domain", "a@b",
                                     "@example.com", "someone@", "two @spaces.com"])
def test_addresses_that_cannot_be_delivered_are_rejected(address, monkeypatch):
    fake = post({"email": address}, monkeypatch,
                {"WAITLIST_WEBHOOK_URL": "https://hook.example.invalid/x"})
    assert fake.status == 400
    assert fake.payload["ok"] is False


@pytest.mark.parametrize("address", [
    "someone@example.com",
    "first.last+tag@sub.example.co.uk",
    "a@b.io",
])
def test_real_looking_addresses_are_accepted(address, monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(waitlist.urllib.request, "urlopen",
                        lambda *a, **k: Response())
    fake = post({"email": address}, monkeypatch,
                {"WAITLIST_WEBHOOK_URL": "https://hook.example.invalid/x"})
    assert fake.status == 200, f"{address} should be accepted"


def test_the_honeypot_absorbs_bots_without_forwarding(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a honeypot hit must never reach the destination")

    monkeypatch.setattr(waitlist.urllib.request, "urlopen", explode)
    fake = post({"email": "bot@example.com", "company": "Spam Co"}, monkeypatch,
                {"WAITLIST_WEBHOOK_URL": "https://hook.example.invalid/x"})
    assert fake.status == 200          # the bot learns nothing
    assert fake.payload["ok"] is True


def test_an_oversized_body_is_refused(monkeypatch):
    raw = json.dumps({"email": "a@b.io", "pad": "x" * 8000}).encode("utf-8")
    fake = FakeHandler(raw)
    fake.do_POST()
    assert fake.status == 413


def test_a_body_that_is_not_json_is_refused(monkeypatch):
    fake = FakeHandler(b"this is not json")
    fake.do_POST()
    assert fake.status == 400
    assert fake.payload["ok"] is False


def test_an_unexpected_error_never_reports_success(monkeypatch):
    """Any path out of do_POST that is not a delivered signup must not be a 200 ok."""
    def explode(*args, **kwargs):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(waitlist, "_forward", explode)
    fake = post({"email": "someone@example.com"}, monkeypatch,
                {"WAITLIST_WEBHOOK_URL": "https://hook.example.invalid/x"})
    assert fake.status == 500
    assert fake.payload["ok"] is False


# --- the page's own form ------------------------------------------------------
#
# The booking page posts straight to Formspree rather than through the function
# above, so these test that page. The function is kept as the self-hosted alternative and its
# behaviour is still covered by everything above.

PAGE = (ROOT / "website" / "book.html").read_text(encoding="utf-8")
FORMSPREE = "https://formspree.io/f/mnpaqoaz"


def test_the_form_posts_to_the_configured_endpoint():
    assert FORMSPREE in PAGE


def test_the_form_asks_formspree_for_json():
    """Without the Accept header Formspree answers with a redirect to its own page.

    The submit handler would then be unable to tell success from failure, and the
    honest-failure behaviour below would be decorative.
    """
    assert '"Accept": "application/json"' in PAGE


def test_the_form_only_reports_success_on_an_accepted_response():
    """The tick is gated on result.ok, never shown unconditionally."""
    assert "if (result.ok) {" in PAGE
    assert "Request received" in PAGE


def test_the_form_offers_a_fallback_when_signup_fails():
    """If the service is down or refuses, the visitor still has a way through."""
    assert "mailto:rithiksatarla@gmail.com" in PAGE
    assert "Email it to me instead" in PAGE


def test_the_honeypot_field_is_named_for_formspree():
    """_gotcha is dropped by Formspree as well as by our own check."""
    assert 'name="_gotcha"' in PAGE


def test_the_booking_form_asks_what_the_agent_writes_to():
    """The question that makes the session useful before it starts."""
    assert "What does your agent write to?" in PAGE
