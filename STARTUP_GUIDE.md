# AgentCage: fundraising playbook

Internal document.

**The one rule: every number you say out loud must be in `part_a/results.json`.** Anyone
can clone the repo and run `python -m part_a.run`. That reproducibility is the entire
advantage — do not trade it for a rounder number.

| Don't say | Why |
|---|---|
| "23.5% of agent repos have zero test coverage" | Not our finding. No data behind it. |
| "We found 4 untested repos including crewAI-examples" | Not in our results. Our data has 1 repo with no write-tool tests: `aiwaves-cn/agents`. |
| "Agents are causing production incidents" | We measured test coverage. We have no incident data. |
| "Our framework prevents agent failures" | Part B has not run. We have not shown it catches anything in the wild. |
| "Pre-registered study" (about Part A) | Part A is exploratory. Only Part B's evaluation is pre-registered, and the paper says so. |

That last row matters most. The paper itself discloses Part A as exploratory and lists all
seven detector revisions. Call it pre-registered and you contradict your own document in
front of someone who just read it.

---

## Section 1: The problem (quantified)

Of the write-capable agent tools we can mechanically identify across **13** open-source
agent repositories, **20.3% — 32 of 158 — are never referenced by any test.** We screened
**45** candidate repositories to find them.

**The bar was set as low as it goes.** A tool counts as tested if its identifier appears
*anywhere* in the project's test sources — no assertion, no destructive path exercised,
not even executed. One in five fails that, so 20.3% is a floor, not a ceiling.

**Robustness:** restricted to the 7 repositories whose *entire* test suite was read, the
figure is 20.7%. Not a sampling artefact.

**Repository with no test touching any write tool:** `aiwaves-cn/agents` — it ships no
test files at all. That is one repository, and you should present it as one repository.

**APIs these tools reach**, from the detector categories: HTTP writes, filesystem
mutation, shell execution, SQL and ORM writes, cloud SDK writes (S3, EC2, DynamoDB),
version-control writes (GitHub pulls, issues, comments, refs), outbound messaging (email,
Slack), and payment operations. Per-repository detail is in `results.json` under `apis`.

**The caveat you lead with, not bury:** 32 of the 45 candidates screened out. Our
detectors find framework-idiomatic tool definitions — `@tool`, `BaseTool`, JSON tool
schemas. Agents with bespoke architectures (`aider`, `OpenHands`, `SWE-agent`) write to
disk and shell out constantly but expose no such surface. The denominator of 13 reflects
detector coverage as much as it reflects the ecosystem.

Saying it first converts your weakest point into a credibility signal. A sharp investor
finds it in four minutes of reading PROTOCOL.md regardless.

---

## Section 2: Why this matters

An agent holding credentials is a program that changes state it cannot undo. The standard
test — record a response, replay it — holds no state, so it cannot express the failure
mode that only writes have:

```
refund(charge_id, 5000)   # fixture: 200 OK
refund(charge_id, 5000)   # fixture: 200 OK   <- suite passes
                          # Stripe:  400 charge_already_refunded
                          # reality: the customer was paid twice
```

The defect lives in the state transition. No assertion over a fixture's responses reaches
it. The suite is not incomplete — it is structurally incapable.

The same shape recurs wherever an agent writes: a retried refund, a delete applied twice,
a branch force-pushed on a stale read, a message sent again because the first response
was not parsed.

**State what you know and what you don't.** We have measured that these paths are largely
untested. We have *not* measured how often they fail in production — nobody has published
that, and claiming it invites a question you cannot answer.

---

## Section 3: The solution

**Passive interceptor.** Attaches through each HTTP library's own documented hook points
(`httpx` event hooks, `requests` response hooks). Never rewrites a request, injects a
response, or short-circuits the transport — a test asserts the caller receives exactly
what the transport returned. Credential headers are redacted at capture, so traces are
committable.

**Stateful API model.** Not a fixture. Writes mutate state and later reads observe the
mutation: a refund increases `charge.amount_refunded`, and `GET /v1/charges/{id}` reflects
it immediately. Over-refunds are rejected with Stripe's real message and error code, fully
refunded charges reject further refunds, and idempotency keys replay rather than
re-execute.

**Deterministic.** Counter-derived identifiers and an injectable clock, so a run is
byte-for-byte reproducible and a failure is a failure every time.

**The demo that lands** — under a second, in front of them:

```bash
python -m part_b.demo_double_refund
```

A real `httpx` client, an agent that retries a refund, a `400 charge_already_refunded` on
the second attempt, and a customer refunded exactly once. The captured trace is committed
at `traces/example_double_refund.json`.

---

## Section 4: Validation

**What is proven today:**

- Part A is complete, reproducible, and re-runs weekly in CI. Every push also re-runs the
  study pipeline against the live GitHub API.
