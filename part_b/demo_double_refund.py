#!/usr/bin/env python3
"""Run the double-refund scenario end to end and write a real trace.

This is the smallest complete demonstration of the framework: a real ``httpx``
client, the passive interceptor, and the stateful Stripe model behind a transport.
An agent issues a refund, fails to notice it succeeded, and retries. Against a replay
fixture both calls return 200 and the customer is refunded twice. Here the second is
refused, and the interceptor holds the evidence of both attempts.

    python -m part_b.demo_double_refund

Writes traces/example_double_refund.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from part_b.interceptor import Interceptor
from part_b.stripe_mock import StripeMock

OUT = Path(__file__).resolve().parent.parent / "traces" / "example_double_refund.json"
# Fixed clock so the committed trace is byte-stable across runs.
FROZEN_CLOCK = 1_750_000_000


def build_client(cage: Interceptor, stripe: StripeMock) -> httpx.Client:
    """An httpx client whose transport is the stateful model."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        status, payload = stripe.handle(
            request.method,
            request.url.path,
            body,
            idempotency_key=request.headers.get("idempotency-key"),
        )
        return httpx.Response(status, json=payload)

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        event_hooks=cage.httpx_event_hooks(),
        base_url="https://api.stripe.com",
        headers={"Authorization": "Bearer sk_live_not_a_real_key"},
    )


def main() -> int:
    cage = Interceptor()
    stripe = StripeMock(clock=lambda: FROZEN_CLOCK)
    client = build_client(cage, stripe)

    charge = client.post("/v1/charges", json={"amount": 5000, "currency": "usd"}).json()
    print(f"created {charge['id']}: {charge['amount']} {charge['currency']}")

    first = client.post("/v1/refunds", json={"charge": charge["id"], "amount": 5000})
    print(f"refund attempt 1 -> {first.status_code}")

    # The agent did not parse the first response and retries the same operation.
    second = client.post("/v1/refunds", json={"charge": charge["id"], "amount": 5000})
    print(f"refund attempt 2 -> {second.status_code} {second.json().get('error', {}).get('code', '')}")

    final = client.get(f"/v1/charges/{charge['id']}").json()
    print(f"final amount_refunded: {final['amount_refunded']} (charge amount {final['amount']})")

    assert first.status_code == 200, "first refund should succeed"
    assert second.status_code == 400, "second refund should be refused"
    assert final["amount_refunded"] == 5000, "customer must be refunded exactly once"

    cage.dump(str(OUT))
    print(f"\n{len(cage.traces)} requests captured ({len(cage.writes)} writes) -> {OUT}")
    print("Against a replay fixture, attempt 2 would also have returned 200.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
