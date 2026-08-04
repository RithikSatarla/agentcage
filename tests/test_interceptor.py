"""Capture tests for the interceptor.

No network: httpx runs against ``MockTransport`` and requests against a local
adapter, so the real client pipeline (and therefore the real hook dispatch) is
exercised while the socket layer is replaced.
"""

import json

import httpx
import requests
from requests.adapters import HTTPAdapter
from requests.models import Response

from part_b.interceptor import REDACTED, Interceptor, redact_headers
from part_b.stripe_mock import StripeMock

# --- helpers -----------------------------------------------------------------


def echo_transport(status=200, payload=None):
    """An httpx transport that returns a fixed JSON response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload if payload is not None else {"ok": True})

    return httpx.MockTransport(handler)


class StaticAdapter(HTTPAdapter):
    """A requests adapter that answers locally instead of opening a socket."""

    def __init__(self, status=200, body=b'{"ok": true}'):
        self.status = status
        self.body = body
        super().__init__()

    def send(self, request, **kwargs):
        response = Response()
        response.status_code = self.status
        response._content = self.body
        response.headers["Content-Type"] = "application/json"
        response.url = request.url
        response.request = request
        return response


# --- redaction ---------------------------------------------------------------


def test_redact_headers_removes_credentials_but_keeps_the_rest():
    out = redact_headers(
        {
            "Authorization": "Bearer sk_live_supersecret",
            "Cookie": "session=abc",
            "X-Api-Key": "sk_live_other",
            "Content-Type": "application/json",
            "Idempotency-Key": "abc-123",
        }
    )

    assert out["authorization"] == REDACTED
    assert out["cookie"] == REDACTED
    assert out["x-api-key"] == REDACTED
    # non-credential headers survive, because they matter for replay
    assert out["content-type"] == "application/json"
    assert out["idempotency-key"] == "abc-123"


# --- httpx -------------------------------------------------------------------


def test_httpx_capture_records_method_path_and_body():
    cage = Interceptor()
    client = httpx.Client(transport=echo_transport(), event_hooks=cage.httpx_event_hooks())

    client.post(
        "https://api.stripe.com/v1/charges?expand=customer",
        json={"amount": 5000, "currency": "usd"},
        headers={"Authorization": "Bearer sk_live_secret"},
    )

    assert len(cage.traces) == 1
    trace = cage.traces[0]
    assert trace.method == "POST"
    assert trace.host == "api.stripe.com"
    assert trace.path == "/v1/charges"
    assert trace.query == "expand=customer"
    assert json.loads(trace.body) == {"amount": 5000, "currency": "usd"}
    assert trace.headers["authorization"] == REDACTED
    assert trace.status_code == 200
    assert trace.is_write is True
    assert trace.timestamp.endswith("+00:00")


def test_httpx_capture_does_not_modify_the_response_seen_by_the_caller():
    cage = Interceptor()
    payload = {"id": "ch_1", "amount": 5000}
    client = httpx.Client(
        transport=echo_transport(201, payload), event_hooks=cage.httpx_event_hooks()
    )

    response = client.post("https://api.stripe.com/v1/charges", json={"amount": 5000})

    # the interceptor is passive: the caller gets exactly what the transport returned
    assert response.status_code == 201
    assert response.json() == payload


def test_httpx_get_is_not_classified_as_a_write():
    cage = Interceptor()
    client = httpx.Client(transport=echo_transport(), event_hooks=cage.httpx_event_hooks())

    client.get("https://api.stripe.com/v1/charges/ch_1")

    assert cage.traces[0].is_write is False
    assert cage.writes == []


def test_attach_httpx_preserves_existing_hooks():
    seen = []
    cage = Interceptor()
    client = httpx.Client(
        transport=echo_transport(), event_hooks={"request": [lambda r: seen.append(r.method)]}
    )
    cage.attach_httpx(client)

    client.get("https://example.com/thing")

    assert seen == ["GET"]  # pre-existing hook still ran
    assert len(cage.traces) == 1


def test_multiple_requests_are_correlated_to_their_own_responses():
    cage = Interceptor()

    def handler(request):
        return httpx.Response(200 if request.url.path == "/a" else 418, json={})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), event_hooks=cage.httpx_event_hooks()
    )

    client.get("https://example.com/a")
    client.get("https://example.com/b")

    by_path = {t.path: t.status_code for t in cage.traces}
    assert by_path == {"/a": 200, "/b": 418}


# --- requests ----------------------------------------------------------------


def test_requests_capture_records_the_request():
    cage = Interceptor()
    session = requests.Session()
    session.mount("https://", StaticAdapter())
    cage.attach_requests(session)

    session.post(
        "https://api.stripe.com/v1/refunds",
        data={"charge": "ch_1", "amount": "500"},
        headers={"Authorization": "Bearer sk_live_secret"},
    )

    assert len(cage.traces) == 1
    trace = cage.traces[0]
    assert trace.method == "POST"
    assert trace.path == "/v1/refunds"
    assert "charge=ch_1" in trace.body
    assert trace.headers["authorization"] == REDACTED
    assert trace.status_code == 200


def test_requests_capture_returns_the_response_unmodified():
    cage = Interceptor()
    session = requests.Session()
    session.mount("https://", StaticAdapter(status=402, body=b'{"error": "card_declined"}'))
    cage.attach_requests(session)

    response = session.post("https://api.stripe.com/v1/charges", data={"amount": "1"})

    assert response.status_code == 402
    assert response.json() == {"error": "card_declined"}


# --- trace management --------------------------------------------------------


def test_dump_writes_traces_to_disk(tmp_path):
    cage = Interceptor()
    cage.record("DELETE", "https://api.example.com/v1/things/1", {"Authorization": "t"}, None)

    target = cage.dump(str(tmp_path / "run.json"))
    written = json.loads(target.read_text(encoding="utf-8"))

    assert written["count"] == 1
    assert written["traces"][0]["method"] == "DELETE"
    assert written["traces"][0]["headers"]["authorization"] == REDACTED


def test_clear_empties_the_buffer():
    cage = Interceptor()
    cage.record("POST", "https://example.com/x")
    assert len(cage) == 1
    cage.clear()
    assert len(cage) == 0


# --- the two halves together -------------------------------------------------


def test_interceptor_plus_stripe_mock_catches_a_double_refund():
    """The framework's actual thesis, end to end.

    An agent that retries a refund it already made is a no-op against a replay fixture
    and a real double-refund against stateful state. Here the second attempt is
    rejected, and the interceptor holds the evidence of both write attempts.
    """
    cage = Interceptor()
    stripe = StripeMock(clock=lambda: 1_750_000_000)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        status, payload = stripe.handle(request.method, request.url.path, body)
        return httpx.Response(status, json=payload)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), event_hooks=cage.httpx_event_hooks()
    )

    charge = client.post("https://api.stripe.com/v1/charges", json={"amount": 5000}).json()
    first = client.post(
        "https://api.stripe.com/v1/refunds", json={"charge": charge["id"], "amount": 5000}
    )
    second = client.post(
        "https://api.stripe.com/v1/refunds", json={"charge": charge["id"], "amount": 5000}
    )

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "charge_already_refunded"

    # the customer was refunded once, not twice
    assert stripe.get_charge(charge["id"]).amount_refunded == 5000

    # and every write attempt was captured for the record
    assert [t.path for t in cage.writes] == ["/v1/charges", "/v1/refunds", "/v1/refunds"]
