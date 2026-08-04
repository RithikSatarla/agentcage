"""State-mutation tests for the Stripe model.

The point of these tests is that state is real: a refund changes the charge, and a
later read observes the change. A replay-based fixture would pass a "did we call the
endpoint" test but fail every assertion in this file.
"""

import pytest

from part_b.stripe_mock import Charge, Customer, Refund, StripeError, StripeMock


@pytest.fixture
def stripe():
    # frozen clock -> deterministic `created` timestamps
    return StripeMock(clock=lambda: 1_750_000_000)


def test_post_charge_creates_a_real_entity(stripe):
    charge = stripe.post_charge(amount=5000, currency="usd", description="test order")

    assert isinstance(charge, Charge)
    assert charge.id.startswith("ch_")
    assert charge.amount == 5000
    assert charge.amount_captured == 5000
    assert charge.amount_refunded == 0
    assert charge.refunded is False
    assert charge.status == "succeeded"
    assert charge.created == 1_750_000_000


def test_refund_mutates_the_charge_and_the_mutation_is_readable(stripe):
    charge = stripe.post_charge(amount=5000)
    assert stripe.get_charge(charge.id).amount_refunded == 0

    refund = stripe.post_refund(charge=charge.id, amount=2000)

    assert isinstance(refund, Refund)
    assert refund.amount == 2000
    assert refund.charge == charge.id

    # the read reflects the write - this is the property under test
    reread = stripe.get_charge(charge.id)
    assert reread.amount_refunded == 2000
    assert reread.amount_refundable == 3000
    assert reread.refunded is False


def test_partial_refunds_accumulate_until_fully_refunded(stripe):
    charge = stripe.post_charge(amount=5000)

    stripe.post_refund(charge=charge.id, amount=2000)
    stripe.post_refund(charge=charge.id, amount=3000)

    reread = stripe.get_charge(charge.id)
    assert reread.amount_refunded == 5000
    assert reread.amount_refundable == 0
    assert reread.refunded is True


def test_refund_defaults_to_the_remaining_amount(stripe):
    charge = stripe.post_charge(amount=5000)
    stripe.post_refund(charge=charge.id, amount=1500)

    refund = stripe.post_refund(charge=charge.id)

    assert refund.amount == 3500
    assert stripe.get_charge(charge.id).refunded is True


def test_over_refund_is_rejected_with_a_400_and_leaves_state_untouched(stripe):
    charge = stripe.post_charge(amount=5000)

    with pytest.raises(StripeError) as excinfo:
        stripe.post_refund(charge=charge.id, amount=9999)

    err = excinfo.value
    assert err.http_status == 400
    assert err.code == "amount_too_large"
    assert "$99.99" in err.message and "$50.00" in err.message

    # a rejected write must not partially apply
    assert stripe.get_charge(charge.id).amount_refunded == 0
    assert stripe.refunds == {}


def test_double_refund_of_a_fully_refunded_charge_is_rejected(stripe):
    charge = stripe.post_charge(amount=5000)
    stripe.post_refund(charge=charge.id)

    with pytest.raises(StripeError) as excinfo:
        stripe.post_refund(charge=charge.id, amount=100)

    assert excinfo.value.code == "charge_already_refunded"
    assert stripe.get_charge(charge.id).amount_refunded == 5000


def test_unknown_charge_is_a_404_resource_missing(stripe):
    with pytest.raises(StripeError) as excinfo:
        stripe.get_charge("ch_does_not_exist")

    assert excinfo.value.http_status == 404
    assert excinfo.value.code == "resource_missing"


def test_charge_against_unknown_customer_is_rejected(stripe):
    with pytest.raises(StripeError) as excinfo:
        stripe.post_charge(amount=100, customer="cus_nope")

    assert excinfo.value.http_status == 404
    assert stripe.charges == {}


def test_non_positive_amount_is_rejected(stripe):
    with pytest.raises(StripeError) as excinfo:
        stripe.post_charge(amount=0)
    assert excinfo.value.code == "parameter_invalid_integer"


