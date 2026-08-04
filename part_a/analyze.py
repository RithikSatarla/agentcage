#!/usr/bin/env python3
"""Secondary analysis of the Part A results: intervals, categories, figures.

The study itself (``run.py``) answers one question: what fraction of write-capable
tool definitions is never referenced by a test. This module asks the follow-ups that
the raw number cannot:

* how precise is the headline (Wilson interval, not a bare point estimate);
* which *kinds* of write operation are least tested — shell execution, filesystem
  mutation, version-control writes, payments;
* how coverage is distributed across repositories, rather than pooled.

Writes ``part_a/analysis.json`` and the figures used by the paper and the website.
Every figure is generated from ``results.json``; none is drawn by hand.

    python -m part_a.analyze
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "part_a" / "results.json"
OUT_JSON = ROOT / "part_a" / "analysis.json"
PAPER_FIGS = ROOT / "paper" / "figures"
WEB_FIGS = ROOT / "website" / "figures"

# Categorical slots 1 and 2 of the validated reference palette. Verified with the
# palette validator in both modes: chroma floor, CVD separation (ΔE 24.7 protan
# light / 26.8 dark), normal-vision floor and contrast vs surface all pass.
COVERED_LIGHT, UNCOVERED_LIGHT = "#2a78d6", "#eb6834"
COVERED_DARK, UNCOVERED_DARK = "#3987e5", "#d95926"

INK_LIGHT, MUTED_LIGHT = "#0b0b0b", "#52514e"
INK_DARK, MUTED_DARK = "#ffffff", "#c3c2b7"

AMBIGUOUS_CATEGORIES = {"http_post"}

CATEGORY_LABELS = {
    "shell_exec": "Shell execution",
    "filesystem_write": "Filesystem mutation",
    "file_open_write": "File open (write mode)",
    "vcs_write": "Version-control writes",
    "db_write": "Database / ORM writes",
    "messaging_write": "Outbound messaging",
    "cloud_sdk_write": "Cloud SDK writes",
    "http_destructive": "HTTP PUT/PATCH/DELETE",
    "http_verb_arg": "HTTP verb argument",
    "payment_write": "Payment operations",
}


def wilson(successes: int, total: int, z: float = 1.959963985) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Used in preference to the normal approximation, which misbehaves for small
    counts and proportions near 0 or 1 — both of which occur per category here.
    """
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def load() -> dict:
    return json.loads(RESULTS.read_text(encoding="utf-8"))


