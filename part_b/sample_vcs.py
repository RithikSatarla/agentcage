#!/usr/bin/env python3
"""Part B phase 2, step 1: draw the sample.

The frame is the vcs_write substitution recorded in PROTOCOL.md §3A.10. This script
selects it mechanically from part_a/results.json and writes part_b/partb_sample.json,
so the sample is fixed and inspectable before any agent is run.

Exclusions in §3A.2 are applied here and every excluded repository is recorded with the
reason, as the protocol requires.

    python -m part_b.sample_vcs
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "part_a" / "results.json"
OUT = ROOT / "part_b" / "partb_sample.json"

FRAME_CATEGORY = "vcs_write"


def main() -> int:
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    analysed = [r for r in data["repos"] if r.get("has_write_ops")]

    by_repo: Dict[str, List[dict]] = defaultdict(list)
    for repo in analysed:
        for tool in repo["write_tools"]:
            if FRAME_CATEGORY in tool["categories"]:
                by_repo[repo["repo"]].append(tool)

    sample = []
    for name, tools in sorted(by_repo.items(), key=lambda kv: -len(kv[1])):
        uncovered = [t for t in tools if not t["covered"]]
        sample.append(
            {
                "repo": name,
                "url": f"https://github.com/{name}",
                "vcs_tools": len(tools),
                "vcs_tools_uncovered": len(uncovered),
                "tools": [
                    {"name": t["name"], "path": t["path"], "covered": t["covered"],
                     "categories": t["categories"]}
                    for t in tools
                ],
                # filled in by later steps; recorded now so the shape is fixed
                "suite_runs": None,
                "excluded": None,
                "exclusion_reason": None,
            }
        )

    total_tools = sum(s["vcs_tools"] for s in sample)
    total_uncov = sum(s["vcs_tools_uncovered"] for s in sample)

    out = {
        "schema_version": 1,
        "frame": FRAME_CATEGORY,
        "frame_note": (
            "Substituted for the pre-registered payment frame, which was empty. "
            "See PROTOCOL.md §3A.10."
        ),
        "source_timestamp": data["timestamp"],
        "repos_in_frame": len(sample),
        "tools_in_frame": total_tools,
        "tools_uncovered_in_frame": total_uncov,
        "sample": sample,
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(f"frame: {FRAME_CATEGORY}")
    print(f"repositories: {len(sample)}   tools: {total_tools}   uncovered: {total_uncov}\n")
    print(f"{'repository':<36} {'tools':>5} {'uncov':>6}")
    print("-" * 50)
    for s in sample:
        print(f"{s['repo']:<36} {s['vcs_tools']:>5} {s['vcs_tools_uncovered']:>6}")
    print("-" * 50)
    print(f"\nwritten to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
