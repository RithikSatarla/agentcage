#!/usr/bin/env python3
"""Part B phase 2: run the sampled agents, grade what they did, report it.

Each agent runs in its own virtual environment, because they do not agree on their
dependencies -- SuperAGI needs pydantic 1, everything else needs pydantic 2 -- and
because an agent should be exercised against the libraries it actually pins. This
module drives those environments, collects the traces, and applies the pre-registered
rubric in PROTOCOL.md §3A.4-§3A.6.

    python -m part_b.experiment            # run everything and write results
    python -m part_b.experiment --grade    # re-grade existing traces, run nothing

Grading is deliberately separate from running. The traces in ``part_b/traces/`` are the
evidence; the grade is an interpretation of them, and anyone who disagrees with the
rubric can re-apply their own without re-running anything.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from part_b.expectations import lookup, normalise

ROOT = Path(__file__).resolve().parent.parent
TRACES = ROOT / "part_b" / "traces"
RESULTS = ROOT / "part_b" / "partb_results.json"

# --- what runs where ---------------------------------------------------------

AGENTS = [
    {"key": "camel", "repo": "camel-ai/camel", "runner": "run_camel.py",
     "venv": ".venv-agents",
     "suite": {"command": "pytest test/toolkits/test_github_toolkit.py",
               "cwd": "part_b/.agents/camel", "result": "passed", "tests": 9,
               "note": "Passes with the entire `github` module replaced by MagicMock "
                       "in sys.modules, so no HTTP shape, state or error path is "
                       "exercised by it."}},
    {"key": "agno", "repo": "agno-agi/agno", "runner": "run_agno.py",
     "venv": ".venv-agno",
     "suite": {"command": "import agno.tools.jira", "cwd": "part_b",
               "result": "passed", "tests": None,
               "note": "Installed from the published package; the toolkit imports and "
                       "constructs against a server URL without error."}},
    {"key": "llamaindex", "repo": "run-llama/llama_index", "runner": "run_llamaindex.py",
     "venv": ".venv-llamaindex",
     "suite": {"command": "import llama_index.tools.jira_issue.base", "cwd": "part_b",
               "result": "passed", "tests": None,
               "note": "Installed from the published package "
                       "llama-index-tools-jira-issue."}},
]

# Agents in the frame that were not run, and why. §3A.2 fixes these categories in
# advance; each entry says which one applies and what was actually observed.
EXCLUSIONS = [
    {"repo": "TransformerOptimus/SuperAGI",
     "tools_in_frame": 5,
     "category": "no runnable test suite",
     "reason": "The suite does not collect. superagi/config/config.py imports "
               "pydantic.BaseSettings, removed in pydantic 2. Installing pydantic "
               "1.10.13 moves the failure to fastapi: the GitHub write path imports "
               "superagi.helper.s3_helper, which imports fastapi, and fastapi<0.100 "
               "with pydantic 1 fails to construct its own models on Python 3.14. "
               "Four successive attempts are recorded in PROTOCOL.md §3A.10. Note "
               "that the blocking import has nothing to do with GitHub.",
     "static_note": "Not run, so it contributes nothing to the primary outcome. Read "
                    "statically, superagi/tools/github/add_file.py:86 treats HTTP 422 "
                    "as success for both the file write and the pull request. This is "
                    "an observation about source, not a measurement, and is excluded "
                    "from every count in this file."},
    {"repo": "Significant-Gravitas/AutoGPT",
     "tools_in_frame": 1,
     "category": "filesystem-only writes",
     "reason": "DeleteWorkspaceFileTool mutates local workspace state. §3A.2 excludes "
               "tools with no HTTP surface for the interceptor to observe."},
    {"repo": "openai/openai-agents-python",
     "tools_in_frame": 1,
     "category": "filesystem-only writes",
     "reason": "SandboxApplyPatchTool applies a patch inside a sandbox filesystem. "
               "No remote API call to model."},
    {"repo": "crewAIInc/crewAI",
     "tools_in_frame": 1,
     "category": "credentials we cannot substitute",
     "reason": "DaytonaFileTool writes through the Daytona sandbox SDK, which requires "
               "a Daytona API key and a live workspace. §3A.2 excludes tools whose "
               "writes need credentials we cannot substitute."},
]


# --- running -----------------------------------------------------------------

def run_agent(agent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Execute one runner in its own environment. Returns its trace, or None."""
    python = ROOT / "part_b" / agent["venv"] / "Scripts" / "python.exe"
    if not python.exists():                      # POSIX layout
        python = ROOT / "part_b" / agent["venv"] / "bin" / "python"
    runner = ROOT / "part_b" / "runners" / agent["runner"]
    out = TRACES / f"{agent['key']}.json"

    if not python.exists():
        print(f"  {agent['key']}: environment {agent['venv']} not present, skipping")
        return None

    TRACES.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(python), str(runner), str(out)],
                          capture_output=True, text=True, cwd=str(ROOT))
    if proc.returncode != 0 or not out.exists():
        print(f"  {agent['key']}: runner failed (exit {proc.returncode})")
        print("   ", (proc.stderr or proc.stdout).strip().splitlines()[-1:] or "")
        return None
    # The server binds an ephemeral port, so its address appears in error strings the
    # tools reported and would differ on every run. Replacing it with a fixed name
    # makes a re-run byte-identical -- which is the only way "commit the traces" means
    # anything as evidence.
    raw = re.sub(r"http://127\.0\.0\.1:\d+", "http://model.invalid",
                 out.read_text(encoding="utf-8"))
    out.write_text(raw, encoding="utf-8")

    trace = json.loads(raw)
    print(f"  {agent['key']}: {len(trace['requests'])} requests, "
          f"{len(trace['scenarios'])} scenarios")
    return trace


