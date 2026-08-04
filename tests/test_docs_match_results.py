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

README = (ROOT / "README.md").read_text(encoding="utf-8")
WEBSITE = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
MACROS = (ROOT / "paper" / "generated_macros.tex").read_text(encoding="utf-8")
TABLE = (ROOT / "paper" / "generated_table.tex").read_text(encoding="utf-8")

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


def test_untested_repo_list_is_consistent():
    """The repos named as untested must be exactly those the data says are untested."""
    from_data = {r["repo"] for r in RESULTS["repos"]
                 if r.get("has_write_ops") and not r.get("has_test_coverage")}
    assert from_data == set(RESULTS["untested_repos"])
    assert len(from_data) == RESULTS["untested"]
