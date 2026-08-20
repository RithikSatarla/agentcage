"""Backend selection for the fixture arm, so one scenario can drive two backends.

PROTOCOL.md section 3A drove every agent against a stateful model. The paper's central
claim is about what a *replay fixture* cannot express, and until now that claim was an
argument rather than a measurement: `demo_fixture_vs_model.py` demonstrates it on a
hand-written client against the Stripe model, and says in its own docstring that it is a
demonstration and not a measurement.

This module lets the three real runners execute their existing scenarios against a
replay fixture instead, with nothing else changed: same scenario, same interceptor,
same server, same pre-registered detectors. Selection is by environment variable and the
default is the original behaviour, so `python -m part_b.experiment` still reproduces
`part_b/partb_results.json` byte for byte.

    AGENTCAGE_BACKEND=model    (default) the stateful model, as before
    AGENTCAGE_BACKEND=record   the stateful model, faults suppressed, exchanges saved
    AGENTCAGE_BACKEND=replay   a ReplayFixture built from the saved recording

The two-pass shape is deliberate and is how a fixture really comes to exist. The record
pass is a clean run: no injected fault, because a developer producing a cassette is
exercising the happy path, not a transport failure. The replay pass then re-runs the
identical scenario with the fault armed, against a backend that cannot know anything has
happened since.

The fixture used is the generous one. It matches on method and path, which is what
`responses`, `vcrpy` and `betamax` do by default, and it replays in recorded order when
a route was captured more than once rather than returning the first reply forever. A
weaker fixture would be easier to beat and would prove less.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from part_b.replay_fixture import Recording, ReplayFixture

__all__ = ["backend_mode", "backend_for", "fault_for", "save_recording",
           "recording_path"]

ROOT = Path(__file__).resolve().parent.parent
TRACES = ROOT / "part_b" / "traces"


def backend_mode() -> str:
    mode = os.environ.get("AGENTCAGE_BACKEND", "model").strip().lower()
    if mode not in ("model", "record", "replay"):
        raise ValueError("AGENTCAGE_BACKEND must be model, record or replay, "
                         "got %r" % mode)
    return mode


def recording_path(key: str) -> Path:
    return TRACES / ("fixture_recording_%s.json" % key)


class RecordingModel:
    """Delegate to a stateful model and keep every exchange.

    Wraps rather than subclasses so that anything the server reads off the model, such
    as ``base_url``, still reaches the real object.
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self.exchanges: List[Dict[str, Any]] = []

    def handle(self, method: str, path: str,
               body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        status, payload = self._inner.handle(method, path, body)
        self.exchanges.append({"method": method.upper(), "path": path,
                               "request_body": body, "status": status,
                               "response_body": payload})
        return status, payload

    # The server sets and reads base_url on whatever it is given.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_inner", "exchanges"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


_ACTIVE: Dict[str, RecordingModel] = {}


def backend_for(model: Any, key: str) -> Any:
    """Return the backend the runner should serve, given the selected mode."""
    mode = backend_mode()
    if mode == "model":
        return model
    if mode == "record":
        wrapped = RecordingModel(model)
        _ACTIVE[key] = wrapped
        return wrapped
    path = recording_path(key)
    if not path.exists():
        raise FileNotFoundError(
            "no recording at %s; run the record pass first "
            "(AGENTCAGE_BACKEND=record)" % path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReplayFixture([Recording(**item) for item in data])


def fault_for(fault: Optional[Callable[[Any], Optional[str]]]
              ) -> Optional[Callable[[Any], Optional[str]]]:
    """Suppress the injected fault during the record pass only.

    A cassette is recorded from a working interaction. Recording through the dropped
    response would bake the failure into the fixture and make the comparison meaningless.
    """
    return None if backend_mode() == "record" else fault


def save_recording(key: str) -> None:
    """Write the captured exchanges, if this was a record pass."""
    if backend_mode() != "record":
        return
    wrapped = _ACTIVE.get(key)
    if wrapped is None:
        return
    TRACES.mkdir(parents=True, exist_ok=True)
    recording_path(key).write_text(
        json.dumps(wrapped.exchanges, indent=2) + "\n", encoding="utf-8")
