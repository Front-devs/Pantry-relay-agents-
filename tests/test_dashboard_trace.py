"""Tests for what the web dashboard is served.

The dashboard used to describe a morning it had been told about in advance. It
now asks the server for a run and renders the answer, so these tests exist to
pin the property that made that change worth making: the trace is *produced* by
the gate, not written alongside it.

No AWS credentials required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pantryrelay import CoordinatorGate  # noqa: E402
from pantryrelay.data import LEDGER, PANTRIES, reset  # noqa: E402
from pantryrelay.resolution import apply_resolution, choices_for, coerce_choice  # noqa: E402
from pantryrelay.trace import run_morning  # noqa: E402


@pytest.fixture(autouse=True)
def clean_network():
    reset()
    yield
    reset()


def test_trace_reports_the_run_the_gate_actually_made():
    gate = CoordinatorGate()
    trace = run_morning(gate)

    # The escalations in the payload are the gate's own objects, not a
    # description of them assembled separately.
    assert len(trace["escalations"]) == len(gate.escalations)
    assert [e["reason_code"] for e in trace["escalations"]] == [
        e.reason_code for e in gate.escalations
    ]


def test_trace_totals_match_the_ledger():
    trace = run_morning()
    assert trace["stats"]["bookings"] == len(LEDGER)
    assert trace["stats"]["routed"] + trace["stats"]["held"] == trace["stats"]["offers"]


def test_held_offers_contribute_no_bookings():
    """The gate runs before the tool, so a hold leaves nothing to undo."""
    trace = run_morning()
    for offer in trace["offers"]:
        if offer["status"] == "held":
            assert offer["bookings"] == []
            assert offer["messages"] == []


def test_every_hold_offers_a_decision_a_human_can_take():
    trace = run_morning()
    assert trace["escalations"], "the seeded morning should hold something"
    for payload in trace["escalations"]:
        keys = [c["key"] for c in payload["choices"]]
        assert keys, f"{payload['reason_code']} offers no decision"
        assert "decline" in keys, "a coordinator must always be able to say no"
        assert all(c["label"] for c in payload["choices"])


def test_capacity_only_moves_when_a_decision_is_taken():
    gate = CoordinatorGate()
    run_morning(gate)

    held = next(e for e in gate.escalations if e.reason_code == "storage_conflict")
    pantry = next(p for p in PANTRIES if p.id == held.pantry_id)
    before = pantry.capacity_for(held.storage)

    apply_resolution(held, "decline")
    assert pantry.capacity_for(held.storage) == before, "a decline must cost nothing"

    apply_resolution(held, "split")
    assert pantry.capacity_for(held.storage) < before, "a split must actually place food"


def test_a_blanket_decision_degrades_to_one_that_fits():
    """`--resolve split` across a mixed queue has to mean something for each hold."""
    gate = CoordinatorGate()
    run_morning(gate)

    for esc in gate.escalations:
        chosen = coerce_choice(esc, "split")
        assert chosen in choices_for(esc), (
            f"{esc.reason_code} was handed a decision it does not offer"
        )
