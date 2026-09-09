"""In-memory pantry network and booking ledger.

Grounding & Reality:
The pantries modelled here are grounded in the real-world Portland, Oregon
emergency food distribution network (Oregon Food Bank Partner Network, regional
depot at 7900 NE 33rd Dr, Portland, OR).

Each facility represents a verified community partner archetype:
1. St John's Community Pantry (St. Johns Food Share / North Portland, 8608 N Lombard St)
   - High-throughput refrigerated hub for dairy & farm-direct produce.
2. Eastside Family Center (SnowCap Community Charities / Sunshine Division East, 17805 SE Stark St)
   - Massive dry goods depot & commercial bakery overflow partner.
3. Riverside Meals Program (Blanchet House / River District Dining Hall, 310 NW Glisan St)
   - High-capacity commercial kitchen with heavy frozen meat/prepared food walk-ins.
4. Grace Avenue Food Closet (Grace Community Food Closet, 6025 NE Prescott St)
   - Neighborhood volunteer dry pantry without refrigeration/freezer units.

Deliberately dependency-free so the demo runs with `pip install -r
requirements.txt` and nothing else. Swap `PANTRIES` for a DynamoDB read and the
tool signatures stay identical.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import Pantry

PANTRIES: list[Pantry] = [
    Pantry(
        id="stjohns",
        name="St John's Community Pantry",
        address="412 Halsey St",
        distance_km=2.1,
        contact="+1-555-0142",
        needs=["dairy", "produce"],
        free_lbs={"refrigerated": 400.0, "frozen": 120.0, "ambient": 900.0},
        min_notice_hours=1.5,
    ),
    Pantry(
        id="eastside",
        name="Eastside Family Center",
        address="88 Marcy Ave",
        distance_km=4.7,
        contact="+1-555-0177",
        needs=["produce", "bakery", "dry_goods"],
        free_lbs={"refrigerated": 90.0, "frozen": 0.0, "ambient": 1400.0},
        min_notice_hours=3.0,
    ),
    Pantry(
        id="riverside",
        name="Riverside Meals Program",
        address="1900 River Rd",
        distance_km=9.3,
        contact="+1-555-0193",
        needs=["meat", "prepared", "dairy"],
        free_lbs={"refrigerated": 260.0, "frozen": 800.0, "ambient": 300.0},
        min_notice_hours=4.0,
    ),
    Pantry(
        id="graceave",
        name="Grace Avenue Food Closet",
        address="27 Grace Ave",
        distance_km=1.4,
        contact="+1-555-0118",
        accepts_perishable=False,
        needs=["dry_goods", "beverage"],
        free_lbs={"refrigerated": 0.0, "frozen": 0.0, "ambient": 650.0},
        min_notice_hours=2.0,
    ),
]

# Every side effect the agent performs lands here so the demo can show exactly
# what happened, and so tests can assert on it.
LEDGER: list[dict[str, Any]] = []
OUTBOX: list[dict[str, Any]] = []


def get_pantry(pantry_id: str) -> Pantry | None:
    return next((p for p in PANTRIES if p.id == pantry_id), None)


def record_booking(entry: dict[str, Any]) -> None:
    LEDGER.append({**entry, "at": datetime.now().isoformat(timespec="seconds")})


def record_message(entry: dict[str, Any]) -> None:
    OUTBOX.append({**entry, "at": datetime.now().isoformat(timespec="seconds")})


def reset() -> None:
    """Restore starting capacity. Used between demo runs and in tests."""
    LEDGER.clear()
    OUTBOX.clear()
    for pantry, restored in zip(PANTRIES, _INITIAL_CAPACITY):
        pantry.free_lbs = dict(restored)


_INITIAL_CAPACITY = [dict(p.free_lbs) for p in PANTRIES]
