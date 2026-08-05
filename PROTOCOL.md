# AgentCage Protocol

Methodology for the two halves of AgentCage:

- **Part A** — a measurement study: how much of the open-source agent ecosystem tests the
  tools that change external state.
- **Part B** — the artefact: a passive HTTP interceptor plus a stateful API model, so that
  agent write paths can be exercised without touching a real account.

Everything below is implemented in this repository. `part_a/run.py` is the executable
form of §2, and re-running it regenerates `part_a/results.json` from scratch.

---

## 0. Status of this document

Research-integrity note, stated up front because it changes how §2's numbers should be
read:

- **Part A is exploratory, not pre-registered.** The detectors in §2.3 were developed
  iteratively against the corpus: the first implementation was run, its output inspected,
  and specific false positives and false negatives were traced to specific regexes, which
  were then changed. §2.7 lists each of those revisions and what it did to the headline
  number. Treat the result as a measurement with a documented derivation, not as a
  confirmatory test of a hypothesis fixed in advance.
- **Part B's evaluation (§3.4) is pre-registered.** It is written before the experiment is
  run, and no Part B results exist yet.

Anyone reproducing this should read §2.7 and §2.8 before citing §2.6.

---

## 1. Motivation

An LLM agent with tool access is a program that issues writes against real systems: it
refunds payments, deletes rows, force-pushes branches, sends mail. The industry tests
these agents the way it tests read paths — by recording a response and replaying it.

Replay fixtures cannot catch the failure mode that matters. A fixture answers
`POST /v1/refunds` with a stored `200` no matter how many times it is asked. An agent that
retries a refund because it did not parse the first response therefore passes its test
suite and double-refunds in production. The bug lives in the *state transition*, and a
fixture has no state.

**RQ1** — How much of the open-source agent ecosystem has tests that exercise its
write-capable tools at all? (§2)

**RQ2** — Does replacing replay fixtures with a stateful model surface write-path defects
that fixtures cannot, in agents that pass their existing suites? (§3, not yet run)

---

## 2. Part A — measurement

### 2.1 Sampling frame

`part_a/repos.json` holds 45 candidate repositories with the inclusion and exclusion
criteria recorded alongside them. The frame is a **convenience sample** of recognisable
Python agent projects — frameworks, autonomous agents, coding agents, browser agents — not
a random sample of GitHub.

Restricted to Python-primary repositories, because every detector in §2.3 is a Python
source pattern and would silently under-detect on TypeScript.

The frame is biased toward large, well-maintained projects. That biases measured coverage
*upward*: the long tail of small agent repositories is very unlikely to be better tested
than CrewAI. The headline should be read as a **lower bound** on how much goes untested.

### 2.2 Screening — what enters the denominator

A candidate is **analysed** only if the detectors find at least one *write-capable tool
definition*. Screening is mechanical, not curatorial: 32 of the 45 candidates were screened
out by the code, and they are listed by name in `results.json` under
`screened_out_no_write_tools`.

Screening out is not a claim that a project performs no writes. It means the project has no
**framework-idiomatic tool definition whose body performs a detectable write**. See §2.8.

### 2.3 Detectors

A tool definition is recognised in three styles:

| Style | Pattern |
|---|---|
| `decorator` | `@tool`, `@agent.tool`, `@mcp.tool()`, `@server.tool()`, `@registry.action(...)`, `@function_tool`, `@kernel_function`, `@register_action`, followed by the `def` it decorates |
| `class` | `class X(...)` where a base name matches `*BaseTool`, `*Toolkit`, `*ToolSpec`, `*BaseAction`, `Tool`, or `Action` |
| `schema` | a JSON tool schema: `"name": "..."` followed within 600 characters by `"parameters"` or `"input_schema"` |

`@click.command` and `@app.command` are deliberately **not** matched — they are CLI entry
points, and matching them would classify ordinary command-line programs as agents.

