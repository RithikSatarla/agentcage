#!/usr/bin/env python3
"""Part B phase 2 runner: llama_index's JiraIssueToolSpec against the Jira model.

Run inside the llama-index environment. Like agno's toolkit this one takes a
``server_url``, so the tool code is used exactly as published.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from part_b.jira_mock import JiraMock  # noqa: E402
from part_b.server import ModelServer  # noqa: E402

AGENT = "run-llama/llama_index"
TOOL = "JiraIssueToolSpec"
TOOL_PATH = ("llama-index-integrations/tools/llama-index-tools-jira-issue/"
             "llama_index/tools/jira_issue/base.py")


def main() -> int:
    from llama_index.tools.jira_issue.base import JiraIssueToolSpec

    model = JiraMock(projects=["KAN"], clock=lambda: 1_700_000_000)
    scenarios = []
    drop_state = {"armed": True}

    def fault(request):
        if (drop_state["armed"] and request.method == "POST"
                and request.path.endswith("/issue")):
            drop_state["armed"] = False
            return "drop"
        return None

    with ModelServer(model, fault=fault) as srv:
        model.base_url = srv.base_url
        spec = JiraIssueToolSpec(email="bot@example.invalid", api_key="not-a-real-token",
                                 server_url=srv.base_url)

        def record(name, description, fn):
            before = len(srv.requests)
            raised = None
            try:
                reported = fn()
            except BaseException as exc:      # noqa: BLE001 - the report is the datum
                reported = None
                raised = f"{type(exc).__name__}: {exc}"
            scenarios.append({
                "name": name,
                "description": description,
                "reported": reported if isinstance(reported, str) else repr(reported),
                "raised": raised,
                "requests": [r.to_dict() for r in srv.requests[before:]],
            })

        record(
            "create-interrupted",
            "create_issue whose response is dropped after the model applied it",
            lambda: spec.create_issue("KAN", "Checkout total is wrong",
                                      "off by the tax amount", "Bug"),
        )
        record(
            "create-retry",
            "the identical create_issue call again, as a caller that saw a failure "
            "would retry it",
            lambda: spec.create_issue("KAN", "Checkout total is wrong",
                                      "off by the tax amount", "Bug"),
        )
        record(
            "status-allowed",
            "move an issue to a status the workflow offers from where it is",
            lambda: spec.update_issue_status("KAN-1", "Done"),
        )
        record(
            "status-not-in-workflow",
            "move the same issue to a status not reachable from its new one; the "
            "available transitions changed as a result of the previous write, which "
            "is the part a recorded response cannot represent",
            lambda: spec.update_issue_status("KAN-1", "In Progress"),
        )
        record(
            "delete",
            "delete an issue that exists",
            lambda: spec.delete_issue("KAN-1"),
        )
        record(
            "delete-again",
            "delete the same issue a second time",
            lambda: spec.delete_issue("KAN-1"),
        )
        record(
            "update-deleted-issue",
            "update the summary of an issue that was already deleted",
            lambda: spec.update_issue_summary("KAN-1", "Checkout total is wrong (dup)"),
        )
        record(
            "due-date-invalid",
            "set a due date in a format Jira rejects",
            lambda: spec.update_issue_due_date("KAN-2", "05/08/2026"),
        )

        final = {
            "issues": [{"key": k, "summary": v["summary"], "status": v["status"]}
                       for k, v in model.issues.items()],
            "issue_count": len(model.issues),
            "deleted": list(model.deleted),
        }
        requests = [r.to_dict() for r in srv.requests]

    payload = {
        "agent": AGENT, "tool": TOOL, "tool_path": TOOL_PATH, "api": "jira",
        "seam": "configuration (server_url)",
        "scenarios": scenarios, "requests": requests, "final_state": final,
    }
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if out is None:
        json.dump(payload, sys.stdout, indent=2)
    else:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
