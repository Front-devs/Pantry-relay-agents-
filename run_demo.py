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

from pantryrelay.config import load_dotenv  # noqa: E402

# Before anything reads AWS settings. The README tells a reader to copy
# .env.example to .env, so .env has to actually be loaded.
load_dotenv()

from pantryrelay import CoordinatorGate, route_offer  # noqa: E402
from pantryrelay.data import LEDGER, OUTBOX, PANTRIES, reset  # noqa: E402
from pantryrelay.fixtures import MORNING, SAMPLE_FILES  # noqa: E402
from pantryrelay.resolution import (  # noqa: E402
    CHOICE_LABELS,
    apply_resolution,
    choices_for,
    coerce_choice,
)

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


def _render(result: dict) -> str:
    """One line of terminal output for one thing a coordinator's answer did."""
    if result["outcome"] == "resolved":
        note = result["note"]
        tail = " (split load)" if "primary" in note else (
            " (split spillover)" if "spillover" in note else (
                f" ({note})" if "overflow" in note else " (authorised)"
            )
        )
        return (
            f"  {GREEN}resolved{RESET}  {result['lbs']:.0f} lbs → "
            f"{result['pantry_name']}{tail}"
        )
    colour = RED
    label = "declined" if result["outcome"] == "declined" else "stuck   "
    return f"  {colour}{label}{RESET}  {result['summary']} — {result['note']}"


def resolve_escalations(gate, resolution: str) -> None:
    """Apply a coordinator's verdict to every held escalation."""
    if not gate.escalations:
        return

    print(f"\n{BOLD}Applying Coordinator Decision:{RESET} {resolution.upper()}")
    for esc in list(gate.escalations):
        for result in apply_resolution(esc, coerce_choice(esc, resolution)):
            print(_render(result))
    gate.escalations.clear()


def handle_waiting_resolution(gate, *, interactive: bool = False, resolve: str | None = None) -> None:
    if not gate.escalations:
        return
    show_waiting(gate)
    choice = resolve
    if not choice and interactive:
        # Options are built from the first hold, so the prompt describes the
        # decision actually waiting rather than a fixed menu.
        options = choices_for(gate.escalations[0])
        print()
        print(f"{YELLOW}{BOLD}Human-in-the-Loop Decision Required:{RESET}")
        for i, option in enumerate(options, start=1):
            print(f"  [{i}] {CHOICE_LABELS[option]}")
        try:
            val = input(f"{BOLD}Select action [1-{len(options)}, default {len(options)}]: {RESET}").strip()
            index = int(val) - 1 if val.isdigit() else len(options) - 1
            choice = options[index] if 0 <= index < len(options) else options[-1]
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
        choices=["overflow", "split", "approve", "decline"],
        default=None,
        help=(
            "pre-select the coordinator decision. overflow and split apply to holds "
            "about space; approve and decline apply to the rest. A decision that does "
            "not fit a given hold degrades to the nearest one that does."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether this machine can reach Bedrock, and stop",
    )
    args = parser.parse_args()

    if args.check:
        return run_check()
    if args.live:
        return run_live_guarded(quiet=args.quiet, interactive=args.interactive, resolve=args.resolve)
    return run_offline(quiet=args.quiet, interactive=args.interactive, resolve=args.resolve)


def run_check() -> int:
    """Say whether `--live` would work, and if not, exactly what to fix.

    Worth its own command because the five reasons Bedrock says no need five
    different fixes, and finding out which during a demo is too late.
    """
    from pantryrelay.config import preflight, working_models

    print()
    print(f"{BOLD}PantryRelay{RESET} {DIM}— live readiness check{RESET}")
    print(rule())

    result = preflight()
    print(f"  region    {result.region}")
    print(f"  model     {result.model_id}")
    if result.account:
        print(f"  account   {result.account}")
    if result.arn:
        # Naming the identity matters because root is itself a cause of
        # refusal, and "account 9056..." hides which principal signed the call.
        caller = "root" if result.arn.endswith(":root") else result.arn.rsplit("/", 1)[-1]
        print(f"  identity  {caller}")

    if result:
        print()
        print(f"{GREEN}Bedrock answered. `py -3.14 run_demo.py --live` will run.{RESET}")
        print()
        return 0

    print()
    print(f"{YELLOW}Not ready: {result.problem}.{RESET}")
    print(f"{DIM}{result.remedy}{RESET}")

    # Only worth offering a different model when the model is the problem. If
    # the whole account is refused, every candidate fails and the list is noise.
    model_is_the_problem = (
        result.code in {"AccessDeniedException", "ResourceNotFoundException"}
        and "activating" not in result.problem
    )
    if model_is_the_problem:
        usable = working_models()
        if usable:
            print(f"{DIM}Models this account can call: {', '.join(usable)}{RESET}")
            print(f"{DIM}Put one in .env as PANTRYRELAY_MODEL_ID.{RESET}")

    print(f"{DIM}The offline demo needs none of this: py -3.14 run_demo.py{RESET}")
    print()
    return 2


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
                    "ExpiredTokenException", "InvalidSignatureException",
                    "ThrottlingException"}:
            from pantryrelay.config import explain_client_error
            return bail(*explain_client_error(exc))
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