A definition is **write-capable** if its body matches at least one non-ambiguous marker:

| Category | Matches |
|---|---|
| `http_destructive` | `.put(`, `.patch(`, `.delete(` on a requests/httpx/aiohttp client or session |
| `http_verb_arg` | `method="PUT"` / `"PATCH"` / `"DELETE"` |
| `filesystem_write` | `os.remove`, `os.unlink`, `os.rmdir`, `os.makedirs`, `os.rename`, `os.replace`, `shutil.rmtree`, `shutil.move`, `shutil.copy*`, `.unlink(`, `.write_text(`, `.write_bytes(`, `.mkdir(`, `.touch(` |
| `file_open_write` | `open(..., "w"/"a"/"x")` |
| `shell_exec` | `subprocess.run/call/check_call/check_output/Popen`, `os.system`, `os.popen`, `pty.spawn`, `exec_run`, `run_command`, `execute_command` |
| `db_write` | `INSERT INTO`, `UPDATE ... SET`, `DELETE FROM`, `DROP TABLE`, `TRUNCATE`, `.commit()`, `.insert_one/-many(`, `.update_one/-many(`, `.delete_one/-many(`, `.bulk_write(`, and `session/db/conn/cursor.add|execute|save|delete(` |
| `cloud_sdk_write` | `put_object`, `delete_object`, `create_bucket`, `delete_bucket`, `upload_file(obj)`, `run/stop/terminate_instances`, `put_item`, `delete_item` |
| `vcs_write` | `git push/commit/checkout/reset/clean/rm`, `create_pull(_request)`, `create_issue`, `create_comment`, `create_review`, `merge_pull_request`, `create/update/delete_file`, `create_git_ref` |
| `messaging_write` | `send_message`, `send_email`, `send_mail`, `sendmail`, `chat_postMessage`, `post_message`, `smtplib.SMTP`, `messages.create`, `.send()` |
| `payment_write` | `stripe.<Resource>.create/modify/cancel/delete`, `Refund/PaymentIntent/Charge/Subscription.create(` |

A separate `http_post` tier exists and is **excluded** from write-capability on its own.
Plenty of read-only search and inference endpoints are queried with POST, so a bare POST is
not evidence of state mutation. Tools matching only `http_post` are counted in
`post_only_tool_count` and excluded from the analysis.

Two rules prevent a definition from being its own evidence:

1. **Signature stripping.** Markers are matched against the body *after* the `def`/`class`
   signature. Without this, `def delete_file(path)` satisfies a pattern written to catch
   *calls* to `delete_file(`, and any tool whose name reads like a mutation is counted as
   one on the strength of its own name.
2. **Self-reference neutralisation.** Every occurrence of the tool's own identifier is
   replaced before matching.

Both rules were added after the false positives in §2.7 were found by manual inspection.

### 2.4 Coverage

For each analysed repository the study reads its test files — paths matching `tests?/`,
`testing/`, `test_*.py`, or `*_test.py` — and a write tool counts as **covered** when its
identifier appears as a whole word anywhere in that corpus.

This is a *generous* bar, chosen deliberately. It does not require an assertion, a
destructive-path exercise, or a branch to be reached; a single import or a passing mention
in an unrelated test satisfies it. Coverage measured this way is an **upper bound** on real
coverage, so the uncovered fraction is a **lower bound**. A tool the study calls untested is
untested under the weakest definition available.

**The unit of analysis is the tool, not the repository.** Repo-level "has at least one test
touching at least one write tool" is close to meaningless: a project shipping 34 write tools
with 1 of them referenced passes it. Counting tools puts 158 items in the denominator
instead of 13, and it is the tool-level figure that is reported as the headline.

### 2.5 Determinism and caps

- Files are read at the `HEAD` ref. Re-running at a later date reads later code; results
  carry a timestamp and the study is scheduled weekly in CI.
