"""Tools the PantryRelay agent can call.

Read-only lookups run unattended. The two tools with real-world consequences —
`reserve_pickup` and `notify_pantry_coordinator` — are the ones the coordinator
gate watches, so their arguments are shaped to carry everything the gate needs
to judge risk without re-deriving it.
"""

from __future__ import annotations

import math
from typing import Any

from strands import tool

from .data import PANTRIES, get_pantry, record_booking, record_message
from .models import StorageClass


@tool
def find_candidate_pantries(
    category: str,
    storage: str,
    quantity_lbs: float,
    max_distance_km: float = 15.0,
) -> list[dict[str, Any]]:
    """Find pantries that could take a donation, best match first.

    Ranks by whether the pantry currently lists the category as a need, then by
    spare capacity in the required storage class, then by distance. Pantries
    that cannot hold the storage class at all are excluded, as are pantries that
    do not accept perishables when the storage class is chilled or frozen.

    Args:
        category: Food category, e.g. "dairy", "produce", "bakery", "meat".
        storage: One of "frozen", "refrigerated", "ambient".
        quantity_lbs: Approximate weight to place, in pounds.
        max_distance_km: Furthest pantry worth considering.

    Returns:
        Ranked candidates with their free capacity and whether they need this
        category right now. An empty list means nothing in range can take it.
    """
    candidates = []
    for pantry in PANTRIES:
        if pantry.distance_km > max_distance_km:
            continue
        if storage in ("frozen", "refrigerated") and not pantry.accepts_perishable:
            continue
        free = pantry.capacity_for(storage)  # type: ignore[arg-type]
        if free <= 0:
            continue
        candidates.append(
            {
                "pantry_id": pantry.id,
                "name": pantry.name,
                "distance_km": pantry.distance_km,
                "free_lbs": free,
                "fits_fully": free >= quantity_lbs,
                "needs_this_category": category in pantry.needs,
                "min_notice_hours": pantry.min_notice_hours,
                # Share of this pantry's free space the load would consume.
                # Placing a large load in a nearly-full pantry is what starves
                # the next offer, so this outranks raw distance.
                "crowding": round(quantity_lbs / free, 3),
            }
        )

    candidates.sort(
        key=lambda c: (
            not c["needs_this_category"],
            not c["fits_fully"],
            c["crowding"],
            c["distance_km"],
        )
    )
    return candidates


@tool
def check_storage_capacity(pantry_id: str, storage: str, quantity_lbs: float) -> dict[str, Any]:
    """Check whether one pantry can physically hold a quantity right now.

    Args:
        pantry_id: The pantry's id.
        storage: One of "frozen", "refrigerated", "ambient".
        quantity_lbs: Weight to place, in pounds.

    Returns:
        Free capacity, whether it fits, and the shortfall if it does not.
    """
    pantry = get_pantry(pantry_id)
    if pantry is None:
        return {"error": f"no pantry with id {pantry_id!r}"}

    free = pantry.capacity_for(storage)  # type: ignore[arg-type]
    return {
        "pantry_id": pantry.id,
        "name": pantry.name,
        "storage": storage,
        "free_lbs": free,
        "requested_lbs": quantity_lbs,
        "fits": free >= quantity_lbs,
        "shortfall_lbs": max(0.0, quantity_lbs - free),
        "min_notice_hours": pantry.min_notice_hours,
    }


@tool
def reserve_pickup(
    pantry_id: str,
    donor_name: str,
    quantity_lbs: float,
    storage: str,
    hours_until_unusable: float,
    rationale: str,
) -> dict[str, Any]:
    """Commit a donation to a pantry and decrement its available capacity.

    This is a real commitment: it books space the pantry can no longer offer to
    anyone else. Call it only once you have confirmed capacity and are ready to
    route the food.

    Args:
        pantry_id: Receiving pantry.
        donor_name: Who the food is coming from.
        quantity_lbs: Weight being committed.
        storage: One of "frozen", "refrigerated", "ambient".
        hours_until_unusable: Hours before the food can no longer be given out,
            as read from the offer. Report what the source actually said; the
            gate checks this against the offer's own window, so a roomier
            number here does not buy a roomier deadline.
        rationale: One sentence on why this pantry, for the coordinator's log.

    Returns:
        The booking record, or an error describing why it could not be made.
    """
    pantry = get_pantry(pantry_id)
    if pantry is None:
        return {"error": f"no pantry with id {pantry_id!r}"}

    # Defence in depth behind the coordinator gate, which refuses these calls
    # before they reach the tool. A non-positive weight is not a booking:
    # subtracting it would hand the pantry capacity it does not have, turning a
    # load the gate refused into a load that fits.
    if not math.isfinite(quantity_lbs) or quantity_lbs <= 0:
        return {
            "error": "quantity_lbs must be a positive, finite weight",
            "requested_lbs": quantity_lbs,
        }

    free = pantry.capacity_for(storage)  # type: ignore[arg-type]
    if free < quantity_lbs:
        return {
            "error": "insufficient capacity",
            "free_lbs": free,
            "requested_lbs": quantity_lbs,
        }

    pantry.free_lbs[storage] = free - quantity_lbs  # type: ignore[index]
    booking = {
        "pantry_id": pantry.id,
        "pantry_name": pantry.name,
        "donor": donor_name,
        "lbs": quantity_lbs,
        "storage": storage,
        "hours_until_unusable": hours_until_unusable,
        "rationale": rationale,
    }
    record_booking(booking)
    return {"booked": True, **booking, "remaining_free_lbs": pantry.free_lbs[storage]}  # type: ignore[index]


@tool
def notify_pantry_coordinator(pantry_id: str, message: str) -> dict[str, Any]:
    """Send a message to a pantry's coordinator about an incoming donation.

    This reaches a real person. Keep it short and specific: what is coming, how
    much, what storage it needs, and by when it has to be collected.

    Args:
        pantry_id: Pantry whose coordinator should be contacted.
        message: The message body, already written for a human reader.

    Returns:
        Delivery record including the contact it was sent to.
    """
    pantry = get_pantry(pantry_id)
    if pantry is None:
        return {"error": f"no pantry with id {pantry_id!r}"}

    entry = {"pantry_id": pantry.id, "to": pantry.contact, "message": message}
    record_message(entry)
    return {"sent": True, **entry}


ALL_TOOLS = [
    find_candidate_pantries,
    check_storage_capacity,
    reserve_pickup,
    notify_pantry_coordinator,
]

#: Tools that only look things up. The coordinator gate lets exactly these
#: through unexamined.
#:
#: This is deliberately the allow-list rather than the deny-list. The gate asks
#: "is this known to be harmless?", not "is this known to be dangerous?", so a
#: tool added to ALL_TOOLS and forgotten here is guarded from the moment it
#: exists instead of running unattended until somebody notices.
READ_ONLY_TOOLS = {"find_candidate_pantries", "check_storage_capacity"}

#: Tools that change the world. Kept for callers that want to name them, and as
#: the paired half of READ_ONLY_TOOLS: every tool belongs to exactly one set.
CONSEQUENTIAL_TOOLS = {"reserve_pickup", "notify_pantry_coordinator"}
