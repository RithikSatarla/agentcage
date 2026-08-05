"""The two pieces that put a model in front of real client code.

``ModelServer`` has to be dumb: it must not invent a status, a body or a rule, because
anything it decided would show up in a trace as though the model had decided it. These
tests pin that down, and pin down the one thing it is allowed to do — tell the model
which address it is answering on.

``redirect`` has to catch every request. The one time it did not, the traffic went to the
real api.github.com and only a certificate failure stopped it, so the origin-matching
rules are tested directly.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

import pytest

requests = pytest.importorskip("requests")

from part_b.redirect import Redirect, _origin, redirect      # noqa: E402
from part_b.server import ModelServer                        # noqa: E402


class Recorder:
    """A minimal model: echoes what it was asked and counts writes."""

    def __init__(self):
        self.base_url = "https://example.invalid"
        self.applied = []

    def handle(self, method: str, path: str,
               body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        if path.startswith("/boom"):
            raise RuntimeError("model exploded")
        if method in ("POST", "PUT", "DELETE"):
            self.applied.append((method, path, body))
            if path.startswith("/nocontent"):
                return 204, None
            return 201, {"method": method, "path": path, "body": body}
        return 200, {"method": method, "path": path, "base_url": self.base_url}


# --- the server --------------------------------------------------------------

def test_the_server_passes_the_request_through_unchanged():
    with ModelServer(Recorder()) as srv:
        r = requests.post(f"{srv.base_url}/things?a=1", json={"name": "x"}, timeout=5)
    assert r.status_code == 201
    assert r.json()["body"] == {"name": "x"}
    # The model is handed the query string too; see the next test for why.
    assert r.json()["path"] == "/things?a=1"


def test_the_server_records_what_it_was_asked():
    with ModelServer(Recorder()) as srv:
        requests.get(f"{srv.base_url}/a", timeout=5)
        requests.post(f"{srv.base_url}/b", json={"k": 1}, timeout=5)
        seen = list(srv.requests)
        writes = srv.writes()
    assert [r.method for r in seen] == ["GET", "POST"]
    assert [r.seq for r in seen] == [1, 2]
    assert [r.method for r in writes] == ["POST"]


def test_the_query_string_is_given_to_the_model():
    """?ref= decides which branch a read sees; dropping it changes the answer."""
    with ModelServer(Recorder()) as srv:
        r = requests.get(f"{srv.base_url}/contents/x?ref=feature", timeout=5)
        recorded = srv.requests[0]
    assert r.json()["path"] == "/contents/x?ref=feature"
    assert recorded.query == "ref=feature"
    assert recorded.path == "/contents/x"


def test_no_content_responses_carry_no_body():
    with ModelServer(Recorder()) as srv:
        r = requests.put(f"{srv.base_url}/nocontent", json={}, timeout=5)
    assert r.status_code == 204
    assert r.content == b""


def test_a_model_that_raises_does_not_take_the_server_down():
    with ModelServer(Recorder()) as srv:
        r = requests.get(f"{srv.base_url}/boom", timeout=5)
        after = requests.get(f"{srv.base_url}/fine", timeout=5)
    assert r.status_code == 500
    assert after.status_code == 200


def test_the_server_tells_the_model_where_it_is_listening():
    model = Recorder()
    with ModelServer(model) as srv:
        assert model.base_url == srv.base_url


def test_advertise_overrides_what_the_model_claims_to_be():
    """Under a redirect the client still believes it is talking to the real API."""
    model = Recorder()
    with ModelServer(model, advertise="https://api.github.com") as srv:
        assert model.base_url == "https://api.github.com"
        assert srv.base_url.startswith("http://127.0.0.1:")


def test_a_dropped_response_still_leaves_the_write_applied():
    """The shape of a duplicate write: the write landed, the caller never learned."""
    model = Recorder()
    state = {"armed": True}

    def fault(request):
        if state["armed"] and request.method == "POST":
            state["armed"] = False
            return "drop"
        return None

    with ModelServer(model, fault=fault) as srv:
        with pytest.raises(requests.exceptions.RequestException):
            requests.post(f"{srv.base_url}/things", json={"n": 1}, timeout=5)
        assert model.applied == [("POST", "/things", {"n": 1})]
        assert srv.requests[0].dropped is True
        assert srv.requests[0].to_dict()["dropped"] is True


def test_the_port_is_released_when_the_block_ends():
    srv = ModelServer(Recorder()).start()
    base = srv.base_url
    srv.stop()
    with pytest.raises(requests.exceptions.RequestException):
        requests.get(base, timeout=2)


# --- the redirect ------------------------------------------------------------

def test_a_redundant_default_port_is_the_same_origin():
    """PyGithub asks for api.github.com:443; requests.put asks for api.github.com."""
    assert _origin("https://api.github.com:443/repos/a/b") == "https://api.github.com"
    assert _origin("https://api.github.com/repos/a/b") == "https://api.github.com"
    assert _origin("http://example.test:80/x") == "http://example.test"


def test_a_non_default_port_is_a_different_origin():
    assert _origin("https://example.test:8443/x") == "https://example.test:8443"


def test_the_host_is_matched_case_insensitively():
    assert _origin("https://API.GitHub.com/x") == "https://api.github.com"


def test_only_the_origin_is_replaced():
    state = Redirect({"https://api.github.com": "http://127.0.0.1:9999"})
    out = state.apply("https://api.github.com/repos/a/b/contents/x.md?ref=main")
    assert out == "http://127.0.0.1:9999/repos/a/b/contents/x.md?ref=main"


def test_an_unmapped_origin_is_left_completely_alone():
    """A run reaching an API nobody modelled must fail loudly, not be answered."""
    state = Redirect({"https://api.github.com": "http://127.0.0.1:9999"})
    url = "https://example.atlassian.net/rest/api/2/issue"
    assert state.apply(url) == url
    assert state.passed_through == [url]
    assert state.rewrites == []


def test_redirect_moves_real_traffic_to_the_model():
    model = Recorder()
    with ModelServer(model, advertise="https://api.github.com") as srv:
        with redirect({"https://api.github.com": srv.base_url}) as red:
            r = requests.post("https://api.github.com/repos/a/b/issues",
                              json={"title": "t"}, timeout=5)
        rewritten = list(red.rewrites)
    assert r.status_code == 201
    assert model.applied == [("POST", "/repos/a/b/issues", {"title": "t"})]
    assert len(rewritten) == 1
    assert rewritten[0][0].startswith("https://api.github.com")


def test_the_patch_is_removed_when_the_block_ends():
    original = requests.sessions.Session.send
    with redirect({"https://api.github.com": "http://127.0.0.1:1"}):
        assert requests.sessions.Session.send is not original
    assert requests.sessions.Session.send is original


def test_the_patch_is_removed_even_if_the_block_raises():
    original = requests.sessions.Session.send
    with pytest.raises(ValueError):
        with redirect({"https://api.github.com": "http://127.0.0.1:1"}):
            raise ValueError("boom")
    assert requests.sessions.Session.send is original


# --- the two together --------------------------------------------------------

def test_a_client_library_reaches_the_model_through_both():
    """The whole path: unmodified client, redirect, socket, model, back."""
    from part_b.github_mock import GitHubMock

    model = GitHubMock(repos=["acme/widgets"], clock=lambda: 1_700_000_000,
                       base_url="https://api.github.com")
    with ModelServer(model, advertise="https://api.github.com") as srv:
        with redirect({"https://api.github.com": srv.base_url}):
            r = requests.post("https://api.github.com/repos/acme/widgets/issues",
                              json={"title": "Broken build"}, timeout=5)
    assert r.status_code == 201
    assert r.json()["number"] == 1
    assert json.loads(r.text)["title"] == "Broken build"
    assert len(model.issues["acme/widgets"]) == 1