def analyze(data: dict) -> dict:
    analysed = [r for r in data["repos"] if r.get("has_write_ops")]
    tools = [t for r in analysed for t in r["write_tools"]]

    total = len(tools)
    uncovered = sum(1 for t in tools if not t["covered"])
    lo, hi = wilson(uncovered, total)

    # Per category. A tool may carry several categories, so these sets overlap and
    # the counts do not sum to the total — stated wherever the table is presented.
    by_cat_total: Counter = Counter()
    by_cat_uncov: Counter = Counter()
    for tool in tools:
        for cat in tool["categories"]:
            # http_post is the ambiguous tier: it never qualifies a tool on its own
            # and only appears here alongside a qualifying category, so reporting it
            # as a write category would misrepresent the detector definition.
            if cat in AMBIGUOUS_CATEGORIES:
                continue
            by_cat_total[cat] += 1
            by_cat_uncov[cat] += 0 if tool["covered"] else 1

    categories = []
    for cat, n in by_cat_total.most_common():
        u = by_cat_uncov[cat]
        c_lo, c_hi = wilson(u, n)
        categories.append(
            {
                "category": cat,
                "label": CATEGORY_LABELS.get(cat, cat),
                "tools": n,
                "uncovered": u,
                "covered": n - u,
                "uncovered_pct": round(100.0 * u / n, 1),
                "ci95": [round(100 * c_lo, 1), round(100 * c_hi, 1)],
            }
        )

    # Per style of tool definition.
    by_style_total: Counter = Counter()
    by_style_uncov: Counter = Counter()
    for tool in tools:
        by_style_total[tool["style"]] += 1
        by_style_uncov[tool["style"]] += 0 if tool["covered"] else 1
    styles = [
        {
            "style": s,
            "tools": n,
            "uncovered": by_style_uncov[s],
            "uncovered_pct": round(100.0 * by_style_uncov[s] / n, 1),
        }
        for s, n in by_style_total.most_common()
    ]

    repos = []
    for r in sorted(analysed, key=lambda x: -x["write_tool_count"]):
        n = r["write_tool_count"]
        cov = r["covered_write_tools"]
        repos.append(
            {
                "repo": r["repo"],
                "tools": n,
                "covered": cov,
                "uncovered": n - cov,
                "uncovered_pct": round(100.0 * (n - cov) / n, 1),
                "has_ci": bool(r.get("has_ci")),
                "exhaustive_scan": bool(r.get("coverage_scan_complete")),
            }
        )

    # Does having CI say anything about whether write tools are tested?
    ci_tools = sum(x["tools"] for x in repos if x["has_ci"])
    ci_uncov = sum(x["uncovered"] for x in repos if x["has_ci"])
    noci_tools = sum(x["tools"] for x in repos if not x["has_ci"])
    noci_uncov = sum(x["uncovered"] for x in repos if not x["has_ci"])

    # Concentration: how much of the uncovered mass sits in the worst repositories?
    ranked = sorted(repos, key=lambda x: -x["uncovered"])
    top3 = sum(x["uncovered"] for x in ranked[:3])

    return {
        "schema_version": 1,
        "source_timestamp": data["timestamp"],
        "tools_total": total,
        "tools_uncovered": uncovered,
        "uncovered_pct": round(100.0 * uncovered / total, 1),
        "uncovered_ci95": [round(100 * lo, 1), round(100 * hi, 1)],
        "interval_note": (
            "Wilson 95% score interval on the pooled proportion of write-capable tool "
            "definitions never referenced by a test."
        ),
        "categories": categories,
        "category_note": (
            "A tool may match several categories, so these groups overlap and their "
            "counts do not sum to the total."
        ),
        "styles": styles,
        "repos": repos,
        "ci_comparison": {
            "with_ci": {"tools": ci_tools, "uncovered": ci_uncov,
                        "uncovered_pct": round(100.0 * ci_uncov / ci_tools, 1) if ci_tools else 0.0},
            "without_ci": {"tools": noci_tools, "uncovered": noci_uncov,
                           "uncovered_pct": round(100.0 * noci_uncov / noci_tools, 1)
                           if noci_tools else 0.0},
            "note": (
                "Descriptive only. Almost every analysed repository runs CI, so this "
                "contrast rests on very few projects and no inference is drawn from it."
            ),
        },
        "concentration": {
            "uncovered_in_top3_repos": top3,
            "share_of_all_uncovered": round(100.0 * top3 / uncovered, 1) if uncovered else 0.0,
        },
    }


# --- figures -----------------------------------------------------------------


def _style_axes(ax, muted: str, ink: str) -> None:
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(muted)
    ax.spines["bottom"].set_linewidth(0.6)
    ax.tick_params(colors=muted, labelsize=8, length=0)
    ax.xaxis.label.set_color(muted)
    for label in ax.get_yticklabels():
        label.set_color(ink)
        label.set_fontsize(8.5)


