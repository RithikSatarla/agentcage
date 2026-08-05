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
LANDING = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
DATA_PAGE = (ROOT / "website" / "data.html").read_text(encoding="utf-8")
# The site is multi-page; several assertions are about the site as a whole.
WEBSITE = "\n".join(
    p.read_text(encoding="utf-8") for p in sorted((ROOT / "website").glob("*.html"))
)
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


# Parametrise on the document NAME, never its contents: pytest embeds parameters in the
# test id and in PYTEST_CURRENT_TEST, and a file's full text blows the 32767-character
# environment-variable limit on Windows.
DOCS = {"README.md": README, "website": WEBSITE}


@pytest.mark.parametrize("name", sorted(DOCS))
def test_document_quotes_the_real_headline(name):
    doc = DOCS[name]
    assert f"{PCT}%" in doc, f"{name} does not quote the real headline {PCT}%"
    assert str(TOOLS) in doc, f"{name} does not quote the tool count {TOOLS}"
    assert str(REPOS) in doc, f"{name} does not quote the repo count {REPOS}"


@pytest.mark.parametrize("name", sorted(DOCS))
def test_document_reports_the_sensitivity_check(name):
    assert f"{SENS['write_tools_uncovered_pct']}%" in DOCS[name], f"{name} omits sensitivity"


@pytest.mark.parametrize("name", sorted(DOCS))
def test_document_discloses_the_screening_rate(name):
    """The screening rate bounds what the headline means; it must travel with it."""
    screened_out = len(RESULTS["screened_out_no_write_tools"])
    assert str(screened_out) in DOCS[name], f"{name} omits screened-out count {screened_out}"


# --- no unreproducible figures ------------------------------------------------

# A number that appeared in early drafts with no data behind it. If it ever shows up in a
# published document again, the build fails. STARTUP_GUIDE.md is exempt: it names the
# figure explicitly in a "do not say this" table.
UNREPRODUCIBLE = ["23.5", "crewAI-examples"]


@pytest.mark.parametrize("name", ["README.md", "website", "generated_macros.tex"])
def test_no_unreproducible_figures(name):
    doc = {**DOCS, "generated_macros.tex": MACROS}[name]
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


def test_website_quotes_the_interval():
    lo, hi = ANALYSIS["uncovered_ci95"]
    assert f"{lo}" in WEBSITE and f"{hi}" in WEBSITE, "site omits the confidence interval"


def test_data_page_carries_every_repository_and_category():
    """The data page is generated; it must reflect the whole study, not a selection."""
    for r in ANALYSIS["repos"]:
        assert r["repo"] in DATA_PAGE, f"data page omits {r['repo']}"
        assert f'>{r["tools"]}</td>' in DATA_PAGE or str(r["tools"]) in DATA_PAGE
    for c in ANALYSIS["categories"]:
        assert c["label"] in DATA_PAGE, f"data page omits {c['label']}"
        assert f'{c["uncovered_pct"]}%' in DATA_PAGE


def test_data_page_is_generated_not_hand_edited():
    assert "GENERATED by website/make_data_page.py" in DATA_PAGE


EXPLAINED = (ROOT / "website" / "explained.html").read_text(encoding="utf-8")


def test_explained_page_is_generated_not_hand_edited():
    assert "GENERATED by tools/make_explained_page.py" in EXPLAINED


def test_explained_page_regenerates_identically():
    """The page must be reproducible from the data, not drifted from it by hand."""
    import subprocess
    import sys

    before = EXPLAINED
    subprocess.run([sys.executable, "tools/make_explained_page.py"],
                   cwd=str(ROOT), check=True, capture_output=True)
    after = (ROOT / "website" / "explained.html").read_text(encoding="utf-8")
    assert after == before, (
        "website/explained.html differs from what the generator produces; "
        "re-run python tools/make_explained_page.py and commit the result")


def test_explained_page_quotes_the_measured_part_b_outcome():
    p = json.loads((ROOT / "part_b" / "partb_results.json").read_text(encoding="utf-8"))
    primary = p["primary_outcome"]
    assert f'{primary["numerator"]} of {primary["denominator"]}' in EXPLAINED
    assert str(p["secondary_outcomes"]["requests_graded"]) in EXPLAINED


