"""Agent construction: one reader, one router.

The split is deliberate. Reading a messy offer and routing it are different jobs
with different failure modes — the reader must be willing to say it is unsure,
the router must be willing to act. Keeping them apart means the reader's
uncertainty survives into the router's invocation state, where the gate can see
it, instead of being smoothed away inside a single conversation.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from .gate import CoordinatorGate
from .models import DonationOffer
from .tools import ALL_TOOLS

# Cross-region inference profile, so the demo is not pinned to one region's
# capacity. Override with PANTRYRELAY_MODEL_ID if Opus is not enabled in your
# account — global.anthropic.claude-sonnet-4-6 is the usual fallback.
DEFAULT_MODEL_ID = os.getenv("PANTRYRELAY_MODEL_ID", "global.anthropic.claude-opus-5")
DEFAULT_REGION = os.getenv("AWS_REGION", "us-west-2")


READER_PROMPT = """\
You read donation offers that arrive at a food bank and turn them into structured \
records. Offers arrive as forwarded email chains, text messages with photos, and \
voicemail transcripts. They are written by busy people and they are frequently \
incomplete.

Your job is to extract what is actually there — not to produce a tidy record.

Rules that matter:
- Weights are usually approximate ("a couple of pallets", "maybe forty pounds"). \
Estimate, and say so in notes.
- Only set hours_until_unusable when the source gives you a real signal: a date, a \
"today", a "use by Friday". If there is no signal, leave it null. Do not invent a \
number to make the record look complete.
- Lower extraction_confidence whenever the source is ambiguous, partially \
illegible, or contradicts itself, and list what you could not resolve in \
ambiguities. An honest 0.5 is far more useful downstream than a confident guess.
- Set needs_same_day_answer only when the donor actually asks for a decision today.
"""


ROUTER_PROMPT = """\
You route rescued food from donors to community pantries. You work for the pantry \
network, not the donor, and your goal is that food reaches people while it is \
still usable.

How to work:
1. Find candidates with find_candidate_pantries, using the storage class the food \
actually requires.
2. Confirm the specific pantry can hold it with check_storage_capacity before \
committing anything.
3. Commit with reserve_pickup, then tell the coordinator with \
notify_pantry_coordinator. Always in that order — never announce a delivery you \
have not booked.

Judgment:
- Prefer a pantry that currently needs the category over a closer one that does not.
- A pantry that can take the whole load beats splitting it, unless splitting is the \
only way the food gets used in time.
- Between two pantries that both need it and both fit, favour the one where the load \
takes the smaller share of free space — the `crowding` figure. Filling a small \
pantry to the brim because it happens to be nearer is how the next offer ends up \
with nowhere to go.
- Messages to coordinators go to real people. Say what is coming, how much, what \
storage it needs, and by when it must be collected. No preamble.

Some of your commitments will be held for a human coordinator to approve. That is \
expected and is not a failure. Do not try to work around it, restate it as done, or \
soften the facts to get it through.
"""


def build_model(model_id: str | None = None, *, temperature: float = 0.2) -> BedrockModel:
    """Bedrock-backed model shared by both agents."""
    return BedrockModel(
        model_id=model_id or DEFAULT_MODEL_ID,
        region_name=DEFAULT_REGION,
        temperature=temperature,
    )


def build_reader(model: BedrockModel | None = None) -> Agent:
    """Agent that turns a messy offer into a DonationOffer."""
    return Agent(
        name="offer-reader",
        description="Reads messy multi-channel donation offers into structured records.",
        model=model or build_model(),
        system_prompt=READER_PROMPT,
        # Silent by default: the caller decides what a run looks like, and the
        # demo's own formatting would otherwise be interleaved with raw tokens.
        callback_handler=None,
    )


def build_router(
    model: BedrockModel | None = None,
    gate: CoordinatorGate | None = None,
) -> tuple[Agent, CoordinatorGate]:
    """Agent that places an offer with a pantry, behind the coordinator gate."""
    gate = gate or CoordinatorGate()
    agent = Agent(
        name="offer-router",
        description="Matches donation offers to pantries and books the pickup.",
        model=model or build_model(),
        system_prompt=ROUTER_PROMPT,
        tools=ALL_TOOLS,
        interventions=[gate],
        callback_handler=None,
    )
    return agent, gate


def read_offer(raw: str, *, reader: Agent | None = None) -> DonationOffer:
    """Parse one raw offer into a DonationOffer via structured output.

    Asking for the model on the invocation is the SDK's current shape;
    `Agent.structured_output()` still works but is deprecated, and its warning
    would print over the first line of a live demo.
    """
    reader = reader or build_reader()
    result = reader(raw, structured_output_model=DonationOffer)
    if result.structured_output is None:
        raise ValueError("the reader returned no structured offer for this source")
    return result.structured_output
