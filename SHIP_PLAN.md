# PantryRelay — ship plan

Nine days. Deadline **14 September 2026, 5:00pm PDT**.
Agents for Humans hackathon · Good Neighbor Agents track.

Ordered by leverage, not by comfort. The first two items only stop you losing on
a technicality; items 3–5 are what move you past the winners.

> **Note:** this file enumerates open holes in the gate. Add it to `.gitignore`
> or delete it before the repo goes public — `gate-redteam`'s survivor list is
> the version of this that belongs in the README.

---

## Calibration — what the last comparable field rewarded

AWS AI Agent Global Hackathon, Sept–Oct 2025, 9,466 participants (this one has
7,606). Winners:

| | Project | What it does |
|---|---|---|
| 1st | EcoLafaek | Waste management, deployed in Timor-Leste |
| 2nd | AegisAgent | Insurance claims, multi-agent orchestration |
| 3rd | Province | Tax filing, "100% accuracy on Form 1040" |
| Best Strands SDK | AgentShell | Agent control via context management |

All three top finishers automate a heavy civic or bureaucratic burden. Two of
three are explicitly multi-agent. **Every one of them had a foothold in reality**
— a real form, a real country, real users.

PantryRelay's shape matches theirs and its framing is sharper. Its four pantries
are a Python list.

Of the four reasons the gate wakes a human, the demo a judge watches exercises
one:

| Reason code | In the demo? |
|---|---|
| `storage_conflict` | **yes** |
| `thin_expiry_margin` | unit-tested only |
| `same_day_commitment` | unit-tested only |
| `low_confidence_extraction` | unit-tested only |

---

## Today — stop losing on a technicality

- [ ] **Run the agents once, for real** · *existential*

      py -3.14 run_demo.py --live

  Every claim in the README is about two agents on Bedrock, and that path has
  never executed. The gate, the interlock and all 18 tests were proven against a
  model scripted by hand — the reader has never parsed a voicemail, the router
  has never chosen a pantry. If a judge runs this and it throws, nothing else
  matters.

- [ ] **Commit the working tree** · *existential*

  The deny tier, the 18 tests and the rewritten live path are all unstaged. One
  power cut and the strongest evidence in the submission is gone.

## Days 1–3 — get one thread to reality

- [ ] **Replace the invented pantries with real ones** · *closes the gap*
      → `src/pantryrelay/data.py`

  Pick one real city, pull its food bank's published partner-pantry list, cite
  the source in `data.py`. Every winner above had a foothold in reality; this is
  the cheapest one available, and it costs an afternoon.

- [ ] **Talk to one food bank coordinator** · *closes the gap*

  Fifteen minutes on the phone, then two quoted sentences in the README about
  what their morning actually looks like. EcoLafaek took first place on this
  kind of grounding, not on architecture.

- [ ] **Re-check the four escalation reasons against what they tell you** · *closes the gap*

  If a real coordinator says expiry margin rarely matters but volunteer
  scheduling always does, that finding is worth more than any test in the suite
  — and changing the policy because a real person told you to is the strongest
  paragraph you could put in the write-up.

## Days 3–4 — close the known holes

- [ ] **Fix argument laundering in the gate** · *closes the gap*
      → `src/pantryrelay/gate.py`

  The gate judges `hours_until_unusable` and `quantity_lbs` from whatever the
  model passes, never checking them against the offer it already holds in
  `invocation_state`. Proven: produce with a five-hour window booked unattended
  because the model said 8760. Worse, `tools.py` instructs it to — *"Pass a
  large number when the donor gave no expiry signal."* Fix in the gate, never in
  the prompt (CLAUDE.md's rule).

- [ ] **Run `gate-redteam` properly and keep its output** · *polish*
      → `tests/test_gate_adversarial.py`

  Four of its five attack axes are closed (missing-input bypass, asymmetric
  coverage, retry/state, path divergence). It exists to find the fifth and to
  produce the **survivor list** — the attacks the gate withstood. Commit those
  tests so they outlive the session that found them.

- [ ] **Make the demo show more than one of four reasons** · *polish*
      → `src/pantryrelay/fixtures.py`

  All six fixture confidences sit between 0.82 and 0.96 against a 0.75 bar, and
  nothing is same-day, so only `storage_conflict` ever fires. A judge watching
  ninety seconds takes the other three on faith. Add offers — don't doctor the
  existing ones.

## Days 5–7 — presentation is one of five stated criteria

Judging criteria: Technological Implementation · Design · Potential Impact ·
Creativity & Originality · **Presentation**.

- [ ] **Cut the demo video** · *polish*

  Structure: a coordinator's morning → the run → the one held decision → why
  restraint is the product. Not a code tour; the argument is the asset.

- [ ] **Write the Devpost submission copy** · *polish*

  `narrative-editor` exists for this, and its rule is the right one: every claim
  traceable to code that runs.

- [ ] **Check the mermaid diagram renders on Devpost** · *polish*

  It may not. Have a PNG fallback ready rather than discovering it at 4:55pm on
  the 14th.

## Days 7–9 — what turns good into wins

- [ ] **Show the coordinator answering** · *closes the gap*
      → `run_demo.py`

  The demo pauses for a human and then stops. It never shows the answer coming
  back and the booking completing — half the human-in-the-loop story, and
  `tests/test_agent_loop.py` already proves it works. Remember a resume is a
  fresh invocation: pass `invocation_state={"offer": offer}` again.

- [ ] **Earn a headline number** · *closes the gap*

  Province cited 100% accuracy on Form 1040. Yours is the survivor list: *N
  adversarial attacks, zero gate bypasses, five of six offers handled without
  waking anyone.* That is your Form 1040.

---

## Do not

Add features, add tools, or widen scope. This submission's strength is the
tightness of one argument — every new surface dilutes it, and hands
`gate-redteam` more to attack.

---

## State as of 5 September 2026

- 18 tests passing, no credentials needed
- Offline demo: 5 of 6 offers routed, 1 held (`storage_conflict`)
- Gate has two tiers: four `Confirm` escalations (wake a human) and two `Deny`
  refusals (unbacked announcement, missing offer — the agent acting malformed)
- `--live`: **never executed**
- Working tree: **uncommitted**

Sources: [current hackathon](https://agentsforhumans.devpost.com/) ·
[2025 winners](https://aws-agent-hackathon.devpost.com/updates/38140-congratulations-to-the-winners-of-the-aws-ai-agent-global-hackathon)
