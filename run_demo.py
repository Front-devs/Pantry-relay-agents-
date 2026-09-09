#!/usr/bin/env python
"""A chaotic Tuesday morning at a food bank, in about ninety seconds.

Six donation offers land across three channels. Five are routed without anyone
being interrupted. One is held for a coordinator, because it is a decision a
person should make.

    python run_demo.py              # offline: no AWS credentials needed
    python run_demo.py --live       # full agent run against Bedrock
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

# Windows consoles still default to cp1252, which cannot render the box drawing
# below. Reconfiguring is cheaper than stripping the output down to ASCII.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

from pantryrelay import CoordinatorGate, route_offer  # noqa: E402
from pantryrelay.data import LEDGER, OUTBOX, PANTRIES, reset  # noqa: E402
from pantryrelay.fixtures import MORNING, SAMPLE_FILES  # noqa: E402

SAMPLES_DIR = Path(__file__).parent / "samples"

DIM, BOLD, GREEN, YELLOW, RED, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)


def rule(char: str = "─", width: int = 74) -> str:
    return DIM + char * width + RESET


def show_source(path: Path) -> None:
    raw = path.read_text(encoding="utf-8").strip()
    head = raw.splitlines()[0]
    body = "\n".join(raw.splitlines()[1:]).strip()
    print(f"{DIM}  source  {head}{RESET}")
    for line in textwrap.wrap(body, 68)[:4]:
        print(f"{DIM}          {line}{RESET}")
    if len(textwrap.wrap(body, 68)) > 4:
        print(f"{DIM}          …{RESET}")


def show_waiting(gate) -> None:
    """The coordinator's queue at the end of a run — same shape either path."""
    if not gate.escalations:
        return
    print()
    print(f"{YELLOW}{BOLD}Waiting on a coordinator{RESET}")
    for esc in gate.escalations:
        print(rule())
        print(f"  {BOLD}{esc.summary}{RESET}  {DIM}[{esc.reason_code}]{RESET}")
        for line in textwrap.wrap(esc.detail, 68):
            print(f"  {line}")
        print(f"  {DIM}proposed: {esc.proposed_action[:120]}{RESET}")
    print(rule())


def resolve_escalations(gate, resolution: str) -> None:
    """Apply a human coordinator's verdict to held escalations."""
    if not gate.escalations:
        return

    from pantryrelay.data import get_pantry, record_booking, record_message

    print(f"\n{BOLD}Applying Coordinator Decision:{RESET} {resolution.upper()}")
    for esc in list(gate.escalations):
        if esc.reason_code == "storage_conflict":
            riverside = get_pantry("riverside")
            stjohns = get_pantry("stjohns")
            if resolution == "overflow":
                riverside.free_lbs["frozen"] = 0.0
                booking = {
                    "pantry_id": "riverside",
                    "pantry_name": riverside.name,
                    "donor": "Cold Storage (name inaudible)",
                    "lbs": 900.0,
                    "storage": "frozen",
                    "hours_until_unusable": 48.0,
                    "rationale": "Coordinator approved emergency overflow staging (+100 lbs authorized)",
                }
                record_booking(booking)
                record_message({
                    "pantry_id": "riverside",
                    "to": riverside.contact,
                    "message": "URGENT: 900 lbs frozen protein inbound. Coordinator authorized emergency overflow staging.",
                })
                print(f"  {GREEN}resolved{RESET}  900 lbs → Riverside Meals Program (emergency overflow authorized)")
            elif resolution == "split":
                riverside.free_lbs["frozen"] = 0.0
                stjohns.free_lbs["frozen"] = max(0.0, stjohns.free_lbs.get("frozen", 0.0) - 100.0)
                record_booking({
                    "pantry_id": "riverside",
                    "pantry_name": riverside.name,
                    "donor": "Cold Storage (name inaudible)",
                    "lbs": 800.0,
                    "storage": "frozen",
                    "hours_until_unusable": 48.0,
                    "rationale": "Coordinator authorized load split: primary allocation",
                })
                record_booking({
                    "pantry_id": "stjohns",
                    "pantry_name": stjohns.name,
                    "donor": "Cold Storage (name inaudible)",
                    "lbs": 100.0,
                    "storage": "frozen",
                    "hours_until_unusable": 48.0,
                    "rationale": "Coordinator authorized load split: spillover allocation",
                })
                record_message({
                    "pantry_id": "riverside",
                    "to": riverside.contact,
                    "message": "800 lbs frozen protein inbound (split lot). Remaining 100 lbs routed to St John's.",
                })
                record_message({
                    "pantry_id": "stjohns",
                    "to": stjohns.contact,
                    "message": "100 lbs frozen protein inbound (split lot from Cold Storage).",
                })
                print(f"  {GREEN}resolved{RESET}  800 lbs → Riverside Meals Program (split load)")
                print(f"  {GREEN}resolved{RESET}  100 lbs → St John's Community Pantry (split spillover)")
            elif resolution == "decline":
                print(f"  {RED}declined{RESET}  Offer held in limbo; coordinator will follow up with donor.")
    gate.escalations.clear()


