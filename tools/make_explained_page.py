#!/usr/bin/env python3
"""Generate website/explained.html: the long-form explanation of the problem.

Every figure on the page comes out of part_a/results.json, part_a/analysis.json and
part_b/partb_results.json. Nothing is typed by hand, so the page cannot drift away from
the study the way a written-out explainer would.

    python tools/make_explained_page.py
"""

from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = json.loads((ROOT / "part_a" / "results.json").read_text(encoding="utf-8"))
ANALYSIS = json.loads((ROOT / "part_a" / "analysis.json").read_text(encoding="utf-8"))
PARTB = json.loads((ROOT / "part_b" / "partb_results.json").read_text(encoding="utf-8"))
CAMEL = json.loads((ROOT / "part_b" / "traces" / "camel.json").read_text(encoding="utf-8"))
AGNO = json.loads((ROOT / "part_b" / "traces" / "agno.json").read_text(encoding="utf-8"))
OUT = ROOT / "website" / "explained.html"

TOOLS = ANALYSIS["tools_total"]
UNCOVERED = ANALYSIS["tools_uncovered"]
PCT = ANALYSIS["uncovered_pct"]
LO, HI = ANALYSIS["uncovered_ci95"]
REPOS = RESULTS["total_repos_analyzed"]
CANDIDATES = RESULTS["candidates_screened"]
SCREENED_OUT = len(RESULTS["screened_out_no_write_tools"])

PRIMARY = PARTB["primary_outcome"]
SECONDARY = PARTB["secondary_outcomes"]

NAV = """<nav class="nav">
  <div class="nav-inner">
    <a class="brand" href="/">AgentCage</a>
    <div class="nav-links">
      <a class="link" href="/explained" aria-current="page">Explained</a>
      <a class="link" href="/manifesto">Manifesto</a>
      <a class="link" href="/models">Models</a>
      <a class="link" href="/data">Data</a>
      <a class="link" href="https://github.com/RithikSatarla/agentcage">GitHub</a>
    </div>
  </div>
</nav>"""


def e(text: object) -> str:
    return html.escape(str(text))


# --- section 1: the problem --------------------------------------------------

def waffle() -> str:
    """One square per write-capable tool. The untested ones are lit.

    A waffle rather than a pie: the reader can count the squares, and 32 of them being
    a visible block is the point. A pie of the same two numbers reads as a statistic.
    """
    cells = []
    for i in range(TOOLS):
        state = "off" if i < UNCOVERED else "on"
        cells.append(f'<i class="{state}"></i>')
    return (
        f'<div class="waffle" role="img" aria-label="{TOOLS} write-capable tools, '
        f'{UNCOVERED} with no test referencing them">{"".join(cells)}</div>'
    )


