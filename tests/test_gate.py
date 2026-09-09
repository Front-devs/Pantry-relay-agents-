"""Tests for the coordinator gate.

The gate is the project's central claim — that a human is pulled in when, and
only when, there is a real decision — so it is tested directly rather than
through the agent. No AWS credentials required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pantryrelay import CoordinatorGate, route_offer  # noqa: E402
from pantryrelay.data import LEDGER, OUTBOX, reset  # noqa: E402
from pantryrelay.fixtures import DRY_GOODS, FROZEN, MORNING  # noqa: E402
from pantryrelay.models import DonationOffer, FoodItem  # noqa: E402
from pantryrelay.routing import _call  # noqa: E402
from pantryrelay.tools import reserve_pickup  # noqa: E402

# A comfortable, well-formed booking: the shape every attack below deforms.
GOOD_BOOKING = {
    "pantry_id": "eastside",
    "donor_name": DRY_GOODS.donor_name,
    "quantity_lbs": 620.0,
    "storage": "ambient",
    "hours_until_unusable": 720.0,
    "rationale": "Eastside lists dry_goods as a current need",
}

# The same dry goods, but the donor's own signal says three hours, not a month.
URGENT = DRY_GOODS.model_copy(
    update={"items": [DRY_GOODS.items[0].model_copy(update={"hours_until_unusable": 3.0})]}
)


@pytest.fixture(autouse=True)
def clean_network():
    reset()
    yield
    reset()


@pytest.fixture
def gate():
    return CoordinatorGate()


# -- routine traffic proceeds unattended ---------------------------------


def test_read_only_tools_are_never_gated(gate):
    assert gate.assess(tool_name="find_candidate_pantries", args={}, offer=DRY_GOODS) is None
    assert gate.assess(tool_name="check_storage_capacity", args={}, offer=DRY_GOODS) is None


def test_comfortable_booking_proceeds(gate):
    args = {
        "pantry_id": "eastside",
        "donor_name": "Meridian Logistics",
        "quantity_lbs": 620.0,
        "storage": "ambient",
        "hours_until_unusable": 720.0,
    }
    assert gate.assess(tool_name="reserve_pickup", args=args, offer=DRY_GOODS) is None


# -- the four escalation reasons -----------------------------------------


def test_storage_conflict_escalates(gate):
    args = {
        "pantry_id": "riverside",
        "donor_name": "Cold Storage",
        "quantity_lbs": 900.0,
        "storage": "frozen",
        "hours_until_unusable": 8760.0,
    }
    esc = gate.assess(tool_name="reserve_pickup", args=args, offer=FROZEN)
    assert esc is not None
    assert esc.reason_code == "storage_conflict"
    assert "100" in esc.detail  # names the actual shortfall


def test_thin_expiry_margin_escalates(gate):
    args = {
        "pantry_id": "riverside",  # needs 4h notice
        "donor_name": "Someone",
        "quantity_lbs": 10.0,
        "storage": "refrigerated",
        "hours_until_unusable": 5.0,  # 1h of usable life after handover
    }
    esc = gate.assess(tool_name="reserve_pickup", args=args, offer=DRY_GOODS)
    assert esc is not None
    assert esc.reason_code == "thin_expiry_margin"


def test_same_day_commitment_escalates(gate):
    offer = DRY_GOODS.model_copy(update={"needs_same_day_answer": True})
    args = {"pantry_id": "eastside", "message": "incoming"}
    esc = gate.assess(tool_name="notify_pantry_coordinator", args=args, offer=offer)
    assert esc is not None
    assert esc.reason_code == "same_day_commitment"


def test_low_confidence_extraction_escalates(gate):
    offer = DRY_GOODS.model_copy(
        update={"extraction_confidence": 0.4, "ambiguities": ["weight illegible"]}
    )
    args = {"pantry_id": "eastside", "message": "incoming"}
    esc = gate.assess(tool_name="reserve_pickup", args=args, offer=offer)
    assert esc is not None
    assert esc.reason_code == "low_confidence_extraction"
    assert "weight illegible" in esc.detail


# -- the gate must not be bypassable -------------------------------------


def test_escalated_booking_never_reaches_the_ledger(gate):
    """A held offer must leave no trace: no capacity taken, no message sent."""
    outcome = route_offer(FROZEN, gate)
    assert outcome.escalated
    assert outcome.booked == []
    assert all(entry["donor"] != FROZEN.donor_name for entry in LEDGER)
    assert OUTBOX == []


def test_no_expiry_signal_is_not_treated_as_urgent(gate):
    """A null window must not be silently read as 'expires immediately'."""
    offer = DonationOffer(
        donor_name="Unknown",
        pickup_location="somewhere",
        items=[
            FoodItem(
                description="tinned goods",
                category="dry_goods",
                quantity_lbs=50.0,
                storage="ambient",
                hours_until_unusable=None,
            )
        ],
    )
    outcome = route_offer(offer, gate)
    assert not outcome.escalated
    assert len(outcome.booked) == 1


# -- the whole morning ----------------------------------------------------


def test_tuesday_morning_holds_exactly_one_offer(gate):
    outcomes = [route_offer(offer, gate) for offer in MORNING]
    held = [o for o in outcomes if o.escalated]
    routed = [o for o in outcomes if o.booked and not o.escalated]

    assert len(held) == 1, "exactly one offer should need a human"
    assert len(routed) == 5, "the other five should route unattended"
    assert held[0].offer.donor_name == FROZEN.donor_name
    assert len(OUTBOX) == 6  # one message per booked item


def test_capacity_is_conserved(gate):
    """Every booked pound must come out of some pantry's free space."""
    from pantryrelay.data import PANTRIES

    before = {p.id: dict(p.free_lbs) for p in PANTRIES}
    for offer in MORNING:
        route_offer(offer, gate)

    booked_by_pantry: dict[str, float] = {}
    for entry in LEDGER:
        booked_by_pantry[entry["pantry_id"]] = (
            booked_by_pantry.get(entry["pantry_id"], 0.0) + entry["lbs"]
        )

    for pantry in PANTRIES:
        consumed = sum(before[pantry.id].values()) - sum(pantry.free_lbs.values())
        assert consumed == pytest.approx(booked_by_pantry.get(pantry.id, 0.0))


