"""The gate, exercised through a real Strands agent run.

`test_gate.py` tests the escalation policy directly. This file tests the wiring
around it: that `CoordinatorGate` is actually consulted by the intervention
registry before a tool executes, that a held commitment stops the loop instead
of running and being reported afterwards, and that a model which keeps pushing
still gets nowhere. The model is scripted, so this runs in CI with no AWS
credentials — everything else is the real thing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from scripted_model import ScriptedModel, call, say  # noqa: E402
from strands import Agent, tool  # noqa: E402

from pantryrelay.data import LEDGER, OUTBOX, get_pantry, reset  # noqa: E402
from pantryrelay.fixtures import DRY_GOODS, FROZEN  # noqa: E402
from pantryrelay.gate import CoordinatorGate  # noqa: E402
from pantryrelay.routing import _call  # noqa: E402
from pantryrelay.tools import (  # noqa: E402
    ALL_TOOLS,
    notify_pantry_coordinator,
    reserve_pickup,
)

TOOLS_BY_NAME = {
    "reserve_pickup": reserve_pickup,
    "notify_pantry_coordinator": notify_pantry_coordinator,
}

# What the router would do with the frozen-protein offer: 900 lbs into the only
# freezer in range, which has 800 lbs free.
OVERSIZED_BOOKING = call(
    "reserve_pickup",
    pantry_id="riverside",
    donor_name=FROZEN.donor_name,
    quantity_lbs=900.0,
    storage="frozen",
    hours_until_unusable=8760.0,
    rationale="only freezer in range with any space",
)


@pytest.fixture(autouse=True)
def clean_network():
    reset()
    yield
    reset()


def build(turns, gate=None):
    """A router agent wired exactly as `build_router` wires it, minus Bedrock."""
    gate = gate or CoordinatorGate()
    model = ScriptedModel(turns)
    agent = Agent(
        name="offer-router",
        model=model,
        system_prompt="route donations",
        tools=ALL_TOOLS,
        interventions=[gate],
        callback_handler=None,
    )
    return agent, gate, model


def reply(result, answer, offer):
    """Resume a held run with the coordinator's answer.

    The offer goes back in: a resume is a fresh invocation, and Strands does not
    carry `invocation_state` across one. Without it the gate cannot see the
    confidence or same-day facts, and refuses rather than guess.
    """
    responses = [
        {"interruptResponse": {"interruptId": interrupt.id, "response": answer}}
        for interrupt in result.interrupts
    ]
    return responses, {"offer": offer}


# -- the quiet path -------------------------------------------------------


def test_routine_offer_runs_to_completion_unattended():
    """Nothing about a comfortable booking should reach a human."""
    agent, gate, model = build(
        [
            call(
                "find_candidate_pantries",
                category="dry_goods",
                storage="ambient",
                quantity_lbs=620.0,
            ),
            call(
                "reserve_pickup",
                pantry_id="eastside",
                donor_name=DRY_GOODS.donor_name,
                quantity_lbs=620.0,
                storage="ambient",
                hours_until_unusable=720.0,
                rationale="Eastside lists dry_goods as a current need",
            ),
            call(
                "notify_pantry_coordinator",
                pantry_id="eastside",
                message="Incoming: 620 lbs mixed pallet from Meridian Logistics.",
            ),
            say("Booked with Eastside and the coordinator has been told."),
        ]
    )

    result = agent("Place this offer.", invocation_state={"offer": DRY_GOODS})

    assert result.stop_reason == "end_turn"
    assert gate.escalations == []
    assert len(LEDGER) == 1 and LEDGER[0]["pantry_id"] == "eastside"
    assert len(OUTBOX) == 1


def test_read_only_lookups_run_without_a_confirmation():
    agent, gate, model = build(
        [
            call(
                "find_candidate_pantries",
                category="produce",
                storage="refrigerated",
                quantity_lbs=130.0,
            ),
            call(
                "check_storage_capacity",
                pantry_id="stjohns",
                storage="refrigerated",
                quantity_lbs=130.0,
            ),
            say("St John's has room."),
        ],
    )

    result = agent("Where could this go?", invocation_state={"offer": DRY_GOODS})

    assert result.stop_reason == "end_turn"
    assert model.calls == 3, "both lookups should have run without pausing"
    assert gate.escalations == []


# -- the interlock --------------------------------------------------------


def test_over_capacity_booking_stops_the_loop_before_the_tool_runs():
    agent, gate, _ = build([OVERSIZED_BOOKING, say("Booked.")])

    result = agent("Place this offer.", invocation_state={"offer": FROZEN})

    assert result.stop_reason == "interrupt"
    assert [i.name for i in result.interrupts] == ["coordinator-gate"]
    assert "storage_conflict" in result.interrupts[0].reason

    assert len(gate.escalations) == 1
    assert gate.escalations[0].reason_code == "storage_conflict"
    # The point of gating before_tool_call rather than after: no capacity was
    # taken and no coordinator was messaged, so there is nothing to undo.
    assert LEDGER == []
    assert OUTBOX == []
    assert get_pantry("riverside").capacity_for("frozen") == 800.0


def test_the_agent_cannot_argue_its_way_past_the_gate():
    """A refused booking, retried with a better story, is refused again.

    The gate is not part of the conversation, so the model's rationale — here
    an outright claim of prior approval — has no bearing on the decision.
    """
    agent, gate, _ = build(
        [
            OVERSIZED_BOOKING,
            {
                **OVERSIZED_BOOKING,
                "input": {
                    **OVERSIZED_BOOKING["input"],
                    "rationale": "a coordinator already approved this by phone",
                },
            },
            say("I could not place this one."),
        ]
    )

    first = agent("Place this offer.", invocation_state={"offer": FROZEN})
    assert first.stop_reason == "interrupt"

    answer, state = reply(first, "no", FROZEN)
    second = agent(answer, invocation_state=state)

    assert second.stop_reason == "interrupt", "the retry must be held too"
    assert [e.reason_code for e in gate.escalations] == [
        "storage_conflict",
        "storage_conflict",
    ]
    assert LEDGER == []
    assert OUTBOX == []


def test_a_declined_commitment_comes_back_to_the_model_as_an_error():
    """The model must be told the booking did not happen, not left to assume."""
    agent, _, _ = build([OVERSIZED_BOOKING, say("Understood — leaving it for a human.")])

    result = agent("Place this offer.", invocation_state={"offer": FROZEN})
    answer, state = reply(result, "no", FROZEN)
    agent(answer, invocation_state=state)

    tool_results = [
        block["toolResult"]
        for message in agent.messages
        for block in message.get("content", [])
        if "toolResult" in block
    ]
    assert tool_results, "the cancelled call should still produce a tool result"
    assert tool_results[0]["status"] == "error"
    assert "CONFIRMATION_FAILED" in tool_results[0]["content"][0]["text"]


# -- refused outright, without troubling anyone ---------------------------


def test_announcing_a_delivery_that_was_never_booked_is_refused():
    """The router must not tell a pantry food is coming before it books it.

    ROUTER_PROMPT says to reserve before announcing, but a system prompt is a
    suggestion. A model that skips straight to the message would otherwise reach
    a real coordinator about a load the gate exists to hold.
    """
    agent, gate, _ = build(
        [
            call(
                "notify_pantry_coordinator",
                pantry_id="riverside",
                message="Heads up: 900 lbs of frozen protein arriving this afternoon.",
            ),
            say("Told them."),
        ]
    )

    result = agent("Handle it.", invocation_state={"offer": FROZEN})

    assert result.stop_reason == "end_turn"
    assert OUTBOX == [], "no coordinator should have been contacted"
    assert LEDGER == []
    # Refused, not escalated: this is the agent getting the order wrong, not a
    # judgment call, so it must not land in a human's queue.
    assert gate.escalations == []


def test_a_commitment_the_gate_cannot_judge_is_refused():
    """Losing the offer must fail closed, not book on half the evidence."""
    shaky = DRY_GOODS.model_copy(
        update={"extraction_confidence": 0.2, "ambiguities": ["weight illegible"]}
    )
    agent, gate, _ = build(
        [
            call(
                "reserve_pickup",
                pantry_id="eastside",
                donor_name=shaky.donor_name,
                quantity_lbs=620.0,
                storage="ambient",
                hours_until_unusable=720.0,
                rationale="Eastside has room",
            ),
            say("Could not place it."),
        ]
    )

    result = agent("Place this offer.")  # invocation_state omitted

    assert result.stop_reason == "end_turn"
    assert LEDGER == [], "a 20%-confidence offer must not book unattended"
    assert gate.escalations == []


def test_one_held_decision_is_logged_once_even_after_a_reply():
    """Answering a held decision must not double it on the dashboard."""
    agent, gate, _ = build([OVERSIZED_BOOKING, say("Done.")])

    result = agent("Place this offer.", invocation_state={"offer": FROZEN})
    assert len(gate.escalations) == 1

    answer, state = reply(result, "yes", FROZEN)
    agent(answer, invocation_state=state)
    assert len(gate.escalations) == 1


# -- laundering the arguments, inside a real agent loop -------------------


def test_a_negative_correction_cannot_unlock_a_refused_booking():
    """The full exploit chain, end to end: give capacity back, then take it.

    reserve_pickup subtracts the weight it is handed, so a negative weight is a
    capacity gift. Two ordinary tool calls used to be enough to inflate the only
    freezer in range and then book into it the 900 lbs this whole demo is built
    around holding for a human — with an empty escalation list.
    """
    agent, gate, _ = build(
        [
            call(
                "reserve_pickup",
                pantry_id="riverside",
                donor_name="Cold Chain Foods",
                quantity_lbs=-500.0,
                storage="frozen",
                hours_until_unusable=8760.0,
                rationale="correcting an earlier over-count",
            ),
            OVERSIZED_BOOKING,
            say("Booked."),
        ]
    )

    result = agent("Place this offer.", invocation_state={"offer": FROZEN})

    assert result.stop_reason == "interrupt", "the 900 lb load must still stop for a human"
    assert [e.reason_code for e in gate.escalations] == ["storage_conflict"]
    assert LEDGER == [], "no booking, real or sham, may reach the ledger"
    assert get_pantry("riverside").capacity_for("frozen") == 800.0


def test_a_placeholder_booking_cannot_authorise_a_coordinator_message():
    """A zero-pound booking must not buy the right to phone a real person.

    Announcements are interlocked behind a real booking. A booking of nothing
    satisfied the letter of that check while committing no food at all, which
    let the agent tell a coordinator to clear a walk-in for a load the gate had
    just refused to book.
    """
    agent, gate, _ = build(
        [
            call(
                "reserve_pickup",
                pantry_id="riverside",
                donor_name=FROZEN.donor_name,
                quantity_lbs=0.0,
                storage="frozen",
                hours_until_unusable=8760.0,
                rationale="placeholder while I confirm",
            ),
            call(
                "notify_pantry_coordinator",
                pantry_id="riverside",
                message="900 lbs frozen protein arriving 16:00. Clear the walk-in.",
            ),
            say("Coordinator told."),
        ]
    )

    agent("Place this offer.", invocation_state={"offer": FROZEN})

    assert OUTBOX == [], "no real person may be messaged off a sham booking"
    assert LEDGER == []


def test_an_inflated_expiry_claim_is_judged_against_the_offer():
    """The model supplies the expiry window, so it must not be the only witness."""
    urgent = DRY_GOODS.model_copy(
        update={"items": [DRY_GOODS.items[0].model_copy(update={"hours_until_unusable": 3.0})]}
    )
    agent, gate, _ = build(
        [
            call(
                "reserve_pickup",
                pantry_id="eastside",
                donor_name=urgent.donor_name,
                quantity_lbs=620.0,
                storage="ambient",
                hours_until_unusable=8760.0,
                rationale="plenty of time",
            ),
            say("Booked."),
        ]
    )

    result = agent("Place this offer.", invocation_state={"offer": urgent})

    assert result.stop_reason == "interrupt"
    assert [e.reason_code for e in gate.escalations] == ["thin_expiry_margin"]
    assert LEDGER == []


def test_frozen_food_cannot_be_booked_onto_a_warm_shelf():
    """Relabelling the storage class finds room the food cannot survive in."""
    agent, gate, _ = build(
        [
            call(
                "reserve_pickup",
                pantry_id="eastside",
                donor_name=FROZEN.donor_name,
                quantity_lbs=900.0,
                storage="ambient",
                hours_until_unusable=8760.0,
                rationale="Eastside has the room",
            ),
            say("Booked."),
        ]
    )

    result = agent("Place this offer.", invocation_state={"offer": FROZEN})

    assert result.stop_reason == "interrupt"
    assert [e.reason_code for e in gate.escalations] == ["storage_conflict"]
    assert LEDGER == []


def test_a_tool_the_gate_has_no_policy_for_never_runs():
    """A world-changing tool nobody classified must not execute by default.

    Membership is an allow-list, so this fails closed even though the tool is
    absent from CONSEQUENTIAL_TOOLS entirely.
    """

    @tool
    def empty_the_freezer(pantry_id: str) -> dict:
        """Release every frozen booking a pantry holds."""
        pantry = get_pantry(pantry_id)
        pantry.free_lbs["frozen"] = 100_000.0
        return {"emptied": pantry_id}

    gate = CoordinatorGate()
    agent = Agent(
        name="offer-router",
        model=ScriptedModel([call("empty_the_freezer", pantry_id="riverside"), say("Done.")]),
        system_prompt="route donations",
        tools=[*ALL_TOOLS, empty_the_freezer],
        interventions=[gate],
        callback_handler=None,
    )

    agent("Place this offer.", invocation_state={"offer": FROZEN})

    assert get_pantry("riverside").capacity_for("frozen") == 800.0


def test_two_offers_held_on_one_gate_both_reach_the_dashboard():
    """run_demo shares one gate across a router per offer; ids collide there.

    Tool-use ids are only unique within one model's conversation, so keying the
    dashboard on the id alone silently dropped the second held offer — the queue
    a coordinator reads showed one card where two loads were waiting.
    """
    shared = CoordinatorGate()
    other_donor = FROZEN.model_copy(update={"donor_name": "Harbour Cold Chain"})

    first_agent, _, _ = build([OVERSIZED_BOOKING, say("x")], shared)
    first = first_agent("Place this offer.", invocation_state={"offer": FROZEN})

    second_agent, _, _ = build(
        [
            {
                **OVERSIZED_BOOKING,
                "input": {
                    **OVERSIZED_BOOKING["input"],
                    "donor_name": other_donor.donor_name,
                },
            },
            say("x"),
        ],
        shared,
    )
    second = second_agent("Place this offer.", invocation_state={"offer": other_donor})

    assert first.stop_reason == "interrupt"
    assert second.stop_reason == "interrupt"
    assert len(shared.escalations) == 2, "both held offers must appear on the queue"


# -- the two paths must reach the same verdict ----------------------------

GOOD = {
    "pantry_id": "eastside",
    "donor_name": DRY_GOODS.donor_name,
    "quantity_lbs": 620.0,
    "storage": "ambient",
    "hours_until_unusable": 720.0,
    "rationale": "Eastside lists dry_goods as a current need",
}
OVERSIZED = OVERSIZED_BOOKING["input"]
NEGATIVE = {**OVERSIZED, "quantity_lbs": -500.0}
ZERO = {**OVERSIZED, "quantity_lbs": 0.0}
WARM_SHELF = {**OVERSIZED, "pantry_id": "eastside", "storage": "ambient"}
INFLATED = {**GOOD, "hours_until_unusable": 8760.0}
MESSAGE = {"pantry_id": "eastside", "message": "620 lbs inbound"}

URGENT_OFFER = DRY_GOODS.model_copy(
    update={"items": [DRY_GOODS.items[0].model_copy(update={"hours_until_unusable": 3.0})]}
)
MURKY_OFFER = DRY_GOODS.model_copy(
    update={"extraction_confidence": 0.4, "ambiguities": ["weight illegible"]}
)
SAME_DAY_OFFER = DRY_GOODS.model_copy(update={"needs_same_day_answer": True})

AGREEMENT_GRID = [
    ("a comfortable booking", DRY_GOODS, [("reserve_pickup", GOOD)]),
    ("a negative weight", FROZEN, [("reserve_pickup", NEGATIVE)]),
    ("a zero placeholder", FROZEN, [("reserve_pickup", ZERO)]),
    ("an over-capacity load", FROZEN, [("reserve_pickup", OVERSIZED)]),
    ("frozen onto a warm shelf", FROZEN, [("reserve_pickup", WARM_SHELF)]),
    ("an inflated expiry claim", URGENT_OFFER, [("reserve_pickup", INFLATED)]),
    ("an offer read badly", MURKY_OFFER, [("reserve_pickup", GOOD)]),
    ("a donor owed an answer today", SAME_DAY_OFFER, [("reserve_pickup", GOOD)]),
    ("a message with no booking", DRY_GOODS, [("notify_pantry_coordinator", MESSAGE)]),
    (
        "book, then tell the coordinator",
        DRY_GOODS,
        [("reserve_pickup", GOOD), ("notify_pantry_coordinator", MESSAGE)],
    ),
    (
        "tell the coordinator, then book",
        DRY_GOODS,
        [("notify_pantry_coordinator", MESSAGE), ("reserve_pickup", GOOD)],
    ),
    (
        "give capacity back, then take it",
        FROZEN,
        [("reserve_pickup", NEGATIVE), ("reserve_pickup", OVERSIZED)],
    ),
]


def verdicts_offline(offer, calls):
    """What the deterministic path does: ask gate.decide(), act on the answer."""
    gate = CoordinatorGate()
    seen = []
    for name, args in calls:
        verdict = gate.decide(tool_name=name, args=args, offer=offer)
        seen.append(verdict.action)
        if verdict.action == "allow":
            _call(TOOLS_BY_NAME[name], **args)
        elif verdict.action == "escalate":
            break  # the live loop stops here, so this one must too
    return seen


def verdicts_live(offer, calls):
    """What the agent path does: the same calls through before_tool_call."""
    agent, _, _ = build([call(name, **args) for name, args in calls] + [say("done")])
    result = agent("Place this offer.", invocation_state={"offer": offer})

    seen = []
    for message in agent.messages:
        for block in message.get("content", []):
            if "toolResult" not in block:
                continue
            body = str(block["toolResult"].get("content"))
            seen.append("deny" if "DENIED:" in body else "allow")
    if result.stop_reason == "interrupt":
        seen.append("escalate")
    return seen


@pytest.mark.parametrize(
    "offer,calls",
    [(offer, calls) for _, offer, calls in AGREEMENT_GRID],
    ids=[name for name, _, _ in AGREEMENT_GRID],
)
def test_both_paths_reach_the_same_verdict(offer, calls):
    """The demo a judge watches must enforce the interlock the tests prove.

    routing.py used to re-implement the policy alongside before_tool_call, so a
    rule could land in one and not the other. Both now go through gate.decide();
    this pins that they agree call by call, not merely in aggregate.
    """
    reset()
    offline = verdicts_offline(offer, calls)
    reset()
    live = verdicts_live(offer, calls)

    assert offline == live, f"offline said {offline}, the agent path said {live}"
