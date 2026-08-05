"""Serve a stateful model over a real localhost socket.

Part B phase 2 runs *unmodified* agent code. That rules out monkeypatching the client
library, so the model has to be reachable the way the real API is: over HTTP, at a base
URL. Every client library in the sample already accepts one — PyGithub takes
``base_url``, the Jira client takes ``server`` — so pointing an agent at the model
requires changing configuration, not code.

The server is deliberately dumb. It parses the request, hands ``(method, path, body)``
to the model's ``handle`` and writes back whatever the model decided. It holds no state
of its own and makes no decisions, so nothing observed in a run can be an artefact of
this file.

It also keeps a log of what it was asked. The interceptor records the same traffic from
the client side; two independent records of the same run means a disagreement between
them is visible rather than silent.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple
from urllib.parse import unquote

__all__ = ["ServedRequest", "ModelServer"]


class SupportsHandle(Protocol):
    def handle(self, method: str, path: str,
               body: Optional[Dict[str, Any]]) -> Tuple[int, Any]: ...


class ServedRequest:
    """One request as the server saw it."""

    __slots__ = ("seq", "method", "path", "query", "body", "status", "dropped")

    def __init__(self, seq: int, method: str, path: str, query: str,
                 body: Optional[Dict[str, Any]], status: int):
        self.seq = seq
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.status = status
        # True when the scenario dropped the response after the model applied it.
        # The state change still happened; only the client's knowledge of it is lost.
        self.dropped = False

    def to_dict(self) -> Dict[str, Any]:
        return {"seq": self.seq, "method": self.method, "path": self.path,
                "query": self.query, "body": self.body, "status": self.status,
                "dropped": self.dropped}

    def __repr__(self) -> str:
        tail = " (response dropped)" if self.dropped else ""
        return f"<{self.method} {self.path} -> {self.status}{tail}>"


class ModelServer:
    """Run a model on an ephemeral localhost port for the life of a ``with`` block."""

    def __init__(self, model: SupportsHandle, *, host: str = "127.0.0.1",
                 advertise: Optional[str] = None,
                 fault: Optional["Callable[[ServedRequest], Optional[str]]"] = None):
        """``advertise`` is the origin the model should claim to be in its payloads.

        It defaults to this server's own address, which is right when the client was
        configured to talk here. When the client still believes it is talking to the
        real API and the traffic is being rewritten underneath it (see
        ``part_b/redirect.py``), the payload URLs have to keep saying so -- PyGithub
        asserts that they match the host it was configured with, and would reject
        payloads pointing at localhost.
        """
        self.model = model
        self.host = host
        self.advertise = advertise
        # Optional fault injector. Called with the request *after* the model has
        # already applied it; returning "drop" closes the connection without sending
        # the response. That is the shape of the failure that produces duplicate
        # writes in production -- the write landed, the caller never learned it did,
        # and the retry is entirely reasonable from where the caller is standing.
        # Faults belong to a scenario, never to the model: the model's rules are the
        # same whether or not one fires.
        self.fault = fault
        self.requests: List[ServedRequest] = []
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "ModelServer":
        server = self  # closed over by the handler

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # BaseHTTPRequestHandler logs every request to stderr. A test run makes
            # hundreds; the log we care about is server.requests.
            def log_message(self, fmt, *args):  # noqa: A003
                pass

            def _run(self) -> None:
                raw_path, _, query = self.path.partition("?")
                path = unquote(raw_path)

                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body: Optional[Dict[str, Any]] = None
                if raw:
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                        body = parsed if isinstance(parsed, dict) else {"_raw": parsed}
                    except (ValueError, UnicodeDecodeError):
                        # A body the model cannot read is still a request the agent
                        # made. Record it rather than dropping it.
                        body = {"_unparsed": raw[:2048].decode("utf-8", "replace")}

                # The model is given the query string as well as the path: on the real
                # API ``?ref=`` decides which branch a read sees, so dropping it would
                # make every read look like a read of the default branch.
                full = path + ("?" + query if query else "")
                try:
                    status, payload = server.model.handle(self.command, full, body)
                except Exception as exc:  # the model must not take the server down
                    status, payload = 500, {"message": f"model error: {exc!r}"}

                with server._lock:
                    seq = len(server.requests) + 1
                    served = ServedRequest(seq, self.command, path, query, body, status)
                    server.requests.append(served)

                if server.fault is not None and server.fault(served) == "drop":
                    served.dropped = True
                    self.close_connection = True
                    return

                # 204 means no content, and a client that reads a body anyway will
                # choke on the "null" a naive encoder would send. Jira answers most of
                # its writes this way.
                empty = status in (204, 304)
                encoded = b"" if empty else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                if not empty:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                # Clients read rate-limit headers and some refuse to proceed without
                # them. These are static: the model does not rate limit.
                self.send_header("X-RateLimit-Limit", "5000")
                self.send_header("X-RateLimit-Remaining", "4999")
                self.send_header("X-RateLimit-Reset", "0")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(encoded)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _run

        self._httpd = ThreadingHTTPServer((self.host, 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

        # Tell the model where it is listening. Real API payloads carry absolute URLs
        # and clients follow them -- PyGithub goes further and asserts the host matches
        # the one it was configured with, so a model still advertising api.github.com
        # fails on the second call with a bare AssertionError. The port is ephemeral,
        # so the model cannot know it at construction time and the server has to say.
        # This sets an address and nothing else: no status, body or rule comes from here.
        if hasattr(self.model, "base_url"):
            self.model.base_url = self.advertise or self.base_url
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "ModelServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- accessors -----------------------------------------------------------

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("server is not running")
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def writes(self) -> List[ServedRequest]:
        """Requests that attempted to change state."""
        return [r for r in self.requests
                if r.method in ("POST", "PUT", "PATCH", "DELETE")]

    def __repr__(self) -> str:
        where = self.base_url if self._httpd is not None else "stopped"
        return f"<ModelServer {where} requests={len(self.requests)}>"
