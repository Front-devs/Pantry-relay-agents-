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
  Storage class, weight and remaining life are properties of the food, not of
  the sentence the model wrote, so a call that disagrees with the offer is
  judged on the offer's terms. Otherwise a model could pick its arguments to
  miss a check while the real-world action stayed exactly as consequential.

And one rule keeps it honest about *who* it is judging for:

* A coordinator's answer belongs to the commitment it was given for. Strands
  derives the interrupt id from the model's own tool-use id, so anything reusing
  that id inherits the standing answer; the gate refuses to spend one yes on a
  second, different commitment.

``decide()`` is the single entry point for policy. ``before_tool_call`` is a thin
translation of its verdict into Strands actions, and ``routing.py`` calls the
same method, so the offline demo and the live agent cannot drift apart.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import weakref
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

#: Offers are written by people with clipboards, so weights are rounded ("about
#: four hundred pounds"). A declared weight counts as one the offer states when
#: it is within this much of it: wide enough for an honest rounding, far too
#: narrow to hide a load in.
_WEIGHT_TOLERANCE = 0.02
_WEIGHT_FLOOR_LBS = 1.0


def _same_weight(claimed: float, stated: float) -> bool:
    """Is a declared weight the offer's own figure, allowing for rounding?"""
    return abs(claimed - stated) <= max(_WEIGHT_FLOOR_LBS, _WEIGHT_TOLERANCE * abs(stated))


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
        # Bookings this gate has judged, keyed by exactly what was proposed:
        # (offer, pantry, lbs, storage) -> "allowed" | "held". A coordinator is
        # only told food is coming if this run judged *this* load into *that*
        # pantry and a ledger row matching it landed; a stale row from an
        # earlier offer, or a booking still sitting in a human's queue, is not
        # permission to send a message about it.
        self._proposed: dict[tuple[str, str, float, str], str] = {}
        # Bookings that actually happened, as the tool itself reported them —
        # one entry per booking, so the same load promised twice is two entries.
        # A proposal is what the model asked for; this is what the world did.
        self._confirmed: list[tuple[str, str, float, str]] = []
        # What each held decision was actually about, keyed by the agent and the
        # model's tool-use id. Strands builds the interrupt id out of that same
        # tool-use id, so a coordinator's answer is addressable by anything that
        # reuses it; this is what stops one yes covering a second commitment.
        self._confirmations: dict[tuple[Any, Any], tuple[Any, ...]] = {}
        # Stable per-agent identity. id() is recycled once an agent is collected
        # and run_demo rebinds a router per offer, so an id alone could quietly
        # merge two agents' decisions into one.
        self._agent_tokens: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
        self._next_agent_token = itertools.count(1)

    # -- Strands lifecycle -------------------------------------------------

    def before_tool_call(self, event: Any) -> Any:
        # Read defensively: everything here is shaped by the model. A tool input
        # that is not an object at all reaches this handler as a list or a
        # string, and a handler that raises takes the whole run down with it —
        # fail-closed on the tool, but the morning stops. Hand it to decide() as
        # it arrived and let the policy refuse it.
        tool_use = getattr(event, "tool_use", None)
        tool_use = tool_use if isinstance(tool_use, dict) else {}
        args = tool_use.get("input")
        if args is None:
            args = {}
        state = getattr(event, "invocation_state", None)
        offer = state.get("offer") if isinstance(state, dict) else None

        verdict = self.decide(tool_name=tool_use.get("name"), args=args, offer=offer)

        if verdict.action == "allow":
            return Proceed(reason=verdict.reason)
        if verdict.action == "deny":
            return Deny(reason=verdict.reason or "refused by the coordinator gate")

        escalation = verdict.escalation
        assert escalation is not None  # an escalate verdict always carries one

        token = self._agent_token(getattr(event, "agent", None))
        tool_use_id = tool_use.get("toolUseId")
        stale = self._answer_already_spoken_for(
            token, tool_use_id, tool_use.get("name"), args, escalation
        )
        if stale is not None:
            return Deny(reason=stale)

        key = self._dashboard_key(token, tool_use_id, escalation)
        if key not in self._held:
            self._held.add(key)
            self.escalations.append(escalation)
        return Confirm(prompt=self.format_prompt(escalation), reason=escalation.reason_code)

    def after_tool_call(self, event: Any) -> Any:
        """Record what the world actually did, from the tool's own result.

        ``before_tool_call`` only ever sees a proposal. Whether the commitment
        was really made is a fact about the world, and the honest witness to it
        is the result ``reserve_pickup`` returned — not the model's next
        sentence, and not a ledger row that any earlier offer could have
        written. Two things depend on knowing the difference: an announcement
        may only follow a booking that happened, and a load may only be placed
        as many times as the donor actually has it.

        Anything else driving these tools — ``routing.py`` does, for the offline
        demo — must call ``note_booking_made`` itself, for the same reason.
        """
        tool_use = getattr(event, "tool_use", None)
        tool_use = tool_use if isinstance(tool_use, dict) else {}
        args = tool_use.get("input")
        state = getattr(event, "invocation_state", None)
        offer = state.get("offer") if isinstance(state, dict) else None

        if (
            tool_use.get("name") == "reserve_pickup"
            and isinstance(args, dict)
            and isinstance(offer, DonationOffer)
            and getattr(event, "cancel_message", None) is None
            and getattr(event, "exception", None) is None
        ):
            self.note_booking_made(
                args=args, offer=offer, result=getattr(event, "result", None)
            )
        return Proceed(reason="nothing left to guard once the tool has run")

    def note_booking_made(
        self, *, args: dict[str, Any], offer: DonationOffer, result: Any
    ) -> bool:
        """Record a booking the tool really made. Returns whether it counted.

        Both paths call this — the live agent through ``after_tool_call``, the
        deterministic router straight after it invokes the tool — so the two
        cannot disagree about what has been committed.
        """
        if not self._booking_succeeded(result):
            return False
        self._confirmed.append(self._booking_key(args, offer))
        return True

    @staticmethod
    def _booking_succeeded(result: Any) -> bool:
        """Did reserve_pickup actually book, by its own account?

        Accepts the tool's return value directly or wrapped in a Strands
        ToolResult, because the two paths see different shapes of the same fact.
        An error return — no capacity, no such pantry — is not a booking.
        """
        payload: Any = result
        if isinstance(result, dict) and "content" in result:
            if result.get("status") not in (None, "success"):
                return False
            payload = None
            for block in result.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("json"), dict):
                    payload = block["json"]
                    break
                if isinstance(block.get("text"), str):
                    try:
                        decoded = json.loads(block["text"])
                    except ValueError:
                        continue
                    if isinstance(decoded, dict):
                        payload = decoded
                        break
        return bool(isinstance(payload, dict) and payload.get("booked"))

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
        # Arguments the gate cannot read are arguments it cannot judge. The
        # model streams these as JSON, and JSON that parses to a list or a
        # string is not a call any policy here can weigh.
        if not isinstance(args, dict):
            return Verdict(
                "deny",
                reason=(
                    f"tool arguments arrived as {type(args).__name__}, not an object; the "
                    "coordinator gate cannot judge a call whose arguments it cannot read"
                ),
            )

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

        overcommitted = self.overcommitment_reason(tool_name=tool_name, args=args, offer=offer)
        if overcommitted is not None:
            return Verdict("deny", reason=overcommitted)

        if tool_name == "notify_pantry_coordinator" and not self.is_backed_by_booking(args, offer):
            return Verdict("deny", reason=self.unbacked_message_reason(args, offer))

        escalation = self.assess(tool_name=tool_name, args=args, offer=offer)
        if escalation is not None:
            # A commitment a human still owns is *not* a commitment. It is
            # recorded so that, if the coordinator says yes and the booking
            # goes through, the message that follows can be matched to it — and
            # as nothing more, because only a booking the tool actually reports
            # making redeems it.
            if tool_name == "reserve_pickup":
                self._proposed.setdefault(self._booking_key(args, offer), "held")
            return Verdict("escalate", reason=escalation.reason_code, escalation=escalation)

        # Registered only now, once every check has passed: a booking the gate
        # refused or held must not read as one it proposed.
        if tool_name == "reserve_pickup":
            self._proposed[self._booking_key(args, offer)] = "allowed"

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
        # Every escalation below carries the held call twice: once rendered for
        # a coordinator to read, once as data so a resolution path can act on
        # the load that was actually held instead of recognising it by name.
        held = {
            "proposed_action": proposed,
            "proposed_args": dict(args),
            "donor_name": offer.donor_name if offer is not None else None,
        }

        if offer is not None and offer.extraction_confidence < self.min_confidence:
            return Escalation(
                reason_code="low_confidence_extraction",
                summary=f"Offer from {offer.donor_name} was hard to read",
                detail=(
                    f"Extraction confidence {offer.extraction_confidence:.0%}, below the "
                    f"{self.min_confidence:.0%} bar. Unresolved: "
                    + ("; ".join(offer.ambiguities) if offer.ambiguities else "not specified")
                ),
                **held,
            )

        if offer is not None and offer.needs_same_day_answer:
            return Escalation(
                reason_code="same_day_commitment",
                summary=f"{offer.donor_name} needs a same-day yes or no",
                detail=(
                    "The donor asked for a commitment today. Promising collection on the "
                    "day binds volunteer time the agent cannot see."
                ),
                **held,
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
                    **held,
                )

            declared = _to_float(args.get("quantity_lbs"), default=0.0) or 0.0
            # How much food is actually moving is a property of the offer, not
            # of the number in the call. Shaving it is the cheapest way past the
            # capacity check: the pantry has room for what the call admits to,
            # and the truck arrives with the load the donor described.
            committed = self.authoritative_lbs(args, offer)
            if committed is None:
                committed = declared

            if committed > declared:
                return Escalation(
                    reason_code="storage_conflict",
                    summary=f"Only {declared:.0f} of {committed:.0f} lbs would be booked",
                    detail=(
                        f"The offer from {offer.donor_name} lists {committed:.0f} lbs of "
                        f"{storage} goods, but the booking commits {declared:.0f} lbs at "
                        f"{pantry.name}, leaving {committed - declared:.0f} lbs with nowhere "
                        "to go. Splitting a load, or placing part of it and leaving the "
                        "rest with the donor, is a coordinator's call."
                    ),
                    **held,
                )

            free = pantry.capacity_for(storage)
            if committed > free:
                return Escalation(
                    reason_code="storage_conflict",
                    summary=f"{pantry.name} cannot hold this load",
                    detail=(
                        f"Needs {committed:.0f} lbs of {storage} space but only "
                        f"{free:.0f} lbs is free — short by {committed - free:.0f} lbs. "
                        "Splitting the load or bumping an existing booking is a "
                        "coordinator's call."
                    ),
                    **held,
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
                        **held,
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

    def overcommitment_reason(
        self, *, tool_name: str | None, args: dict[str, Any], offer: DonationOffer
    ) -> str | None:
        """Why this booking commits food the offer does not have, or None.

        A load can only be placed once, and nothing inside a single call reveals
        that the same pallet was already promised somewhere else. So the gate
        keeps the sum: what this offer has already booked, plus what this call
        adds, cannot exceed what the donor actually offered in that storage
        class.

        Without it one 220 lb load books 220 lbs at two pantries — 440 lbs of
        capacity consumed by a network that was given 220 lbs of food, and two
        coordinators clearing space for a delivery only one of them will get.
        Both calls pass every other check, because each is individually
        reasonable; only the running total says otherwise.

        This is a denial rather than an escalation. Splitting a load between two
        pantries is a coordinator's decision, but promising the same food to
        both is not a decision at all — it is a booking that cannot be honoured.
        """
        if tool_name != "reserve_pickup":
            return None

        storage = str(args.get("storage"))
        lines = [item.quantity_lbs for item in offer.items if item.storage == storage]
        if not lines:
            # The offer says nothing about this class, so there is no figure to
            # conserve. Other checks still apply; this one has no evidence.
            return None

        # This check is only as good as the gate's picture of what has already
        # been booked, and that picture is kept by whoever drives the tools. If
        # the ledger has grown behind the gate's back it cannot tell how much of
        # this offer is already placed, and guessing low would let exactly the
        # double commitment above through. Refusing is the honest answer, and it
        # makes forgetting loud instead of silent.
        unreported = len(LEDGER) - len(self._confirmed)
        if unreported > 0:
            return (
                f"{unreported} booking(s) have been made that the coordinator gate was not "
                "told about, so it cannot tell how much of this offer is already placed; "
                "whatever calls reserve_pickup must report the result through "
                "note_booking_made, as after_tool_call and route_offer both do"
            )

        available = math.fsum(lines)
        offer_key = self._offer_key(offer)
        booked = math.fsum(
            key[2] for key in self._confirmed if key[0] == offer_key and key[3] == storage
        )
        declared = _to_float(args.get("quantity_lbs"), default=0.0) or 0.0
        slack = max(_WEIGHT_FLOOR_LBS, _WEIGHT_TOLERANCE * available)
        if booked + declared <= available + slack:
            return None
        return (
            f"{offer.donor_name} offered {available:.0f} lbs of {storage} goods and "
            f"{booked:.0f} lbs of it is already booked on this offer; committing "
            f"{declared:.0f} lbs more would promise food the donor does not have"
        )

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

    @staticmethod
    def authoritative_lbs(args: dict[str, Any], offer: DonationOffer | None) -> float | None:
        """How much food is really being committed, not how much the call admits to.

        The mirror image of ``authoritative_hours``. There the shorter window
        wins; here the *larger* weight does, because for weight it is the bigger
        number that is the careful one — it books more space, not less, and
        makes every downstream check stricter rather than looser.

        A declared weight is taken at face value when the offer can account for
        it: one of the offer's own lines in that storage class, the class total,
        or more than the class total. Anything short of that is a split the
        offer does not describe, and the class total is what is really moving.

        Returns None only when the offer says nothing about this storage class —
        the one case where the call is the only witness there is.
        """
        declared = _to_float(args.get("quantity_lbs"), default=None)
        if declared is None or offer is None:
            return declared

        storage = args.get("storage", "ambient")
        lines = [item.quantity_lbs for item in offer.items if item.storage == storage]
        if not lines:
            return None

        total = math.fsum(lines)
        if declared >= total or _same_weight(declared, total):
            return declared
        # Booking one line of a multi-line offer is ordinary; the router places
        # an offer item by item and says so in the call.
        if any(_same_weight(declared, line) for line in lines):
            return declared
        return total

    def is_backed_by_booking(self, args: dict[str, Any], offer: DonationOffer) -> bool:
        """Has this donor's food actually been booked into this pantry, on this offer?

        The router is told to reserve before it announces, but an instruction in
        a system prompt is a suggestion. This makes the ordering an interlock:
        the message only goes out if a booking for this offer at this pantry
        actually happened — the tool said so — and a ledger row for that exact
        load, same donor, same pantry, same weight, same storage, is there.

        Matching on donor and pantry alone was not enough. The same donor gives
        twice in a morning, and the earlier row is real; it said nothing about
        the load this message describes. Nor is a booking still sitting in a
        coordinator's queue permission to announce it: a held entry is only
        redeemed by a real row, which exists only if a human said yes and the
        tool then ran. A zero-pound placeholder buys nothing either.
        """
        pantry_id = args.get("pantry_id")
        for entry in LEDGER:
            if entry["pantry_id"] != pantry_id or entry["donor"] != offer.donor_name:
                continue
            if not (_to_float(entry.get("lbs"), default=0.0) or 0.0) > 0:
                continue
            key = self._booking_key(
                {
                    "pantry_id": entry["pantry_id"],
                    "quantity_lbs": entry.get("lbs"),
                    "storage": entry.get("storage"),
                },
                offer,
            )
            # Judged here *and* made out there. The two records are kept for
            # different reasons and a message needs both: a booking this gate
            # never weighed is not one it can vouch for, however real the row,
            # and a booking it weighed but that never happened is not a
            # delivery.
            if key in self._proposed and key in self._confirmed:
                return True
        return False

    def unbacked_message_reason(self, args: dict[str, Any], offer: DonationOffer) -> str:
        """Why this announcement is refused — named precisely, for the model."""
        pantry_id = args.get("pantry_id")
        held = [
            key
            for key, status in self._proposed.items()
            if status == "held" and key[0] == self._offer_key(offer) and key[1] == str(pantry_id)
        ]
        if held:
            return (
                f"the booking of {held[0][2]:g} lbs for {offer.donor_name} at pantry "
                f"{pantry_id!r} is still held for a coordinator; a decision waiting on a "
                "human is not a delivery to announce"
            )
        return (
            f"there is no booking for {offer.donor_name} at pantry {pantry_id!r} on this "
            "offer; reserve_pickup must succeed before a coordinator is told the food is "
            "coming"
        )

    # -- Bookkeeping -------------------------------------------------------

    @staticmethod
    def _offer_key(offer: DonationOffer) -> str:
        """A stable identity for one offer, so permissions cannot leak between them."""
        return hashlib.sha256(offer.model_dump_json().encode("utf-8")).hexdigest()

    @classmethod
    def _booking_key(
        cls, args: dict[str, Any], offer: DonationOffer
    ) -> tuple[str, str, float, str]:
        """Identity of one booking: which offer, which pantry, how much, how cold.

        Deliberately the whole load and not just its address. Two deliveries
        from one donor to one pantry in a morning are two different commitments,
        and only the one that actually happened may be announced.
        """
        return (
            cls._offer_key(offer),
            str(args.get("pantry_id")),
            round(_to_float(args.get("quantity_lbs"), default=0.0) or 0.0, 3),
            str(args.get("storage")),
        )

    def _agent_token(self, agent: Any) -> Any:
        """A stable identity for one deciding agent.

        ``id()`` is recycled the moment an agent is collected, and run_demo
        rebinds a router per offer against a shared gate, so an id alone can
        silently hand one agent's held decisions to its successor.
        """
        if agent is None:
            return None
        try:
            token = self._agent_tokens.get(agent)
            if token is None:
                token = next(self._next_agent_token)
                self._agent_tokens[agent] = token
            return token
        except TypeError:  # not weak-referenceable or not hashable
            return id(agent)

    def _answer_already_spoken_for(
        self,
        agent_token: Any,
        tool_use_id: Any,
        tool_name: str | None,
        args: dict[str, Any],
        escalation: Escalation,
    ) -> str | None:
        """Why this held call may not ride on an answer already given, or None.

        Strands derives the interrupt id from the model's own tool-use id, so a
        coordinator's answer is addressable by anything that reuses that id. A
        model that puts two different commitments under one id is asking a yes
        given for the first to cover the second, which no human ever saw: the
        gate escalates, the dashboard shows one card, and two tools run.

        The legitimate re-entry — the same call, replayed when the loop resumes
        — carries the same commitment, so it matches and passes through.
        """
        key = (agent_token, tool_use_id)
        commitment = (
            tool_name,
            escalation.reason_code,
            tuple(sorted((str(k), repr(v)) for k, v in args.items())),
        )
        held = self._confirmations.setdefault(key, commitment)
        if held == commitment:
            return None
        return (
            f"tool-use id {tool_use_id!r} is already holding a different commitment for a "
            f"coordinator ({held[0]}, {held[1]}); an answer covers the call it was asked "
            "about, so this one needs a tool-use id of its own"
        )

    @staticmethod
    def _dashboard_key(
        agent_token: Any, tool_use_id: Any, escalation: Escalation
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
            agent_token,
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
