#!/usr/bin/env python3
"""Part A: measure how much of the open-source agent ecosystem tests its write operations.

For every candidate repository in ``repos.json`` this script:

1. pulls the full file tree (one GitHub API call per repo, ``HEAD`` ref);
2. reads the plausible tool-surface Python files over ``raw.githubusercontent.com``
   (which does not consume the API rate limit);
3. extracts *agent tool definitions* (decorator style, ``BaseTool`` subclass style,
   and JSON tool-schema style) and keeps the ones whose body performs a
   **write / destructive operation** (HTTP POST-PUT-PATCH-DELETE, filesystem
   mutation, shell execution, SQL writes, cloud SDK writes, VCS writes, outbound
   messaging, payments);
4. reads the repository's test files and checks whether the *specific* write-tool
   symbols are referenced anywhere in them;
5. records whether CI exists at all.

A repository enters the analysed set (the headline denominator) only if step 3
finds at least one write-capable tool definition. Screening is therefore part of
the measurement, not a manual curation step.

Run:  ``python -m part_a.run``  or  ``python part_a/run.py``
Docs: see ../PROTOCOL.md for the pre-registered detector definitions.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import httpx

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
REPOS_FILE = ROOT / "repos.json"
DEFAULT_OUT = ROOT / "results.json"
CACHE_DIR = REPO_ROOT / ".cache" / "part_a"

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
TOOL_VERSION = "0.1.0"

# --- pre-registered analysis caps (recorded verbatim in results.json) ---------
MAX_TOOL_FILES_PER_REPO = 250
MAX_TEST_FILES_PER_REPO = 400
MAX_FILE_BYTES = 400_000
DEFAULT_WORKERS = 16

# --- detectors ---------------------------------------------------------------

# Decorator-style tool definitions: @tool, @function_tool, @agent.tool,
# @kernel_function, @register_tool ... followed by the def it decorates.
# Covers @tool, @agent.tool, @mcp.tool(), @server.tool(), @registry.action(...),
# @function_tool, @kernel_function. "command" is deliberately absent: @click.command
# and @app.command are CLI entry points, not agent tools, and matching them would
# count ordinary CLI programs as agents.
DECORATOR_TOOL_RE = re.compile(
    r"@(?:[\w.]+\.)?(?:tool|tools|function_tool|tool_plugin|register_tool|kernel_function"
    r"|action|register_action)"
    r"(?:\s*\([^()]*\))?\s*"
    r"(?:\r?\n[ \t]*@[^\r\n]*)*"
    r"\r?\n[ \t]*(?:async[ \t]+)?def[ \t]+(?P<name>\w+)",
    re.MULTILINE,
)

# Class-style tool definitions: class GithubCreateIssue(BaseTool): ...
CLASS_TOOL_RE = re.compile(r"^[ \t]*class[ \t]+(?P<name>\w+)[ \t]*\((?P<bases>[^)]*)\)", re.MULTILINE)
TOOL_BASE_RE = re.compile(r"\b\w*(?:BaseTool|Toolkit|ToolSpec|BaseAction|Tool|Action)\b")

# JSON tool-schema style: {"name": "create_issue", ..., "parameters"/"input_schema": {...}}
SCHEMA_TOOL_RE = re.compile(
    r"""["']name["']\s*:\s*["'](?P<name>[A-Za-z_][\w.\-]{2,63})["'](?P<gap>.{0,600}?)"""
    r"""["'](?:parameters|input_schema)["']\s*:""",
    re.DOTALL,
)
SCHEMA_WINDOW = 2000