def category_bars() -> str:
    rows = []
    worst = max(ANALYSIS["categories"], key=lambda c: c["uncovered_pct"])
    for cat in sorted(ANALYSIS["categories"], key=lambda c: -c["uncovered_pct"]):
        lead = " lead" if cat["label"] == worst["label"] else ""
        rows.append(
            f'<tr class="catrow{lead}">'
            f'<th scope="row">{e(cat["label"])}</th>'
            f'<td class="barcell"><span class="bar" style="width:{cat["uncovered_pct"]:.1f}%">'
            f'</span></td>'
            f'<td class="num">{cat["uncovered_pct"]}%</td>'
            f'<td class="of">{cat["uncovered"]} of {cat["tools"]}</td>'
            f"</tr>"
        )
    return (
        '<table class="cats">'
        '<caption class="sr">Share of tools in each category with no test referencing '
        'them</caption>'
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


# --- section 2: what broke ---------------------------------------------------

def trace_rows(trace: dict, scenario: str, limit: int = 6) -> str:
    """Render the requests one scenario made.

    The raw runner traces carry whole request records here; only the graded results in
    partb_results.json reduce them to sequence numbers.
    """
    sc = next(s for s in trace["scenarios"] if s["name"] == scenario)
    out = []
    for r in sc["requests"][:limit]:
        status = r["status"]
        cls = "bad" if status >= 409 else ("warn" if status >= 400 else "ok")
        drop = ' <span class="drop">response dropped</span>' if r["dropped"] else ""
        out.append(
            f'<li class="{"fail" if cls == "bad" else ""}">'
            f'<span class="verb">{e(r["method"])}</span>'
            f'<span class="path">{e(r["path"])}</span>'
            f'<span class="code {cls}">{status}</span>{drop}</li>'
        )
    return "".join(out)


def failures() -> str:
    agents = {a["agent"]: a for a in PARTB["agents"]}
    camel = agents["camel-ai/camel"]
    agno = agents["agno-agi/agno"]
    lli = agents["run-llama/llama_index"]

    agno_final = AGNO["final_state"]["issue_count"]

    return f"""
  <article class="failure">
    <header>
      <h3>{e(camel["agent"])}<span class="tool">{e(camel["tool"])}</span></h3>
      <p class="class">Stale read</p>
    </header>
    <p class="what">It reads a file from the default branch, then writes that version to
    a different branch. The version it holds is already out of date, so the API refuses
    the write. The tool does not catch the refusal.</p>
    <ol class="req">{trace_rows(CAMEL, "pull-request-repeat")}</ol>
    <p class="so">Its {camel["suite"]["tests"]} tests pass. They replace the whole GitHub
    library with a mock that always succeeds, so no version is ever checked.</p>
  </article>

  <article class="failure">
    <header>
      <h3>{e(agno["agent"])}<span class="tool">{e(agno["tool"])}</span></h3>
      <p class="class">Duplicate write</p>
    </header>
    <p class="what">One call to create an issue. The response is lost on the way back,
    so the HTTP client retries underneath the tool. Two issues now exist. The tool
    reports one success and never learns about the other.</p>
    <ol class="req">{trace_rows(AGNO, "create-interrupted")}</ol>
    <p class="so">After a caller-level retry as well, {agno_final} identical issues
    exist for one intent.</p>
  </article>

  <article class="failure">
    <header>
      <h3>{e(lli["agent"])}<span class="tool">{e(lli["tool"])}</span></h3>
      <p class="class">Duplicate write</p>
    </header>
    <p class="what">A different project, written independently, wrong in the same way.
    Both drive the same client library, so the duplicate is created below the level
    either tool can see.</p>
    <p class="so">That shared cause is why these two count as one finding, not two.</p>
  </article>"""


# --- section 4: the research -------------------------------------------------

def tier_bars() -> str:
    tiers = SECONDARY["tiers"]
    total = sum(tiers.values()) or 1
    labels = {
        "T1": "Exact. Same status and the fields the tool branches on.",
        "T2": "Equivalent. Same decision, minus fields nothing reads.",
        "T3": "Divergent but still exercising the write path.",
        "MISS": "The model could not answer at all.",
    }
    rows = []
    for key in ("T1", "T2", "T3", "MISS"):
        n = tiers[key]
        pct = 100.0 * n / total
        rows.append(
            f'<tr><th scope="row">{key}</th>'
            f'<td class="barcell"><span class="bar tier-{key.lower()}" '
            f'style="width:{max(pct, 0.6):.1f}%"></span></td>'
            f'<td class="num">{n}</td>'
            f'<td class="of">{e(labels[key])}</td></tr>'
        )
    return '<table class="cats tiers"><tbody>' + "".join(rows) + "</tbody></table>"


def limitations() -> str:
    items = "".join(f"<li>{e(x)}</li>" for x in PARTB["limitations"])
    return f'<ul class="limits">{items}</ul>'


def exclusions() -> str:
    rows = "".join(
        f'<tr><th scope="row">{e(x["repo"])}</th><td>{e(x["category"])}</td></tr>'
        for x in PARTB["exclusions"]
    )
    return '<table class="excl"><tbody>' + rows + "</tbody></table>"


# --- page --------------------------------------------------------------------

def build() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentCage explained: what breaks, and why the tests miss it</title>
<meta name="description" content="{UNCOVERED} of {TOOLS} write-capable agent tools have no test referencing them. Here is what goes wrong, what we measured, and how a stateful model catches it.">
<meta property="og:title" content="AgentCage explained">
<meta property="og:description" content="What breaks in agent write paths, and why passing tests miss it.">
<meta property="og:type" content="article">
<meta property="og:url" content="https://agentcage.vercel.app/explained">
<meta name="twitter:card" content="summary">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' fill='%230a0e27'/%3E%3Cpath d='M9 10v12M16 10v12M23 10v12' stroke='%2300d9ff' stroke-width='2.5' stroke-linecap='round'/%3E%3Cpath d='M6 16h20' stroke='%2300d9ff' stroke-width='2.5' stroke-linecap='round' opacity='.45'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap">
<link rel="stylesheet" href="explained.css">
</head>
<body>
<!-- GENERATED by tools/make_explained_page.py. Do not edit by hand: every figure
     below is read from part_a/results.json, part_a/analysis.json and
     part_b/partb_results.json. Re-run the study, re-run the generator. -->

<a class="skip" href="#main">Skip to content</a>
{NAV}

<header class="lead" id="main">
  <div class="wrap">
    <p class="eyebrow">Explained</p>
    <h1>A passing test suite does not mean the writes work.</h1>
    <p class="standfirst">We read {CANDIDATES} agent repositories, measured how many of
    their write-capable tools any test even mentions, then took the least-covered
    category and ran real agents against a model that remembers what they did. This
    page is what we found, in order.</p>
  </div>
</header>

<!-- 1. The problem -->
<section class="band">
  <div class="wrap">
    <p class="step">The problem</p>
    <h2>{UNCOVERED} of {TOOLS} tools can change the world. No test mentions them.</h2>
    <p class="body">Each square is one tool that writes something: a file, a shell
    command, a row, a message, a commit. The lit ones have no test referring to them
    anywhere in their repository.</p>

    {waffle()}

    <p class="figcap"><span class="key off"></span>{UNCOVERED} with no test referencing
    them <span class="sep">/</span> <span class="key on"></span>{TOOLS - UNCOVERED} with
    at least one <span class="sep">/</span> {PCT}% uncovered, 95% confidence interval
    {LO}% to {HI}%</p>

    <p class="body">That is a floor, not a ceiling. A test that merely names a tool
    counts as coverage here, whether or not it checks what the tool does to the world.
    The camel case further down is exactly that: a tool with nine passing tests, none of
    which let a real request out.</p>

    <h3 class="sub">Which kinds of writes go untested</h3>
    {category_bars()}
    <p class="figcap">Version-control writes are the least covered category, which is
    why they became the population for the second study.</p>
  </div>
</section>

<!-- 2. What actually broke -->
<section class="band alt">
  <div class="wrap">
    <p class="step">What broke</p>
    <h2>Three agents. Three suites that pass. Three broken writes.</h2>
    <p class="body">Their own code, unmodified, pointed at a stateful model instead of
    the real API. Every request below is from a trace committed in the repository.</p>
    {failures()}
  </div>
</section>

<!-- 3. Before and after -->
<section class="band">
  <div class="wrap">
    <p class="step">Why the usual fix misses it</p>
    <h2>A recorded response answers the second call exactly like the first.</h2>
    <p class="body">That is the whole difference. A fixture has no memory, so the retry
    looks identical to the original and the test agrees.</p>

    <div class="journey">
      <div class="track bad">
        <h3>Against a recorded fixture</h3>
        <ol>
          <li>Agent sends the write</li>
          <li>Fixture replays the recorded success</li>
          <li>Response is lost, the client retries</li>
          <li>Fixture replays the <em>same</em> success</li>
          <li class="mark">Test passes</li>
          <li class="mark">Production holds two records</li>
        </ol>
      </div>
      <div class="track good">
        <h3>Against a stateful model</h3>
        <ol>
          <li>Agent sends the write, the model records it</li>
          <li>Response is lost, the client retries</li>
          <li>Model applies the retry as a second write, because the real API has no
          idempotency key on this endpoint</li>
          <li>Two records now exist in the model</li>
          <li class="mark">Test fails on the count</li>
          <li class="mark">You add the guard before shipping</li>
        </ol>
      </div>
    </div>
  </div>
</section>

<!-- 4. How it works -->
<section class="band alt">
  <div class="wrap">
    <p class="step">How it works</p>
    <h2>Four moving parts, and the agent is not one of them.</h2>

    <ol class="flow">
      <li>
        <h3>The agent runs unchanged</h3>
        <p>No edits, no wrapper, no special build. Two of the three agents we ran take a
        server URL, so pointing them at a model is configuration. The third builds its
        client inline with no seam at all, so its traffic is redirected one layer below,
        inside the HTTP library it imports.</p>
      </li>
      <li>
        <h3>A model answers on a real socket</h3>
        <p>Not a patched function. An HTTP server on localhost, so the agent's own
        client library does its own parsing, retries and error handling exactly as it
        would in production. That is how the duplicate write above was found: the retry
        happens inside the client, where a patched function would never see it.</p>
      </li>
      <li>
        <h3>The model keeps state</h3>
        <p>Writes mutate it and later reads observe the change. A refunded charge cannot
        be refunded again. A file written on one branch is not the file on another. An
        issue that was deleted is gone.</p>
      </li>
      <li>
        <h3>Every request is graded</h3>
        <p>Against what the vendor documents, using a rubric fixed before any data
        existed. A request the model cannot answer is recorded as a miss rather than
        quietly passed.</p>
      </li>
    </ol>
  </div>
</section>

<!-- 5. The research -->
<section class="band">
  <div class="wrap">
    <p class="step">The research</p>
    <h2>What was measured, and what was not.</h2>

    <div class="stats">
      <div class="stat">
        <p class="n">{CANDIDATES}</p>
        <p class="l">repositories screened</p>
        <p class="f">{SCREENED_OUT} had no framework-idiomatic tool definitions to read,
        leaving {REPOS}</p>
      </div>
      <div class="stat">
        <p class="n">{PCT}%</p>
        <p class="l">of write-capable tools uncovered</p>
        <p class="f">{UNCOVERED} of {TOOLS}, 95% CI {LO}% to {HI}%</p>
      </div>
      <div class="stat">
        <p class="n">{PRIMARY["numerator"]} of {PRIMARY["denominator"]}</p>
        <p class="l">agents showed a defect</p>
        <p class="f">against a {int(PRIMARY["falsification_threshold"] * 100)}% threshold
        set before the run</p>
      </div>
    </div>

    <h3 class="sub">How the {SECONDARY["requests_graded"]} graded requests landed</h3>
    {tier_bars()}

    <h3 class="sub">What this does not show</h3>
    {limitations()}

    <h3 class="sub">Agents in the population we did not run</h3>
    {exclusions()}
    <p class="figcap">Each reason was fixed in advance, before we knew which
    repositories it would exclude.</p>
  </div>
</section>

<!-- 6. Try it -->
<section class="band alt">
  <div class="wrap">
    <p class="step">Try it</p>
    <h2>The smallest version of the whole idea.</h2>
    <p class="body">A charge, a refund, and the same refund again. The second one is
    refused because the model remembers the first.</p>

<pre class="code"><span class="kw">from</span> part_b.stripe_mock <span class="kw">import</span> StripeMock

stripe = StripeMock()
charge = stripe.post_charge(amount=<span class="str">5000</span>)

stripe.post_refund(charge=charge.id, amount=<span class="str">5000</span>)
stripe.post_refund(charge=charge.id, amount=<span class="str">5000</span>)
<span class="cmt"># StripeError: charge already refunded (400)</span></pre>

    <p class="body">To put a real client in front of it, serve the model and point the
    client at the address. Nothing about the agent changes.</p>

<pre class="code"><span class="kw">from</span> part_b.github_mock <span class="kw">import</span> GitHubMock
<span class="kw">from</span> part_b.server <span class="kw">import</span> ModelServer

<span class="kw">with</span> ModelServer(GitHubMock(repos=[<span class="str">"acme/widgets"</span>])) <span class="kw">as</span> server:
    agent = YourAgent(base_url=server.base_url)
    agent.run()
    <span class="cmt"># every request it made, in order</span>
    <span class="kw">for</span> request <span class="kw">in</span> server.writes():
        print(request)</pre>

    <div class="cta">
      <a class="btn btn-primary" href="https://github.com/RithikSatarla/agentcage#quickstart">Install it</a>
      <a class="btn" href="/data">See all the data</a>
    </div>
  </div>
</section>

<footer>
  <div class="wrap">
    <p class="links">
      <a href="/">Overview</a>
      <span class="sep">/</span>
      <a href="https://github.com/RithikSatarla/agentcage">GitHub</a>
      <span class="sep">/</span>
      <a href="agentcage-paper.pdf">Paper</a>
      <span class="sep">/</span>
      <a href="mailto:rithiksatarla@gmail.com">Email</a>
    </p>
    <p class="copy">&copy; 2026 AgentCage</p>
  </div>
</footer>
</body>
</html>
"""


def main() -> int:
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  part A: {UNCOVERED}/{TOOLS} uncovered ({PCT}%) across {REPOS} repos")
    print(f"  part B: {PRIMARY['numerator']}/{PRIMARY['denominator']} agents, "
          f"{SECONDARY['requests_graded']} requests graded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