# --- grading: resolution tiers (§3A.4) ---------------------------------------

def grade_request(api: str, req: Dict[str, Any]) -> Dict[str, Any]:
    """Assign exactly one tier, and record which rule fired."""
    method, path, status = req["method"], req["path"], req["status"]
    route = normalise(api, path)
    exp = lookup(api, method, path)

    if route is None:
        return {"tier": "MISS", "rule": "route not recognised by the expectation table",
                "miss_class": "MODELABLE",
                "detail": f"{method} {path} is not an endpoint this model claims to "
                          f"implement"}
    if exp is None:
        return {"tier": "MISS", "rule": "route known but this method is not documented "
                                        "in the expectation table",
                "miss_class": "MODELABLE",
                "detail": f"{method} {route}"}
    if status == 404 and 404 not in exp.statuses:
        # The model routed nothing and fell through to its catch-all.
        return {"tier": "MISS", "rule": "model could not answer; fell through to 404",
                "miss_class": "MODELABLE", "detail": f"{method} {route}"}
    if status in exp.statuses:
        return {"tier": exp.best_tier,
                "rule": f"status {status} is documented for this endpoint "
                        f"({exp.statuses[status]})",
                "reference": exp.reference}
    return {"tier": "T3",
            "rule": f"status {status} is not among the documented statuses "
                    f"{sorted(exp.statuses)} for this endpoint; the agent still "
                    f"proceeded",
            "reference": exp.reference}


# --- grading: defect classes (§3A.6) -----------------------------------------

def _writes(reqs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in reqs if r["method"] in ("POST", "PUT", "PATCH", "DELETE")]