WRITE_MARKERS: Dict[str, "re.Pattern[str]"] = {
    "http_destructive": re.compile(
        r"\b(?:requests|httpx|session|_?session|client|_?client|http|aiohttp|self\.[\w.]*(?:client|session))"
        r"\s*\.\s*(?:put|patch|delete)\s*\(",
        re.IGNORECASE,
    ),
    # Bare POST is deliberately kept in its own tier: plenty of read-only search and
    # inference endpoints are queried with POST, so POST alone is not evidence of
    # state mutation. See AMBIGUOUS_CATEGORIES.
    "http_post": re.compile(
        r"\b(?:requests|httpx|session|_?session|client|_?client|http|aiohttp|self\.[\w.]*(?:client|session))"
        r"\s*\.\s*post\s*\(",
        re.IGNORECASE,
    ),
    "http_verb_arg": re.compile(
        r"""(?:method|verb|http_method)\s*=\s*["'](?:PUT|PATCH|DELETE)["']""", re.IGNORECASE
    ),
    "filesystem_write": re.compile(
        r"\b(?:os\.remove|os\.unlink|os\.rmdir|os\.makedirs|os\.rename|os\.replace"
        r"|shutil\.rmtree|shutil\.move|shutil\.copy\w*)\s*\(|\.(?:unlink|write_text|write_bytes|mkdir|touch)\s*\("
    ),
    "file_open_write": re.compile(r"""open\s*\([^)]*["'][rbt+]*[wax]\+?[rbt+]*["']"""),
    "shell_exec": re.compile(
        r"\b(?:subprocess\.(?:run|call|check_call|check_output|Popen)|os\.system|os\.popen|pty\.spawn"
        r"|exec_run|run_command|execute_command)\s*\("
    ),
    # Bare `.save(` and `.delete(` are deliberately absent: they match PIL image saves,
    # model checkpointing and countless other non-database calls.
    "db_write": re.compile(
        r"(?:\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b|\bDROP\s+TABLE\b|\bTRUNCATE\b)"
        r"|\.commit\s*\(\s*\)"
        r"|\.(?:insert_one|insert_many|update_one|update_many|delete_one|delete_many|bulk_write)\s*\("
        r"|\b(?:session|db|conn|cursor|connection)\s*\.\s*(?:add|execute|save|delete)\s*\(",
        re.IGNORECASE,
    ),
    "cloud_sdk_write": re.compile(
        r"\b(?:put_object|delete_object|create_bucket|delete_bucket|upload_file|upload_fileobj"
        r"|terminate_instances|run_instances|stop_instances|put_item|delete_item)\s*\("
    ),
    "vcs_write": re.compile(
        r"""(?:git["'\s]+(?:push|commit|checkout|reset|clean|rm)\b)"""
        r"""|\b(?:create_pull|create_pull_request|create_issue|create_comment|create_review"
        r"|merge_pull_request|create_file|update_file|delete_file|create_git_ref)\s*\("""
    ),
    "messaging_write": re.compile(
        r"\b(?:send_message|send_email|send_mail|sendmail|chat_postMessage|post_message"
        r"|smtplib\.SMTP|messages\.create)\s*\(|\.send\s*\(\s*\)"
    ),
    "payment_write": re.compile(
        r"\bstripe\.[A-Z]\w+\.(?:create|modify|cancel|delete)\b"
        r"|\b(?:Refund|PaymentIntent|Charge|Subscription)\.create\s*\("
    ),
}

# Categories that on their own are NOT sufficient evidence of a state-mutating tool.
# A tool must carry at least one non-ambiguous category to enter the analysed set.
AMBIGUOUS_CATEGORIES = {"http_post"}

API_MARKERS: Dict[str, "re.Pattern[str]"] = {
    "openai": re.compile(r"\bopenai\b|api\.openai\.com", re.IGNORECASE),
    "anthropic": re.compile(r"\banthropic\b|api\.anthropic\.com", re.IGNORECASE),
    "aws": re.compile(r"\bboto3\b|\bbotocore\b|amazonaws\.com", re.IGNORECASE),
    "github": re.compile(r"\bgithub3?\b|\bPyGithub\b|api\.github\.com", re.IGNORECASE),
    "google": re.compile(r"\bgoogleapiclient\b|googleapis\.com|\bgenerativeai\b", re.IGNORECASE),
    "slack": re.compile(r"\bslack_sdk\b|slack\.com/api", re.IGNORECASE),
    "stripe": re.compile(r"\bstripe\b", re.IGNORECASE),
    "email": re.compile(r"\bsmtplib\b|\bsendgrid\b|\bimaplib\b", re.IGNORECASE),
    "database": re.compile(r"\bsqlalchemy\b|\bpsycopg2?\b|\bpymongo\b|\bsqlite3\b", re.IGNORECASE),
    "browser": re.compile(r"\bplaywright\b|\bselenium\b|\bpuppeteer\b", re.IGNORECASE),
    "shell": re.compile(r"\bsubprocess\b|\bdocker\b|\bparamiko\b", re.IGNORECASE),
}

