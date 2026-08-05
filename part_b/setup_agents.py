#!/usr/bin/env python3
"""Build the environments Part B phase 2 runs the sampled agents in.

Each agent gets its own virtual environment. That is not tidiness: the sampled projects
disagree about their dependencies badly enough that no single environment can hold them.
SuperAGI pins pydantic 1; camel, agno and llama_index all require pydantic 2. camel
additionally needs mcp<2, which is older than what a fresh install of camel-ai pulls.

    python -m part_b.setup_agents            # build everything
    python -m part_b.setup_agents --check    # report what exists, install nothing

Versions are pinned where a floating install is known to break, and left floating
otherwise, so that a later run measures the tools as they are published rather than as
they were in August 2026.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
PART_B = ROOT / "part_b"
AGENTS_DIR = PART_B / ".agents"

ENVIRONMENTS: List[Dict[str, object]] = [
    {
        "venv": ".venv-agents",
        "packages": [
            "PyGithub", "jira", "requests", "httpx", "pydantic", "pytest",
            "python-dotenv",
            "camel-ai",
            # camel-ai 0.2.90 imports FastMCP from mcp.server, which mcp 2.x removed.
            "mcp<2",
        ],
        "clones": [("camel", "https://github.com/camel-ai/camel.git")],
        "for": "camel-ai/camel (GithubToolkit)",
    },
    {
        "venv": ".venv-agno",
        "packages": ["agno", "jira", "pytest"],
        "clones": [],
        "for": "agno-agi/agno (JiraTools)",
    },
    {
        "venv": ".venv-llamaindex",
        "packages": ["llama-index-tools-jira-issue", "pytest"],
        "clones": [],
        "for": "run-llama/llama_index (JiraIssueToolSpec)",
    },
    {
        # Built so the exclusion in PROTOCOL.md §3A.10 can be reproduced rather than
        # taken on trust. It is expected to fail to import; that failure is the result.
        "venv": ".venv-superagi",
        "packages": ["pydantic==1.10.13", "fastapi==0.99.1", "sqlalchemy", "pyyaml",
                     "requests", "boto3", "pytest"],
        "clones": [("SuperAGI", "https://github.com/TransformerOptimus/SuperAGI.git")],
        "for": "TransformerOptimus/SuperAGI (excluded; suite does not collect)",
    },
]


def venv_python(name: str) -> Path:
    win = PART_B / name / "Scripts" / "python.exe"
    return win if win.exists() else PART_B / name / "bin" / "python"


def build(env: Dict[str, object]) -> bool:
    name = str(env["venv"])
    target = PART_B / name
    print(f"\n{name}  --  {env['for']}")

    if not target.exists():
        print("  creating virtual environment")
        subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)

    python = venv_python(name)
    packages = list(env["packages"])          # type: ignore[arg-type]
    print(f"  installing {len(packages)} packages")
    proc = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
         *packages],
        capture_output=True, text=True)
    if proc.returncode != 0:
        print("  pip failed:")
        print("   ", (proc.stderr or proc.stdout).strip()[-500:])
        return False

    for folder, url in env["clones"]:          # type: ignore[union-attr]
        dest = AGENTS_DIR / folder
        if dest.exists():
            print(f"  clone {folder} already present")
            continue
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"  cloning {url}")
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
                       check=True)
    print("  ready")
    return True


def check() -> int:
    missing = 0
    for env in ENVIRONMENTS:
        name = str(env["venv"])
        python = venv_python(name)
        state = "present" if python.exists() else "MISSING"
        if not python.exists():
            missing += 1
        print(f"  {name:18} {state:8} {env['for']}")
        for folder, _ in env["clones"]:        # type: ignore[union-attr]
            here = (AGENTS_DIR / folder).exists()
            print(f"    clone {folder:10} {'present' if here else 'MISSING'}")
            if not here:
                missing += 1
    return missing


def main(argv: List[str]) -> int:
    if "--check" in argv:
        print("Part B phase 2 environments:")
        missing = check()
        print(f"\n{missing} missing" if missing else "\nall present")
        return 1 if missing else 0

    ok = True
    for env in ENVIRONMENTS:
        ok = build(env) and ok
    print("\nrun the experiment with:  python -m part_b.experiment")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