- File trees come from the GitHub API (one call per repository); file contents come from
  `raw.githubusercontent.com`, which does not consume the API rate limit. The whole study
  costs 45 API calls and runs unauthenticated.
- Responses are cached under `.cache/`, so re-runs cost nothing and are reproducible.
- Caps: 250 source files and 400 test files per repository, 400 kB per file. When a cap
  binds it is recorded per repository (`source_files_skipped_by_cap`,
  `coverage_scan_complete`), never silently applied. Read order is by
  `tool_file_rank`, which prioritises files whose own name names a tool surface.
- The sensitivity analysis in `results.json` recomputes the headline over only those
  repositories whose **entire** test suite was read, where a missed reference cannot be a
  sampling artefact.

### 2.6 Results

Run of 2026-08-04, 45 candidates, 0 unresolved, 45 API calls.

| | |
|---|---|
| Candidates screened | 45 |
| Screened out (no write-capable tool definition) | 32 |
| **Repositories analysed** | **13** |
| **Write-capable tool definitions found** | **158** |
| Referenced by at least one test | 126 |
| **Never referenced by any test** | **32 (20.3%)** |
| Repositories with no test touching any write tool | 1 (7.7%) — `aiwaves-cn/agents`, which has no test files at all |

Sensitivity — restricted to the 7 repositories whose entire test suite was read (87 tools):
**20.7% uncovered**. The headline does not depend on the test-file cap.

Per-repository detail, including every tool name and the categories that classified it, is
in `part_a/results.json`.

### 2.7 Revision history of the detectors

Each revision below changed the headline. They are recorded because the final number is not
meaningful without them.

| # | Problem found | Change | Effect |
|---|---|---|---|
| 1 | `TOOL_PATH_RE` matched substrings, so `openai.py` matched the `op` alternative and the candidate set filled with model clients | Match whole delimiter-separated path words only | Candidate files became actual tool surfaces |
| 2 | The file cap was applied lexicographically, so large repos were represented only by whichever top-level directory sorted first | Rank by tool-likeness before applying the cap | — |
| 3 | Reading only files whose *path* contained a tool keyword missed write tools defined in files like `controller/service.py` | Make every non-test, non-vendor source file eligible; keep ranking for read order | 13 → 17 repos analysed |
| 4 | `@registry.action` and MCP-style decorators were not in the vocabulary | Added `action` / `register_action` | included in the above |
| 5 | `db_write` matched bare `.save(`, catching `arg.save(temp_file.name)` — a PIL image save in `smolagents` | Removed bare `.save(`/`.delete(`; require a db-ish receiver | removed a false positive from the untested set |
| 6 | Patterns matched the definition line itself: `haystack`'s `delete_file` was classified as a write on the strength of `def delete_file(` | Strip signatures; neutralise self-reference | 17 → 13 repos analysed |
| 7 | Repo-level binary coverage was too weak to carry a headline, and after #5–#6 rested on a single repository | Switch the unit of analysis to the tool | headline became 20.3% of 158 tools |

Revisions 5 and 6 removed findings that would have *raised* the headline. Revision 3 added
repositories. The number moved in both directions.

### 2.8 Threats to validity

**Construct.** The study measures *framework-idiomatic tool definitions*. Agents with
bespoke architectures — `aider`, `OpenHands`, `SWE-agent`, `gpt-engineer` — write to disk and
run shell commands constantly but expose no `@tool`/`BaseTool` surface, so they screen out.
This is a scope boundary, not a finding about them, and it is the single largest limitation:
32 of 45 candidates screened out, and the denominator of 13 is a statement about detector
coverage as much as about the ecosystem.

**Coverage inference.** Name reference in a test file is not execution. A referenced tool may
have no assertion on its destructive path; an unreferenced tool may be covered through an
integration harness that never names it. The first error inflates coverage, the second
deflates it. §2.4 argues the first dominates, which makes 20.3% a floor.

