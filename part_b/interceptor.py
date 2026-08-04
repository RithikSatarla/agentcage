"""Passive HTTP capture for agent runs.

The interceptor is deliberately read-only: it observes requests and responses and
records them, and it never rewrites a request, injects a response, or short-circuits
the transport. Everything an agent would have sent still gets sent.

Two integrations, both using the library's own documented hook points:

    httpx     -> ``client.event_hooks`` (``request`` / ``response``)
    requests  -> ``session.hooks["response"]``

Usage::

    cage = Interceptor()
    client = httpx.Client(event_hooks=cage.httpx_event_hooks())
    client.post("https://api.stripe.com/v1/charges", data={"amount": 500})
    cage.dump("traces/run.json")
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

__all__ = ["Interceptor", "Trace", "redact_headers", "REDACTED", "SENSITIVE_HEADERS"]

REDACTED = "<redacted>"

#: Headers never written to a trace. Traces are meant to be committable artefacts.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-amz-security-token",
        "stripe-signature",
    }
)

MAX_BODY_BYTES = 64_000


def redact_headers(headers: Any) -> Dict[str, str]:
    """Lowercase header names and replace credential values with ``<redacted>``."""
    out: Dict[str, str] = {}
    items = headers.items() if hasattr(headers, "items") else (headers or [])
    for key, value in items:
        name = str(key).lower()
        out[name] = REDACTED if name in SENSITIVE_HEADERS else str(value)
    return out


def _decode(body: Any) -> Optional[str]:
    if body is None:
        return None
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = str(body)
    if len(text) > MAX_BODY_BYTES:
        return text[:MAX_BODY_BYTES] + f"...<truncated {len(text) - MAX_BODY_BYTES} chars>"
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Trace:
    """One observed HTTP request, plus the response status if one came back."""

    timestamp: str
    method: str
    url: str
    host: str
    path: str
    query: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[str] = None
    status_code: Optional[int] = None
    response_body: Optional[str] = None
    seq: Optional[int] = None

    @property
    def is_write(self) -> bool:
        """True for verbs that can mutate remote state."""
        return self.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Interceptor:
    """Collects :class:`Trace` records. Thread-safe; never modifies traffic."""

    def __init__(self, capture_response_body: bool = True):
        self._traces: List[Trace] = []
        self._lock = threading.Lock()
        self.capture_response_body = capture_response_body

    # -- collection ----------------------------------------------------------

    @property
    def traces(self) -> List[Trace]:
        with self._lock:
            return list(self._traces)

    @property
    def writes(self) -> List[Trace]:
        return [t for t in self.traces if t.is_write]

    def record(self, method: str, url: str, headers: Any = None, body: Any = None) -> Trace:
        """Record a request directly. Used by the adapters and available standalone."""
        parts = urlsplit(str(url))
        trace = Trace(
            timestamp=_now(),
            method=str(method).upper(),
            url=str(url),
            host=parts.netloc,
            path=parts.path or "/",
            query=parts.query,
            headers=redact_headers(headers or {}),
            body=_decode(body),
        )
        with self._lock:
            trace.seq = len(self._traces)
            self._traces.append(trace)
        return trace

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()

    def dump(self, path: str) -> Path:
        """Write all traces to ``path`` as JSON and return the path."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"captured_at": _now(), "count": len(self._traces),
                   "traces": [t.to_dict() for t in self.traces]}
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return target

    # -- httpx ---------------------------------------------------------------

    def httpx_event_hooks(self) -> Dict[str, list]:
        """Return an ``event_hooks`` mapping for ``httpx.Client``."""
        return {"request": [self._on_httpx_request], "response": [self._on_httpx_response]}

    def attach_httpx(self, client: Any) -> Any:
        """Attach to a live ``httpx.Client``, preserving any hooks already installed."""
        hooks = dict(getattr(client, "event_hooks", {}) or {})
        hooks["request"] = list(hooks.get("request", [])) + [self._on_httpx_request]
        hooks["response"] = list(hooks.get("response", [])) + [self._on_httpx_response]
        client.event_hooks = hooks
        return client

    def _on_httpx_request(self, request: Any) -> None:
        trace = self.record(request.method, str(request.url), request.headers,
                            getattr(request, "content", None))
        # Correlate the response hook back to this trace without touching the request.
        request.extensions["agentcage_trace_index"] = trace.seq

    def _on_httpx_response(self, response: Any) -> None:
        index = response.request.extensions.get("agentcage_trace_index")
        if index is None:
            return
        body = None
        if self.capture_response_body:
            try:
                response.read()
                body = _decode(response.content)
            except Exception:  # streaming or already-consumed responses
                body = None
        with self._lock:
            if 0 <= index < len(self._traces):
                self._traces[index].status_code = response.status_code
                self._traces[index].response_body = body

    # -- requests ------------------------------------------------------------

    def requests_response_hook(self):
        """Return a hook for ``requests.Session().hooks['response']``."""

        def hook(response: Any, *args: Any, **kwargs: Any) -> Any:
            req = response.request
            trace = self.record(req.method, req.url, req.headers, req.body)
            trace.status_code = response.status_code
            if self.capture_response_body:
                trace.response_body = _decode(response.text)
            return response  # returning the response unchanged is what keeps this passive

        return hook

    def attach_requests(self, session: Any) -> Any:
        """Attach to a ``requests.Session``, preserving existing response hooks."""
        existing = list(session.hooks.get("response") or [])
        session.hooks["response"] = existing + [self.requests_response_hook()]
        return session

    def __len__(self) -> int:
        return len(self.traces)

    def __repr__(self) -> str:
        return f"<Interceptor traces={len(self.traces)} writes={len(self.writes)}>"
