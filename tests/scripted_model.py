"""A model that plays a fixed script, so the agent loop can be run in CI.

The gate's whole claim is about what happens *inside* a Strands agent run — the
tool call is inspected before it executes, and the loop stops. Testing that
needs a real `Agent`, a real tool executor and the real intervention registry;
the only piece that has to be faked is the model itself.

This yields the Bedrock converse-stream events Strands parses, so the agent
sees an ordinary model turn and has no idea it is scripted.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Any

from strands.models.model import Model


def call(name: str, **kwargs: Any) -> dict[str, Any]:
    """One scripted turn: the model asks for a tool."""
    return {"tool": name, "input": kwargs}


def call_as(tool_use_id: str, name: str, **kwargs: Any) -> dict[str, Any]:
    """A tool call under a tool-use id the script chooses.

    Ids normally come from the model, which is the point: Strands derives the
    interrupt id a coordinator's answer is filed under from the tool-use id, so
    an attack that reuses one has to be expressible here.
    """
    return {"tool": name, "input": kwargs, "id": tool_use_id}


def raw_call(name: str, raw_input: str, tool_use_id: str | None = None) -> dict[str, Any]:
    """A tool call whose arguments are sent verbatim, valid JSON or not.

    Real models stream tool arguments as a JSON string. Strands parses it and
    hands the result to the gate without checking that it is an object, so the
    gate has to survive being handed a list.
    """
    return {"tool": name, "raw": raw_input, "id": tool_use_id}


def same_turn(*calls: dict[str, Any]) -> dict[str, Any]:
    """Several tool calls in one assistant message, as a parallel model turn."""
    return {"blocks": list(calls)}


def say(text: str) -> dict[str, Any]:
    """One scripted turn: the model answers and stops."""
    return {"text": text}


class ScriptedModel(Model):
    """Plays `turns` in order, one per model call, repeating the last forever.

    Repeating rather than raising keeps a test honest about what it is
    asserting: a script that runs short means the agent took more turns than
    expected, and the assertion on `calls` says so, instead of a StopIteration
    surfacing as an unrelated failure.
    """

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.turns = list(turns)
        self.calls = 0

    # -- Model interface ---------------------------------------------------

    def update_config(self, **model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("ScriptedModel drives the router, not the reader")

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[dict[str, Any]]:
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1

        blocks = turn.get("blocks") or ([turn] if "tool" in turn else [])

        yield {"messageStart": {"role": "assistant"}}
        if blocks:
            for index, block in enumerate(blocks):
                # A single-call turn keeps the plain "tu-N" id the rest of the
                # suite is written against; only a parallel turn needs the
                # suffix, and an attack that reuses an id says so explicitly.
                default_id = f"tu-{self.calls}" if len(blocks) == 1 else f"tu-{self.calls}-{index}"
                yield {
                    "contentBlockStart": {
                        "start": {
                            "toolUse": {
                                "toolUseId": block.get("id") or default_id,
                                "name": block["tool"],
                            }
                        },
                        "contentBlockIndex": index,
                    }
                }
                # `raw` goes out exactly as written, so a test can send tool
                # arguments that are not an object at all.
                payload = block["raw"] if "raw" in block else json.dumps(block["input"])
                yield {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": payload}},
                        "contentBlockIndex": index,
                    }
                }
                yield {"contentBlockStop": {"contentBlockIndex": index}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
            yield {
                "contentBlockDelta": {"delta": {"text": turn["text"]}, "contentBlockIndex": 0}
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 0},
            }
        }
