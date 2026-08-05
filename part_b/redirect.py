"""Send an agent's API traffic to a model instead of the internet.

This is deliberately a separate module from ``interceptor``. The interceptor is
passive: it watches traffic and never alters it, which is what makes its traces
evidence. This module does alter traffic -- it is the cage -- and keeping the two
apart means a recorded trace can never be quietly the product of a rewrite.

Why it has to exist at all is a finding in its own right. The tools measured in Part A
build their URLs inline::

    file_url = f'https://api.github.com/repos/{owner}/{name}/contents/{path}'
    requests.put(file_url, json=params, headers=headers)

There is no base URL setting, no injected client, no seam. Short of editing the tool
there is nowhere to point it somewhere safe, so the redirect happens one layer down, in
the HTTP client the tool imports. The agent code runs byte-for-byte as published.

The rewrite is origin-only: scheme, host and port are replaced, path and query are
untouched, and a request to an unmapped origin is left completely alone -- so a run
that reaches for an API nobody modelled fails loudly against a real DNS lookup rather
than silently receiving a plausible answer.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Dict, Iterator, List, Tuple
from urllib.parse import urlsplit, urlunsplit

__all__ = ["Redirect", "redirect"]


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> str:
    """Scheme and host, with a redundant default port removed.

    Clients disagree about whether to spell the port out. PyGithub asks for
    ``https://api.github.com:443/repos/...`` while ``requests.put`` on the same API
    produces ``https://api.github.com/repos/...``; the two are the same origin and must
    map the same way. Comparing raw netlocs matches only one of them, and the traffic
    that fails to match goes to the real internet.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, "", "", ""))


class Redirect:
    """Record of what a redirect block rewrote."""

    def __init__(self, mapping: Dict[str, str]):
        self.mapping = {_origin(k): v.rstrip("/") for k, v in mapping.items()}
        self.rewrites: List[Tuple[str, str]] = []
        self.passed_through: List[str] = []
        self._lock = threading.Lock()

    def apply(self, url: str) -> str:
        parts = urlsplit(url)
        target = self.mapping.get(_origin(url))
        if target is None:
            with self._lock:
                self.passed_through.append(url)
            return url
        t = urlsplit(target)
        new = urlunsplit((t.scheme, t.netloc, parts.path, parts.query, parts.fragment))
        with self._lock:
            self.rewrites.append((url, new))
        return new

    def __repr__(self) -> str:
        return (f"<Redirect origins={len(self.mapping)} rewritten={len(self.rewrites)} "
                f"passed_through={len(self.passed_through)}>")


@contextmanager
def redirect(mapping: Dict[str, str]) -> Iterator[Redirect]:
    """Rewrite the origin of every ``requests`` call made inside the block.

    ``mapping`` maps a real origin to a replacement::

        with redirect({"https://api.github.com": server.base_url}) as r:
            tool._execute(...)
        print(r.rewrites)

    The patch goes on ``Session.send`` rather than ``Session.request``. By the time a
    request reaches ``send`` it has been prepared, and a prepared request always carries
    a fully-qualified URL. ``request`` sees whatever the caller passed, which is not
    always absolute -- PyGithub hands it a path and lets the connection supply the host,
    so a redirect installed there silently lets the call through to the real API. That
    is not a hypothetical: it happened on the first run of this experiment, and only a
    certificate failure stopped the traffic from reaching github.com.
    """
    import requests.sessions

    state = Redirect(mapping)
    original = requests.sessions.Session.send

    def patched(self, request, **kwargs):
        request.url = state.apply(str(request.url))
        return original(self, request, **kwargs)

    requests.sessions.Session.send = patched
    try:
        yield state
    finally:
        requests.sessions.Session.send = original