# -- argument laundering: the gate judges the food, not the sentence ------


def test_a_negative_weight_is_refused_instead_of_handing_back_capacity(gate):
    """The attack that made the morning's one held offer bookable.

    reserve_pickup subtracts the weight from the pantry's free space, so a
    negative weight *adds* capacity. Through the gate it read as "-500 is not
    more than the 800 free", proceeded, and inflated the freezer enough that the
    900 lb load the gate exists to hold sailed through on the very next call.
    Committing a negative amount is not a judgment call for a coordinator, it is
    a malformed call, so the gate refuses it outright.
    """
    args = {
        "pantry_id": "riverside",
        "donor_name": "Cold Chain Foods",
        "quantity_lbs": -500.0,
        "storage": "frozen",
        "hours_until_unusable": 8760.0,
        "rationale": "correcting an earlier over-count",
    }
    verdict = gate.decide(tool_name="reserve_pickup", args=args, offer=FROZEN)
    assert verdict.action == "deny"
    assert "positive" in verdict.reason


def test_a_zero_weight_placeholder_is_refused(gate):
    """Zero pounds books nothing, but would still have counted as a booking."""
    verdict = gate.decide(
        tool_name="reserve_pickup",
        args={**GOOD_BOOKING, "quantity_lbs": 0.0},
        offer=DRY_GOODS,
    )
    assert verdict.action == "deny"


def test_an_unreadable_weight_stops_the_call_not_the_run(gate):
    """A weight the gate cannot parse must fail closed without raising."""
    verdict = gate.decide(
        tool_name="reserve_pickup",
        args={**GOOD_BOOKING, "quantity_lbs": "six hundred"},
        offer=DRY_GOODS,
    )
    assert verdict.action == "deny"
    assert "not a number" in verdict.reason


def test_the_expiry_window_comes_from_the_offer_not_from_the_call(gate):
    """A generous hours_until_unusable must not buy a generous deadline.

    That field is supplied by the model, so on its own it is a claim. The offer
    says this food dies in three hours; claiming a year does not make it so, and
    the thin-margin floor is judged on the shorter of the two.
    """
    laundered = {**GOOD_BOOKING, "hours_until_unusable": 8760.0}
    escalation = gate.assess(tool_name="reserve_pickup", args=laundered, offer=URGENT)
    assert escalation is not None
    assert escalation.reason_code == "thin_expiry_margin"
    assert "3.0h" in escalation.detail, "the offer's real window should be the one named"


def test_an_honest_long_window_still_proceeds(gate):
    """The fix must not turn every ordinary booking into an escalation."""
    verdict = gate.decide(tool_name="reserve_pickup", args=GOOD_BOOKING, offer=DRY_GOODS)
    assert verdict.action == "allow"


def test_frozen_food_booked_onto_a_warm_shelf_is_a_storage_conflict(gate):
    """The capacity check alone cannot catch this: warm shelves have room.

    Relabelling frozen protein as ambient finds 1400 lbs of free shelf at
    Eastside and passes every capacity test there is. What the food needs is a
    property of the food, so the gate reads the storage class off the offer.
    """
    lie = {
        "pantry_id": "eastside",
        "donor_name": FROZEN.donor_name,
        "quantity_lbs": 900.0,
        "storage": "ambient",
        "hours_until_unusable": 8760.0,
        "rationale": "Eastside has the room",
    }
    escalation = gate.assess(tool_name="reserve_pickup", args=lie, offer=FROZEN)
    assert escalation is not None
    assert escalation.reason_code == "storage_conflict"
    assert "frozen" in escalation.detail


def test_booking_somewhere_colder_than_needed_is_not_a_downgrade(gate):
    """Erring towards colder storage is caution, not a conflict."""
    careful = {
        "pantry_id": "stjohns",
        "donor_name": DRY_GOODS.donor_name,
        "quantity_lbs": 100.0,
        "storage": "refrigerated",
        "hours_until_unusable": 720.0,
        "rationale": "keeping it cold",
    }
    assert gate.assess(tool_name="reserve_pickup", args=careful, offer=DRY_GOODS) is None


