# AgentCage: Stateful Mocking for AI Agents with Untested Write Operations

**The Problem:** Of 13 open-source agent repositories with write-capable tools,
**20.3% of the 158 write-capable tools they ship have zero test coverage.**

[![tests](https://github.com/RithikSatarla/agentcage/actions/workflows/test.yml/badge.svg)](https://github.com/RithikSatarla/agentcage/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

- 📊 **Part A Results:** [Quantified the problem](part_a/results.json) — machine-generated, reproducible
- 📄 **Paper:** [Full LaTeX source](paper/agentcage_arxiv.tex) — compiles to 9 pages. *Not yet submitted to arXiv;* this line gets the listing link when it is
- 💻 **Code:** [Interceptor + State Models](part_b/) — plus a [runnable demo](part_b/demo_double_refund.py) and its [captured trace](traces/example_double_refund.json)
- 🚀 **Vercel:** [`website/`](website/) is built and configured — *not yet deployed;* this line gets the live URL when it is
- 📖 **Methodology:** [Full protocol + Part B pre-registration](PROTOCOL.md)

Every statistic in this README, the paper, and the website is generated from
`part_a/results.json`. None is typed by hand, and
[a test fails the build](tests/test_docs_match_results.py) if any of them drift.

---

## The problem

An agent with tool access is a program that issues writes against real systems. It refunds
payments, deletes rows, force-pushes branches, sends mail. We test those agents the way we
test read paths: record a response, replay it.

That cannot catch the bug that matters.

```python
# The agent retries because it didn't parse the first response.
refund(charge_id, 5000)   # fixture: 200 OK
refund(charge_id, 5000)   # fixture: 200 OK   <-- test passes
                          # Stripe:  400 charge_already_refunded
                          # reality: the customer was paid twice
```

A fixture answers `POST /v1/refunds` with a stored `200` however many times it is asked. The
defect lives in the *state transition*, and a fixture has no state.

AgentCage replaces the fixture with a model that does: writes mutate state, and later reads
observe the mutation.

```python
from part_b.stripe_mock import StripeMock, StripeError

stripe = StripeMock()
charge = stripe.post_charge(amount=5000, currency="usd")

stripe.post_refund(charge=charge.id, amount=5000)
stripe.get_charge(charge.id).amount_refunded   # 5000  <- the read sees the write

stripe.post_refund(charge=charge.id, amount=5000)
# StripeError: Charge ch_00000001 has already been refunded.  (400 charge_already_refunded)
```

## How much of this is untested today?

Part A measures it. Every number below is produced by [part_a/run.py](part_a/run.py) and
regenerated into [part_a/results.json](part_a/results.json) — nothing is hand-entered.

> Across 13 open-source agent repositories, **20.3% of the 158 write-capable tool
> definitions they ship are never referenced by any test.**

| | |
|---|---|
| Candidate repositories screened | 45 |
| Screened out — no write-capable tool definition | 32 |
| **Analysed** | **13** |
| **Write-capable tool definitions** | **158** |
| Referenced by at least one test | 126 |
| **Never referenced by any test** | **32 (20.3%)** |

Restricted to the 7 repositories whose *entire* test suite was read, the figure is **20.7%** —
the result does not depend on sampling.

Three things worth knowing before you quote that number:

- **It is a floor, not a ceiling.** A tool counts as tested if its name appears *anywhere*
  in the test corpus — no assertion, no destructive path, not even an execution required.
  Under the weakest bar available, a fifth of write tools fail it.
- **32 of 45 candidates screened out.** The study detects framework-idiomatic tool
  definitions (`@tool`, `BaseTool`, JSON tool schemas). Agents with bespoke architectures —
  `aider`, `OpenHands`, `SWE-agent` — write to disk and shell out constantly but expose no
  such surface. The denominator of 13 says as much about detector coverage as about the
  ecosystem.
- **The detectors were revised seven times**, twice to remove false positives that would
  have made the number look worse. Each revision and its effect on the headline is in
  [PROTOCOL.md §2.7](PROTOCOL.md).

Part A is exploratory. Part B's evaluation is pre-registered and has not been run.

## Quickstart

```bash
git clone https://github.com/RithikSatarla/agentcage.git
cd agentcage
pip install -e ".[dev]"

pytest tests/            # full suite, no network
python -m part_a.run     # reproduce the study (~45 API calls, no credentials needed)
```

`python -m part_a.run` reads 45 repositories at their current `HEAD`, so re-running it later
measures later code. Responses cache under `.cache/`, so a second run is nearly instant.
`GITHUB_TOKEN` is optional and only raises the rate limit.

## Capturing an agent's writes

The interceptor is passive — it attaches through each library's own hook points and never
rewrites a request, injects a response, or short-circuits the transport.

```python
import httpx
from part_b.interceptor import Interceptor

cage = Interceptor()
client = httpx.Client(event_hooks=cage.httpx_event_hooks())

run_my_agent(client)

for trace in cage.writes:                 # POST / PUT / PATCH / DELETE only
    print(trace.method, trace.path, trace.status_code)

cage.dump("traces/run.json")              # credentials already redacted
```

`requests` works the same way:

```python
import requests
session = cage.attach_requests(requests.Session())
```

Point the two halves at each other and you have the cage: the agent's real HTTP client,
talking to a model that keeps state, with every write attempt on the record. That test is
in [tests/test_interceptor.py](tests/test_interceptor.py) — the agent tries the refund
twice, is refused the second time, and the customer is paid once.

## Layout

| Path | What it is |
|---|---|
| [part_a/run.py](part_a/run.py) | The measurement study, executable |
| [part_a/repos.json](part_a/repos.json) | 45-repository sampling frame, with inclusion criteria |
| [part_a/results.json](part_a/results.json) | Full results, per repository and per tool |
| [part_b/interceptor.py](part_b/interceptor.py) | Passive HTTP capture for httpx and requests |
| [part_b/stripe_mock.py](part_b/stripe_mock.py) | Stateful Stripe model — charges, refunds, idempotency |
| [tests/](tests/) | Unit, integration and doc-consistency tests; no network |
| [PROTOCOL.md](PROTOCOL.md) | Methodology, detector definitions, threats to validity |

## Status

Part A is complete and reproducible. Part B is a working foundation, not a finished
framework: the interceptor and the Stripe model do what this README shows, and the
[pre-registered evaluation in PROTOCOL.md §3A](PROTOCOL.md) has **not been run** — it
fixes the hypothesis, resolution tiers, miss classification, defect classes, a 20%
falsification threshold and a 50% kill criterion, all before any data exists.

Not built yet: other API models (GitHub, S3, Postgres), trace-driven replay, a pytest
plugin, and a live deployment of [`website/`](website/).

## License

MIT — see [LICENSE](LICENSE).
