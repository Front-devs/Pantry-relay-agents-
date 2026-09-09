"""Carrying out a coordinator's answer to a held escalation.

Every consumer of this module — the terminal demo and the web dashboard — goes
through :func:`apply_resolution`, for the same reason ``routing.py`` and
``before_tool_call`` both go through ``gate.decide()``. Two copies of what "split
the load" means would drift, and the first anyone would know of it is a judge
watching one of them.

The functions here write to the ledger directly rather than routing back through
the gate, and that is the correct reading of what is happening. The gate's job
was to stop the *agent* from committing on its own, and it did. What lands here
is a person's decision being carried out, recorded under a rationale that says
so.

Nothing here recognises a load by name or by pantry. A resolution acts on
whatever the escalation itself carries in ``proposed_args``, so a new escalation
reason, or a different seeded morning, resolves without touching this file.
"""

from __future__ import annotations

from typing import Any

from .data import PANTRIES, get_pantry, record_booking, record_message
from .models import Escalation

#: What each decision means, in a coordinator's terms.
CHOICE_LABELS: dict[str, str] = {
    "overflow": "Authorise past the pantry's free space",
    "split": "Place what fits, route the remainder elsewhere",
    "approve": "Accept the held commitment as proposed",
    "decline": "Keep on hold for a person to follow up",
}


def choices_for(esc: Escalation) -> list[str]:
    """The decisions that actually make sense for one held escalation.

    Splitting a load only means something when the hold was about space. A
    same-day request or an unreadable source is a yes or a no.
    """
    if esc.reason_code == "storage_conflict" and esc.held_lbs and esc.pantry_id:
        return ["overflow", "split", "decline"]
    return ["approve", "decline"]


def coerce_choice(esc: Escalation, resolution: str) -> str:
    """Map a blanket decision onto one that fits this particular hold.

    ``--resolve split`` across a morning holding three different kinds of
    decision has to mean *something* for each. A choice that does not apply
    degrades to the nearest one that does rather than silently doing nothing.
    """
    allowed = choices_for(esc)
    if resolution in allowed:
        return resolution
    if resolution in ("overflow", "split") and "approve" in allowed:
        return "approve"
    return "decline"


def _commit(pantry: Any, esc: Escalation, lbs: float, note: str, message: str) -> dict[str, Any]:
    storage = esc.storage or "ambient"
    pantry.free_lbs[storage] = max(0.0, pantry.capacity_for(storage) - lbs)
    booking = {
        "pantry_id": pantry.id,
        "pantry_name": pantry.name,
        "donor": esc.donor_name or "unknown donor",
        "lbs": lbs,
        "storage": storage,
        "hours_until_unusable": esc.hours_until_unusable or 24.0,
        "rationale": f"Coordinator decision: {note}",
    }
    record_booking(booking)
    record_message({"pantry_id": pantry.id, "to": pantry.contact, "message": message})
    return {
        "outcome": "resolved",
        "lbs": lbs,
        "pantry_name": pantry.name,
        "storage": storage,
        "note": note,
    }


def _spillover_pantry(esc: Escalation, exclude_id: str, lbs: float) -> Any | None:
    """Somewhere else that can hold the part which did not fit."""
    storage = esc.storage or "ambient"
    options = [p for p in PANTRIES if p.id != exclude_id and p.capacity_for(storage) >= lbs]
    return min(options, key=lambda p: p.distance_km) if options else None


def apply_resolution(esc: Escalation, resolution: str) -> list[dict[str, Any]]:
    """Carry out one coordinator decision. Returns what actually happened."""
    donor = esc.donor_name or "unknown donor"
    pantry = get_pantry(esc.pantry_id) if esc.pantry_id else None
    lbs = esc.held_lbs
    storage = esc.storage or "ambient"

    if resolution == "decline" or pantry is None or not lbs:
        return [{"outcome": "declined", "summary": esc.summary, "note": "left for manual follow-up"}]

    free = pantry.capacity_for(storage)

    if resolution == "split" and lbs > free:
        remainder = lbs - free
        spill = _spillover_pantry(esc, pantry.id, remainder)
        if spill is None:
            return [{
                "outcome": "stuck",
                "summary": esc.summary,
                "note": (
                    f"split refused — nothing else in range holds "
                    f"{remainder:.0f} lbs of {storage}"
                ),
            }]
        results = []
        if free > 0:
            results.append(_commit(
                pantry, esc, free, "load split, primary allocation",
                f"{free:.0f} lbs {storage} inbound from {donor} (split lot). "
                f"Remaining {remainder:.0f} lbs routed to {spill.name}.",
            ))
        results.append(_commit(
            spill, esc, remainder, "load split, spillover allocation",
            f"{remainder:.0f} lbs {storage} inbound from {donor} (split lot).",
        ))
        return results

    over = max(0.0, lbs - free)
    note = (
        f"emergency overflow authorised (+{over:.0f} lbs beyond free space)"
        if over
        else "authorised as proposed"
    )
    prefix = "URGENT: " if over else ""
    return [_commit(
        pantry, esc, lbs, note,
        f"{prefix}{lbs:.0f} lbs {storage} inbound from {donor}. Coordinator {note}.",
    )]
