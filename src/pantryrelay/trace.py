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
            "address": p.address,
            "distance_km": p.distance_km,
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
        "mode": "offline",
        "mode_label": "deterministic routing policy, no model call",
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


# -- the live path ---------------------------------------------------------
#
# Everything above reaches the gate through routing.py. Everything below reaches
# the *same* gate through Strands' before_tool_call, with two real agents and a
# real Bedrock model in between. The payload shape is identical on purpose: the
# dashboard renders either one without knowing which it asked for, and says so
# on screen.
#
# This is the only part of the web path that needs AWS credentials, and it is
# imported lazily so that a deployment without them still serves the offline
# run rather than failing at start-up.


class LiveUnavailable(RuntimeError):
    """Bedrock could not be reached or would not answer.

    Carries a sentence a viewer can act on rather than a stack trace, because
    this surfaces on a public dashboard.
    """

    def __init__(self, problem: str, remedy: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.remedy = remedy


def run_morning_live(gate: CoordinatorGate | None = None) -> dict[str, Any]:
    """Read every sample with the reader agent, place it with the router agent.

    Unlike the offline path this does not use ``fixtures.py`` at all. The offers
    are whatever the reader agent actually extracts from the raw files, which is
    the point: the confidence scores and ambiguities the gate then judges are the
    model's own, not ours.
    """
    from pathlib import Path

    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        NoRegionError,
    )

    from .agent import build_model, build_reader, build_router, read_offer

    samples_dir = Path(__file__).resolve().parents[2] / "samples"

    reset()
    gate = gate or CoordinatorGate()

    try:
        model = build_model()
        reader = build_reader(model)

        offers: list[dict[str, Any]] = []
        for filename in SAMPLE_FILES:
            raw = (samples_dir / filename).read_text(encoding="utf-8")
            offer = read_offer(raw, reader=reader)

            booked_before, sent_before = len(LEDGER), len(OUTBOX)
            held_before = len(gate.escalations)

            # A fresh router per offer: each is its own decision, and an offer
            # that stops for a human leaves its agent mid-loop. The gate is
            # shared, so the coordinator's queue accumulates across the morning.
            router, _ = build_router(model, gate)
            router(
                f"Place this donation offer:\n\n{offer.model_dump_json(indent=2)}",
                invocation_state={"offer": offer},
            )

            new_bookings = LEDGER[booked_before:]
            new_messages = OUTBOX[sent_before:]
            new_holds = gate.escalations[held_before:]

            events: list[dict[str, str]] = []
            for booking in new_bookings:
                events.append({
                    "verdict": "proceed",
                    "text": (
                        f"reserve_pickup({booking['pantry_name']}, "
                        f"{booking['storage']}, {booking['lbs']:.0f} lbs) -> PROCEED"
                    ),
                })
            for message in new_messages:
                events.append({
                    "verdict": "proceed",
                    "text": f"notify_pantry_coordinator({message['to']}) -> PROCEED",
                })
            for esc in new_holds:
                events.append({
                    "verdict": "escalate",
                    "text": f"reserve_pickup(...) -> CONFIRM ({esc.reason_code})",
                })

            offers.append({
                "donor": offer.donor_name,
                "channel": offer.channel,
                "source_file": filename,
                "lbs": offer.total_lbs,
                "confidence": offer.extraction_confidence,
                "ambiguities": offer.ambiguities,
                "summary": _offer_summary(offer),
                "status": "held" if new_holds else ("routed" if new_bookings else "stuck"),
                "events": events,
                "bookings": [
                    {
                        "pantry_name": b["pantry_name"],
                        "lbs": b["lbs"],
                        "storage": b["storage"],
                        "rationale": b["rationale"],
                    }
                    for b in new_bookings
                ],
                "messages": [
                    {"to": m["to"], "message": m["message"]} for m in new_messages
                ],
            })

    except (NoCredentialsError, NoRegionError) as exc:
        raise LiveUnavailable(
            "this deployment is not configured for Bedrock",
            f"{exc} — set AWS credentials and a region in the environment.",
        ) from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {
            "AccessDeniedException", "UnrecognizedClientException",
            "ValidationException", "ResourceNotFoundException",
            "ExpiredTokenException", "InvalidSignatureException",
            "ThrottlingException",
        }:
            from .agent import DEFAULT_MODEL_ID, DEFAULT_REGION
            raise LiveUnavailable(
                f"Bedrock refused the call ({code})",
                f"Model {DEFAULT_MODEL_ID!r} in {DEFAULT_REGION!r} may not be "
                f"enabled for this account.",
            ) from exc
        raise
    except BotoCoreError as exc:
        raise LiveUnavailable("AWS could not be reached", str(exc)) from exc

    return {
        "mode": "live",
        "mode_label": "reader and router agents on Amazon Bedrock",
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