# -- coverage is an allow-list, so forgetting fails closed ----------------


def test_a_tool_the_gate_has_no_policy_for_is_refused(gate):
    """Forgetting to classify a world-changing tool must fail closed.

    Membership used to be a deny-list: a tool that changed the world and was
    left out of CONSEQUENTIAL_TOOLS ran unattended, because no check fired and
    "no check fired" read as "safe".
    """
    verdict = gate.decide(
        tool_name="cancel_booking",
        args={"pantry_id": "riverside", "donor_name": "someone"},
        offer=DRY_GOODS,
    )
    assert verdict.action == "deny"
    assert "no policy" in verdict.reason


def test_declared_read_only_tools_still_run_unattended(gate):
    for name in ("find_candidate_pantries", "check_storage_capacity"):
        assert gate.decide(tool_name=name, args={}, offer=DRY_GOODS).action == "allow"


def test_a_commitment_with_no_offer_cannot_be_judged(gate):
    """Two of the four reasons need the offer; without it the gate must not guess."""
    verdict = gate.decide(tool_name="reserve_pickup", args=GOOD_BOOKING, offer=None)
    assert verdict.action == "deny"


# -- announcements must be backed by a real booking ----------------------


def test_a_message_needs_a_booking_made_for_this_offer(gate):
    """A ledger row from an earlier offer is not permission to announce a new one.

    Same donor, same pantry, different offer. The old booking is real, but it
    says nothing about the load this message describes.
    """
    assert gate.decide(
        tool_name="reserve_pickup", args=GOOD_BOOKING, offer=DRY_GOODS
    ).action == "allow"
    _call(reserve_pickup, **GOOD_BOOKING)
    assert LEDGER, "the setup booking should be real"

    later_offer = DRY_GOODS.model_copy(update={"extraction_confidence": 0.99})
    verdict = gate.decide(
        tool_name="notify_pantry_coordinator",
        args={"pantry_id": "eastside", "message": "620 lbs inbound"},
        offer=later_offer,
    )
    assert verdict.action == "deny"
    assert OUTBOX == []


def test_a_message_rides_on_the_booking_it_belongs_to(gate):
    """The interlock must not block the ordinary case it exists to sequence."""
    assert gate.decide(
        tool_name="reserve_pickup", args=GOOD_BOOKING, offer=DRY_GOODS
    ).action == "allow"
    booking = _call(reserve_pickup, **GOOD_BOOKING)
    # The step both real paths take: after_tool_call in the live agent,
    # route_offer in the offline one. The gate is told what actually happened.
    gate.note_booking_made(args=GOOD_BOOKING, offer=DRY_GOODS, result=booking)
    assert gate.decide(
        tool_name="notify_pantry_coordinator",
        args={"pantry_id": "eastside", "message": "620 lbs inbound"},
        offer=DRY_GOODS,
    ).action == "allow"


# -- coordinator resolution of held escalations -----------------------------


def test_coordinator_resolution_overflow_updates_ledger_and_clears_queue(gate):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from run_demo import resolve_escalations
    from pantryrelay.models import Escalation
    from pantryrelay.data import get_pantry

    esc = Escalation(
        reason_code="storage_conflict",
        summary="Riverside Meals Program cannot hold this load",
        detail="Needs 900 lbs of frozen space but only 800 lbs is free",
        proposed_action="reserve_pickup(pantry_id='riverside', quantity_lbs=900.0, storage='frozen')",
    )
    gate.escalations.append(esc)

    booked_before = len(LEDGER)
    resolve_escalations(gate, "overflow")

    assert len(gate.escalations) == 0
    assert len(LEDGER) == booked_before + 1
    assert LEDGER[-1]["pantry_id"] == "riverside"
    assert LEDGER[-1]["lbs"] == 900.0
    assert "overflow" in LEDGER[-1]["rationale"]
    assert get_pantry("riverside").free_lbs["frozen"] == 0.0


def test_coordinator_resolution_split_allocates_across_pantries(gate):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from run_demo import resolve_escalations
    from pantryrelay.models import Escalation
    from pantryrelay.data import get_pantry, reset

    reset()
    esc = Escalation(
        reason_code="storage_conflict",
        summary="Riverside Meals Program cannot hold this load",
        detail="Needs 900 lbs of frozen space but only 800 lbs is free",
        proposed_action="reserve_pickup(pantry_id='riverside', quantity_lbs=900.0, storage='frozen')",
    )
    gate.escalations.append(esc)

    resolve_escalations(gate, "split")

    assert len(gate.escalations) == 0
    assert get_pantry("riverside").free_lbs["frozen"] == 0.0
    assert get_pantry("stjohns").free_lbs["frozen"] == 20.0
    split_bookings = [b for b in LEDGER if b.get("donor") == "Cold Storage (name inaudible)"]
    assert len(split_bookings) == 2
    assert {b["pantry_id"] for b in split_bookings} == {"riverside", "stjohns"}