def handle_waiting_resolution(gate, *, interactive: bool = False, resolve: str | None = None) -> None:
    if not gate.escalations:
        return
    show_waiting(gate)
    choice = resolve
    if not choice and interactive:
        print()
        print(f"{YELLOW}{BOLD}Human-in-the-Loop Decision Required:{RESET}")
        print("  [1] Authorize Emergency Overflow (+100 lbs temporary staging at Riverside)")
        print("  [2] Split the Load (800 lbs to Riverside, 100 lbs to St John's)")
        print("  [3] Keep on hold / Decline")
        try:
            val = input(f"{BOLD}Select action [1-3, default 3]: {RESET}").strip()
            if val == "1":
                choice = "overflow"
            elif val == "2":
                choice = "split"
            elif val == "3":
                choice = "decline"
        except (EOFError, KeyboardInterrupt):
            choice = None

    if choice:
        resolve_escalations(gate, choice)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="run the real agents against Bedrock instead of the offline policy",
    )
    parser.add_argument("--quiet", action="store_true", help="skip the raw sources")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="interactively prompt the coordinator to resolve held decisions",
    )
    parser.add_argument(
        "--resolve",
        choices=["overflow", "split", "decline"],
        default=None,
        help="pre-select coordinator decision for held decisions (overflow, split, decline)",
    )
    args = parser.parse_args()

    if args.live:
        return run_live_guarded(quiet=args.quiet, interactive=args.interactive, resolve=args.resolve)
    return run_offline(quiet=args.quiet, interactive=args.interactive, resolve=args.resolve)


def run_live_guarded(*, quiet: bool = False, interactive: bool = False, resolve: str | None = None) -> int:
    """`--live` needs AWS. Say so in one line rather than in a stack trace.

    Only the ways a machine can be un-set-up are caught here — missing keys, no
    region, a model the account cannot call. A real bug still raises, because a
    demo that swallows its own errors is worse than one that crashes.
    """
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        NoRegionError,
    )

    def bail(problem: str, remedy: str) -> int:
        print(f"\n{YELLOW}--live needs AWS, and {problem}.{RESET}")
        print(f"{DIM}{remedy}{RESET}")
        print(f"{DIM}The offline demo needs no credentials and shows the same "
              f"gate: py -3.14 run_demo.py{RESET}\n")
        return 2

    try:
        return run_live(quiet=quiet, interactive=interactive, resolve=resolve)
    except (NoCredentialsError, NoRegionError) as exc:
        return bail(
            "this machine is not configured for it",
            f"{exc}  —  see .env.example, or run `aws configure`.",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AccessDeniedException", "UnrecognizedClientException",
                    "ValidationException", "ResourceNotFoundException",
                    "ExpiredTokenException", "InvalidSignatureException"}:
            from pantryrelay.agent import DEFAULT_MODEL_ID, DEFAULT_REGION
            return bail(
                f"Bedrock refused the call ({code})",
                f"Model {DEFAULT_MODEL_ID!r} in {DEFAULT_REGION!r} may not be "
                f"enabled for this account. Override with PANTRYRELAY_MODEL_ID "
                f"/ AWS_REGION.",
            )
        raise
    except BotoCoreError as exc:
        return bail("AWS could not be reached", str(exc))