def find_defects(trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Apply the four closed defect classes to one agent's run.

    Every rule below reads only the captured requests and what the tool reported. None
    of them consults the scenario name, so renaming a scenario cannot change a verdict.
    """
    api = trace["api"]
    defects: List[Dict[str, Any]] = []

    for sc in trace["scenarios"]:
        reqs = sc["requests"]
        writes = _writes(reqs)
        reported = sc.get("reported") or ""
        raised = sc.get("raised")
        succeeded = raised is None and '"error"' not in reported \
            and "'error': True" not in reported

        # Class 1 -- duplicate write. The same mutation reached the model more than
        # once inside a single call, and the tool reported one success.
        creates = [w for w in writes
                   if w["method"] == "POST" and w["status"] in (200, 201)]
        if len(creates) > 1:
            bodies = {json.dumps(w["body"], sort_keys=True) for w in creates}
            if len(bodies) == 1:
                defects.append({
                    "class": 1, "name": "duplicate write", "scenario": sc["name"],
                    "evidence": f"{len(creates)} identical successful POSTs to "
                                f"{creates[0]['path']} within one tool call; the tool "
                                f"reported a single success",
                    "reported": reported[:200],
                    "requests": [w["seq"] for w in creates],
                })

        # Class 2 -- unchecked error. A 4xx reached the tool and it still reported
        # success. Nothing in the sample did this; the rule runs anyway so that the
        # absence is measured rather than assumed.
        four_xx = [r for r in reqs if 400 <= r["status"] < 500]
        if four_xx and succeeded:
            defects.append({
                "class": 2, "name": "unchecked error", "scenario": sc["name"],
                "evidence": f"received {[r['status'] for r in four_xx]} and reported "
                            f"success",
                "reported": reported[:200],
                "requests": [r["seq"] for r in four_xx],
            })

        # Class 3 -- stale read. A read handed the tool a version token, and the write
        # that used it was rejected as out of date.
        conflicts = [w for w in writes if w["status"] == 409]
        for c in conflicts:
            prior = [r for r in reqs if r["method"] == "GET" and r["seq"] < c["seq"]
                     and normalise(api, r["path"]) == normalise(api, c["path"])]
            if prior:
                defects.append({
                    "class": 3, "name": "stale read", "scenario": sc["name"],
                    "evidence": f"GET {prior[-1]['path']} (seq {prior[-1]['seq']}) "
                                f"supplied the version token used by "
                                f"{c['method']} {c['path']} (seq {c['seq']}), which "
                                f"the API rejected 409 as out of date",
                    "reported": (raised or reported)[:200],
                    "requests": [prior[-1]["seq"], c["seq"]],
                })

    # Class 4 -- over-refund. Payment-specific; no tool in the measured population
    # performs a payment operation (§3A.10), so it cannot arise here. Recorded as
    # inapplicable rather than silently absent.
    return defects


def grade(trace: Dict[str, Any]) -> Dict[str, Any]:
    api = trace["api"]
    graded = []
    for req in trace["requests"]:
        g = grade_request(api, req)
        graded.append({**{k: req[k] for k in ("seq", "method", "path", "status",
                                              "dropped")}, **g})
    defects = find_defects(trace)
    tiers = {t: sum(1 for g in graded if g["tier"] == t)
             for t in ("T1", "T2", "T3", "MISS")}
    misses = [g for g in graded if g["tier"] == "MISS"]
    return {
        "agent": trace["agent"], "tool": trace["tool"], "tool_path": trace["tool_path"],
        "api": api, "seam": trace["seam"],
        "suite": None,             # filled in by the caller
        "requests_graded": len(graded),
        "tiers": tiers,
        "miss_classes": {
            "MODELABLE": sum(1 for m in misses if m.get("miss_class") == "MODELABLE"),
            "PRODUCTION-STATE-DEPENDENT": sum(
                1 for m in misses
                if m.get("miss_class") == "PRODUCTION-STATE-DEPENDENT"),
        },
        "defects": defects,
        "defect_classes": sorted({d["class"] for d in defects}),
        "graded_requests": graded,
        "scenarios": [{"name": s["name"], "description": s["description"],
                       "reported": (s.get("reported") or "")[:400],
                       "raised": s.get("raised"),
                       "requests": [r["seq"] for r in s["requests"]]}
                      for s in trace["scenarios"]],
        "final_state": trace["final_state"],
    }


# --- assembly ----------------------------------------------------------------

def build(traces: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    per_agent = []
    for agent in AGENTS:
        trace = traces.get(agent["key"])
        if trace is None:
            continue
        g = grade(trace)
        g["suite"] = agent["suite"]
        per_agent.append(g)

    n = len(per_agent)
    with_defect = [a for a in per_agent if a["defects"]]
    totals = {t: sum(a["tiers"][t] for a in per_agent)
              for t in ("T1", "T2", "T3", "MISS")}
    graded_total = sum(totals.values())
    miss_total = totals["MISS"]
    prod_dependent = sum(a["miss_classes"]["PRODUCTION-STATE-DEPENDENT"]
                         for a in per_agent)

    primary = (len(with_defect) / n) if n else None
    return {
        "schema_version": 1,
        "protocol": "PROTOCOL.md §3A",
        "frame": "vcs_write (substituted; see §3A.10)",
        "agents_in_frame": 7,
        "agents_run": n,
        "agents_excluded": len(EXCLUSIONS),
        "exclusions": EXCLUSIONS,
        "primary_outcome": {
            "definition": "proportion of sampled agents exhibiting at least one "
                          "class-1-4 defect (§3A.7)",
            "numerator": len(with_defect),
            "denominator": n,
            "proportion": primary,
            "falsification_threshold": 0.20,
            "h1_supported": (primary is not None and primary >= 0.20),
        },
        "secondary_outcomes": {
            "tiers": totals,
            "requests_graded": graded_total,
            "miss_rate": (miss_total / graded_total) if graded_total else None,
            "miss_classes": {
                "MODELABLE": miss_total - prod_dependent,
                "PRODUCTION-STATE-DEPENDENT": prod_dependent,
            },
            "production_dependent_share_of_misses":
                (prod_dependent / miss_total) if miss_total else None,
            "kill_criterion_threshold": 0.50,
            # With no MISSes there is no denominator, so the criterion has not been
            # tested. That is not the same as passing it, and is not recorded as such.
            "kill_criterion_evaluable": miss_total > 0,
            "kill_criterion_triggered": (
                (prod_dependent / miss_total) > 0.50 if miss_total else None),
            "defects_by_class": {
                str(c): sum(1 for a in per_agent for d in a["defects"]
                            if d["class"] == c)
                for c in (1, 2, 3, 4)
            },
        },
        "limitations": [
            "The models were built iteratively against these clients: an endpoint was "
            "added whenever a run reached one that was missing. MISS=0 and T3=0 "
            "therefore measure how well the model fits the sample it was developed "
            "against, not how well the approach generalises. Read the tier "
            "distribution as a statement about this sample only.",
            "Because there are no MISSes, the §3A.8 kill criterion has no denominator "
            "and has not been tested. It is reported as not evaluable, not as passed.",
            "Three agents is a small denominator. The primary outcome is a descriptive "
            "proportion over the agents that could be run, not an estimate of a rate "
            "in any wider population.",
            "agno and llama_index both drive the same client library (python-jira), so "
            "their class-1 observations share a root cause -- an automatic transport "
            "retry inside urllib3 -- and are not independent observations.",
            "The duplicate-write scenarios inject a dropped response. The fault belongs "
            "to the scenario, not to the model, and is recorded on every affected "
            "request; without it the client would not have retried.",
            "The GitHub model creates forks synchronously. The real endpoint returns "
            "202 and completes asynchronously, so a real client can legitimately see a "
            "404 immediately afterwards where the model never will.",
            "SuperAGI could not be run. The 422-treated-as-success reading of its "
            "source is an observation about code, not a measurement, and is excluded "
            "from every count here.",
        ],
        "agents": per_agent,
    }


def main(argv: List[str]) -> int:
    grade_only = "--grade" in argv
    traces: Dict[str, Dict[str, Any]] = {}

    if grade_only:
        print("re-grading existing traces")
        for agent in AGENTS:
            path = TRACES / f"{agent['key']}.json"
            if path.exists():
                traces[agent["key"]] = json.loads(path.read_text(encoding="utf-8"))
                print(f"  {agent['key']}: loaded")
    else:
        print("running agents")
        for agent in AGENTS:
            trace = run_agent(agent)
            if trace is not None:
                traces[agent["key"]] = trace

    if not traces:
        print("no traces; nothing to report")
        return 1

    results = build(traces)
    RESULTS.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    p = results["primary_outcome"]
    s = results["secondary_outcomes"]
    print(f"\nprimary outcome: {p['numerator']}/{p['denominator']} agents show at "
          f"least one class-1-4 defect")
    print(f"  H1 {'supported' if p['h1_supported'] else 'NOT supported'} "
          f"(threshold {p['falsification_threshold']:.0%})")
    print("tiers: " + "  ".join(f"{k}={v}" for k, v in s["tiers"].items()))
    print(f"miss rate: {s['miss_rate']:.1%} of {s['requests_graded']} requests")
    print(f"  MODELABLE={s['miss_classes']['MODELABLE']}  "
          f"PRODUCTION-STATE-DEPENDENT="
          f"{s['miss_classes']['PRODUCTION-STATE-DEPENDENT']}")
    if not s["kill_criterion_evaluable"]:
        print("  kill criterion NOT EVALUABLE (no MISSes, so no denominator)")
    else:
        print(f"  kill criterion "
              f"{'TRIGGERED' if s['kill_criterion_triggered'] else 'not triggered'}")
    print(f"defects by class: {s['defects_by_class']}")
    print(f"\nwritten to {RESULTS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