# Files that plausibly hold an agent's tool surface. Matching is on whole
# delimiter-separated path words, never substrings: a substring test makes
# "openai.py" match "op" and floods the candidate set with model clients.
TOOL_KEYWORDS = {
    "tool", "tools", "toolkit", "toolkits", "action", "actions", "skill", "skills",
    "plugin", "plugins", "integration", "integrations", "connector", "connectors",
    "command", "commands", "agent", "agents", "ability", "abilities",
}


def path_words(path: str) -> Set[str]:
    return set(re.split(r"[/_\-.]+", path.lower()))


def tool_file_rank(path: str) -> Tuple[int, int, int, str]:
    """Deterministic priority for which tool files to read when the cap binds.

    Screening asks "does this repo define at least one write-capable tool?", so recall
    matters most: files whose *own name* names a tool surface come first, re-export
    shims come last. A plain lexicographic cap instead clusters on whichever top-level
    directory sorts first, which is not representative of the repo.
    """
    stem = path.rsplit("/", 1)[-1]
    return (
        0 if path_words(stem) & TOOL_KEYWORDS else 1,
        1 if stem == "__init__.py" else 0,
        -len(path_words(path) & TOOL_KEYWORDS),
        path,
    )
TEST_PATH_RE = re.compile(
    r"(?:^|/)tests?(?:_\w+)?/|(?:^|/)testing/|(?:^|/)test_[\w\-]+\.py$|_test\.py$", re.IGNORECASE
)
VENDOR_PATH_RE = re.compile(
    r"(?:^|/)(?:node_modules|site-packages|\.venv|venv|vendor|third_party|docs|examples?|samples?"
    r"|cookbook|notebooks?|templates?|migrations)/",
    re.IGNORECASE,
)
WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$", re.IGNORECASE)

GENERIC_TOKENS = {
    "src", "lib", "libs", "core", "main", "base", "common", "utils", "util", "init", "api", "apis",
    "tool", "tools", "toolkit", "toolkits", "agent", "agents", "action", "actions", "skill",
    "skills", "plugin", "plugins", "test", "tests", "testing", "python", "packages", "package",
    "modules", "module", "internal", "impl", "helpers", "helper", "integration", "integrations",
}


# --- HTTP with a disk cache --------------------------------------------------


class Fetcher:
    """Cached HTTP GET. Raw file reads never touch the GitHub API rate limit."""

    def __init__(self, token: Optional[str] = None, use_cache: bool = True, timeout: float = 30.0):
        headers = {"User-Agent": f"agentcage-part-a/{TOOL_VERSION}", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(headers=headers, timeout=timeout, follow_redirects=True)
        self._use_cache = use_cache
        self.api_calls = 0
        if use_cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str) -> Path:
        return CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".txt")

    def get_text(self, url: str) -> Optional[str]:
        """Return response text, or None for 404/410. Missing bodies are cached too."""
        cache = self._cache_path(url)
        if self._use_cache and cache.exists():
            blob = cache.read_text(encoding="utf-8", errors="replace")
            return None if blob == "\x00MISSING" else blob

        text: Optional[str] = None
        for attempt in range(3):
            try:
                resp = self._client.get(url)
            except httpx.HTTPError:
                time.sleep(1.5 * (attempt + 1))
                continue
            if url.startswith(GITHUB_API):
                self.api_calls += 1
            if resp.status_code in (404, 410, 451):
                text = None
                break
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                raise RateLimited(resp.headers.get("x-ratelimit-reset", ""))
            if resp.status_code >= 500 or resp.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            text = resp.text
            break

        if self._use_cache:
            cache.write_text("\x00MISSING" if text is None else text, encoding="utf-8")
        return text

    def close(self) -> None:
        self._client.close()