def figure_categories(analysis: dict, path_stem: Path, mode: str,
                      formats: Sequence[str] = ("pdf",)) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    covered_c = COVERED_LIGHT if mode == "light" else COVERED_DARK
    uncovered_c = UNCOVERED_LIGHT if mode == "light" else UNCOVERED_DARK
    ink = INK_LIGHT if mode == "light" else INK_DARK
    muted = MUTED_LIGHT if mode == "light" else MUTED_DARK

    cats = [c for c in analysis["categories"] if c["tools"] >= 5]
    cats = sorted(cats, key=lambda c: c["uncovered_pct"])
    labels = [c["label"] for c in cats]
    cov = [c["covered"] for c in cats]
    unc = [c["uncovered"] for c in cats]
    y = range(len(cats))

    fig, ax = plt.subplots(figsize=(6.4, 0.42 * len(cats) + 1.15))
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)

    ax.barh(y, cov, height=0.62, color=covered_c, label="Referenced by a test", zorder=3)
    # 2px surface gap between adjacent fills, expressed as a small left offset.
    ax.barh(y, unc, height=0.62, left=[c + 0.35 for c in cov], color=uncovered_c,
            label="Never referenced", zorder=3)

    for i, c in enumerate(cats):
        ax.text(c["tools"] + 2.0, i, f"{c['uncovered_pct']:.0f}%  (n={c['tools']})",
                va="center", ha="left", fontsize=8, color=muted)

    ax.set_yticks(list(y))
    ax.set_yticklabels(labels)
    ax.set_xlabel("write-capable tool definitions", fontsize=8.5)
    ax.set_xlim(0, max(c["tools"] for c in cats) * 1.34)
    ax.grid(axis="x", color=muted, alpha=0.16, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    _style_axes(ax, muted, ink)

    leg = ax.legend(loc="lower right", frameon=False, fontsize=8, ncol=1)
    for text in leg.get_texts():
        text.set_color(ink)

    fig.tight_layout(pad=0.4)
    for ext in formats:
        fig.savefig(f"{path_stem}.{ext}", transparent=True, bbox_inches="tight")
    plt.close(fig)


def figure_repos(analysis: dict, path_stem: Path, mode: str,
                 formats: Sequence[str] = ("pdf",)) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    covered_c = COVERED_LIGHT if mode == "light" else COVERED_DARK
    uncovered_c = UNCOVERED_LIGHT if mode == "light" else UNCOVERED_DARK
    ink = INK_LIGHT if mode == "light" else INK_DARK
    muted = MUTED_LIGHT if mode == "light" else MUTED_DARK

    repos = sorted(analysis["repos"], key=lambda r: r["tools"])
    labels = [r["repo"] for r in repos]
    cov = [r["covered"] for r in repos]
    unc = [r["uncovered"] for r in repos]
    y = range(len(repos))

    fig, ax = plt.subplots(figsize=(6.4, 0.4 * len(repos) + 1.15))
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)

    ax.barh(y, cov, height=0.6, color=covered_c, label="Referenced by a test", zorder=3)
    ax.barh(y, unc, height=0.6, left=[c + 0.35 for c in cov], color=uncovered_c,
            label="Never referenced", zorder=3)

    for i, r in enumerate(repos):
        ax.text(r["tools"] + 0.9, i, f"{r['uncovered']}/{r['tools']}",
                va="center", ha="left", fontsize=8, color=muted)

    ax.set_yticks(list(y))
    ax.set_yticklabels(labels)
    ax.set_xlabel("write-capable tool definitions", fontsize=8.5)
    ax.set_xlim(0, max(r["tools"] for r in repos) * 1.22)
    ax.grid(axis="x", color=muted, alpha=0.16, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    _style_axes(ax, muted, ink)

    leg = ax.legend(loc="lower right", frameon=False, fontsize=8)
    for text in leg.get_texts():
        text.set_color(ink)

    fig.tight_layout(pad=0.4)
    for ext in formats:
        fig.savefig(f"{path_stem}.{ext}", transparent=True, bbox_inches="tight")
    plt.close(fig)


def print_report(a: dict) -> None:
    lo, hi = a["uncovered_ci95"]
    print()
    print("AGENTCAGE PART A - secondary analysis")
    print("=" * 62)
    print(f"pooled: {a['tools_uncovered']}/{a['tools_total']} tools uncovered = "
          f"{a['uncovered_pct']}%  (95% CI {lo}-{hi}%)")
    print()
    print(f"{'category':26} {'tools':>6} {'uncov':>6} {'pct':>6}  95% CI")
    print("-" * 62)
    for c in a["categories"]:
        lo_c, hi_c = c["ci95"]
        print(f"{c['label'][:26]:26} {c['tools']:>6} {c['uncovered']:>6} "
              f"{c['uncovered_pct']:>5}%  {lo_c}-{hi_c}%")
    print("-" * 62)
    print("(categories overlap; a tool may match several)")
    print()
    for s in a["styles"]:
        print(f"  style {s['style']:<10} {s['tools']:>4} tools, {s['uncovered_pct']}% uncovered")
    conc = a["concentration"]
    print()
    print(f"  {conc['share_of_all_uncovered']}% of untested tools sit in 3 repositories")
    print()


def main() -> int:
    data = load()
    analysis = analyze(data)
    OUT_JSON.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")

    PAPER_FIGS.mkdir(parents=True, exist_ok=True)
    WEB_FIGS.mkdir(parents=True, exist_ok=True)

    # LaTeX takes PDF; the website takes SVG in both themes.
    figure_categories(analysis, PAPER_FIGS / "fig_category", "light", ("pdf",))
    figure_repos(analysis, PAPER_FIGS / "fig_repos", "light", ("pdf",))
    for mode in ("light", "dark"):
        figure_categories(analysis, WEB_FIGS / f"fig_category_{mode}", mode, ("svg",))
        figure_repos(analysis, WEB_FIGS / f"fig_repos_{mode}", mode, ("svg",))

    print_report(analysis)
    print(f"analysis written to {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