def test_invalid_refund_reason_is_rejected(stripe):
    charge = stripe.post_charge(amount=1000)
    with pytest.raises(StripeError) as excinfo:
        stripe.post_refund(charge=charge.id, amount=100, reason="because")
    assert excinfo.value.param == "reason"
    assert stripe.get_charge(charge.id).amount_refunded == 0


def test_uncaptured_charge_cannot_be_refunded(stripe):
    charge = stripe.post_charge(amount=1000, capture=False)
    assert charge.amount_captured == 0

    with pytest.raises(StripeError) as excinfo:
        stripe.post_refund(charge=charge.id)
    assert excinfo.value.code == "charge_not_captured"


def test_customer_lifecycle(stripe):
    customer = stripe.post_customer(email="a@example.com", name="Ada")
    assert isinstance(customer, Customer)
    assert stripe.get_customer(customer.id).email == "a@example.com"

    charge = stripe.post_charge(amount=250, customer=customer.id)
    assert charge.customer == customer.id


def test_idempotency_key_replays_instead_of_charging_twice(stripe):
    first = stripe.post_charge(amount=5000, idempotency_key="key-1")
    second = stripe.post_charge(amount=5000, idempotency_key="key-1")

    assert first.id == second.id
    assert len(stripe.charges) == 1


def test_idempotency_key_reuse_with_different_params_is_rejected(stripe):
    stripe.post_charge(amount=5000, idempotency_key="key-2")

    with pytest.raises(StripeError) as excinfo:
        stripe.post_charge(amount=9999, idempotency_key="key-2")

    assert excinfo.value.code == "idempotency_key_in_use"
    assert len(stripe.charges) == 1


def test_idempotent_refund_does_not_double_refund(stripe):
    charge = stripe.post_charge(amount=5000)

    stripe.post_refund(charge=charge.id, amount=2000, idempotency_key="ref-1")
    stripe.post_refund(charge=charge.id, amount=2000, idempotency_key="ref-1")

    assert stripe.get_charge(charge.id).amount_refunded == 2000


def test_list_refunds_filters_by_charge(stripe):
    a = stripe.post_charge(amount=1000)
    b = stripe.post_charge(amount=2000)
    stripe.post_refund(charge=a.id, amount=100)
    stripe.post_refund(charge=b.id, amount=200)

    listing = stripe.list_refunds(charge=a.id)

    assert listing["object"] == "list"
    assert len(listing["data"]) == 1
    assert listing["data"][0]["charge"] == a.id


# --- HTTP surface ------------------------------------------------------------


def test_handle_routes_a_full_charge_then_refund_then_read(stripe):
    status, charge = stripe.handle("POST", "/v1/charges", {"amount": 5000, "currency": "usd"})
    assert status == 200

    status, refund = stripe.handle(
        "POST", "/v1/refunds", {"charge": charge["id"], "amount": 1500}
    )
    assert status == 200
    assert refund["object"] == "refund"

    status, reread = stripe.handle("GET", f"/v1/charges/{charge['id']}")
    assert status == 200
    assert reread["amount_refunded"] == 1500


def test_handle_returns_stripe_error_envelope_not_an_exception(stripe):
    _, charge = stripe.handle("POST", "/v1/charges", {"amount": 1000})

    status, body = stripe.handle(
        "POST", "/v1/refunds", {"charge": charge["id"], "amount": 999_999}
    )

    assert status == 400
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "amount_too_large"


def test_handle_supports_the_nested_refund_route(stripe):
    _, charge = stripe.handle("POST", "/v1/charges", {"amount": 4000})

    status, _ = stripe.handle("POST", f"/v1/charges/{charge['id']}/refunds", {"amount": 1000})

    assert status == 200
    assert stripe.get_charge(charge["id"]).amount_refunded == 1000


def test_handle_unknown_route_is_404(stripe):
    status, body = stripe.handle("GET", "/v1/widgets/wid_1")
    assert status == 404
    assert body["error"]["code"] == "url_invalid"