class RateLimited(RuntimeError):
    """GitHub API rate limit exhausted."""


# --- source analysis ---------------------------------------------------------


def line_offsets(text: str) -> List[int]:
    offsets = [0]
    idx = text.find("\n")
    while idx != -1:
        offsets.append(idx + 1)
        idx = text.find("\n", idx + 1)
    return offsets


def block_at(text: str, lines: Sequence[str], offsets: Sequence[int], pos: int,
             max_lines: int = 250) -> str:
    """Return the indented suite that starts on the line containing ``pos``."""
    start = bisect.bisect_right(offsets, pos) - 1
    if start < 0 or start >= len(lines):
        return ""
    head = lines[start]
    base_indent = len(head) - len(head.lstrip())
    out = [head]
    for line in lines[start + 1: start + 1 + max_lines]:
        if not line.strip():
            out.append(line)
            continue
        if (len(line) - len(line.lstrip())) <= base_indent:
            break
        out.append(line)
    return "\n".join(out)


def write_categories(body: str) -> List[str]:
    return sorted(name for name, pattern in WRITE_MARKERS.items() if pattern.search(body))


def body_after_signature(block: str) -> str:
    """Drop a def/class signature (possibly spanning lines) and return the body.

    Without this the detectors match the declaration itself: ``def delete_file(path)``
    satisfies a pattern written to catch *calls* to ``delete_file(``, so any tool whose
    name happens to read like a mutation would be counted as one on the strength of its
    own signature.
    """
    lines = block.splitlines()
    idx = 0
    while idx < len(lines):
        code = lines[idx].split("#")[0].rstrip()
        idx += 1
        if code.endswith(":"):
            break
    return "\n".join(lines[idx:])


def find_write_tools(path: str, text: str) -> List[Dict[str, object]]:
    """Extract tool definitions from one source file and keep the write-capable ones."""
    lines = text.splitlines()
    offsets = line_offsets(text)
    found: List[Dict[str, object]] = []
    seen: Set[str] = set()

    def consider(name: str, body: str, style: str) -> None:
        if not name or name in seen or not body:
            return
        if style != "schema":
            body = body_after_signature(body)
        # Neutralise self-reference so a tool can never be its own evidence.
        body = re.sub(r"\b" + re.escape(name) + r"\b", "_SELF_", body)
        cats = write_categories(body)
        if not cats:
            return
        seen.add(name)
        found.append({"name": name, "path": path, "style": style, "categories": cats})

    for match in DECORATOR_TOOL_RE.finditer(text):
        consider(match.group("name"), block_at(text, lines, offsets, match.start("name")),
                 "decorator")

    for match in CLASS_TOOL_RE.finditer(text):
        if not TOOL_BASE_RE.search(match.group("bases")):
            continue
        consider(match.group("name"), block_at(text, lines, offsets, match.start("name")), "class")

    for match in SCHEMA_TOOL_RE.finditer(text):
        window = text[match.start(): match.start() + SCHEMA_WINDOW]
        consider(match.group("name"), window, "schema")

    return found


def detect_apis(blobs: Iterable[str]) -> List[str]:
    joined = "\n".join(blobs)
    return sorted(name for name, pattern in API_MARKERS.items() if pattern.search(joined))


def path_tokens(path: str) -> Set[str]:
    parts = re.split(r"[/_\-.]+", path.lower())
    return {p for p in parts if len(p) > 2 and p not in GENERIC_TOKENS and not p.isdigit()}


def rank_test_files(test_paths: Sequence[str], tool_paths: Sequence[str]) -> List[str]:
    """Order test files by token overlap with the files that defined write tools.

    Every test file remains eligible; overlap only decides who is read first when
    ``MAX_TEST_FILES_PER_REPO`` binds. If the cap does not bind, the scan is exhaustive
    and ``coverage_scan_complete`` is set on the record.
    """
    wanted: Set[str] = set()
    for p in tool_paths:
        wanted |= path_tokens(p)
    scored = []
    for path in test_paths:
        overlap = len(path_tokens(path) & wanted)
        scored.append((-overlap, len(path), path))
    scored.sort()
    return [path for _, _, path in scored]


