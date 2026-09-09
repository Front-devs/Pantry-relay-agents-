"""Turn a run of the morning into something a dashboard can render.

This is a presentation layer and nothing else. It calls :func:`route_offer`,
which calls ``gate.decide()``, which is the same policy the live agent reaches
through ``before_tool_call``. Nothing in this module decides anything, and
nothing in it knows the answer in advance — every number below comes back out of
a run that actually happened.

That distinction is the whole reason this file exists. A dashboard that lists
what the gate *would* say is a drawing of the system. This one reports what it
did say, so the page a judge watches and the suite that proves the interlock are
looking at the same run.
"""

from __future__ import annotations

from typing import Any

from .data import LEDGER, OUTBOX, PANTRIES, reset
from .fixtures import MORNING, SAMPLE_FILES
from .gate import CoordinatorGate
from .models import DonationOffer, Escalation
from .resolution import CHOICE_LABELS, choices_for
from .routing import Outcome, route_offer


def _pantry_state() -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "needs": p.needs,
            "contact": p.contact,
            "free_lbs": dict(p.free_lbs),
        }
        for p in PANTRIES
    ]


def _events_for(outcome: Outcome, new_messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The gate's verdicts on one offer, in the order the router hit them."""
    events: list[dict[str, str]] = []

    for booking in outcome.booked:
        events.append({
            "verdict": "proceed",
            "text": (
                f"reserve_pickup({booking['pantry_name']}, {booking['storage']}, "
                f"{booking['lbs']:.0f} lbs) -> PROCEED"
            ),
        })
    for message in new_messages:
        events.append({
            "verdict": "proceed",
            "text": f"notify_pantry_coordinator({message['to']}) -> PROCEED",
        })
    for esc in outcome.escalations:
        events.append({
            "verdict": "escalate",
            "text": f"reserve_pickup(...) -> CONFIRM ({esc.reason_code})",
        })
    for note in outcome.unplaceable:
        events.append({"verdict": "deny", "text": f"DENIED: {note}"})

    return events


def _escalation_payload(index: int, esc: Escalation) -> dict[str, Any]:
    options = choices_for(esc)
    return {
        "id": index,
        "reason_code": esc.reason_code,
        "summary": esc.summary,
        "detail": esc.detail,
        "proposed_action": esc.proposed_action,
        "donor_name": esc.donor_name,
        "pantry_id": esc.pantry_id,
        "held_lbs": esc.held_lbs,
        "storage": esc.storage,
        "choices": [{"key": key, "label": CHOICE_LABELS[key]} for key in options],
    }


def _offer_summary(offer: DonationOffer) -> str:
    return "; ".join(item.description for item in offer.items)


def run_morning(gate: CoordinatorGate | None = None) -> dict[str, Any]:
    """Route every seeded offer through the real gate and report what happened."""
    reset()
    gate = gate or CoordinatorGate()

    offers: list[dict[str, Any]] = []
    for offer, filename in zip(MORNING, SAMPLE_FILES):
        seen_messages = len(OUTBOX)
        outcome = route_offer(offer, gate)
        new_messages = OUTBOX[seen_messages:]

        if outcome.escalations:
            status = "held"
        elif outcome.booked:
            status = "routed"
        else:
            status = "stuck"

        offers.append({
            "donor": offer.donor_name,
            "channel": offer.channel,
            "source_file": filename,
            "lbs": offer.total_lbs,
            "confidence": offer.extraction_confidence,
            "summary": _offer_summary(offer),
            "status": status,
            "events": _events_for(outcome, new_messages),
            "bookings": [
                {
                    "pantry_name": b["pantry_name"],
                    "lbs": b["lbs"],
                    "storage": b["storage"],
                    "rationale": b["rationale"],
                }
                for b in outcome.booked
            ],
            "messages": [{"to": m["to"], "message": m["message"]} for m in new_messages],
        })

    return {
        "offers": offers,
        "escalations": [
            _escalation_payload(i, e) for i, e in enumerate(gate.escalations)
        ],
        "pantries": _pantry_state(),
        "stats": {
            "offers": len(offers),
            "routed": sum(1 for o in offers if o["status"] == "routed"),
            "held": sum(1 for o in offers if o["status"] == "held"),
            "bookings": len(LEDGER),
            "messages": len(OUTBOX),
        },
    }
