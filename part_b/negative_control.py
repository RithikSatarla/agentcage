#!/usr/bin/env python3
"""Negative control: a deliberately correct client, graded by the same rubric.

Part B measures third-party agent toolkits and finds a write-path defect in every one
of them. On its own that is not evidence that the instrument works, because nothing in
the study shows the instrument ever stays quiet. A detector that fires on everything
and a detector that is right are indistinguishable from three positives.

This module supplies the missing arm. It drives the *same* state models, behind the
*same* server, through the *same* injected fault, and grades the result with the *same*
imported functions from ``part_b.experiment`` -- but the client is written to handle
the write path correctly. The expected outcome is zero class-1-4 defects.

What "correctly" means is fixed by the defect classes in PROTOCOL.md 3A.6, and the
client is the obvious remedy for each:

* class 1 (duplicate write) -- after a write whose outcome is unknown, reconcile by
  reading before writing again; never retry a create blind.
* class 2 (unchecked error) -- read the status code, and surface a 4xx as a failure.
* class 3 (stale read) -- take the version token from the same ref you are about to
  write to, not from whatever the default branch happened to be.

Each scenario is paired with the hazard it mirrors, so the two arms can be compared
scenario by scenario rather than only in aggregate.

This is a CONTROL, not a sample member. It is deliberately not added to
``experiment.AGENTS``: the primary outcome in 3A.7 is a proportion over *sampled
agents*, and adding a client written here would corrupt that denominator. Its results
go to their own file.

    python -m part_b.negative_control

Writes part_b/negative_control_results.json and part_b/traces/negative_control_*.json.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from part_b.experiment import find_defects, grade  # noqa: E402
from part_b.github_mock import GitHubMock  # noqa: E402
from part_b.jira_mock import JiraMock  # noqa: E402
from part_b.server import ModelServer  # noqa: E402

TRACES = ROOT / "part_b" / "traces"
RESULTS = ROOT / "part_b" / "negative_control_results.json"

REPO = "acme/widgets"
CLOCK = 1_700_000_000


class Unexpected(Exception):
    """Raised when a response is not what the client asked for.

    The class-2 rule looks for a 4xx reported as success. A client that raises here is
    doing the thing that rule looks for the absence of.
    """


def _check(response: httpx.Response, *ok: int) -> httpx.Response:
    if response.status_code not in ok:
        raise Unexpected(
            "{} {} -> {}, expected {}: {}".format(
                response.request.method, response.request.url.path,
                response.status_code, ok, response.text[:200]))
    return response


class Recorder:
    """Collects scenarios in the trace shape ``experiment.grade`` already reads."""

    def __init__(self, server: ModelServer):
        self.server = server
        self.scenarios: List[Dict[str, Any]] = []

    def run(self, name: str, hazard: str, description: str,
            fn: Callable[[], Any]) -> None:
        before = len(self.server.requests)
        raised: Optional[str] = None
        try:
            reported = fn()
        except BaseException as exc:  # noqa: BLE001 - the report is the datum
            reported = None
            raised = "{}: {}".format(type(exc).__name__, exc)
        self.scenarios.append({
            "name": name,
            "hazard": hazard,
            "description": description,
            "reported": reported if isinstance(reported, str) else repr(reported),
            "raised": raised,
            "requests": [r.to_dict() for r in self.server.requests[before:]],
        })


# --- the Jira arm ------------------------------------------------------------

def jira_arm() -> Dict[str, Any]:
    """Mirrors the agno and llama_index scenarios, handled correctly."""
    model = JiraMock(projects=["KAN"], clock=lambda: CLOCK)

    # Identical to the fault in run_agno.py and run_llamaindex.py: the first
    # POST /issue has its response dropped after the model has already applied it.
    drop_state = {"armed": True}

    def fault(request):
        if (drop_state["armed"] and request.method == "POST"
                and request.path.endswith("/issue")):
            drop_state["armed"] = False
            return "drop"
        return None

    summary_text = "Login returns 500"
    fields = {
        "project": {"key": "KAN"},
        "summary": summary_text,
        "description": "on submit",
        "issuetype": {"name": "Bug"},
    }

    with ModelServer(model, fault=fault) as srv:
        model.base_url = srv.base_url
        # retries=0 is httpx's default, and it is load-bearing here: the class-1
        # defect in the sampled agents is produced by a transport that retries
        # beneath the caller. This client must not do that.
        client = httpx.Client(base_url=srv.base_url, timeout=10.0)
        rec = Recorder(srv)

        def find_by_summary(summary: str) -> Optional[str]:
            """Reconcile against server state.

            The model's JQL understands equality on project and status and nothing
            else, so the summary filter is applied here rather than in the query.
            """
            r = _check(client.get("/rest/api/2/search",
                                  params={"jql": "project = KAN", "maxResults": 50}),
                       200)
            for issue in r.json().get("issues", []):
                fields_of = issue.get("fields") or {}
                if issue.get("summary") == summary or \
                        fields_of.get("summary") == summary:
                    return issue.get("key")
            return None

        def create_reconciling(payload: Dict[str, Any]) -> str:
            """Create an issue at most once, whatever the transport does.

            Pre-flight read, then the write; and where the write's outcome is unknown,
            reconcile rather than retry. Neither GitHub nor Jira offers an idempotency
            key on create, so reconciliation is the only remedy available at this
            layer.
            """
            summary = payload["summary"]
            existing = find_by_summary(summary)
            if existing is not None:
                return "exists:{}".format(existing)
            try:
                r = client.post("/rest/api/2/issue", json={"fields": payload})
            except Exception:
                # The write may or may not have landed. Ask; do not assume.
                settled = find_by_summary(summary)
                if settled is None:
                    raise
                return "reconciled:{}".format(settled)
            return "created:{}".format(_check(r, 201).json()["key"])

        rec.run(
            "create-interrupted-then-reconcile",
            "class 1 (duplicate write)",
            "create whose response is dropped after the model applied it; the client "
            "reconciles by reading instead of retrying the write",
            lambda: create_reconciling(fields),
        )
        rec.run(
            "create-again-preflighted",
            "class 1 (duplicate write)",
            "the identical create request again; the pre-flight read finds the issue "
            "and no second write is issued",
            lambda: create_reconciling(fields),
        )
        rec.run(
            "comment-on-missing-issue",
            "class 2 (unchecked error)",
            "comment on an issue key that does not exist; the 404 is checked and "
            "surfaced rather than reported as success",
            lambda: _check(client.post("/rest/api/2/issue/KAN-999/comment",
                                       json={"body": "any progress?"}), 201).text,
        )
        rec.run(
            "worklog-invalid-duration",
            "class 2 (unchecked error)",
            "worklog with a duration Jira rejects; the 400 is checked and surfaced",
            lambda: _check(client.post("/rest/api/2/issue/KAN-1/worklog",
                                       json={"timeSpent": "quite a while",
                                             "comment": "investigating"}), 201).text,
        )

        client.close()
        final = {
            "issues": [{"key": k, "summary": v["summary"], "status": v["status"]}
                       for k, v in model.issues.items()],
            "issue_count": len(model.issues),
            "deleted": list(model.deleted),
        }
        requests = [r.to_dict() for r in srv.requests]

    return {
        "agent": "negative-control (jira)",
        "tool": "reference client written for this study",
        "tool_path": "part_b/negative_control.py",
        "api": "jira",
        "seam": "configuration (base_url)",
        "scenarios": rec.scenarios,
        "requests": requests,
        "final_state": final,
    }


# --- the GitHub arm ----------------------------------------------------------

def github_arm() -> Dict[str, Any]:
    """Mirrors the camel scenario, handled correctly."""
    model = GitHubMock(repos=[REPO], clock=lambda: CLOCK)
    model.put_contents(REPO, "README.md", "v0\n", "seed the default branch")

    with ModelServer(model) as srv:
        model.base_url = srv.base_url
        client = httpx.Client(base_url=srv.base_url, timeout=10.0)
        rec = Recorder(srv)

        def branch_then_update() -> str:
            """The camel scenario, with the read taken from the branch being written.

            camel reads contents with no ``ref``, which resolves to the default
            branch, then writes the sha it got to a different branch. The blob it
            names is not the blob it is replacing, so the write is rejected 409.
            Passing the ref is the whole fix.
            """
            head = _check(client.get("/repos/{}/branches/main".format(REPO)),
                          200).json()
            commit = head.get("commit")
            sha = commit["sha"] if isinstance(commit, dict) else head.get("sha")
            _check(client.post("/repos/{}/git/refs".format(REPO),
                               json={"ref": "refs/heads/feature", "sha": sha}), 201)
            current = _check(
                client.get("/repos/{}/contents/README.md".format(REPO),
                           params={"ref": "feature"}), 200).json()
            body = {
                "message": "update readme on feature",
                "content": base64.b64encode(b"v1\n").decode("ascii"),
                "sha": current["sha"],
                "branch": "feature",
            }
            r = _check(client.put("/repos/{}/contents/README.md".format(REPO),
                                  json=body), 200, 201)
            return "updated:{}".format(r.json()["content"]["sha"])

        rec.run(
            "branch-then-update-with-correct-ref",
            "class 3 (stale read)",
            "read the file on the branch being written, then write it there; the "
            "version token is current, so no 409 arises",
            branch_then_update,
        )
        rec.run(
            "update-existing-without-sha",
            "class 2 (unchecked error)",
            "update an existing file with no sha; the 422 is checked and surfaced "
            "rather than reported as success",
            lambda: _check(client.put(
                "/repos/{}/contents/README.md".format(REPO),
                json={"message": "clobber",
                      "content": base64.b64encode(b"v2\n").decode("ascii")}),
                200, 201).text,
        )

        client.close()
        requests = [r.to_dict() for r in srv.requests]

    return {
        "agent": "negative-control (github)",
        "tool": "reference client written for this study",
        "tool_path": "part_b/negative_control.py",
        "api": "github",
        "seam": "configuration (base_url)",
        "scenarios": rec.scenarios,
        "requests": requests,
        "final_state": {"note": "model state is exercised, not asserted, in this arm"},
    }


# --- run and grade -----------------------------------------------------------

def main(argv: List[str]) -> int:
    TRACES.mkdir(parents=True, exist_ok=True)
    arms = []
    for key, build in (("jira", jira_arm), ("github", github_arm)):
        trace = build()
        (TRACES / "negative_control_{}.json".format(key)).write_text(
            json.dumps(trace, indent=2) + "\n", encoding="utf-8")
        graded = grade(trace)
        defects = find_defects(trace)
        arms.append({
            "arm": key,
            "requests_graded": graded["requests_graded"],
            "tiers": graded["tiers"],
            "defects": defects,
            "defect_count": len(defects),
            "scenarios": [{"name": s["name"], "hazard": s["hazard"],
                           "raised": s["raised"]} for s in trace["scenarios"]],
            "graded_requests": graded["graded_requests"],
        })

    total_defects = sum(a["defect_count"] for a in arms)
    totals = {t: sum(a["tiers"][t] for a in arms) for t in ("T1", "T2", "T3", "MISS")}
    out = {
        "schema_version": 1,
        "protocol": "PROTOCOL.md 3A.4-3A.6, applied to a control rather than a "
                    "sample member",
        "what_this_is": "A client written to handle the write path correctly, driven "
                        "through the same models, the same server and the same "
                        "injected fault as the sampled agents, and graded by the same "
                        "imported functions. It is not part of the 3A.7 denominator.",
        "arms": arms,
        "totals": {
            "requests_graded": sum(a["requests_graded"] for a in arms),
            "tiers": totals,
            "defects": total_defects,
        },
        "expected": "zero class-1-4 defects",
        "passed": total_defects == 0,
        "limitations": [
            "The control client was written by the same author as the models and the "
            "grader, and written knowing which defects the sampled agents exhibited. "
            "It shows the detectors are not unconditional; it does not show they are "
            "correct.",
            "It exercises the same scenarios as the sampled agents and no others. A "
            "clean result here says nothing about false positives on write paths this "
            "study never drove.",
        ],
    }
    RESULTS.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print("negative control: {} requests graded".format(
        out["totals"]["requests_graded"]))
    print("tiers: " + "  ".join("{}={}".format(k, v) for k, v in totals.items()))
    for a in arms:
        print("  {}: {} requests, {} defects".format(
            a["arm"], a["requests_graded"], a["defect_count"]))
        for d in a["defects"]:
            print("    UNEXPECTED class {} in {}: {}".format(
                d["class"], d["scenario"], d["evidence"]))
    print("defects: {} (expected 0) -> {}".format(
        total_defects, "PASS" if total_defects == 0 else "FAIL"))
    print("wrote {}".format(RESULTS.relative_to(ROOT)))
    return 0 if total_defects == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