# --- per-repository analysis -------------------------------------------------


def fetch_tree(fetcher: Fetcher, repo: str) -> Optional[dict]:
    raw = fetcher.get_text(f"{GITHUB_API}/repos/{repo}/git/trees/HEAD?recursive=1")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def read_many(fetcher: Fetcher, repo: str, paths: Sequence[str], workers: int) -> Dict[str, str]:
    """Read files over raw.githubusercontent.com concurrently."""

    def one(path: str) -> Tuple[str, Optional[str]]:
        url = f"{RAW_BASE}/{repo}/HEAD/{path}"
        try:
            return path, fetcher.get_text(url)
        except (httpx.HTTPError, RateLimited):
            return path, None

    out: Dict[str, str] = {}
    if not paths:
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, text in pool.map(one, paths):
            if text is not None and len(text) <= MAX_FILE_BYTES:
                out[path] = text
    return out


def analyze_repo(fetcher: Fetcher, candidate: dict, workers: int) -> Dict[str, object]:
    repo = candidate["repo"]
    record: Dict[str, object] = {
        "repo": repo,
        "url": f"https://github.com/{repo}",
        "framework": candidate.get("framework", ""),
        "resolved": False,
        "has_write_ops": False,
    }

    tree = fetch_tree(fetcher, repo)
    if tree is None or "tree" not in tree:
        record["error"] = "tree_unavailable"
        return record

    record["resolved"] = True
    record["tree_truncated"] = bool(tree.get("truncated"))
    entries = [node["path"] for node in tree["tree"] if node.get("type") == "blob"]

    workflows = sorted(p for p in entries if WORKFLOW_RE.match(p))
    record["has_ci"] = bool(workflows)
    record["ci_workflows"] = workflows[:10]

    py_files = [p for p in entries if p.endswith(".py") and not VENDOR_PATH_RE.search(p)]
    test_paths = sorted(p for p in py_files if TEST_PATH_RE.search(p))
    # Every non-test, non-vendor source file is eligible. Restricting reads to paths
    # containing a tool keyword looks like a sensible prefilter but silently destroys
    # recall: agents routinely define write tools in files like controller/service.py.
    # Precision is preserved by the tool-definition detectors, not by the path filter;
    # ranking only decides read order when the cap binds.
    tool_candidates = sorted(
        (p for p in py_files if not TEST_PATH_RE.search(p)), key=tool_file_rank
    )
    record["source_files_total"] = len(tool_candidates)
    scanned_tool_paths = tool_candidates[:MAX_TOOL_FILES_PER_REPO]
    record["source_files_scanned"] = len(scanned_tool_paths)
    record["source_files_skipped_by_cap"] = len(tool_candidates) - len(scanned_tool_paths)
    record["source_scan_complete"] = len(scanned_tool_paths) == len(tool_candidates)

    sources = read_many(fetcher, repo, scanned_tool_paths, workers)

    detected: List[Dict[str, object]] = []
    for path, text in sorted(sources.items()):
        detected.extend(find_write_tools(path, text))

    # Only tools with a non-ambiguous mutation category count as write-capable.
    write_tools = [t for t in detected if set(t["categories"]) - AMBIGUOUS_CATEGORIES]
    record["post_only_tool_count"] = len(detected) - len(write_tools)

    record["apis"] = detect_apis(sources.values())
    record["write_tool_count"] = len(write_tools)
    record["has_write_ops"] = bool(write_tools)

    if not write_tools:
        record["screened_out"] = "no_write_capable_tool_definition"
        return record

    tool_paths = sorted({str(t["path"]) for t in write_tools})
    ordered_tests = rank_test_files(test_paths, tool_paths)
    scanned_tests = ordered_tests[:MAX_TEST_FILES_PER_REPO]
    record["test_files_total"] = len(test_paths)
    record["test_files_scanned"] = len(scanned_tests)
    record["coverage_scan_complete"] = len(scanned_tests) == len(test_paths)

    test_sources = read_many(fetcher, repo, scanned_tests, workers)
    corpus = "\n".join(test_sources.values())

    # Every tool is recorded with its own coverage flag, not a sample. Downstream
    # analysis needs the full population to break coverage down by category.
    covered = 0
    detail: List[Dict[str, object]] = []
    for tool in write_tools:
        name = str(tool["name"])
        is_covered = bool(re.search(r"\b" + re.escape(name) + r"\b", corpus))
        covered += int(is_covered)
        detail.append(
            {
                "name": name,
                "path": tool["path"],
                "style": tool["style"],
                "categories": tool["categories"],
                "covered": is_covered,
            }
        )
    record["write_tools"] = detail
    record["covered_write_tools"] = covered
    record["has_test_coverage"] = covered > 0
    return record


