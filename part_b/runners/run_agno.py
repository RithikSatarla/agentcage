#!/usr/bin/env python3
"""Part B phase 2 runner: agno's JiraTools against the Jira model.

Run inside the agno environment; emits one JSON document on stdout for the
orchestrator in ``part_b/experiment.py`` to grade. The tool code is the published
package, unmodified and unpatched -- ``JiraTools`` takes ``server_url``, so pointing it
at the model is configuration, not interference.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from part_b.jira_mock import JiraMock  # noqa: E402
from part_b.server import ModelServer  # noqa: E402

AGENT = "agno-agi/agno"
TOOL = "JiraTools"
TOOL_PATH = "libs/agno/agno/tools/jira.py"


def main() -> int:
    from agno.tools.jira import JiraTools

    model = JiraMock(projects=["KAN"], clock=lambda: 1_700_000_000)
    scenarios = []

    # The first POST /issue has its response dropped after the model applies it. The
    # write lands; the client sees a broken connection. Everything after that is the
    # tool's own behaviour.
    drop_state = {"armed": True}

    def fault(request):
        if (drop_state["armed"] and request.method == "POST"
                and request.path.endswith("/issue")):
            drop_state["armed"] = False
            return "drop"
        return None

    with ModelServer(model, fault=fault) as srv:
        model.base_url = srv.base_url
        tools = JiraTools(server_url=srv.base_url, username="bot@example.invalid",
                          token="not-a-real-token")

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
            lambda: tools.create_issue("KAN", "Login returns 500", "on submit", "Bug"),
        )
        record(
            "create-retry",
            "the identical create_issue call again, as a caller that saw a failure "
            "would retry it",
            lambda: tools.create_issue("KAN", "Login returns 500", "on submit", "Bug"),
        )
        record(
            "search-after-retry",
            "search the project to count what actually exists after the retry",
            lambda: tools.search_issues("project = KAN"),
        )
        record(
            "comment-on-missing-issue",
            "add_comment to an issue key that does not exist",
            lambda: tools.add_comment("KAN-999", "any progress?"),
        )
        record(
            "worklog-invalid-duration",
            "add_worklog with a duration Jira rejects",
            lambda: tools.add_worklog("KAN-1", "quite a while", "investigating"),
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
    # Written to a path rather than stdout: these libraries log to both streams, and
    # a single stray line would make the document unparseable.
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if out is None:
        json.dump(payload, sys.stdout, indent=2)
    else:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
