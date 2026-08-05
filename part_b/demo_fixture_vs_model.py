#!/usr/bin/env python3
"""Run one agent against a recorded fixture and against a stateful model.

``demo_double_refund`` shows the stateful side and then *asserts*, in prose, that a
replay fixture would have let the second refund through. This runs both and shows it.

The agent below is the same object in both runs. It is handed an ``httpx.Client`` and
cannot tell what is behind it, which is the only way the comparison means anything.

    python -m part_b.demo_fixture_vs_model

Writes traces/fixture_vs_model.json.

This is a demonstration, not a measurement. It exercises code written here against a
scenario chosen here, so it says nothing about how often real agents do this. The
measured study is `part_b/experiment.py`, which runs third-party agents and grades
them; its results are in part_b/partb_results.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx

from part_b.replay_fixture import ReplayFixture
from part_b.stripe_mock import StripeMock

OUT = Path(__file__).resolve().parent.parent / "traces" / "fixture_vs_model.json"
FROZEN_CLOCK = 1_750_000_000
AMOUNT = 5000


def client_for(backend: Any) -> httpx.Client:
    """An httpx client whose transport is whatever backend was passed."""

    def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        status, payload = backend.handle(request.method, request.url.path, body)
        return httpx.Response(status, json=payload)

    return httpx.Client(
        transport=httpx.MockTransport(transport),
        base_url="https://api.stripe.com",
    )


# --- the agent ---------------------------------------------------------------

def run_agent(client: httpx.Client) -> Dict[str, Any]:
    """Charge, refund, lose the response, retry the refund, read back the charge.

    The retry is the realistic part. An agent that never sees a reply, because the
    connection dropped or it crashed mid-step or a queue redelivered the job, has no
    way to know the refund already landed. Retrying is the correct thing for it to do.
    Whether that is safe is a property of the API, and of whether your test can see it.
    """
    steps: List[Dict[str, Any]] = []

    def call(label: str, method: str, path: str, **kwargs) -> httpx.Response:
        response = client.request(method, path, **kwargs)
        steps.append({"step": label, "method": method, "path": path,
                      "status": response.status_code})
        return response

    charge = call("create charge", "POST", "/v1/charges",
                  json={"amount": AMOUNT, "currency": "usd"}).json()
    charge_id = charge["id"]

    first = call("refund", "POST", "/v1/refunds",
                 json={"charge": charge_id, "amount": AMOUNT})
    second = call("refund again", "POST", "/v1/refunds",
                  json={"charge": charge_id, "amount": AMOUNT})
    final = call("read the charge back", "GET", f"/v1/charges/{charge_id}").json()

    accepted = [r for r in (first, second) if r.status_code == 200]
    return {
        "steps": steps,
        "refunds_the_agent_believes_succeeded": len(accepted),
        "amount_refunded_the_agent_reads_back": final.get("amount_refunded"),
        "charge_amount": final.get("amount"),
    }


# --- the two backends --------------------------------------------------------

def build_fixture() -> ReplayFixture:
    """Record the happy path once, the way a fixture is really made.

    The recording is of a *correct* run: one charge, one refund, one read. Nothing in
    it is wrong. The test that replays it is what goes wrong, when the agent does
    something the recording never covered.
    """
    source = StripeMock(clock=lambda: FROZEN_CLOCK)
    charge_id = "ch_00000001"
    return ReplayFixture.record(source, [
        ("POST", "/v1/charges", {"amount": AMOUNT, "currency": "usd"}),
        ("POST", "/v1/refunds", {"charge": charge_id, "amount": AMOUNT}),
        ("GET", f"/v1/charges/{charge_id}", None),
    ])


def main() -> int:
    print("=" * 74)
    print("One agent. Two backends. Same code, same calls.")
    print("=" * 74)

    fixture = build_fixture()
    with client_for(fixture) as client:
        against_fixture = run_agent(client)

    model = StripeMock(clock=lambda: FROZEN_CLOCK)
    with client_for(model) as client:
        against_model = run_agent(client)

    for title, result in (("Recorded fixture", against_fixture),
                          ("Stateful model", against_model)):
        print(f"\n{title}")
        print("-" * 74)
        for step in result["steps"]:
            print(f"  {step['step']:<24} {step['method']:<5} "
                  f"{step['path']:<28} {step['status']}")
        print(f"  refunds the agent believes succeeded: "
              f"{result['refunds_the_agent_believes_succeeded']}")
        print(f"  amount_refunded it reads back:        "
              f"{result['amount_refunded_the_agent_reads_back']}")

    # What actually happened inside the stateful model, which is the only backend that
    # knows. The fixture has no answer to this question at all.
    truth = model.get_charge("ch_00000001")

    same_readback = (against_fixture["amount_refunded_the_agent_reads_back"]
                     == against_model["amount_refunded_the_agent_reads_back"])

    print("\n" + "=" * 74)
    print("The difference, and the part that is worse than it looks")
    print("=" * 74)
    print(f"  The fixture accepted {against_fixture['refunds_the_agent_believes_succeeded']}"
          f" refunds. The model accepted"
          f" {against_model['refunds_the_agent_believes_succeeded']}.")
    if same_readback:
        print()
        print("  But both runs read the charge back as")
        print(f"  amount_refunded={against_fixture['amount_refunded_the_agent_reads_back']}.")
        print("  The fixture replays the state it recorded before the retry existed, so")
        print("  the duplicate refund leaves no mark on anything the agent can see.")
        print()
        print("  A test asserting the final amount therefore PASSES against both. The")
        print("  only observable difference is the status of the second refund, which")
        print("  is the one thing a recording can never get right. If your assertion")
        print("  looks at end state rather than at what was accepted, the fixture will")
        print("  agree with you all the way to production.")
    print()
    print("  Inside the model, which is the only backend that actually knows, the")
    print(f"  charge holds amount_refunded={truth.amount_refunded} of {truth.amount}.")

    payload = {
        "kind": "demonstration",
        "not_a_measurement": (
            "This exercises code written in this repository against a scenario chosen "
            "in this repository. It shows a mechanism; it does not estimate how often "
            "real agents hit it. The measured study is part_b/partb_results.json."
        ),
        "scenario": "refund, lose the response, retry the refund, read the charge back",
        "finding": (
            "Both backends return the same amount_refunded on the read-back, because "
            "the fixture replays state recorded before the retry existed. An assertion "
            "on final state passes against both; only an assertion on how many refunds "
            "were accepted separates them."
        ),
        "against_recorded_fixture": against_fixture,
        "against_stateful_model": against_model,
        "model_final_state": {
            "charge": truth.id,
            "amount": truth.amount,
            "amount_refunded": truth.amount_refunded,
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwritten to {OUT.relative_to(OUT.parent.parent)}")

    # The demonstration is only worth committing if it still demonstrates the thing.
    assert against_fixture["refunds_the_agent_believes_succeeded"] == 2
    assert against_model["refunds_the_agent_believes_succeeded"] == 1
    assert truth.amount_refunded == AMOUNT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
