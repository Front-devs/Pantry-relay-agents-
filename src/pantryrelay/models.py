"""Domain models for PantryRelay.

These doubles as the structured-output schema the ingest agent fills in when it
reads a messy donation offer, so the field descriptions are written for the
model as much as for the reader.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

StorageClass = Literal["frozen", "refrigerated", "ambient"]

FoodCategory = Literal[
    "dairy", "produce", "bakery", "meat", "prepared", "dry_goods", "beverage", "other"
]


class FoodItem(BaseModel):
    """A single line on a donation offer."""

    description: str = Field(description="What the food is, in the donor's own words.")
    category: FoodCategory = Field(description="Best-fit category for matching.")
    quantity_lbs: float = Field(description="Approximate weight in pounds.", gt=0)
    storage: StorageClass = Field(
        description="Storage the item requires in transit and on arrival."
    )
    hours_until_unusable: float | None = Field(
        default=None,
        description=(
            "Hours until the food can no longer be distributed. Null when the donor "
            "gave no expiry signal at all — do not guess a number to fill this in."
        ),
    )
    notes: str | None = Field(
        default=None, description="Anything a coordinator would want flagged."
    )


class DonationOffer(BaseModel):
    """A donation offer, normalised out of whatever channel it arrived on."""

    donor_name: str
    donor_contact: str | None = None
    channel: Literal["email", "sms", "voicemail", "unknown"] = "unknown"
    pickup_location: str
    items: list[FoodItem]
    needs_same_day_answer: bool = Field(
        default=False,
        description="True when the donor explicitly needs a decision today.",
    )
    extraction_confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "How confident you are that this reading is correct. Lower it when the "
            "source was ambiguous, partially illegible, or self-contradictory."
        ),
    )
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Specific things you could not resolve from the source text.",
    )

    @property
    def total_lbs(self) -> float:
        return sum(item.quantity_lbs for item in self.items)

    @property
    def tightest_window_hours(self) -> float | None:
        windows = [i.hours_until_unusable for i in self.items if i.hours_until_unusable is not None]
        return min(windows) if windows else None


class Pantry(BaseModel):
    """A receiving pantry and its live capacity."""

    id: str
    name: str
    address: str
    distance_km: float
    contact: str
    accepts_perishable: bool = True
    needs: list[FoodCategory] = Field(default_factory=list)
    free_lbs: dict[StorageClass, float] = Field(default_factory=dict)
    # Hours of notice the pantry needs before a delivery lands.
    min_notice_hours: float = 2.0

    def capacity_for(self, storage: StorageClass) -> float:
        return float(self.free_lbs.get(storage, 0.0))


class MatchPlan(BaseModel):
    """A proposed routing of one offer to one pantry."""

    offer_donor: str
    pantry_id: str
    pantry_name: str
    lbs: float
    storage: StorageClass
    pickup_by: datetime
    rationale: str

    @classmethod
    def build(
        cls,
        *,
        offer: DonationOffer,
        pantry: Pantry,
        storage: StorageClass,
        lbs: float,
        window_hours: float | None,
        rationale: str,
    ) -> MatchPlan:
        hours = window_hours if window_hours is not None else 24.0
        return cls(
            offer_donor=offer.donor_name,
            pantry_id=pantry.id,
            pantry_name=pantry.name,
            lbs=lbs,
            storage=storage,
            pickup_by=datetime.now() + timedelta(hours=hours),
            rationale=rationale,
        )


class Escalation(BaseModel):
    """Raised when the agent declines to act without a human."""

    reason_code: Literal[
        "thin_expiry_margin",
        "storage_conflict",
        "same_day_commitment",
        "low_confidence_extraction",
    ]
    summary: str
    detail: str
    #: The held call rendered for a human to read.
    proposed_action: str
    #: The same call as data. ``proposed_action`` is for a coordinator's eyes;
    #: this is what a resolution path acts on, so that answering an escalation
    #: works off the load actually held rather than off a hard-coded special
    #: case. Empty only for escalations raised without a tool call to judge.
    proposed_args: dict[str, Any] = Field(default_factory=dict)
    #: Who offered the food. Carried separately because the donor is a property
    #: of the offer, not of the arguments the model chose.
    donor_name: str | None = None

    @property
    def pantry_id(self) -> str | None:
        value = self.proposed_args.get("pantry_id")
        return str(value) if value else None

    @property
    def held_lbs(self) -> float | None:
        try:
            return float(self.proposed_args["quantity_lbs"])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def storage(self) -> str | None:
        value = self.proposed_args.get("storage")
        return str(value) if value else None

    @property
    def hours_until_unusable(self) -> float | None:
        try:
            return float(self.proposed_args["hours_until_unusable"])
        except (KeyError, TypeError, ValueError):
            return None