def run_offline(*, quiet: bool = False, interactive: bool = False, resolve: str | None = None) -> int:
    reset()
    gate = CoordinatorGate()

    print()
    print(f"{BOLD}PantryRelay{RESET} {DIM}— Tuesday, 08:41{RESET}")
    print(rule())

    outcomes = []
    for offer, filename in zip(MORNING, SAMPLE_FILES):
        path = SAMPLES_DIR / filename
        print(f"\n{BOLD}{offer.donor_name}{RESET} {DIM}via {offer.channel}{RESET}")
        if not quiet and path.exists():
            show_source(path)

        outcome = route_offer(offer, gate)
        outcomes.append(outcome)

        for booking in outcome.booked:
            print(
                f"  {GREEN}routed{RESET}  {booking['lbs']:.0f} lbs → "
                f"{booking['pantry_name']}  {DIM}({booking['rationale']}){RESET}"
            )
        for note in outcome.unplaceable:
            print(f"  {RED}stuck{RESET}   {note}")
        for esc in outcome.escalations:
            print(f"  {YELLOW}held{RESET}    {esc.summary}")

    print()
    print(rule("═"))
    handled = sum(1 for o in outcomes if not o.escalated and o.booked)
    print(
        f"{BOLD}{handled} of {len(outcomes)} offers routed without interrupting anyone.{RESET}"
    )
    print(f"{DIM}{len(LEDGER)} bookings, {len(OUTBOX)} coordinator messages sent.{RESET}")

    handle_waiting_resolution(gate, interactive=interactive, resolve=resolve)

    print()
    print(f"{BOLD}Pantry capacity after the morning{RESET}")
    for pantry in PANTRIES:
        parts = ", ".join(
            f"{k} {v:.0f}" for k, v in sorted(pantry.free_lbs.items()) if v or k == "ambient"
        )
        print(f"  {pantry.name:<32} {DIM}{parts} lbs free{RESET}")
    print()
    return 0


def run_live(*, quiet: bool = False, interactive: bool = False, resolve: str | None = None) -> int:
    """Full path: reader agent parses each source, router agent places it."""
    from pantryrelay.agent import build_model, build_reader, build_router, read_offer

    reset()
    model = build_model()
    reader = build_reader(model)
    gate = CoordinatorGate()

    print()
    print(f"{BOLD}PantryRelay{RESET} {DIM}— live, via Bedrock{RESET}")
    print(rule())

    held = 0
    routed = 0
    for filename in SAMPLE_FILES:
        path = SAMPLES_DIR / filename
        raw = path.read_text(encoding="utf-8")

        offer = read_offer(raw, reader=reader)
        print(f"\n{BOLD}{offer.donor_name}{RESET} {DIM}via {offer.channel} — "
              f"{offer.total_lbs:.0f} lbs, read with {offer.extraction_confidence:.0%} "
              f"confidence{RESET}")
        if not quiet:
            show_source(path)

        # A fresh router per offer: each one is its own decision, and an offer
        # that stops for a human leaves its agent mid-loop. The gate is shared,
        # so the coordinator's queue still accumulates across the morning.
        router, _ = build_router(model, gate)
        booked_before, sent_before = len(LEDGER), len(OUTBOX)
        escalated_before = len(gate.escalations)

        result = router(
            f"Place this donation offer:\n\n{offer.model_dump_json(indent=2)}",
            invocation_state={"offer": offer},
        )

        for booking in LEDGER[booked_before:]:
            print(
                f"  {GREEN}routed{RESET}  {booking['lbs']:.0f} lbs → "
                f"{booking['pantry_name']}  {DIM}({booking['rationale']}){RESET}"
            )
        if result.stop_reason == "interrupt":
            held += 1
            # The gate's own escalation carries the human-readable summary; the
            # interrupt only carries the reason_code slug it was tagged with.
            for esc in gate.escalations[escalated_before:]:
                print(f"  {YELLOW}held{RESET}    {esc.summary}")
        elif len(LEDGER) > booked_before:
            routed += 1
        else:
            print(f"  {DIM}nothing booked{RESET}")
        if len(OUTBOX) > sent_before:
            print(f"  {DIM}coordinator notified{RESET}")

    print()
    print(rule("═"))
    print(
        f"{BOLD}{routed} of {len(SAMPLE_FILES)} offers routed without "
        f"interrupting anyone.{RESET}"
    )
    print(f"{DIM}{len(LEDGER)} bookings, {len(OUTBOX)} coordinator messages sent.{RESET}")

    handle_waiting_resolution(gate, interactive=interactive, resolve=resolve)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
