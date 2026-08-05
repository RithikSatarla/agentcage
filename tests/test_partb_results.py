"""The Part B phase 2 results must stay consistent with the traces and the protocol.

Two things can rot here. The written claims can drift away from the measurement, which
these tests catch by recomputing every headline number from the committed traces. And the
grading can quietly acquire a special case that flatters the result, which they catch by
re-deriving the tiers and defects with the shipped rubric and comparing.

Everything runs offline against committed files. No agent environment is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "part_b" / "partb_results.json"
TRACES = ROOT / "part_b" / "traces"
PROTOCOL = (ROOT / "PROTOCOL.md").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    not RESULTS.exists(), reason="part_b/partb_results.json has not been generated")


@pytest.fixture(scope="module")
def results():
    return json.loads(RESULTS.read_text(encoding="utf-8"))


# --- the results describe the traces that exist ------------------------------

def test_every_agent_has_a_committed_trace(results):
    for agent in results["agents"]:
        key = {"camel-ai/camel": "camel", "agno-agi/agno": "agno",
               "run-llama/llama_index": "llamaindex"}[agent["agent"]]
        assert (TRACES / f"{key}.json").exists(), (
            f"{agent['agent']} is reported but its trace is not committed; the "
            f"protocol requires the raw evidence to be published")


def test_request_counts_match_the_traces(results):
    for agent in results["agents"]:
        key = {"camel-ai/camel": "camel", "agno-agi/agno": "agno",
               "run-llama/llama_index": "llamaindex"}[agent["agent"]]
        trace = json.loads((TRACES / f"{key}.json").read_text(encoding="utf-8"))
        assert agent["requests_graded"] == len(trace["requests"])


def test_grading_reproduces_from_the_traces(results):
    """Re-run the shipped rubric over the committed traces and compare."""
    from part_b.experiment import build

    traces = {}
    for key in ("camel", "agno", "llamaindex"):
        path = TRACES / f"{key}.json"
        if path.exists():
            traces[key] = json.loads(path.read_text(encoding="utf-8"))
    if len(traces) != len(results["agents"]):
        pytest.skip("not every trace is present")

    rebuilt = build(traces)
    assert rebuilt["primary_outcome"] == results["primary_outcome"]
    assert rebuilt["secondary_outcomes"]["tiers"] == \
        results["secondary_outcomes"]["tiers"]
    assert rebuilt["secondary_outcomes"]["defects_by_class"] == \
        results["secondary_outcomes"]["defects_by_class"]


# --- arithmetic --------------------------------------------------------------

def test_primary_outcome_arithmetic(results):
    p = results["primary_outcome"]
    with_defect = [a for a in results["agents"] if a["defects"]]
    assert p["numerator"] == len(with_defect)
    assert p["denominator"] == len(results["agents"])
    assert p["proportion"] == pytest.approx(p["numerator"] / p["denominator"])
    assert p["h1_supported"] == (p["proportion"] >= p["falsification_threshold"])


def test_tiers_sum_to_requests_graded(results):
    s = results["secondary_outcomes"]
    assert sum(s["tiers"].values()) == s["requests_graded"]
    assert s["requests_graded"] == sum(a["requests_graded"] for a in results["agents"])


def test_every_defect_names_a_closed_class(results):
    """§3A.6 fixes four classes and forbids extending them."""
    for agent in results["agents"]:
        for defect in agent["defects"]:
            assert defect["class"] in (1, 2, 3, 4), (
                f"{agent['agent']} reports class {defect['class']}, which is not one "
                f"of the four classes fixed in advance")
            assert defect["evidence"], "a defect with no evidence is not a measurement"
            assert defect["requests"], "a defect must point at the requests behind it"


def test_defect_request_numbers_exist_in_the_trace(results):
    for agent in results["agents"]:
        seqs = {g["seq"] for g in agent["graded_requests"]}
        for defect in agent["defects"]:
            assert set(defect["requests"]) <= seqs, (
                f"{agent['agent']} cites request numbers that are not in its trace")


# --- honesty guards ----------------------------------------------------------

def test_kill_criterion_is_not_claimed_when_it_cannot_be_evaluated(results):
    """No MISSes means no denominator. That is not the same as passing."""
    s = results["secondary_outcomes"]
    if s["tiers"]["MISS"] == 0:
        assert s["kill_criterion_evaluable"] is False
        assert s["kill_criterion_triggered"] is None, (
            "with zero MISSes the kill criterion has not been tested and must not be "
            "reported as false")


def test_limitations_record_the_model_development_order(results):
    """MISS=0 is a property of how the model was built and must be disclosed."""
    joined = " ".join(results["limitations"]).lower()
    assert "iteratively" in joined
    assert "generalis" in joined or "generaliz" in joined
    if results["secondary_outcomes"]["tiers"]["MISS"] == 0:
        assert "kill criterion" in joined


def test_excluded_agents_contribute_nothing_to_the_counts(results):
    excluded = {e["repo"] for e in results["exclusions"]}
    reported = {a["agent"] for a in results["agents"]}
    assert not (excluded & reported), "an agent cannot be both excluded and counted"
    assert results["agents_run"] + results["agents_excluded"] == \
        results["agents_in_frame"]


def test_superagi_static_reading_is_marked_as_not_measured(results):
    """The 422-as-success finding is read from source; it must not look measured."""
    entry = next(e for e in results["exclusions"]
                 if e["repo"] == "TransformerOptimus/SuperAGI")
    note = entry["static_note"].lower()
    assert "not run" in note
    assert "excluded from every count" in note


def test_every_exclusion_names_a_preregistered_category(results):
    """§3A.2 fixes the exclusion categories in advance."""
    allowed = {"no runnable test suite", "filesystem-only writes",
               "credentials we cannot substitute"}
    for entry in results["exclusions"]:
        assert entry["category"] in allowed, (
            f"{entry['repo']} was excluded as '{entry['category']}', which is not one "
            f"of the categories §3A.2 fixed in advance")
        assert entry["reason"]


# --- the protocol says what the results say ----------------------------------

def test_protocol_reports_the_measured_primary_outcome(results):
    p = results["primary_outcome"]
    claim = f"{p['numerator']} of {p['denominator']} agents"
    assert claim in PROTOCOL, (
        f"PROTOCOL.md should state the measured outcome as '{claim}'")


def test_protocol_no_longer_claims_the_outcome_is_unmeasured(results):
    """The phase-1 log said no number was claimed. Phase 2 must supersede it."""
    tail = PROTOCOL.split("phase 2 ran")[-1]
    assert "no number is claimed" not in tail


def test_protocol_discloses_that_the_kill_criterion_was_not_tested(results):
    if results["secondary_outcomes"]["tiers"]["MISS"] == 0:
        assert "has not been tested" in PROTOCOL
