"""In-memory pantry network and booking ledger.

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
