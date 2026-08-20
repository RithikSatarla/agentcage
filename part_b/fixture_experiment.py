#!/usr/bin/env python3
"""The fixture arm: run the same scenarios against a replay fixture and grade them.

Section VI of the write-up names this as the experiment that would test the paper's
central claim, and states the result that would kill it. This runs it.

Design. Each agent is executed twice in its own environment, with nothing changed but
the backend behind the socket:

1. **record pass** -- the stateful model, faults suppressed. Every exchange is saved.
   This is how a cassette really comes to exist: a developer points the client at a
   working service and keeps what came back.
2. **replay pass** -- a ``ReplayFixture`` built from that recording, with the fault
   armed exactly as in the measured run. The fixture matches on method and path and
   walks recorded order, which is what ``responses``, ``vcrpy`` and ``betamax`` do.

The scenario, the interceptor, the server and the four pre-registered detectors are
identical across the model arm and the fixture arm. Only the backend differs, so a
difference in what the detectors report is attributable to statefulness and to nothing
else.

The comparison is per defect class, not per agent, because the classes are not alike:
class 1 is detected from a pattern in the captured requests, while class 3 is detected
from a 409 that only a backend tracking versions can produce. Reporting a single
aggregate would hide that.

    python -m part_b.fixture_experiment

Writes part_b/fixture_arm_results.json and part_b/traces/fixture_*.json.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from part_b.experiment import AGENTS, find_defects, grade  # noqa: E402

TRACES = ROOT / "part_b" / "traces"
RESULTS = ROOT / "part_b" / "fixture_arm_results.json"


def _python_for(agent: Dict[str, Any]) -> Optional[Path]:
    p = ROOT / "part_b" / agent["venv"] / "Scripts" / "python.exe"
    if not p.exists():
        p = ROOT / "part_b" / agent["venv"] / "bin" / "python"
    return p if p.exists() else None


def run_pass(agent: Dict[str, Any], mode: str, out: Path) -> Optional[Dict[str, Any]]:
    python = _python_for(agent)
    if python is None:
        print("  %s: environment %s missing" % (agent["key"], agent["venv"]))
        return None
    runner = ROOT / "part_b" / "runners" / agent["runner"]
    env = dict(os.environ)
    env["AGENTCAGE_BACKEND"] = mode
    TRACES.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(python), str(runner), str(out)],
                          capture_output=True, text=True, cwd=str(ROOT), env=env)
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        print("  %s [%s]: runner failed (exit %d): %s"
              % (agent["key"], mode, proc.returncode, " | ".join(tail)))
        return None
    raw = re.sub(r"http://127\.0\.0\.1:\d+", "http://model.invalid",
                 out.read_text(encoding="utf-8"))
    out.write_text(raw, encoding="utf-8")
    return json.loads(raw)


def classes_in(defects: List[Dict[str, Any]]) -> List[int]:
    return sorted({d["class"] for d in defects})


def main(argv: List[str]) -> int:
    rows = []
    for agent in AGENTS:
        key = agent["key"]
        print("%s:" % key)
        rec = run_pass(agent, "record", TRACES / ("fixture_record_%s.json" % key))
        if rec is None:
            continue
        rep = run_pass(agent, "replay", TRACES / ("fixture_%s.json" % key))
        if rep is None:
            continue

        model_trace = json.loads(
            (TRACES / ("%s.json" % key)).read_text(encoding="utf-8"))
        model_defects = find_defects(model_trace)
        fixture_defects = find_defects(rep)
        g_model = grade(model_trace)
        g_fix = grade(rep)

        # A fixture that cannot answer a route returns a server-side error. Count it:
        # an unanswerable request is a different failure from a missed defect.
        unanswerable = sum(1 for r in rep["requests"] if r["status"] >= 500)

        row = {
            "agent": agent["repo"],
            "key": key,
            "model_arm": {"defect_classes": classes_in(model_defects),
                          "defects": len(model_defects),
                          "requests_graded": g_model["requests_graded"],
                          "tiers": g_model["tiers"]},
            "fixture_arm": {"defect_classes": classes_in(fixture_defects),
                            "defects": len(fixture_defects),
                            "requests_graded": g_fix["requests_graded"],
                            "tiers": g_fix["tiers"],
                            "unanswerable_requests": unanswerable},
            "missed_by_fixture": sorted(set(classes_in(model_defects))
                                        - set(classes_in(fixture_defects))),
            "found_by_both": sorted(set(classes_in(model_defects))
                                    & set(classes_in(fixture_defects))),
            "fixture_defect_detail": [
                {"class": d["class"], "name": d["name"], "scenario": d["scenario"],
                 "evidence": d["evidence"]} for d in fixture_defects],
        }
        rows.append(row)
        print("   model arm  : classes %s" % (row["model_arm"]["defect_classes"] or "none"))
        print("   fixture arm: classes %s%s"
              % (row["fixture_arm"]["defect_classes"] or "none",
                 " (%d unanswerable)" % unanswerable if unanswerable else ""))
        print("   missed by fixture: %s" % (row["missed_by_fixture"] or "none"))

    by_class: Dict[str, Dict[str, int]] = {}
    for row in rows:
        for c in row["model_arm"]["defect_classes"]:
            slot = by_class.setdefault(str(c), {"model": 0, "fixture": 0})
            slot["model"] += 1
            if c in row["fixture_arm"]["defect_classes"]:
                slot["fixture"] += 1

    out = {
        "schema_version": 1,
        "what_this_is": "The same scenarios, interceptor, server and pre-registered "
                        "detectors, with only the backend swapped: a stateful model "
                        "versus a replay fixture recorded from a clean run against it.",
        "fixture": "matches on method and path, walks recorded order then holds on the "
                   "last recording, raises on an unrecorded route",
        "agents": rows,
        "by_defect_class": by_class,
        "limitations": [
            "The recording is made against the stateful model, not the real service, "
            "so the fixture holds responses that are correct about what the API says "
            "and wrong only about when.",
            "The record pass suppresses the injected fault, because a cassette recorded "
            "through the failure would bake it in. Recording through the fault would "
            "make the fixture arm look better than a real cassette would.",
            "Two arms over three agents. This compares backends, not populations.",
        ],
    }
    RESULTS.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print("\nby defect class (agents exhibiting it, model arm vs fixture arm):")
    for c, v in sorted(by_class.items()):
        print("  class %s: model %d, fixture %d" % (c, v["model"], v["fixture"]))
    print("wrote %s" % RESULTS.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
