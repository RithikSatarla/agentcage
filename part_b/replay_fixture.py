"""A recorded-response fixture: the thing AgentCage is an argument against.

Most agent test suites mock HTTP this way. You call the real API once, save what came
back, and replay it forever. ``responses``, ``vcrpy``, ``betamax``, ``nock`` and a
hand-rolled dict of JSON files all behave like this at the level that matters here: the
reply is chosen by what was *asked*, never by what has already *happened*.

That is the whole defect, and it is not a bug in those libraries. A recording is a
photograph of one moment. Replaying it is asserting that the moment never ends, which
is exactly false for any endpoint that changes something.

This class exists so the claim can be executed rather than asserted. It is deliberately
faithful rather than strawmanned:

* it records from a real run, the way a fixture is really produced;
* it matches on method and path, which is what the common libraries do by default;
* it replays in recorded order when a route was called more than once, so a fixture
  built from a two-call recording is not made artificially stupid;
* it raises on an unrecorded route instead of inventing a reply.

Even with all of that, the second refund still comes back 200, because nothing recorded
in the past knows that the refund has since happened.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["Recording", "ReplayFixture", "UnrecordedRoute"]


class UnrecordedRoute(LookupError):
    """The agent called something the recording never saw."""


class Recording:
    """One captured exchange."""

    __slots__ = ("method", "path", "request_body", "status", "response_body")

    def __init__(self, method: str, path: str, request_body: Optional[Dict[str, Any]],
                 status: int, response_body: Any):
        self.method = method.upper()
        self.path = path
        self.request_body = request_body
        self.status = status
        self.response_body = response_body

    def key(self) -> Tuple[str, str]:
        return (self.method, self.path)

    def to_dict(self) -> Dict[str, Any]:
        return {"method": self.method, "path": self.path,
                "request_body": self.request_body,
                "status": self.status, "response_body": self.response_body}

    def __repr__(self) -> str:
        return f"<{self.method} {self.path} -> {self.status}>"


class ReplayFixture:
    """Answer requests from a recording, with no notion of state."""

    def __init__(self, recordings: Optional[List[Recording]] = None):
        self._by_route: Dict[Tuple[str, str], List[Recording]] = {}
        self._cursor: Dict[Tuple[str, str], int] = {}
        self.replayed: List[Recording] = []
        for record in recordings or []:
            self._by_route.setdefault(record.key(), []).append(record)

    # -- construction --------------------------------------------------------

    @classmethod
    def record(cls, model: Any, script: List[Tuple[str, str, Optional[Dict[str, Any]]]],
               ) -> "ReplayFixture":
        """Build a fixture by running a script against a live model once.

        This is how a real fixture comes to exist: someone points their test at the
        actual API, saves the replies, and never looks again. Recording from the model
        rather than hand-writing the JSON matters, because it means the fixture holds
        genuinely correct responses. It is not wrong about what the API said. It is
        only wrong about *when*.
        """
        recordings = []
        for method, path, body in script:
            status, payload = model.handle(method, path, body)
            recordings.append(Recording(method, path, body, status, payload))
        return cls(recordings)

    @classmethod
    def from_json(cls, text: str) -> "ReplayFixture":
        data = json.loads(text)
        return cls([Recording(**item) for item in data])

    def to_json(self) -> str:
        flat = [r.to_dict() for group in self._by_route.values() for r in group]
        return json.dumps(flat, indent=2)

    # -- replay --------------------------------------------------------------

    def handle(self, method: str, path: str,
               body: Optional[Dict[str, Any]] = None,
               **_ignored: Any) -> Tuple[int, Any]:
        """Same signature as the stateful models, so an agent cannot tell them apart.

        ``body`` is accepted and ignored, which is the point: two identical requests
        get identical replies, and so do two *different* requests to the same route.
        """
        key = (method.upper(), path)
        group = self._by_route.get(key)
        if not group:
            raise UnrecordedRoute(
                f"{method.upper()} {path} was never recorded. A fixture can only "
                f"answer what it was shown."
            )

        # Walk recorded order when a route was captured more than once, then hold on
        # the last one. Holding rather than cycling is the generous reading: it is what
        # a library does when the agent makes more calls than the recording contains.
        index = min(self._cursor.get(key, 0), len(group) - 1)
        self._cursor[key] = index + 1
        record = group[index]
        self.replayed.append(record)
        return record.status, record.response_body

    def reset(self) -> None:
        """Rewind the cursors. The recording itself never changes."""
        self._cursor.clear()
        self.replayed.clear()

    def __repr__(self) -> str:
        routes = sum(len(v) for v in self._by_route.values())
        return f"<ReplayFixture routes={len(self._by_route)} recordings={routes}>"
