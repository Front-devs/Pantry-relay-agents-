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
| `web_app.py`, deployed or local | Python 3.10+ and `pip install -r requirements.txt`. Nothing else. |
| `run_demo.py --live` | AWS access key, secret, region, and Bedrock model access for `global.anthropic.claude-opus-5`. Keep this local. |

So the public link costs no credentials and no Bedrock spend.

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
