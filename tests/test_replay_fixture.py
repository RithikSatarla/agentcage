"""The replay fixture must be a fair opponent, and must lose for the right reason.

If the fixture here were a strawman, the comparison it appears in would prove nothing.
So these tests pin down that it behaves like the real libraries: it replays what it
recorded, in order, keyed on the route, and it is honest about routes it never saw.

Then they pin down the failure. Not "the fixture is wrong", it is not. It replays
correct responses. It is wrong about *when*, and the sharpest form of that is that the
read-back after a duplicate write looks exactly like the read-back after a correct one.
"""

from __future__ import annotations

import pytest

from part_b.replay_fixture import Recording, ReplayFixture, UnrecordedRoute
from part_b.stripe_mock import StripeMock

AMOUNT = 5000
FROZEN = 1_750_000_000


@pytest.fixture
def model():
    return StripeMock(clock=lambda: FROZEN)


@pytest.fixture
def recorded(model):
    """A fixture recorded from a correct run: charge, refund, read."""
    charge_id = "ch_00000001"
    return ReplayFixture.record(model, [
        ("POST", "/v1/charges", {"amount": AMOUNT, "currency": "usd"}),
        ("POST", "/v1/refunds", {"charge": charge_id, "amount": AMOUNT}),
        ("GET", f"/v1/charges/{charge_id}", None),
    ])


# --- it is a fair opponent ---------------------------------------------------

def test_it_records_real_responses_not_invented_ones(recorded, model):
    """Everything in the recording actually came out of a working API model."""
    status, payload = recorded.handle("POST", "/v1/charges",
                                      {"amount": AMOUNT, "currency": "usd"})
    assert status == 200
    assert payload["amount"] == AMOUNT
    assert payload["object"] == "charge"


def test_it_replays_multiple_recordings_of_a_route_in_order(model):
    fixture = ReplayFixture([
        Recording("POST", "/v1/charges", None, 200, {"id": "first"}),
        Recording("POST", "/v1/charges", None, 200, {"id": "second"}),
    ])
    assert fixture.handle("POST", "/v1/charges")[1]["id"] == "first"
    assert fixture.handle("POST", "/v1/charges")[1]["id"] == "second"


def test_it_holds_on_the_last_recording_rather_than_running_out(model):
    """Generous behaviour: more calls than recordings replays the last one."""
    fixture = ReplayFixture([Recording("POST", "/v1/refunds", None, 200, {"id": "re_1"})])
    assert fixture.handle("POST", "/v1/refunds")[0] == 200
    assert fixture.handle("POST", "/v1/refunds")[0] == 200
    assert fixture.handle("POST", "/v1/refunds")[0] == 200


def test_it_refuses_a_route_it_never_saw(recorded):
    """It does not invent replies. That would be a strawman, and unlike real tools."""
    with pytest.raises(UnrecordedRoute):
        recorded.handle("POST", "/v1/payouts", {"amount": 1})


def test_it_survives_a_round_trip_through_json(recorded):
    again = ReplayFixture.from_json(recorded.to_json())
    assert again.handle("POST", "/v1/charges")[0] == 200


def test_reset_rewinds_the_cursor_without_changing_the_recording(model):
    fixture = ReplayFixture([
        Recording("POST", "/v1/charges", None, 200, {"id": "first"}),
        Recording("POST", "/v1/charges", None, 200, {"id": "second"}),
    ])
    assert fixture.handle("POST", "/v1/charges")[1]["id"] == "first"
    fixture.reset()
    assert fixture.handle("POST", "/v1/charges")[1]["id"] == "first"


# --- and it loses for the right reason ---------------------------------------

def test_the_fixture_accepts_a_refund_the_model_refuses(recorded, model):
    """The same second call: replayed 200, modelled 400."""
    charge_id = "ch_00000001"
    body = {"charge": charge_id, "amount": AMOUNT}

    recorded.handle("POST", "/v1/charges", {"amount": AMOUNT, "currency": "usd"})
    assert recorded.handle("POST", "/v1/refunds", body)[0] == 200
    assert recorded.handle("POST", "/v1/refunds", body)[0] == 200, (
        "a recording has no way to refuse the second one")

    fresh = StripeMock(clock=lambda: FROZEN)
    fresh.handle("POST", "/v1/charges", {"amount": AMOUNT, "currency": "usd"})
    assert fresh.handle("POST", "/v1/refunds", body)[0] == 200
    assert fresh.handle("POST", "/v1/refunds", body)[0] == 400


def test_the_read_back_hides_the_duplicate(recorded):
    """The sharpest form of the problem, and the reason end-state assertions miss it.

    After two accepted refunds the fixture still reports the amount it recorded after
    one. An assertion on final state cannot tell the two runs apart.
    """
    charge_id = "ch_00000001"
    body = {"charge": charge_id, "amount": AMOUNT}
    recorded.handle("POST", "/v1/charges", {"amount": AMOUNT, "currency": "usd"})
    recorded.handle("POST", "/v1/refunds", body)
    recorded.handle("POST", "/v1/refunds", body)

    _, charge = recorded.handle("GET", f"/v1/charges/{charge_id}")
    assert charge["amount_refunded"] == AMOUNT, (
        "the fixture reports one refund's worth after two were accepted")


def test_the_model_and_the_fixture_agree_on_final_state_and_disagree_on_the_write():
    """Both read back the same number. Only the accepted-write count separates them."""
    from part_b.demo_fixture_vs_model import build_fixture, client_for, run_agent

    with client_for(build_fixture()) as client:
        via_fixture = run_agent(client)
    with client_for(StripeMock(clock=lambda: FROZEN)) as client:
        via_model = run_agent(client)

    assert (via_fixture["amount_refunded_the_agent_reads_back"]
            == via_model["amount_refunded_the_agent_reads_back"])
    assert via_fixture["refunds_the_agent_believes_succeeded"] == 2
    assert via_model["refunds_the_agent_believes_succeeded"] == 1


def test_the_demo_is_labelled_as_a_demonstration():
    """It must never be mistaken for the graded study."""
    import part_b.demo_fixture_vs_model as demo

    text = (demo.__doc__ or "").lower()
    assert "demonstration, not a measurement" in text
    assert "partb_results.json" in text