**Browser and GUI writes.** `browser-use`, `Skyvern` and `UFO` mutate external state by
clicking and submitting forms. No marker detects a DOM interaction, so these screen out or
under-count.

**Frameworks vs. applications.** Several screened-out projects (`langgraph`, `pydantic-ai`,
`dspy`, `swarm`, MCP's SDK) ship the *mechanism* for defining tools rather than write tools
themselves. Screening them out is correct, and it means the analysed set skews toward tool
catalogues.

**Vendored code.** `docs/`, `examples/`, `node_modules/`, `site-packages/` and similar are
excluded by path, which may drop genuine tool definitions that live in example directories.

**Single coder.** Classification is fully mechanical, so there is no inter-rater reliability
to report — but equally, no human judgement was applied to individual repositories, and any
error is systematic and reproducible rather than idiosyncratic.

---

## 3. Part B — the cage

### 3.1 Components

**`part_b/interceptor.py`** — passive capture. Attaches via each library's own documented
hook points (`httpx` event hooks, `requests` response hooks). It records method, URL, host,
path, query, headers, body and response status, and it **never** rewrites a request, injects
a response, or short-circuits the transport. Credential headers (§ `SENSITIVE_HEADERS`) are
replaced with `<redacted>` at capture time, so traces are committable artefacts.

**`part_b/stripe_mock.py`** — a stateful model of the money-moving subset of the Stripe API.
Not a fixture: writes mutate state and later reads observe the mutation.

### 3.2 Modelled semantics

- A refund increases `charge.amount_refunded` and, at parity with `amount_captured`, sets
  `charge.refunded`. `GET /v1/charges/{id}` reflects it immediately.
- Refunds are capped at the unrefunded amount, rejected with Stripe's real message —
  `Refund amount ($99.99) is greater than unrefunded amount on charge ($50.00)`, code
  `amount_too_large`.
- A fully refunded charge rejects further refunds with `charge_already_refunded`.
- Uncaptured charges cannot be refunded (`charge_not_captured`).
- Unknown identifiers return HTTP 404 `resource_missing`.
- Idempotency keys replay the original response; reuse with different parameters is
  rejected with `idempotency_key_in_use`.
- Identifiers come from a counter (`ch_00000001`) and the clock is injectable, so a run is
  byte-for-byte reproducible.

### 3.3 Fidelity boundary

Explicitly **not** modelled: payment method validation, 3-D Secure, disputes, payouts,
balance transactions, webhooks, connected accounts, expansion (`expand[]`), pagination
cursors, rate limits, and Stripe's real ID format. `capture=False` refunds are rejected
rather than releasing the authorisation as Stripe does. The model is sufficient to exercise
double-refund, over-refund, refund-after-refund and read-after-write, and nothing more.

### 3.4 Worked example

`part_b/demo_double_refund.py` runs the whole loop and writes
`traces/example_double_refund.json`, which is committed. A real `httpx` client, the
passive interceptor, and the stateful model behind the transport:

```
POST /v1/charges           -> 200   ch_00000001 created, 5000 usd
POST /v1/refunds           -> 200   amount_refunded 0 -> 5000
POST /v1/refunds           -> 400   charge_already_refunded     <- the retry
GET  /v1/charges/ch_...    -> 200   amount_refunded == 5000
```

Against a replay fixture the second POST also returns 200 and the customer is refunded
twice. The captured trace has `authorization` replaced with `<redacted>`, which is why
traces are safe to commit.

---

## 3A. Part B: pre-registered evaluation

**Status: not run. No results exist.** This section is written before the experiment and
is the thing that must not change afterwards.

### 3A.1 Hypothesis

**H1.** Agents that pass their existing replay-based test suites exhibit write-path
defects when the same paths are exercised against a stateful model.

### 3A.2 Sample and exclusions

Sampling frame: the Part A analysed set, restricted to repositories with tools performing
payment-like or otherwise reversible-in-model writes. Pre-specified exclusions: agents
with no runnable test suite; agents whose write tools require credentials we cannot
substitute; agents whose tools only mutate local filesystem state (no HTTP surface for
the interceptor to observe).

Any agent added or dropped **after** its result is known will be reported as such, with
the reason.

### 3A.3 Procedure

For each sampled agent:

1. Run its own suite unmodified. *Expected: passes.* An agent whose suite already fails
   is excluded and reported.
2. Re-run the write paths through the interceptor against the stateful model.
3. Grade every captured request by the resolution tiers below.
4. Classify every divergence by the defect classes below.

### 3A.4 Resolution tiers

Each captured request receives exactly one tier. Where more than one could apply,
precedence is **T1 > T2 > T3 > MISS**, and the grader records which rule fired.

| Tier | Definition |
|---|---|
| **T1 — exact** | The model returns the same status code and the same decision-relevant fields the real API would, and the agent's control flow is identical. |
| **T2 — behaviourally equivalent** | Status class and decision-relevant semantics match; only incidental fields differ (identifier format, timestamps, fields we do not model). The agent's control flow is unchanged. |
| **T3 — divergent but exercising** | The model's response differs from the real API's in a way that still drives the agent down its write path — typically the model is stricter. The interaction remains informative; the divergence is recorded as a fidelity gap. |
| **MISS** | The model cannot answer at all (unmodelled endpoint, parameter, or state), or answers such that the scenario cannot be evaluated. |

Tier assignment for T1/T2 requires a documented expectation of the real API's behaviour
(official documentation or a recorded real response). Where no such reference exists, the
request is graded T3 or MISS, never T1.

### 3A.5 Miss classification

Every MISS is assigned exactly one class. This is the measurement that decides whether
the approach is viable at all.

| Class | Definition | Implication |
|---|---|---|
| **MODELABLE** | The miss is a gap we could close by writing more model: an unmodelled endpoint, field, or error path. | Cost is engineering effort. The approach holds. |
| **PRODUCTION-STATE-DEPENDENT** | The miss requires state that exists only in a real account — a specific pre-existing entity, a live dispute, a provider-side asynchronous event, an inbound webhook. | No amount of modelling closes it without real credentials. The approach has a ceiling. |

### 3A.6 Defect classes

Fixed in advance and **not extendable** after results are seen:

1. **Duplicate write** — the same logical mutation applied twice (a retry with no
   idempotency key).
2. **Unchecked error** — a 4xx response treated as success.
3. **Stale read** — a decision taken on state cached from before a write.
4. **Over-refund** — a refund exceeding the unrefunded amount.

A divergence that is a genuine defect but fits none of these is recorded as
**unclassified** and reported separately. It does not count toward the primary outcome.

### 3A.7 Outcomes

**Primary outcome.** Proportion of sampled agents exhibiting at least one class-1–4
defect.

**Secondary outcomes.** Distribution of resolution tiers across all captured requests;
MISS rate; the MODELABLE / PRODUCTION-STATE-DEPENDENT split; defects per agent by class.

**Analysis.** Descriptive proportions with the denominator stated. No subgroup analysis
beyond the tier and class breakdowns specified here.

### 3A.8 Falsification and kill criterion

**H1 is not supported** if fewer than **20%** of sampled agents exhibit any class-1–4
defect. This is reported as a negative result, not reframed.

**Kill criterion.** If **more than 50% of MISSes are PRODUCTION-STATE-DEPENDENT**, the
stateful-mock approach cannot be made general by further engineering, and the project
pivots rather than continuing to build API models. This threshold is fixed now,
before any data exists, precisely so it cannot be moved later.

### 3A.9 Pre-registration checklist

| Item | Committed |
|---|---|
| Hypothesis stated before data collection | Yes — §3A.1 |
| Sampling frame and exclusions fixed in advance | Yes — §3A.2 |
| Primary outcome specified and single | Yes — §3A.7 |
| Secondary outcomes enumerated | Yes — §3A.7 |
| Grading rubric with precedence rules | Yes — §3A.4 |
| Defect classes closed to extension | Yes — §3A.6 |
| Falsification threshold numeric | Yes — 20%, §3A.8 |
| Kill criterion numeric | Yes — 50%, §3A.8 |
| Analysis plan fixed | Yes — §3A.7 |
| Raw evidence published | Yes — every trace committed to `traces/` |
| Deviations to be logged | Yes — §3A.10 |
| Results known at time of writing | **No — none exist** |

### 3A.10 Deviations log

Any departure from §3A.1–§3A.9 once the experiment starts is recorded here, with the
date, what changed, and why. An empty log at publication means the protocol was followed
exactly.

**2026-08-05 — the pre-registered sampling frame is empty.**

§3A.2 restricts the sample to repositories "whose tools perform payment-like writes."
Counting qualifying categories across all 158 write-capable tool definitions in the
analysed set gives:

| Category | Tools |
|---|---:|
| `filesystem_write` | 57 |
| `shell_exec` | 36 |
| `file_open_write` | 32 |
| `messaging_write` | 23 |
| `db_write` | 18 |
| `vcs_write` | 15 |
| `cloud_sdk_write` | 5 |
| `http_destructive` | 4 |
| **`payment_write`** | **0** |

**No tool in the analysed set performs a payment operation.** Part B cannot run as
written, because the only API model built — Stripe — has no target in the population the
study measured.

This is a scoping error in the pre-registration, and it is recorded rather than quietly
repaired. It was caught only because §3A.2 fixed the frame in advance; a protocol written
afterwards would have selected whichever category the existing model happened to fit.

**What changes.** The frame moves to `vcs_write`: 15 tools, 7 uncovered, the
least-covered category in the study at 46.7%. Nothing else in §3A.1–§3A.9 changes — the
hypothesis, resolution tiers, miss classification, the four defect classes, the 20%
falsification threshold and the 50% kill criterion all stand as written.

**What this required.** A GitHub state model (`part_b/github_mock.py`), since the
substituted frame needs one. It carries the version-control analogues of the payment
defects the Stripe model was built for:

| Defect class | Payment form | Version-control form |
|---|---|---|
| Duplicate write | second refund refused | a retried `POST /issues` creates **two** issues — GitHub has no idempotency key, so the duplicate is real |
| Stale read | decision on a pre-write balance | `PUT /contents` carrying a `sha` read before the agent's own write → 409 |
| Unchecked error | 4xx treated as success | merging an already-merged pull request → 405 |

**Effect on the primary outcome.** None yet: no agent has been run against either model.
The proportion in §3A.7 remains unmeasured, and no number is claimed for it.

**2026-08-05 — phase 2 ran. Four departures, recorded below.**

The seven repositories holding the fifteen `vcs_write` tools were drawn mechanically by
`part_b/sample_vcs.py`. Three were run, four excluded. Results are in
`part_b/partb_results.json`; every trace is in `part_b/traces/`.

**(a) The frame is three APIs, not one.** `vcs_write` matches the verbs `create_issue(`,
`create_file(`, `delete_file(` and `create_pull_request(`. Those names are not specific to
version control, so the category is not "GitHub tools". Of its fifteen tools, three call
the GitHub API, five call issue trackers (Jira, Linear, Redmine) and seven write to a
local filesystem or a sandbox. The GitHub model built in phase 1 therefore addressed
three of fifteen. A Jira model (`part_b/jira_mock.py`) was added, because Jira is the
best-represented single API in the measured population — three clients, written
independently in three projects. This was not foreseen when the frame was substituted.

**(b) Redirection was needed for one agent.** §3A.3 says to re-run the write paths
"through the interceptor". The interceptor is passive by construction and cannot move
traffic. agno and llama_index both accept a `server_url`, so they were pointed at the
model by configuration. camel's toolkit constructs `Github(auth=Token(token))` inline,
with no base URL and no injectable client — there is no seam. Its traffic was rewritten
one layer below the tool, inside the HTTP client the tool imports
(`part_b/redirect.py`); the toolkit source is upstream's, unedited. The rewrite lives in
a separate module from the interceptor precisely so that a recorded trace can never
silently be the product of one.

**(c) The duplicate-write scenarios inject a dropped response.** A retry needs a reason.
The scenario drops the response to the first `POST /issue` *after* the model has applied
it — the shape of the failure that causes duplicate writes in production, where the write
lands and the caller never learns it did. The fault belongs to the scenario, is recorded
on every affected request, and never changes a model rule.

**(d) SuperAGI was excluded after four attempts to run it.** §3A.3 step 1 excludes an
agent whose suite already fails. SuperAGI's does not collect: `superagi/config/config.py`
imports `pydantic.BaseSettings`, removed in pydantic 2. Installing pydantic 1.10.13 moved
the failure to fastapi, and `fastapi<0.100` on pydantic 1 fails to build its own models on
Python 3.14. The blocking import is reached only because the GitHub write path imports
`superagi.helper.s3_helper`, which imports fastapi — coupling that has nothing to do with
GitHub. It contributes nothing to any count. Read statically,
`superagi/tools/github/add_file.py:86` treats HTTP 422 as success for both the file write
and the pull request; that is an observation about source, not a measurement, and is
excluded from every number reported here.

**What the run showed.** 3 of 3 agents exhibited at least one class-1–4 defect, above the
20% falsification threshold, so **H1 is supported on this sample**. camel: a class-3 stale
read — it reads a file from the default branch with no `ref`, then writes that sha to a
different branch, and the 409 that follows is uncaught. agno and llama_index: a class-1
duplicate write — a single call to `create_issue` created two issues, because the
transport retried underneath the tool, and both reported one success. All three suites
pass; camel's passes with the entire `github` module replaced by `MagicMock`.

**What the run does not show.** MISS = 0 and T3 = 0 because the models were built
iteratively against these clients: whenever a run reached a missing endpoint, that
endpoint was added. Those two figures measure how well the model fits the sample it was
developed against, not how well the approach generalises. In particular the §3A.8 kill
criterion has no denominator and **has not been tested** — it is reported as not
evaluable, never as passed. agno and llama_index drive the same client library, so their
class-1 observations share one root cause and are not independent. Three agents is a small
denominator and the proportion is descriptive only. The full list is under `limitations`
in `part_b/partb_results.json`.

---

## 4. Reproduction

```bash
pip install -e ".[dev]"
pytest tests/                    # Part B + doc consistency, no network
python -m part_a.run             # Part A: regenerates part_a/results.json

python -m part_b.sample_vcs      # draw the phase-2 sample from results.json
python -m part_b.setup_agents    # build one environment per sampled agent
python -m part_b.experiment      # run them, grade, write partb_results.json
python -m part_b.experiment --grade   # re-grade committed traces, run nothing
```

Phase 2 needs no credentials: every model runs on a localhost socket and no request
leaves the machine. `setup_agents` clones two repositories and builds four virtual
environments, which takes a few minutes; `--check` reports what is already present.
Traces are byte-identical between runs — the clock is injected and the server's ephemeral
port is normalised out — so a re-run that changes a trace means a behaviour changed.

Part A needs no credentials — 45 unauthenticated API calls fit inside the 60/hour limit.
Set `GITHUB_TOKEN` to raise the limit to 5000/hour. Responses cache under `.cache/`, so a
second run is nearly instant and offline for anything already fetched.

CI runs the unit tests on Python 3.9–3.12, smoke-runs the study pipeline on every push, and
runs the full study weekly.
