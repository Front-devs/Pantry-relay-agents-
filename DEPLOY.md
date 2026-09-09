# Deploying the dashboard

The live demo link is optional for this hackathon but strengthens the Technical
Implementation score, and the dashboard is the artifact worth putting behind it:
it runs the real gate rather than replaying a recording of one.

## What it needs, and what it does not

**No AWS credentials.** This is structural rather than lucky. The web path
imports the gate, the tools, the routing policy and the trace builder. None of
them reach `agent.py`, so `BedrockModel` is never constructed. `run_demo.py`
imports `build_model` inside `run_live`, so even the terminal demo only touches
Bedrock on the `--live` path.

The `strands-agents` package still has to be installed, because `CoordinatorGate`
subclasses `InterventionHandler` from it. Installing a library and authenticating
to a cloud are different things.

| What you run | Needs |
|---|---|
| `web_app.py` in its default mode | Python 3.10+ and `pip install -r requirements.txt`. Nothing else. |
| `web_app.py --live-enabled`, live mode | The above, plus AWS credentials and Bedrock model access in the host environment. |
| `run_demo.py --live` | AWS access key, secret, region, and Bedrock model access for `global.anthropic.claude-opus-5`. |

## Live mode on the deployed dashboard

The dashboard has a **Live agents** toggle. Off, it runs the deterministic
routing policy. On, it runs the reader and router agents against Bedrock, which
is the path worth showing a judge at an AWS hackathon: the confidence scores and
ambiguities the gate then judges are the model's own rather than ours.

Both paths reach the same `CoordinatorGate`. The offline path calls
`gate.decide()` directly; the live path arrives at the same method through
Strands' `before_tool_call`. The dashboard labels every run with the path that
produced it, so a viewer never has to guess which they are looking at.

Live mode is **off unless you opt in**, with `--live-enabled` or
`PANTRYRELAY_LIVE=1`. That default is deliberate. The offline run is the one that
must never fail, because it is what keeps the public link working when a key
expires the night before judging.

### Adding credentials to a deployment

On Render: **Environment > Add Environment Variable**, or the Environment tab of
the service. On App Runner: **Configuration > Environment variables**.

```
PANTRYRELAY_LIVE=1
AWS_ACCESS_KEY_ID=<your key id>
AWS_SECRET_ACCESS_KEY=<your secret>
AWS_REGION=us-west-2
```

Set these yourself in the host's own interface. Do not commit them, and do not
paste them into a chat, a screenshot, or a file in this repository.

Give the key an IAM policy allowing only `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` on the one model you enabled. A demo key
with broad permissions on a public URL is the wrong trade, and this project has
no need for anything wider.

On App Runner, prefer an **instance role** over an access key. The service then
calls Bedrock with a role AWS manages, and there is no long-lived secret to leak
or rotate. That is the better answer if you have the time to set it up.

### What stops the bill running away

A live run is roughly sixteen model calls, and a public URL means strangers can
trigger them. Three limits apply:

- Live mode is off unless the deployment opts in.
- The whole deployment gets `PANTRYRELAY_LIVE_BUDGET` live runs an hour, twenty
  by default. Past that, viewers are told the allowance is spent.
- Only one live run happens at a time. A second is refused immediately rather
  than queued, and told to take the offline run.

A live run holds the shared seeded network for its whole duration, so while one
is in flight the deployment is effectively single-user. That is a real
limitation, and the reason live mode is a toggle rather than the default.

If a live run fails because the host has no credentials, or Bedrock refuses the
model, the dashboard says which in a sentence and leaves the offline run
available. It does not return a stack trace to a viewer.

## Option 1 — Render (free, fastest)

`render.yaml` in the repo root is a complete blueprint.

1. Sign in at [render.com](https://render.com) with GitHub.
2. **New > Blueprint**, select this repository. Render reads `render.yaml`.
3. Apply. First build takes a few minutes.

Render provides `PORT`; `web_app.py` reads it and binds all interfaces when it is
set, which is what the deployment commit added.

The catch is that free services sleep after inactivity and take roughly a minute
to wake. A judge clicking a link that appears dead is a real risk, so **open the
link yourself a few minutes before judging** to warm it.

## Option 2 — AWS App Runner (paid, no cold start)

`apprunner.yaml` in the repo root configures this. It is the thematically
consistent choice for an AWS hackathon and it does not sleep.

1. AWS console > **App Runner** > **Create service**.
2. Source: **Source code repository**, connect GitHub, pick this repo and branch
   `main`. Deployment trigger: automatic.
3. Configuration: **Use a configuration file**. App Runner reads `apprunner.yaml`.
4. Leave the instance role empty. The service needs no AWS permissions, because
   it never calls an AWS API.

Expect a few dollars a month for the smallest instance. Delete the service after
judging.

## Verifying a deployment

Once it is up, three checks tell you it is genuinely running the gate rather
than serving a stale page:

```bash
curl -s -X POST https://<your-host>/api/run | head -c 400
```

You should see a JSON trace whose `stats` read five routed and three held. Then
open the page and press **Run Morning Triage**. Three decision modals should
appear in turn, reading `storage_conflict`, `low_confidence_extraction` and
`same_day_commitment`. If you see those three, the deployed service is running
the real escalation policy.

## Notes for a public link

Each viewer gets their own session, keyed by a cookie, holding their own copy of
the pantry network and their own gate. Two people can run the morning at once
without spending each other's freezer space. The server is threaded, and the
shared seeded network is swapped in per request under a lock, so a slow client
does not stall anyone else.

Sessions are capped and the oldest is evicted, because a public demo link gets
opened by strangers and never closed. Nothing is persisted: every run starts from
the same seeded baseline, and restarting the service forgets everything, which is
correct for a demo and stated plainly in the README.
