# AgentCage: positioning and fundraising notes

Internal document. The point of this file is to keep the pitch tied to what the work
actually shows, because the fastest way to lose a technical investor is to state a
number you cannot reproduce in front of them.

---

## The one rule

**Every number you say out loud must be in `part_a/results.json`.**

Anyone can clone the repo and run `python -m part_a.run`. That is the whole advantage —
do not trade it away for a rounder number.

### Say this

> Of the write-capable agent tools we could mechanically identify across 13 open-source
> agent repositories, **20.3% — 32 of 158 — are never referenced by any test.** We
> screened 45 repositories to find them. The methodology is public and the study re-runs
> in CI every week.

### Do not say this

| Don't say | Why |
|---|---|
| "23.5% of agent repos have zero test coverage" | Not our finding. No data behind it. |
| "We found 4 untested repos including crewAI-examples" | Not in our results. Our data has 1 repo with no write-tool tests: `aiwaves-cn/agents`. |
| "Agents are causing production incidents" | We measured test coverage. We have no incident data. |
| "Our framework prevents agent failures" | Part B has not been run. We have not demonstrated it catches anything in the wild yet. |
| "Pre-registered study" (about Part A) | Part A is exploratory. Only Part B's evaluation is pre-registered. |

That last row matters more than it looks. The paper itself discloses that Part A is
exploratory and lists all seven detector revisions. If you call it pre-registered and an
investor reads the paper, you have contradicted your own document.

---

## Section 1: the problem, precisely stated

Agents hold credentials for systems that change state — payments, infrastructure,
repositories, email. The standard way to test them is to record an HTTP response and
replay it. A replay fixture holds no state, so it cannot express the failure mode
specific to writes: an operation applied twice, or applied when it should have been
refused.

```
refund(charge_id, 5000)   # fixture: 200 OK
refund(charge_id, 5000)   # fixture: 200 OK   <- suite passes
                          # Stripe:  400 charge_already_refunded
                          # reality: customer paid twice
```

The test suite is not merely incomplete. It is structurally incapable of catching this.

**What we measured:** 45 candidate repositories screened; 13 had mechanically
identifiable write-capable tool definitions; those 13 ship 158 such tools; 32 of them
(20.3%) are never referenced by any test.

**The bar was deliberately low.** A tool counts as tested if its name appears *anywhere*
in the test sources — no assertion, no destructive path exercised, not even executed.
One in five fails that. So 20.3% is a floor, not a ceiling.

**Robustness:** restricted to the 7 repositories whose entire test suite was read,
the figure is 20.7%. It is not a sampling artefact.

---

## Section 2: the honest caveat, and why you lead with it

**32 of 45 candidates screened out.** Our detectors find framework-idiomatic tool
definitions — `@tool`, `BaseTool`, JSON tool schemas. Agents with bespoke architectures
(`aider`, `OpenHands`, `SWE-agent`) write to disk and shell out constantly but expose no
such surface, so they are not in the denominator.

A sharp investor will find this in about four minutes of reading PROTOCOL.md. You have
two options: they discover it, or you tell them. Telling them converts a weakness into a
credibility signal — and it is genuinely the honest framing, because n=13 says as much
about detector coverage as about the ecosystem.

Framing that works:

> The denominator is 13 because we only count tools we can identify mechanically. That's
> the conservative read — the agents we *can't* parse are shelling out and writing files
> constantly, and nothing suggests they're better tested. Widening detector coverage is
> the next piece of work.

---

## Section 3: what exists today

| Component | State |
|---|---|
| Part A measurement | Complete, reproducible, re-runs weekly in CI |
| Interceptor (`httpx` + `requests`) | Working. Passive by construction; a test asserts responses are unmodified |
| Stateful Stripe model | Working. Refunds mutate charges, reads observe mutations, over-refund and double-refund rejected with real error codes, idempotency keys honoured |
| Test suite | 31 tests, no network, green on Python 3.9–3.12 |
| Paper | Compiles clean, 7 pages, not yet submitted |
| Part B evaluation | **Pre-registered, not run.** No results exist |
| Other API models (GitHub, S3, Postgres) | Not built |
| Website | Static page only |

The demo that lands: the end-to-end test in `tests/test_interceptor.py`. A real `httpx`
client, a stateful backend, an agent that retries a refund — second attempt returns
`400 charge_already_refunded`, and the customer is refunded once. It runs in under a
second in front of them.

---

## Section 4: why the research posture is the moat

Anyone can write an HTTP mock. Not everyone will:

- publish the sampling frame and inclusion criteria before the result,
- publish a detector revision history including revisions that made their own headline
  weaker,
- pre-register the validation hypothesis with an explicit falsification threshold,
- wire the whole study into CI so it re-runs weekly against a moving ecosystem.

That combination is what makes the number quotable by other people. A statistic that
others cite is worth more than a product demo, because it travels without you.

**Corollary:** if Part B fails its falsification threshold — fewer than 20% of sampled
agents showing a defect — publish that. A negative result on a pre-registered hypothesis
makes the Part A measurement *more* trustworthy. It also tells you something real about
the product thesis, early, while it is cheap to learn.

---

## Section 5: business model (unvalidated)

Label this as hypothesis, because it is. There are no users and no revenue.

- **Open source core** — interceptor plus common API models. Drives adoption and
  contributions.
- **Paid** — state models that are expensive to build and maintain correctly (AWS,
  multi-step payment flows, provider-specific error semantics), CI integration,
  trace storage and diffing.
- **Enterprise** — custom models for internal APIs, compliance evidence that destructive
  agent paths were exercised before deploy.

The real question a good investor will ask: *why doesn't the agent framework ship this
themselves?* Have an answer. The honest one is that fidelity to a third-party API's state
machine is ongoing specialist work that framework maintainers have no incentive to own —
same reason VCR-style libraries exist independently of HTTP clients.

---

## Section 6: sequence

1. **Done** — repo public, CI green, Part A reproducible.
2. **Next** — verify the 10 citations, get arXiv endorsement (this is the long pole,
   start it now), submit.
3. **Then** — run Part B against the pre-registered protocol. This is the real milestone:
   it either validates the thesis or kills it, and either outcome is worth knowing before
   raising.
4. **Then** — second API model (GitHub is the natural one: destructive, widely used, easy
   to reason about).
5. **Only then** — outreach at volume.

Going out before step 3 means pitching a thesis you have not tested. That is exactly the
posture the whole project is a critique of.

---

## Section 7: what to send

```
GitHub:  https://github.com/RithikSatarla/agentcage
Paper:   [after submission]

Of the write-capable agent tools we can mechanically identify across 13 open-source
agent repos, 20.3% (32 of 158) are never referenced by any test. We screened 45 repos
to find them; the methodology, the full results, and the detector revision history are
public, and the study re-runs in CI weekly.

We're building the stateful alternative to replay fixtures. The validation study is
pre-registered and running now.
```

Short. Every claim checkable. No adjectives doing work the data should do.
