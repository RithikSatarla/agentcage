#!/usr/bin/env python3
"""Part B phase 2 runner: camel's GithubToolkit against the GitHub model.

Run inside the camel environment, from a shallow clone of the repository at
``part_b/.agents/camel``. The toolkit source is upstream's, unedited.

Unlike the two Jira toolkits, this one offers no seam. It constructs its client inline::

    self.github = Github(auth=Token(access_token))

with no base URL to set and no client to inject, so there is nowhere to point it at a
model. The traffic is redirected one layer below, inside the HTTP client the toolkit
imports -- see ``part_b/redirect.py``. That the redirect is necessary at all is part of
the result: a tool with no seam cannot be tested against anything but the real API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "part_b" / ".agents" / "camel"))

from part_b.github_mock import GitHubMock      # noqa: E402
from part_b.redirect import redirect           # noqa: E402
from part_b.server import ModelServer          # noqa: E402

AGENT = "camel-ai/camel"
TOOL = "GithubToolkit"
TOOL_PATH = "camel/toolkits/github_toolkit.py"
REAL = "https://api.github.com"
REPO = "acme/widgets"


def main() -> int:
    model = GitHubMock(repos=[REPO], clock=lambda: 1_700_000_000, base_url=REAL)
    model.put_contents(REPO, "README.md", "v0\n", "seed the default branch")
    scenarios = []

    # The model keeps advertising the real origin because PyGithub asserts that the
    # URLs in payloads match the host it was configured with.
    with ModelServer(model, advertise=REAL) as srv:
        with redirect({REAL: srv.base_url}) as red:
            from camel.toolkits.github_toolkit import GithubToolkit
            tk = GithubToolkit(access_token="ghp_" + "x" * 36)

            def record(name, description, fn):
                before = len(srv.requests)
                raised = None
                try:
                    reported = fn()
                except BaseException as exc:  # noqa: BLE001 - the report is the datum
                    reported = None
                    raised = f"{type(exc).__name__}: {exc}"
                scenarios.append({
                    "name": name,
                    "description": description,
                    "reported": reported if isinstance(reported, str) else repr(reported),
                    "raised": raised,
                    "requests": [r.to_dict() for r in srv.requests[before:]],
                })

            def make_pr(content):
                return tk.github_create_pull_request(
                    repo_name=REPO, file_path="README.md", new_content=content,
                    pr_title="Update readme", body="please review",
                    branch_name="agent-patch",
                )

            record(
                "pull-request-first",
                "open a pull request that edits one file on a new branch",
                lambda: make_pr("v1\n"),
            )
            record(
                "pull-request-repeat",
                "run the identical call again, as an agent re-attempting the task "
                "would; the branch now exists and holds the first edit",
                lambda: make_pr("v2\n"),
            )
            record(
                "issue-list-after",
                "read back the issue list to confirm the toolkit's read path works "
                "against the model",
                lambda: str(tk.github_get_issue_list(REPO)),
            )

            rewrites = len(red.rewrites)
            passed = list(red.passed_through)

        final = {
            "branches": {b: sorted(f) for b, f in model.contents[REPO].items()},
            "branch_contents": {b: {p: f.content for p, f in files.items()}
                                for b, files in model.contents[REPO].items()},
            "pull_requests": [{"number": n, "title": p.title, "head": p.head,
                               "merged": p.merged}
                              for n, p in model.pulls[REPO].items()],
            "issue_count": len(model.issues[REPO]),
        }
        requests = [r.to_dict() for r in srv.requests]

    payload = {
        "agent": AGENT, "tool": TOOL, "tool_path": TOOL_PATH, "api": "github",
        "seam": "none (client constructed inline; traffic redirected below the tool)",
        "redirect": {"rewritten": rewrites, "passed_through": passed},
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