- The full test suite passes on Python 3.9–3.12, with no network required.
- Every figure in the README, paper, and website is generated from `results.json`; a test
  fails the build if any drifts.
- The paper compiles clean — 9 pages, zero undefined references or citations.

**What is not proven:** Part B has not run. There are no results for it. What exists is a
pre-registered protocol with:

- a hypothesis fixed before data collection,
- resolution tiers (T1 / T2 / T3 / MISS) with explicit precedence,
- miss classification (MODELABLE vs PRODUCTION-STATE-DEPENDENT),
- four closed defect classes that cannot be extended after results are seen,
- a numeric falsification threshold (20%),
- a numeric kill criterion (>50% of misses production-state-dependent → pivot).

**The kill criterion is a selling point, not a risk.** It says: if the misses turn out to
need real production state, more engineering will not save the approach and we stop. An
investor who has watched founders move goalposts will recognise what a fixed threshold
written before the data is worth.

**If Part B fails its threshold, publish that.** A negative result on a pre-registered
hypothesis makes the Part A measurement more trustworthy, and it tells you something true
about the thesis while it is still cheap to learn.

---

## Section 5: Business model

Label this a hypothesis, because it is. No users, no revenue, no pricing conversations.

| Tier | Scope |
|---|---|
| **Open source (MIT)** | Interceptor + common API models. Drives adoption and contribution. |
| **Pro** | State models expensive to build and keep correct — AWS, multi-step payment flows, provider-specific error semantics — plus CI integration and trace diffing. |
| **Enterprise** | Models of internal APIs; compliance evidence that destructive agent paths were exercised before deploy. |

**The question a good investor will actually ask:** why doesn't the agent framework ship
this themselves? Have the answer ready. Fidelity to a third-party API's state machine is
ongoing specialist work that framework maintainers have no incentive to own — the same
reason VCR-style libraries exist independently of HTTP clients, and why nobody expects
`requests` to model Stripe.

---

## Section 6: Timeline

| Stage | State |
|---|---|
| Repo public, CI green, Part A reproducible | **Done** |
| Verify all 10 citations; obtain arXiv endorsement; submit | Next — endorsement is the long pole, start it now |
| Run Part B against the pre-registered protocol | The real milestone: validates or kills the thesis |
| Second API model (GitHub — destructive, widely used, easy to reason about) | After Part B |
| Outreach at volume | Only after Part B |

**Do not skip to the last row.** Going out before Part B means pitching a thesis you have
not tested, which is precisely the posture this whole project is a critique of. That
inconsistency is the kind of thing a technical investor notices and remembers.

---

## Section 7: Investor talking points

Each of these is checkable, which is the point:

- *"20.3% of the write-capable agent tools we can identify — 32 of 158 across 13 repos —
  are never referenced by a test. Clone it and run it; it takes four minutes and no
  credentials."*
- *"Break it down by what the tool actually does and it sharpens: version-control writes
  are 46.7% untested, shell execution 33.3% of 36 tools. The categories with the least
  bounded blast radius are the least covered."*
- *"84.4% of the untested tools sit in three repositories. That's a real nuance — it's a
  property of large tool catalogues, not of every project."*
- *"We screened 45 repos and 32 screened out, so the denominator says as much about our
  detector coverage as about the ecosystem. That's the honest read."*
- *"The detectors went through seven revisions. Two of them removed false positives that
  would have made our number look worse. They're all in the paper."*
- *"Part A is exploratory. Part B is pre-registered with a numeric kill criterion we wrote
  before collecting any data."*
- *"The measurement re-runs in CI every week, so it stays true as the ecosystem moves."*
- *"MIT licensed. If we disappear, it keeps working — which is why a design partner can
  say yes without a procurement conversation."*

**Do not claim** the framework prevents production incidents, that agents are known to be
failing in the wild at some rate, or that anyone is using this yet.

---

## Section 8: What to share

```
GitHub:  https://github.com/RithikSatarla/agentcage
Paper:   [after arXiv submission]

Of the write-capable agent tools we can mechanically identify across 13 open-source
agent repos, 20.3% (32 of 158) are never referenced by any test. We screened 45 repos
to find them; the methodology, the full results, and the detector revision history are
public, and the study re-runs in CI weekly.

We're building the stateful alternative to replay fixtures. The validation study is
pre-registered, with a kill criterion, and runs next.
```

Short. Every claim checkable. No adjectives doing work the data should do.

**Send the repo, not just the number.** The reproducibility is the strongest thing here —
CI already proves it, since the smoke job reproduces the langchain-community figures on a
clean runner on every push.

**For design partners**, lead with the question rather than the pitch: which API their
agent writes to, and which operations actually scare them. That is genuinely the input
needed to choose the second model, and it is a much better opening than a feature list
when Part B has not run yet.