# --- aggregation and reporting ----------------------------------------------


def summarize(records: Sequence[Dict[str, object]], elapsed: float, config: dict) -> dict:
    unresolved = [r["repo"] for r in records if not r.get("resolved")]
    resolved = [r for r in records if r.get("resolved")]
    analyzed = [r for r in resolved if r.get("has_write_ops")]
    screened_out = [r["repo"] for r in resolved if not r.get("has_write_ops")]

    tested = [r for r in analyzed if r.get("has_test_coverage")]
    untested = [r for r in analyzed if not r.get("has_test_coverage")]
    pct = round(100.0 * len(untested) / len(analyzed), 1) if analyzed else 0.0

    def tool_totals(rows: Sequence[Dict[str, object]]) -> Tuple[int, int, float]:
        total = sum(int(r.get("write_tool_count", 0)) for r in rows)
        covered = sum(int(r.get("covered_write_tools", 0)) for r in rows)
        share = round(100.0 * (total - covered) / total, 1) if total else 0.0
        return total, covered, share

    # Primary unit of analysis. Repo-level "has at least one test touching at least one
    # write tool" is a weak bar: a project with 34 write tools and 1 covered tool passes
    # it. Counting tools instead makes the denominator 158 rather than 13.
    tools_total, tools_covered, tools_pct = tool_totals(analyzed)

    complete = [r for r in analyzed if r.get("coverage_scan_complete")]
    complete_untested = [r for r in complete if not r.get("has_test_coverage")]
    complete_pct = round(100.0 * len(complete_untested) / len(complete), 1) if complete else 0.0
    c_tools_total, c_tools_covered, c_tools_pct = tool_totals(complete)

    return {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_version": TOOL_VERSION,
        "elapsed_seconds": round(elapsed, 1),
        "config": config,
        "candidates_screened": len(records),
        "candidates_unresolved": unresolved,
        "screened_out_no_write_tools": screened_out,
        "total_repos_analyzed": len(analyzed),
        "tested": len(tested),
        "untested": len(untested),
        "untested_pct": pct,
        "untested_repos": [r["repo"] for r in untested],
        "write_tools_total": tools_total,
        "write_tools_covered": tools_covered,
        "write_tools_uncovered": tools_total - tools_covered,
        "write_tools_uncovered_pct": tools_pct,
        "headline": (
            f"Across {len(analyzed)} open-source agent repositories, {tools_pct}% of the "
            f"{tools_total} write-capable tool definitions they ship are never referenced "
            f"by any test."
        ),
        "secondary_repo_level": (
            f"{pct}% of those {len(analyzed)} repositories ({len(untested)}) have no test "
            f"touching any write tool at all."
        ),
        "sensitivity_exhaustive_test_scan_only": {
            "note": (
                "Restricted to repositories whose entire test suite was read (no cap applied), "
                "so a missed reference cannot be an artefact of sampling. The headline is "
                "credible to the extent this figure matches it."
            ),
            "repos_analyzed": len(complete),
            "untested_repos": len(complete_untested),
            "untested_repos_pct": complete_pct,
            "write_tools_total": c_tools_total,
            "write_tools_uncovered": c_tools_total - c_tools_covered,
            "write_tools_uncovered_pct": c_tools_pct,
        },
        "repos": list(records),
    }


