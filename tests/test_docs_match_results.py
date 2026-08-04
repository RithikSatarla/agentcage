"""Guard against documents drifting from the data.

Every headline number in the README, the website, and the paper's generated macros must
come from part_a/results.json. These tests fail the build if a number is edited by hand,
if the study is re-run without regenerating the paper, or if a figure with no data behind
it reappears in a document.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RESULTS = json.loads((ROOT / "part_a" / "results.json").read_text(encoding="utf-8"))
SENS = RESULTS["sensitivity_exhaustive_test_scan_only"]

ANALYSIS = json.loads((ROOT / "part_a" / "analysis.json").read_text(encoding="utf-8"))

README = (ROOT / "README.md").read_text(encoding="utf-8")
WEBSITE = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
MACROS = (ROOT / "paper" / "generated_macros.tex").read_text(encoding="utf-8")
TABLE = (ROOT / "paper" / "generated_table.tex").read_text(encoding="utf-8")
CAT_TABLE = (ROOT / "paper" / "generated_category_table.tex").read_text(encoding="utf-8")

PCT = RESULTS["write_tools_uncovered_pct"]
TOOLS = RESULTS["write_tools_total"]
UNCOVERED = RESULTS["write_tools_uncovered"]
REPOS = RESULTS["total_repos_analyzed"]


def _macro(name: str) -> str:
    match = re.search(r"\\newcommand\{\\" + name + r"\}\{(.+?)\}\n", MACROS)
    assert match, f"macro {name} missing from generated_macros.tex"
    return match.group(1)


# --- the paper is generated, not typed ---------------------------------------


@pytest.mark.parametrize(
    "macro,expected",
    [
        ("AcTools", str(TOOLS)),
        ("AcCovered", str(RESULTS["write_tools_covered"])),
        ("AcUncovered", str(UNCOVERED)),
        ("AcRepos", str(REPOS)),
        ("AcCandidates", str(RESULTS["candidates_screened"])),
        ("AcSensTools", str(SENS["write_tools_total"])),
        ("AcSensRepos", str(SENS["repos_analyzed"])),
    ],
)
def test_paper_macro_matches_results(macro, expected):
    assert _macro(macro) == expected


def test_paper_percentages_match_results():
    assert _macro("AcUncoveredPct") == f"{PCT}\\%"
    assert _macro("AcSensPct") == f"{SENS['write_tools_uncovered_pct']}\\%"


def test_paper_table_row_count_matches_analyzed_repos():
    analyzed = [r for r in RESULTS["repos"] if r.get("has_write_ops")]
    assert len(analyzed) == REPOS
    for record in analyzed:
        assert record["repo"].replace("_", r"\_") in TABLE


def test_paper_table_totals_match_results():
    assert f"\\textbf{{{TOOLS}}}" in TABLE
    assert f"\\textbf{{{UNCOVERED}}}" in TABLE


# --- prose documents quote the real figures ----------------------------------


@pytest.mark.parametrize("doc,name", [(README, "README.md"), (WEBSITE, "website/index.html")])
def test_document_quotes_the_real_headline(doc, name):
    assert f"{PCT}%" in doc, f"{name} does not quote the real headline {PCT}%"
    assert str(TOOLS) in doc, f"{name} does not quote the tool count {TOOLS}"
    assert str(REPOS) in doc, f"{name} does not quote the repo count {REPOS}"


@pytest.mark.parametrize("doc,name", [(README, "README.md"), (WEBSITE, "website/index.html")])
def test_document_reports_the_sensitivity_check(doc, name):
    assert f"{SENS['write_tools_uncovered_pct']}%" in doc, f"{name} omits the sensitivity figure"


@pytest.mark.parametrize("doc,name", [(README, "README.md"), (WEBSITE, "website/index.html")])
def test_document_discloses_the_screening_rate(doc, name):
    """The screening rate bounds what the headline means; it must travel with it."""
    screened_out = len(RESULTS["screened_out_no_write_tools"])
    assert str(screened_out) in doc, f"{name} omits the screened-out count {screened_out}"


# --- no unreproducible figures ------------------------------------------------

# A number that appeared in early drafts with no data behind it. If it ever shows up in a
# published document again, the build fails. STARTUP_GUIDE.md is exempt: it names the
# figure explicitly in a "do not say this" table.
UNREPRODUCIBLE = ["23.5", "crewAI-examples"]


@pytest.mark.parametrize("doc,name", [(README, "README.md"), (WEBSITE, "website/index.html"),
                                      (MACROS, "generated_macros.tex")])
def test_no_unreproducible_figures(doc, name):
    for bad in UNREPRODUCIBLE:
        assert bad not in doc, f"{name} contains unreproducible claim {bad!r}"


def test_paper_source_carries_no_unreproducible_figures():
    tex = (ROOT / "paper" / "agentcage_arxiv.tex").read_text(encoding="utf-8")
    for bad in UNREPRODUCIBLE:
        assert bad not in tex, f"paper contains unreproducible claim {bad!r}"


# --- the secondary analysis is derived from the study, not authored ------------


def test_analysis_totals_match_the_study():
    assert ANALYSIS["tools_total"] == TOOLS
    assert ANALYSIS["tools_uncovered"] == UNCOVERED
    assert ANALYSIS["uncovered_pct"] == PCT


def test_analysis_category_counts_are_internally_consistent():
    for cat in ANALYSIS["categories"]:
        assert cat["covered"] + cat["uncovered"] == cat["tools"]
        assert cat["uncovered"] <= cat["tools"]
        lo, hi = cat["ci95"]
        assert 0.0 <= lo <= cat["uncovered_pct"] <= hi <= 100.0, cat["category"]


def test_ambiguous_category_is_excluded_from_the_breakdown():
    """http_post never qualifies a tool, so it must not appear as a write category."""
    assert "http_post" not in {c["category"] for c in ANALYSIS["categories"]}


def test_confidence_interval_brackets_the_point_estimate():
    lo, hi = ANALYSIS["uncovered_ci95"]
    assert lo < PCT < hi


def test_paper_category_table_covers_every_category():
    for cat in ANALYSIS["categories"]:
        assert cat["label"].replace("/", "/") in CAT_TABLE, cat["label"]


def test_website_quotes_the_interval_and_category_findings():
    lo, hi = ANALYSIS["uncovered_ci95"]
    assert f"{lo}" in WEBSITE and f"{hi}" in WEBSITE, "website omits the confidence interval"
    vcs = next(c for c in ANALYSIS["categories"] if c["category"] == "vcs_write")
    shell = next(c for c in ANALYSIS["categories"] if c["category"] == "shell_exec")
    assert f"{vcs['uncovered_pct']}%" in WEBSITE
    assert f"{shell['uncovered_pct']}%" in WEBSITE


def test_website_figures_exist_in_both_themes():
    for name in ("fig_category", "fig_repos"):
        for mode in ("light", "dark"):
            path = ROOT / "website" / "figures" / f"{name}_{mode}.svg"
            assert path.exists(), f"missing {path.name}"
            assert f'src="figures/{name}_{mode}.svg"' in WEBSITE


def test_quoted_test_counts_agree_across_documents():
    """Three documents once quoted three different suite sizes. Keep them in step."""
    quoted = {}
    for name in ["README.md", "STARTUP_GUIDE.md", "PROTOCOL.md", "website/index.html"]:
        text = (ROOT / name).read_text(encoding="utf-8")
        for count in re.findall(r"\b(\d+) tests\b", text):
            quoted.setdefault(count, []).append(name)
    assert len(quoted) <= 1, f"documents disagree on the suite size: {quoted}"


def test_committed_trace_is_the_real_double_refund():
    """The committed trace is cited by the paper; it must not go stale.

    Regenerate with: python -m part_b.demo_double_refund
    """
    trace = json.loads(
        (ROOT / "traces" / "example_double_refund.json").read_text(encoding="utf-8")
    )
    entries = trace["traces"]
    assert trace["count"] == len(entries) == 4

    calls = [(t["method"], t["path"], t["status_code"]) for t in entries]
    assert calls == [
        ("POST", "/v1/charges", 200),
        ("POST", "/v1/refunds", 200),
        ("POST", "/v1/refunds", 400),
        ("GET", "/v1/charges/ch_00000001", 200),
    ]

    # the two refund attempts are byte-identical; only the responses differ
    assert entries[1]["body"] == entries[2]["body"]
    assert "charge_already_refunded" in entries[2]["response_body"]

    # no credential may ever reach a committed trace
    for entry in entries:
        assert entry["headers"].get("authorization") == "<redacted>"
        assert "sk_live" not in json.dumps(entry)


def test_untested_repo_list_is_consistent():
    """The repos named as untested must be exactly those the data says are untested."""
    from_data = {r["repo"] for r in RESULTS["repos"]
                 if r.get("has_write_ops") and not r.get("has_test_coverage")}
    assert from_data == set(RESULTS["untested_repos"])
    assert len(from_data) == RESULTS["untested"]
