# HubSpot Customer Success agents

Three agents that run a SaaS company's routine Customer Success work inside HubSpot. A health and renewal-risk agent scores every account nightly from product usage, tickets and billing, writes the score to HubSpot, opens a renewal deal 120 days before contract end with its risk tier, and opens recovery and expansion tasks. A follow-up agent sends check-ins, onboarding reminders and ticket follow-ups from the CSM's mailbox, and escalates when a customer talks about cancelling. A QBR agent prepares each quarterly review pack and checks every number in it against the source figures. Claude reads ticket and email text and drafts through typed tools; code computes the scores, decides which actions run on their own and which wait for a CSM in an approval queue, and claims every write in an action ledger so nothing reaches HubSpot twice. A Streamlit dashboard shows the actions, the approvals, each account's timeline and the reliability counters.

This is a reference implementation of the architecture behind a platform I built for a mid-sized B2B SaaS company selling practice-management software to veterinary clinics. The production build is under NDA, so a few integrations are abstracted behind adapters: HubSpot is a local stand-in that implements the CRM v3 and v4 endpoints the agents call, and Snowflake, Stripe, Gmail, Slack and SQS are Postgres tables behind the same adapter interfaces. It runs on synthetic data, and every company, place, person, phone number and figure in it is made up.

[![ci](https://github.com/AlexKurinets/hubspot-customer-success-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/AlexKurinets/hubspot-customer-success-agents/actions/workflows/ci.yml)

## Architecture

```mermaid
flowchart LR
    HS[("HubSpot stand-in<br/>CRM v3 objects, batch, search<br/>v4 associations, properties")]
    WH[("Warehouse<br/>wh.usage_weekly")]
    BL[("Billing<br/>subscriptions, invoices, disputes")]
    HS -- "propertyChange webhooks<br/>v3 HMAC signature" --> API["FastAPI<br/>webhook receiver, approvals, metrics"]
    API -- "one row per event id" --> Q["ops.jobs queue"]
    Q --> W["Worker"]
    SCHED["Nightly run"] --> G1["Health and renewal risk<br/>LangGraph: plan, act, record"]
    SCHED --> G2["Follow-ups"]
    SCHED --> G3["QBR prep"]
    W --> G1
    WH --> G1 & G2 & G3
    BL --> G1 & G2 & G3
    HS -- "batch read, search" --> G1 & G2 & G3
    G1 & G2 & G3 -- "typed tool calls" --> LLM["Claude or offline stand-in<br/>reasons, drafts, QBR narrative"]
    G1 & G2 & G3 --> D["dispatch<br/>preconditions, autonomy level"]
    D -- "act" --> L["Action ledger<br/>claim, write, mark done"]
    D -- "approve" --> AP["Approval thread<br/>LangGraph interrupt,<br/>Postgres checkpointer"]
    AP -- "CSM decides" --> L
    L -- "token bucket, retries,<br/>dead-letter queue" --> HS
    L --> MAIL["Outbox<br/>CSM mailbox"]
    DASH["Streamlit dashboard"] --> API
```

Compose runs five services: `db` (Postgres 16), `hubspot` (the stand-in), `api`, `worker` (seeds on first start, then handles webhooks and answered approvals every five seconds) and `dashboard`. The agents reach HubSpot only over HTTP through `src/csagents/hubspot/client.py`, and never read the stand-in's tables.

## Design decisions

**Code computes the health score; the model explains it.** The score is four signals with fixed points from `config/health_weights.yaml`: 50 for seat utilization, 30 for core module adoption, 10 for ticket load over 90 days weighted by severity, and 10 for invoices being current. `csagents fit-weights` derives them with a logistic regression of renewal on those signals over 18 months of renewal history, rescaled to 100 and rounded to fives so a CSM can check a score in their head; on the last six months, held out, a churned account scores below a renewed one 82% of the time (AUC 0.824). Tiers are green from 60 and yellow from 45. The model only tags the reasons behind a drop from recent ticket and email text (`src/csagents/agents/health.py`). In production an early version let the model set the score, and identical inputs moved by up to 15 points between runs.

**The model proposes through typed tools; code decides.** `src/csagents/tools.py` defines `get_account_context`, `record_reasons`, `create_task`, `send_checkin` and `escalate_to_csm` as Pydantic schemas. A write tool writes nothing: its arguments are validated and returned as a proposal, and the agent turns it into a `PlannedAction` that goes through the same preconditions, autonomy rules and ledger as anything code plans. The model never holds the HubSpot token, and customer text reaches it as data. `LLM_PROVIDER=claude` runs the tool loop in `src/csagents/llm/claude.py` with server-side refusal fallback; the default is a deterministic offline stand-in so the stack and tests run without a key.

**Autonomy is earned per action type.** `config/agents.yaml` sets each action type to act, approve or never. Customer-facing actions always wait for the CSM when the account is over $40k ARR or billing is involved, and cancellation language becomes an escalation with no draft (`src/csagents/autonomy.py`). A draft type starts at approve and runs on its own once the last 50 CSM decisions on it had under 5% edited or rejected; the same rule demotes it if the forced approvals that remain start getting edited. The first week after launch is shadow mode: `src/csagents/agents/dispatch.py` writes what would have happened to `ops.shadow_log` and nothing reaches HubSpot.

**One ledger key per business event.** Every write claims a row in `ops.ledger` under a unique key of action, company and business trigger (for example `create_renewal_deal:<company>:renewal:2026-09-15`) and commits before HubSpot is called (`src/csagents/ledger.py`). The key is stamped on the HubSpot record as `cs_agent_action_key`, so `csagents replay --stale-claims` can tell a write that landed before a crash from one that didn't. In production the first version keyed on HubSpot's webhook event id, and when a webhook and the nightly run reacted to the same renewal-date change, duplicate tasks went out. Here the simulator hands the day's webhooks to the worker while the nightly health run sits between planning and acting, which is that race; the second claim collides and is counted on the dashboard.

**Approvals are LangGraph threads that can wait for days.** Each action that needs a person gets a thread (`src/csagents/approvals.py`): request, wait (an `interrupt()` checkpointed to Postgres), decide. The API only records the CSM's answer; the worker resumes the thread in whatever process picks it up. On resume the thread reads billing again, and for a change to a field people own, such as a renewal deal's amount, it re-reads the deal and cancels if the amount is no longer what the agent saw when it asked. The nightly runs are LangGraph graphs too, so a run that fails halfway resumes from its last completed node with `csagents replay --run`.

**Agents write freely only to properties they own.** `csagents setup-hubspot` creates the `cs_agent_` property group through the properties API, so the CRM schema is in version control (`src/csagents/hubspot/schema.py`). `guard()` in `src/csagents/executor.py` refuses any other property unless an approved action names it. Tasks, emails and notes are created with their associations in the same call, so a failure can't leave an orphan, and every action also logs a note on the company saying what was done and why.

**Preconditions read the system that knows.** Before anything goes to a customer, `src/csagents/preconditions.py` reads billing directly for an open dispute or a payment that failed in the last 30 days, because in production a check-in once reached a practice in a payment dispute that existed only in billing. Blocked actions land in `ops.blocked_actions`, and the dashboard's weekly report shows CSMs what the agents chose not to do.

**One HubSpot client owns pacing and failure.** `src/csagents/hubspot/client.py` uses the batch endpoints at 100 records a call, pages search past the 10,000-result cap by sorting on `hs_object_id` and asking again above the last id, and reads associations through v4. A token bucket sized from the `X-HubSpot-RateLimit-*` headers keeps requests inside the rolling window and never assumes more room than `Remaining` reports (`ratelimit.py`). 429 and 5xx responses retry with jittered exponential backoff; after five attempts the action goes to `ops.dead_letters` and `csagents replay --dead-letters` re-runs it. Webhooks are rejected unless their v3 HMAC signature matches and the timestamp is under five minutes old, stored once by event id, and ignored when they report the app's own change.

**QBR figures are checked, not trusted.** `src/csagents/agents/qbr.py` assembles the figures in code seven days before each review and the model writes around them. Every number in the draft must be one of the figures; one mismatch gets a single regeneration with the bad numbers named, a second sends the pack to the CSM flagged. The offline stand-in misstates a figure in about one first draft in eight so the check has something real to catch.

## Running it

```bash
cp .env.example .env    # optional: change host ports, or set LLM_PROVIDER=claude and ANTHROPIC_API_KEY
docker compose up -d --build --wait --wait-timeout 1500
```

On first start the worker generates a synthetic portal of 1,200 accounts with eight CSMs, 18 months of renewal history and 30 weeks of usage, then runs 2 February to 30 June 2026 one business day at a time: the simulated world changes HubSpot through the API, webhooks are delivered, the three agents run their nightly graphs, and simulated CSMs answer the approval queue until two days before the end. That takes about ten minutes. Then:

- Dashboard: http://localhost:8550 (overview, approvals, accounts, reliability)
- API: http://localhost:8151/docs
- HubSpot stand-in: http://localhost:8150/docs

The seeded run ends with these figures over its last 61 days, which `scripts/smoke.sh` asserts: 2,989 HubSpot actions (1,492 a month), 76% of them with no human touch; renewal risk reaching the CSM a median 101 days before renewal, against 25 days for at-risk renewals in the year before launch; QBR prep a median 45 minutes of CSM review, against 179 minutes when packs were built by hand; check-in emails passing the graduation rule 44 days after launch. Of 1,200 accounts, 797 end green, 256 yellow and 147 red. Along the way the ledger stopped 22 duplicate claims where the webhook path and the nightly run reached the same renewal, and the receiver ignored 17 redelivered webhooks. The before figures (first CSM touch on at-risk renewals, hand-built QBR time) are seeded history; the after figures come from the agents' own ledger and from simulated CSMs logging their review time.

```bash
docker compose exec worker csagents status
```

Approve a waiting check-in the way the Slack button did. Find one with `curl -s http://localhost:8151/approvals`; the token is `APPROVER_TOKEN` in `.env`. Within five seconds the worker resumes the thread, re-checks billing and sends it, and `/approvals/<id>` shows `executed`.

```bash
curl -s -X POST http://localhost:8151/approvals/<id>/decide \
  -H "X-Approver-Token: demo-csm" -H "Content-Type: application/json" \
  -d '{"decision": "approve", "by": "Maya Okafor"}'
```

Edit a company the way a person would and watch the webhook path. Pulling a renewal date inside 120 days makes the stand-in send a signed webhook, the worker re-evaluates that one account, and a renewal deal appears in `/accounts/<company id>` with `source: webhook`.

```bash
curl -s -X PATCH http://localhost:8150/crm/v3/objects/companies/<company id> \
  -H "Authorization: Bearer pat-demo-portal-users" -H "Content-Type: application/json" \
  -d '{"properties": {"cs_renewal_date": "2026-09-15"}}'
```

Take HubSpot's task endpoint down for a day, then replay what failed. The faults endpoint belongs to the stand-in, not to HubSpot.

```bash
curl -s -X POST http://localhost:8150/_mock/faults -H "Content-Type: application/json" \
  -d '{"status": 503, "count": 1000, "match": "/objects/tasks", "methods": ["POST"]}'
docker compose exec worker csagents advance --days 1
curl -s -X DELETE http://localhost:8150/_mock/faults
docker compose exec worker csagents replay --dead-letters --stale-claims
```

Refit the health score points on the renewal history:

```bash
docker compose exec worker csagents fit-weights
```

## Tests

42 tests run against a real Postgres. They cover the stand-in's HubSpot behaviours (batch and search limits, scopes, property history), the client's backoff, give-up and pacing under a real burst limit, webhook signatures and redelivery, the webhook-versus-nightly race, the ledger under a repeated action and a crash, the dead-letter queue and replay, a nightly run resuming from its checkpoint, approval threads resuming in another process, stale and disputed approvals, the graduation rule, the score and its fit, the QBR figure check, the Claude adapter against a stub client, eight simulated weeks of a 150-account portal end to end, and a render of every dashboard page.

```bash
docker compose exec db psql -U cs -d cs -c "CREATE DATABASE cs_test"
docker compose run --rm --no-deps -e TEST_DATABASE_URL=postgresql://cs:cs@db:5432/cs_test worker pytest -q
```

Without Docker you need Python 3.12 and a Postgres reachable at `TEST_DATABASE_URL`, then `pip install -e ".[dashboard,dev]"` and `pytest`. CI runs the tests and ruff on Ubuntu, then builds the Compose stack and runs `scripts/smoke.sh`, which executes every command in this README.

## Layout

```
config/agents.yaml              action catalog, autonomy levels, thresholds and triggers
config/health_weights.yaml      the fitted points per health signal
src/csagents/hubspot/           client (pacing, retries, typed calls), webhook verification, owned properties
src/csagents/agents/            health, follow-up and QBR agents, the nightly graphs, dispatch, webhook jobs
src/csagents/approvals.py       approval threads: request, interrupt, re-check, execute
src/csagents/ledger.py          claim before write, collisions, stale claims
src/csagents/executor.py        the code behind each action, the owned-field guard
src/csagents/llm/               model interface, Claude adapter, offline stand-in
src/csagents/sources/           warehouse, billing and mailbox adapters
src/csagents/synthetic/         the synthetic portal and the day-by-day simulator
src/csagents/api.py             webhook receiver, approval endpoint, metrics for the dashboard
src/hubspot_mock/               the HubSpot stand-in
dashboard/                      Streamlit app
tests/                          pytest suite
scripts/smoke.sh                runs every README command against the stack
```

## What is deliberately left out

- Real HubSpot. The stand-in follows HubSpot's documented shapes for the endpoints used, but it is not HubSpot: its burst limit is raised in Compose so the five-month backfill finishes in minutes, and it has no workflows, sequences or UI.
- Snowflake, Stripe, Gmail, Slack, SQS, EventBridge and AWS Secrets Manager. Each is a table or an environment variable behind the adapter the production system used.
- OAuth and multi-portal installs. Production was one portal with a private app token.
- Live model calls in CI. The Claude adapter is exercised only with `LLM_PROVIDER=claude` and your own key, and the offline stand-in's drafts are templates.
- Renewal outcomes. The simulator does not decide who churns after launch, so the repo reports how early risk was flagged, not retention.

## License

MIT, Alex Kurinets