def print_report(summary: dict) -> None:
    rows = [r for r in summary["repos"] if r.get("has_write_ops")]
    rows.sort(key=lambda r: -int(r.get("write_tool_count", 0)))

    name_w = max([len(r["repo"]) for r in rows] + [20])
    width = name_w + 40
    print()
    print("=" * width)
    print("AGENTCAGE PART A - write-operation test coverage".center(width))
    print("=" * width)
    print(f"{'repository'.ljust(name_w)}  {'tools':>5}  {'covered':>7}  {'ci':>3}  {'scan':>5}")
    print("-" * width)
    for r in rows:
        total = int(r.get("write_tool_count", 0))
        covered = int(r.get("covered_write_tools", 0))
        share = f"{covered}/{total}"
        print(
            f"{r['repo'].ljust(name_w)}  {total:>5}  {share:>7}  "
            f"{'y' if r.get('has_ci') else 'n':>3}  "
            f"{'full' if r.get('coverage_scan_complete') else 'cap':>5}"
        )
    print("-" * width)
    print(f"candidates screened      : {summary['candidates_screened']}")
    print(f"unresolved (404/redirect): {len(summary['candidates_unresolved'])}")
    print(f"screened out (no writes) : {len(summary['screened_out_no_write_tools'])}")
    print(f"analysed (write-capable) : {summary['total_repos_analyzed']}")
    print(f"write tools found        : {summary['write_tools_total']}")
    print(f"  referenced by a test   : {summary['write_tools_covered']}")
    print(f"  never referenced       : {summary['write_tools_uncovered']}")
    print()
    print(f">>> {summary['write_tools_uncovered_pct']}% of write tools are untested")
    print(f">>> {summary['headline']}")
    print(f">>> {summary['secondary_repo_level']}")
    sens = summary["sensitivity_exhaustive_test_scan_only"]
    print(
        f">>> sensitivity (repos whose whole test suite was read, n={sens['repos_analyzed']}, "
        f"{sens['write_tools_total']} tools): {sens['write_tools_uncovered_pct']}% untested"
    )
    if summary["untested_repos"]:
        print()
        print("repositories with no test touching any write tool:")
        for name in summary["untested_repos"]:
            print(f"  - {name}")
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="agentcage Part A study")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="results JSON path")
    parser.add_argument("--limit", type=int, default=0, help="only screen the first N candidates")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--no-cache", action="store_true", help="ignore the on-disk HTTP cache")
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub token; raises the API limit from 60/h to 5000/h",
    )
    args = parser.parse_args(argv)

    frame = json.loads(REPOS_FILE.read_text(encoding="utf-8"))
    candidates = frame["candidates"]
    if args.limit:
        candidates = candidates[: args.limit]

    fetcher = Fetcher(token=args.token or None, use_cache=not args.no_cache)
    config = {
        "max_tool_files_per_repo": MAX_TOOL_FILES_PER_REPO,
        "max_test_files_per_repo": MAX_TEST_FILES_PER_REPO,
        "max_file_bytes": MAX_FILE_BYTES,
        "ref": "HEAD",
        "authenticated": bool(args.token),
        "coverage_detection": "symbol_reference_in_test_sources",
    }

    records: List[Dict[str, object]] = []
    started = time.time()
    try:
        for i, candidate in enumerate(candidates, 1):
            print(f"[{i:>2}/{len(candidates)}] {candidate['repo']}", flush=True)
            try:
                records.append(analyze_repo(fetcher, candidate, args.workers))
            except RateLimited:
                print("\n!! GitHub API rate limit exhausted.", file=sys.stderr)
                print("   Set GITHUB_TOKEN and re-run; cached repos will not be refetched.",
                      file=sys.stderr)
                return 2
    finally:
        fetcher.close()

    summary = summarize(records, time.time() - started, config)
    summary["github_api_calls"] = fetcher.api_calls
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print_report(summary)
    print(f"results written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
