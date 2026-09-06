"""The coordinator gate.

PantryRelay's whole claim is that it runs quietly and only pulls in a human when
there is a real judgment call. That claim is enforced here rather than by asking
the model nicely: the gate sits on Strands' intervention lifecycle, inspects
every consequential tool call before it happens, and either lets it through or
stops the loop for a human.

Four things are judged worth a person's attention:

* the food would arrive with too little usable life left to actually distribute
* the pantry cannot physically hold what is being sent
* the donor is owed a same-day commitment the agent should not make alone
* the offer was read out of a source too ambiguous to trust

Everything else proceeds unattended, which is the point.

Separately from those four, the gate refuses outright — no human involved — when
a consequential call is simply malformed: a weight that is not a positive number,
an announcement to a pantry that is not backed by a booking, or a commitment the
gate cannot judge because the offer was never handed to it. Those are the agent
getting it wrong, not a coordinator's decision, so they are denied back to the
model rather than added to the queue.

Two rules keep the gate honest about *what* it is judging:

* Membership is an allow-list. Only tools named in ``READ_ONLY_TOOLS`` skip the
  gate. A new tool that changes the world is guarded from the moment it exists,
  even if somebody forgets to add it to ``CONSEQUENTIAL_TOOLS``.
* Every fact the gate judges is taken from the offer where the offer knows it.
  Storage class and remaining life are properties of the food, not of the
  sentence the model wrote, so a call that disagrees with the offer is judged on
  the offer's terms. Otherwise a model could pick its arguments to miss a check
  while the real-world action stayed exactly as consequential.

``decide()`` is the single entry point for policy. ``before_tool_call`` is a thin
translation of its verdict into Strands actions, and ``routing.py`` calls the
same method, so the offline demo and the live agent cannot drift apart.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Literal

from strands.interventions import Confirm, Deny, InterventionHandler, Proceed

from .data import LEDGER, get_pantry
from .models import DonationOffer, Escalation
from .tools import CONSEQUENTIAL_TOOLS, READ_ONLY_TOOLS

#: How cold each storage class is. Booking food into a warmer class than it needs
#: is a downgrade the gate treats as a storage conflict; an unrecognised class
#: ranks coldest-of-nothing so it fails closed rather than passing silently.
_STORAGE_RANK: dict[str, int] = {"ambient": 0, "refrigerated": 1, "frozen": 2}


@dataclass(frozen=True)
class Verdict:
    """What the gate decided about one consequential call.

    ``allow``    — run it, nobody needs to be interrupted.
    ``deny``     — refuse it outright; the call is malformed, not a judgment call.
    ``escalate`` — a human owns this one; ``escalation`` says why.
    """

    action: Literal["allow", "deny", "escalate"]
    reason: str | None = None
    escalation: Escalation | None = None

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


class CoordinatorGate(InterventionHandler):
    """Stops the agent short of any commitment a coordinator should own."""

    name = "coordinator-gate"

    def __init__(
        self,
        *,
        thin_margin_hours: float = 6.0,
        min_confidence: float = 0.75,
    ) -> None:
        self.thin_margin_hours = thin_margin_hours
        self.min_confidence = min_confidence
        # Every escalation raised during a run, for the coordinator dashboard.
        self.escalations: list[Escalation] = []
        # Decisions already on the dashboard. The gate is re-entered when a
        # coordinator answers and the loop resumes, and one held decision must
        # not appear twice because a human replied to it. Keyed on the deciding
        # agent, the tool-use id *and* what was actually proposed: tool-use ids
        # are only unique per model, and run_demo shares one gate across a
        # router per offer, so an id alone would silently drop the second
        # offer's escalation off the queue.
        self._held: set[tuple[Any, ...]] = set()
        # (offer, pantry) pairs this gate has actually judged a booking for.
        # A coordinator is only told food is coming if this run tried to book
        # that food into that pantry — a stale ledger row from an earlier offer
        # is not permission to send a message about a new one.
        self._proposed: set[tuple[str, str]] = set()

    # -- Strands lifecycle -------------------------------------------------

    def before_tool_call(self, event: Any) -> Any:
        tool_use = getattr(event, "tool_use", None) or {}
        args = tool_use.get("input") or {}
        offer = (getattr(event, "invocation_state", None) or {}).get("offer")

        verdict = self.decide(tool_name=tool_use.get("name"), args=args, offer=offer)

        if verdict.action == "allow":
            return Proceed(reason=verdict.reason)
        if verdict.action == "deny":
            return Deny(reason=verdict.reason or "refused by the coordinator gate")

        escalation = verdict.escalation
        assert escalation is not None  # an escalate verdict always carries one
        key = self._dashboard_key(
            getattr(event, "agent", None), tool_use.get("toolUseId"), escalation
        )
        if key not in self._held:
            self._held.add(key)
            self.escalations.append(escalation)
        return Confirm(prompt=self.format_prompt(escalation), reason=escalation.reason_code)

    # -- Policy ------------------------------------------------------------

    def decide(
        self,
        *,
        tool_name: str | None,
        args: dict[str, Any],
        offer: DonationOffer | None = None,
    ) -> Verdict:
        """The whole policy, in one place, free of Strands types.

        Both paths call this: ``before_tool_call`` translates the verdict into a
        Strands action, and the deterministic router in ``routing.py`` acts on it
        directly. That is why they cannot disagree.
        """
        # Allow-list, not deny-list: anything not known to be read-only is
        # treated as world-changing until it is declared otherwise.
        if tool_name in READ_ONLY_TOOLS:
            return Verdict("allow", reason="read-only lookup; nothing to guard")

        # A tool the gate has never heard of is a tool it has no checks for, and
        # "no checks fired" must not read as "safe". Refusing here is what makes
        # forgetting to classify a new tool a loud failure instead of a silent
        # hole: the tool simply does not run until somebody says which set it is
        # in. This is the one place the gate's coverage is self-enforcing.
        if tool_name not in CONSEQUENTIAL_TOOLS:
            return Verdict(
                "deny",
                reason=(
                    f"the coordinator gate has no policy for tool {tool_name!r}; it must be "
                    "listed in READ_ONLY_TOOLS or CONSEQUENTIAL_TOOLS before it can run"
                ),
            )

        # Fail closed rather than quietly judging on half the evidence: without
        # the offer the gate cannot see extraction confidence, a same-day
        # request, the real storage class or the real expiry window, so most of
        # its checks would silently never fire.
        if not isinstance(offer, DonationOffer):
            return Verdict(
                "deny",
                reason=(
                    "the gate was not given the offer this call belongs to; pass it as "
                    "invocation_state={'offer': offer} so the commitment can be judged"
                ),
            )

        malformed = self.malformed_reason(tool_name=tool_name, args=args)
        if malformed is not None:
            return Verdict("deny", reason=malformed)

        if tool_name == "reserve_pickup":
            self._proposed.add((self._offer_key(offer), str(args.get("pantry_id"))))

        if tool_name == "notify_pantry_coordinator" and not self.is_backed_by_booking(args, offer):
            return Verdict(
                "deny",
                reason=(
                    f"there is no booking for {offer.donor_name} at pantry "
                    f"{args.get('pantry_id')!r} on this offer; reserve_pickup must succeed "
                    "before a coordinator is told the food is coming"
                ),
            )

        escalation = self.assess(tool_name=tool_name, args=args, offer=offer)
        if escalation is not None:
            return Verdict("escalate", reason=escalation.reason_code, escalation=escalation)

        return Verdict("allow", reason="within routing policy; no human judgment needed")

    def assess(
        self,
        *,
        tool_name: str | None,
        args: dict[str, Any],
        offer: DonationOffer | None = None,
    ) -> Escalation | None:
        """Return an Escalation when a human should decide, else None.

        Kept free of Strands types so it can be unit-tested directly. This
        answers only "does a person own this call"; whether the call is even
        well-formed enough to judge is ``decide()``'s question.
        """
        proposed = f"{tool_name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"

        if offer is not None and offer.extraction_confidence < self.min_confidence:
            return Escalation(
                reason_code="low_confidence_extraction",
                summary=f"Offer from {offer.donor_name} was hard to read",
                detail=(
                    f"Extraction confidence {offer.extraction_confidence:.0%}, below the "
                    f"{self.min_confidence:.0%} bar. Unresolved: "
                    + ("; ".join(offer.ambiguities) if offer.ambiguities else "not specified")
                ),
                proposed_action=proposed,
            )

        if offer is not None and offer.needs_same_day_answer:
            return Escalation(
                reason_code="same_day_commitment",
                summary=f"{offer.donor_name} needs a same-day yes or no",
                detail=(
                    "The donor asked for a commitment today. Promising collection on the "
                    "day binds volunteer time the agent cannot see."
                ),
                proposed_action=proposed,
            )

        pantry_id = args.get("pantry_id")
        pantry = get_pantry(pantry_id) if pantry_id else None

        if tool_name == "reserve_pickup" and pantry is not None:
            storage = args.get("storage", "ambient")

            # What the food needs is a property of the food. A call that books
            # chilled or frozen goods onto a warm shelf would sail past the
            # capacity check below, because the warm shelf has plenty of room.
            colder = self.storage_downgrade(storage, offer) if offer is not None else None
            if colder is not None:
                return Escalation(
                    reason_code="storage_conflict",
                    summary=f"This load needs {colder} storage, not {storage}",
                    detail=(
                        f"The offer from {offer.donor_name} lists this food as {colder}, "
                        f"but the booking asks for {storage} space at {pantry.name}. "
                        f"Placing it on a {storage} shelf would spoil it. Whether the food "
                        "can safely go somewhere warmer is a coordinator's call."
                    ),
                    proposed_action=proposed,
                )

            requested = _to_float(args.get("quantity_lbs"), default=0.0)
            free = pantry.capacity_for(storage)
            if requested > free:
                return Escalation(
                    reason_code="storage_conflict",
                    summary=f"{pantry.name} cannot hold this load",
                    detail=(
                        f"Needs {requested:.0f} lbs of {storage} space but only "
                        f"{free:.0f} lbs is free — short by {requested - free:.0f} lbs. "
                        "Splitting the load or bumping an existing booking is a "
                        "coordinator's call."
                    ),
                    proposed_action=proposed,
                )

            hours = self.authoritative_hours(args, offer)
            if hours is not None:
                margin = hours - pantry.min_notice_hours
                if margin < self.thin_margin_hours:
                    return Escalation(
                        reason_code="thin_expiry_margin",
                        summary=f"Only {margin:.1f}h of usable life after handover",
                        detail=(
                            f"Food is unusable in {hours:.1f}h and {pantry.name} needs "
                            f"{pantry.min_notice_hours:.1f}h notice, leaving {margin:.1f}h to "
                            f"actually distribute it — under the {self.thin_margin_hours:.0f}h "
                            "floor. Worth a human deciding whether it is still worth the trip."
                        ),
                        proposed_action=proposed,
                    )

        return None

    # -- Individual checks -------------------------------------------------

    @staticmethod
    def malformed_reason(*, tool_name: str | None, args: dict[str, Any]) -> str | None:
        """Why this call cannot be judged at all, or None if it is well formed.

        ``reserve_pickup`` commits space. A weight that is zero, negative or not
        a number is not a commitment the gate can weigh — and a negative one is
        worse than meaningless, because the booking tool would subtract it and
        hand the pantry capacity it does not have, which is exactly how a load
        the gate refused becomes a load that fits.
        """
        if tool_name != "reserve_pickup":
            return None

        raw = args.get("quantity_lbs")
        try:
            quantity = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return (
                f"quantity_lbs={raw!r} is not a number; reserve_pickup needs a weight "
                "the gate can check against the pantry's free space"
            )
        if not math.isfinite(quantity):
            return f"quantity_lbs={raw!r} is not a finite weight"
        if quantity <= 0:
            return (
                f"quantity_lbs={quantity:g} is not a positive weight; reserve_pickup "
                "books space and cannot be used to hand capacity back"
            )

        raw_hours = args.get("hours_until_unusable")
        if raw_hours is not None:
            try:
                hours = float(raw_hours)
            except (TypeError, ValueError):
                return (
                    f"hours_until_unusable={raw_hours!r} is not a number; leave it out "
                    "rather than describing the window in words"
                )
            if not math.isfinite(hours) or hours < 0:
                return f"hours_until_unusable={raw_hours!r} is not a usable number of hours"

        return None

    @staticmethod
    def storage_downgrade(storage: Any, offer: DonationOffer) -> str | None:
        """The colder class this offer actually needs, if the call asked for less.

        Returns None when the requested class is one the offer genuinely lists,
        or when the call is being *more* careful than the food requires.
        """
        classes = {item.storage for item in offer.items}
        if not classes or storage in classes:
            return None
        coldest = max(classes, key=lambda c: _STORAGE_RANK.get(c, 0))
        if _STORAGE_RANK.get(coldest, 0) > _STORAGE_RANK.get(storage, 0):
            return coldest
        return None

    @staticmethod
    def authoritative_hours(args: dict[str, Any], offer: DonationOffer | None) -> float | None:
        """How long the food really has, not how long the call says it has.

        The model supplies ``hours_until_unusable`` itself, so on its own it is a
        claim rather than a fact — and a generous claim is the cheapest way to
        step over the thin-margin floor. Where the offer knows the window, the
        shorter of the two wins.
        """
        claimed = _to_float(args.get("hours_until_unusable"), default=None)
        actual = offer.tightest_window_hours if offer is not None else None
        if claimed is None:
            return actual
        if actual is None:
            return claimed
        return min(claimed, actual)

    def is_backed_by_booking(self, args: dict[str, Any], offer: DonationOffer) -> bool:
        """Has this donor's food actually been booked into this pantry, on this offer?

        The router is told to reserve before it announces, but an instruction in
        a system prompt is a suggestion. This makes the ordering an interlock:
        the message only goes out if this run judged a booking for this offer at
        this pantry *and* a real, positive booking for the donor landed in the
        ledger. A zero-pound placeholder buys nothing, and last week's row for
        the same donor is not permission to announce today's load.
        """
        pantry_id = args.get("pantry_id")
        if (self._offer_key(offer), str(pantry_id)) not in self._proposed:
            return False
        return any(
            entry["pantry_id"] == pantry_id
            and entry["donor"] == offer.donor_name
            and _to_float(entry.get("lbs"), default=0.0) > 0
            for entry in LEDGER
        )

    # -- Bookkeeping -------------------------------------------------------

    @staticmethod
    def _offer_key(offer: DonationOffer) -> str:
        """A stable identity for one offer, so permissions cannot leak between them."""
        return hashlib.sha256(offer.model_dump_json().encode("utf-8")).hexdigest()

    @staticmethod
    def _dashboard_key(
        agent: Any, tool_use_id: Any, escalation: Escalation
    ) -> tuple[Any, ...]:
        """Identity of one held decision on the coordinator's queue.

        A tool-use id is only unique within one model's conversation, so it
        cannot carry the whole key: run_demo builds a router per offer around a
        shared gate, and two routers happily mint the same id. Including the
        deciding agent and what was actually proposed means a resumed decision
        still collapses onto its original entry while two genuinely different
        offers stay two entries.
        """
        return (
            id(agent) if agent is not None else None,
            tool_use_id,
            escalation.reason_code,
            escalation.proposed_action,
        )

    # -- Presentation ------------------------------------------------------

    @staticmethod
    def format_prompt(escalation: Escalation) -> str:
        return (
            f"[{escalation.reason_code}] {escalation.summary}\n"
            f"{escalation.detail}\n"
            f"Proposed: {escalation.proposed_action}\n"
            f"Approve?"
        )


def _to_float(value: Any, *, default: float | None) -> float | None:
    """Coerce a model-supplied number, falling back rather than raising.

    A handler that raises takes the whole run down with it. Every value this is
    used on has already been through ``malformed_reason``, so the fallback is a
    backstop for direct ``assess()`` callers, not the primary defence.
    """
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default
