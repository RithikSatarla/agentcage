# arXiv submission guide

The paper compiles clean today: 7 pages, 0 undefined references, 0 undefined citations.

```bash
python paper/make_tables.py                 # regenerate numbers from results.json
cd paper && pdflatex agentcage_arxiv.tex    # run twice for cross-references
```

Everything below is what stands between that PDF and a live arXiv listing.

---

## 1. Do this before you submit anything

### Verify every citation by hand

The bibliography has 10 entries. **Open all 10 arXiv pages and confirm the ID, author
list, and title match.** Do not skip this. A wrong arXiv number is the single most
common way a first paper loses credibility, and it is trivially checkable by anyone
reading it.

| Key | Claimed ID | Paper |
|---|---|---|
| `react` | 2210.03629 | ReAct |
| `toolformer` | 2302.04761 | Toolformer |
| `gorilla` | 2305.15334 | Gorilla |
| `toolllm` | 2307.16789 | ToolLLM |
| `swebench` | 2310.06770 | SWE-bench |
| `webarena` | 2307.13854 | WebArena |
| `agentbench` | 2308.03688 | AgentBench |
| `gaia` | 2311.12983 | GAIA |
| `toolemu` | 2309.15817 | ToolEmu |
| `agentdojo` | 2406.13352 | AgentDojo |

Check each at `https://arxiv.org/abs/<ID>`. Fix any mismatch in the `thebibliography`
block at the end of `agentcage_arxiv.tex`.

### Re-run the study so the paper matches reality

The measurement reads repositories at `HEAD`, so it drifts as those projects change.

```bash
python -m part_a.run
python paper/make_tables.py
cd paper && pdflatex agentcage_arxiv.tex && pdflatex agentcage_arxiv.tex
```

Every statistic in the paper comes from `generated_macros.tex`, which comes from
`part_a/results.json`. You never edit a number by hand — if the finding changes, the
paper changes with it.

### Read Section 3.6 one more time

The paper states plainly that Part A is exploratory, not pre-registered, and it prints
the full detector revision history including the two revisions that made the finding
weaker. **Leave that in.** It is the difference between a paper that survives scrutiny
and one that gets taken apart in public. Reviewers and Hacker News commenters both go
looking for exactly this, and finding it disclosed is disarming.

---

## 2. Compiling

### Option A — locally (MiKTeX is already installed on this machine)

```bash
cd paper
pdflatex agentcage_arxiv.tex
pdflatex agentcage_arxiv.tex        # second pass resolves \ref and \cite
```

Two passes are required. After one pass you get `??` in place of table and section
references.

### Option B — Overleaf

1. Create a project, upload `agentcage_arxiv.tex`, `generated_macros.tex`,
   `generated_table.tex`.
2. Recompile. Overleaf runs multiple passes automatically.

All three files are needed — the `.tex` `\input`s the other two.

---

## 3. Building the submission archive

arXiv wants source, not a PDF. It compiles your LaTeX itself.

```bash
cd paper
tar czf agentcage_arxiv.tar.gz agentcage_arxiv.tex generated_macros.tex generated_table.tex
```

Include exactly those three files. Do **not** include `.aux`, `.log`, `.out`, or the
PDF — arXiv rejects or ignores them, and `.aux` files cause confusing build failures on
their end.

The paper uses only standard packages (`geometry`, `booktabs`, `amsmath`, `listings`,
`xcolor`, `hyperref`), all present in arXiv's TeX Live installation. No custom `.sty`
files to ship.

---

## 4. Account and endorsement

Go to <https://arxiv.org/user/register>. Use an institutional email if you have one.

**The thing that will actually block you:** arXiv requires *endorsement* for first-time
submitters in most categories. You need an established author in that category to
endorse you. Plan for this — it is not instant.

Ways through it:
- Ask a professor, mentor, or researcher you know who has posted to cs.SE or cs.AI.
- Ask an author whose work you cite, with a short specific email — mention the finding
  and link the GitHub repo. A reproducible repo with green CI is a genuinely strong
  case.
- Some accounts are auto-endorsed based on affiliation. You may find you don't need it.

Budget several days. This is the step people are surprised by.

---

## 5. Metadata

**Primary category:** `cs.SE` (Software Engineering). This is a study of test coverage
in real codebases — that is squarely SE, and the audience there is the right one.

**Cross-list:** `cs.AI`, and optionally `cs.LG`.

**Title:** Untested by Construction: Test Coverage of Write-Capable Tools in
Open-Source LLM Agents

**Abstract:** paste the abstract from the `.tex`, with LaTeX commands resolved to plain
text. Replace `\AcUncoveredPct{}` with `20.3%`, `\AcTools{}` with `158`, and so on —
arXiv's abstract field is plain text and will render the macros literally if you forget.
Read it once in the preview.

**Comments field:** `Code and data: https://github.com/RithikSatarla/agentcage`

**License:** CC BY 4.0 is the usual choice and matches the repo's MIT posture.

---

## 6. Submitting

1. Upload `agentcage_arxiv.tar.gz`.
2. arXiv compiles it. If it fails, the log tells you which file is missing — usually a
   forgotten `generated_*.tex`.
3. **Read the generated PDF preview page by page.** This is the last checkpoint.
   Confirm the table renders, the numbers match `results.json`, and no `??` appears.
4. Submit.

Announcement happens on the next cycle: submissions before 14:00 ET on a weekday appear
about 20:00 ET the following weekday. Expect roughly 24–48 hours, longer over weekends.

Moderators can reclassify or hold a submission. If it's held, that is normal and usually
resolves; respond to any email promptly.

---

## 7. After it's live

You get an ID like `2608.XXXXX`. Then:

```bash
# README.md - replace the "not yet submitted" line with the real link
# paper/agentcage_arxiv.tex - optionally add the ID to the title block
git add -A && git commit -m "add arXiv link" && git push
```

**Post the PDF to the repo too.** `paper/agentcage_arxiv.pdf` is currently gitignored as
a build artefact; if you want it browsable on GitHub, force-add it:

```bash
git add -f paper/agentcage_arxiv.pdf
```

### On announcing

Lead with the finding and the caveat in the same breath. "20.3% of write-capable agent
tools are never referenced by a test — and here's the screening rate that bounds what
that means" is a much stronger post than the number alone, because the first person to
read the methodology will find the caveat anyway. Being the one who surfaced it is worth
more than the extra decimal point of impact.

Link the repo, not just the PDF. The reproducibility is the strongest part of this work:
anyone can run `python -m part_a.run` and get your numbers, on any machine, without
credentials. CI already proves it — the study re-runs weekly and the smoke job
reproduces the langchain-community figures on a clean runner every push.

---

## 8. When Part B is done

The paper is written so Part B slots in without restructuring:

1. Section 5.3 is the pre-registration. **Do not edit it after seeing results** — that
   is the entire point of having written it first.
2. Add a Part B results section reporting outcomes against the four pre-specified defect
   classes.
3. If H1 fails its falsification threshold (fewer than 20% of agents showing a defect),
   **report that**. A negative result on a pre-registered hypothesis is publishable and
   makes the Part A measurement more trustworthy, not less.
4. Recompile, then submit as **v2** to the same arXiv ID. Never post a second listing.