def test_explained_page_carries_the_limitations():
    """The caveats travel with the result on the page that states it at length."""
    p = json.loads((ROOT / "part_b" / "partb_results.json").read_text(encoding="utf-8"))
    for limitation in p["limitations"]:
        assert limitation[:60] in EXPLAINED, f"explained page omits: {limitation[:60]}"


def test_website_figures_exist_in_both_themes():
    for name in ("fig_category", "fig_repos"):
        for mode in ("light", "dark"):
            path = ROOT / "website" / "figures" / f"{name}_{mode}.svg"
            assert path.exists(), f"missing {path.name}"
            assert f'src="figures/{name}_{mode}.svg"' in WEBSITE


#: Every page must exist and be reachable from every other page's nav. The paper has no
#: page of its own: the PDF ships and is linked, but the site does not host a reader.
SITE_PAGES = {"index.html", "explained.html", "manifesto.html", "models.html",
              "data.html"}
NAV_TARGETS = ("/", "/explained", "/manifesto", "/models", "/data")


def test_site_has_exactly_the_expected_pages():
    found = {p.name for p in (ROOT / "website").glob("*.html")}
    assert found == SITE_PAGES, f"unexpected page set: {sorted(found)}"


def _site_links():
    """(source page, href) for every non-external, non-mailto link on the site."""
    for page in sorted(SITE_PAGES):
        text = (ROOT / "website" / page).read_text(encoding="utf-8")
        for href in re.findall(r'href="([^"]+)"', text):
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            yield page, href


def _route_to_file(route: str) -> str:
    """Map a cleanUrls route to the file Vercel serves for it."""
    route = route.split("#")[0]
    if route in ("", "/"):
        return "index.html"
    return route.lstrip("/") + ("" if route.endswith(".html") else ".html")


def test_no_internal_link_is_dead():
    """Catches routes that point at a page which does not exist."""
    for page, href in _site_links():
        if href.startswith("#") or not href.startswith("/"):
            continue
        target = _route_to_file(href)
        assert (ROOT / "website" / target).exists(), f"{page} links to missing {href}"


def test_no_anchor_link_is_dead():
    """Catches #fragments pointing at an id that no longer exists.

    This is how /models ended up linking to /#try after the landing page's demo
    section was renamed to #demo.
    """
    for page, href in _site_links():
        if "#" not in href:
            continue
        target_file = "index.html" if href.startswith("#") else _route_to_file(href)
        anchor = href.split("#", 1)[1]
        if not anchor:
            continue
        text = (ROOT / "website" / target_file).read_text(encoding="utf-8")
        assert f'id="{anchor}"' in text, f"{page} links to #{anchor}, absent from {target_file}"


def test_every_asset_reference_exists():
    for page in sorted(SITE_PAGES):
        text = (ROOT / "website" / page).read_text(encoding="utf-8")
        for src in re.findall(r'(?:src|data)="([^"]+)"', text):
            if src.startswith(("http://", "https://", "data:")):
                continue
            assert (ROOT / "website" / src).exists(), f"{page} references missing {src}"


#: The landing page is built to its own locked spec and carries its own stylesheet.
#: The reference pages share styles.css.
PAGE_STYLESHEET = {
    "index.html": "home.css",
    "explained.html": "explained.css",
    "manifesto.html": "styles.css",
    "models.html": "styles.css",
    "data.html": "styles.css",
}


@pytest.mark.parametrize("page", sorted(SITE_PAGES))
def test_every_page_links_its_stylesheet(page):
    text = (ROOT / "website" / page).read_text(encoding="utf-8")
    sheet = PAGE_STYLESHEET[page]
    assert f'<link rel="stylesheet" href="{sheet}">' in text, f"{page} should load {sheet}"
    assert (ROOT / "website" / sheet).exists(), f"{sheet} missing"


@pytest.mark.parametrize("page", sorted(SITE_PAGES))
def test_every_page_can_reach_every_other(page):
    text = (ROOT / "website" / page).read_text(encoding="utf-8")
    for target in NAV_TARGETS:
        assert f'href="{target}"' in text, f"{page} cannot reach {target}"


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
