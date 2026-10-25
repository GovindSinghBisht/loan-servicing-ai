# AI Loan Servicing for Blue Machines

This is a customer service assistant for a lender's loan customers. It answers questions using the
customer's own records from the lender's systems (CRM, loan management, payments, policy and service
operations), carries out requests such as raising a dispute or a waiver after the customer confirms,
and is built so that new workflows can reuse the same engine.

This repository holds the backend: the synthetic data, the mock enterprise systems, the assistant engine
and its API. The web app for text and voice lives in a separate repository,
[blue-machines-fde-web](https://github.com/GovindSinghBisht/blue-machines-fde-web).

Two documents go with the code. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the system is
built, the main decisions and their trade-offs, and how it would move to production.
[docs/PERFORMANCE.md](docs/PERFORMANCE.md) has the measured results. In short, all 19 evaluation
conversations pass with the real model, the first word of a reply arrives in about 2.1 seconds, and
1,000 concurrent conversations completed on six instances, including a run where one instance was killed.

## How it works

A customer's message goes to an agent loop where the language model decides which tools it needs. The
tools fetch data from the enterprise systems, apply the lender's policy rules and do every calculation in
plain Python. The model then explains what the tools returned. It never works out a number on its own.

The agent loop lives in `engine/agent.py`. Independent tool calls run in parallel and the reply streams
to the browser word by word. Every turn records how long the customer waited for the first word, how
long the model took and how long the tools took.

The model sits behind a small provider interface in `engine/llm.py`. OpenAI is the default, and another
vendor can be added by implementing the same three methods. Tools, security checks and evaluation stay
the same whichever model is used.

The tools are in `engine/tools/`. They combine data from several systems, use the loan maths in
`shared/finance.py` and read policy rules from machine readable parameters in the policy system. Every
result includes the rule checks and the source records behind it, so each answer can be traced.

Security does not depend on the prompt. The signed in customer's ID is attached on the server and the
model never supplies it. Every account ID the model passes is checked for ownership. Actions return a
proposal first and only run after the customer confirms in their next message. Every tool call, action
and guardrail decision is stored in the database.

Repayment statements come from the lender, not from the assistant. The assistant asks for one, the mock
loan system builds a PDF in a fixed format from its own records and serves it only to the customer who
owns it. The chat shows a download button.

Sign in returns a random token that the server stores only as a SHA-256 hash. Sessions expire after 30
minutes of inactivity or 8 hours in total, and both limits come from the governance policy. Refreshing the
page or restarting the API does not sign the customer out, because a valid session is restored from the
database together with its conversation.

The client layer in `engine/clients.py` gives each system its own timeout, retries temporary failures,
stops calling a system that keeps failing and caches policy data for five minutes. If a system is down,
the assistant says what it cannot see and answers with the rest.

Workflows are configured as packs in `engine/packs.py`. A pack names the tool groups it uses and adds
domain instructions. Two packs exist, loan servicing and credit card servicing, and a customer who holds
both products gets one assistant that covers both. Adding the card workflow took one new tools file and
a pack entry. The engine, web app and voice were reused as they were.

Voice uses the same session, tools and history as text. The browser handles speech recognition, and
replies are spoken sentence by sentence as they stream in, using OpenAI text to speech with the browser
voice as a fallback. If the customer talks over the assistant, playback stops and the server cancels the
turn while keeping the conversation history consistent. A lookup that takes longer than 0.4 seconds gets
a short spoken filler.

Guardrails and governance are in `engine/governance.py`, with the rules in
`data/governance/policy.json`. Messages containing an OTP, PIN, CVV or password are blocked before the
model sees them, and card, Aadhaar and PAN numbers are redacted. After a reply, a check confirms that every
amount, percentage and account ID in it came from a tool result. Actions are limited by kill switches,
channel rules, a cap per session and amount limits. Prepayments above Rs 10 lakh, for example, go to a
relationship manager. Risk teams can change these rules in the policy file without a code change.

Storage is in `engine/storage.py`. Conversations, messages, tool calls, timings, audit events and
guardrail events go to PostgreSQL, or to SQLite when no database is configured. Writes go through a
background queue so the database never slows a reply down. Each customer keeps their last five
conversations, while audit and guardrail events are kept separately.

Tracing goes to Langfuse when its keys are set. Each turn becomes one trace with the model calls, token
usage, tool calls and guardrail decisions, and personal data is masked before anything leaves the server.

## Running with Docker

Clone this repository and the web app next to each other, then run:

```bash
cp .env.example .env
```
```bash
DOMAIN=localhost docker compose up -d --build
```

Add your `OPENAI_API_KEY` to `.env` first, and the database and Langfuse settings if you use them. Then
open https://localhost and accept the browser warning for Caddy's local certificate.

The compose file runs four containers. Caddy handles HTTPS and is the only container with public ports.
Requests to `/api/` go to the API and everything else goes to the web app, so the whole app is served
from one HTTPS address. On a public server, point a DNS record at the machine and set `DOMAIN` to that
name, and Caddy will get a Let's Encrypt certificate on its own.

For a public demo, set `DEMO_ACCESS_CODE` in `.env` so that sign in also asks for a code you share only
with invited people. Sign ins and messages are rate limited, and the limits are in the `rate_limits`
section of the governance policy. It is also worth setting a monthly spend limit on the OpenAI key.

To run images from a registry such as Amazon ECR instead of building on the server, set `BACKEND_IMAGE`
and `WEB_IMAGE` and run `docker compose pull` followed by `docker compose up -d --no-build`. The server
then only needs `docker-compose.yml`, `deploy/Caddyfile` and `.env`. Build the images for the server's
architecture, which for x86 instances means `docker buildx build --platform linux/amd64`.

## Running locally for development

You need Python 3.10 or newer with [uv](https://docs.astral.sh/uv/), and Node.js 20 or newer for the web
app. Install the dependencies, create your `.env` with an OpenAI key, and generate the synthetic data:

```bash
uv sync
```
```bash
cp .env.example .env
```
```bash
uv run python -m datagen.generate
```

Start the six mock systems in one terminal and the API in another:

```bash
uv run python -m mock_systems.run
```
```bash
uv run uvicorn engine.server:app --port 8000
```

Then start the [web app](https://github.com/GovindSinghBisht/blue-machines-fde-web) with `npm install`
and `npm run dev`, open http://localhost:3000 and sign in as one of the demo customers. The OTP is
`123456`. There is also a terminal client:

```bash
uv run python -m engine.cli --phone +919800000001
```

To work on the interface without an OpenAI key, run `uv run python -m tests.offline_server` instead of the
API. It uses a simple keyword based stand in for the model that still calls the real tools.

No database setup is needed for local development. The section on the database below explains the tables
and how to switch to PostgreSQL.

The tests run without an API key. The evaluation runs 19 graded conversations against the real model and
reports latency:

```bash
uv run pytest -q
```
```bash
uv run python -m evals.run
```

You can also change how a mock system behaves while it runs. This makes the payments system slow and
unreliable, which is useful in a demo:

```bash
curl -X PUT localhost:8103/admin/chaos -H 'content-type: application/json' -d '{"p50_ms": 1500, "error_rate": 0.3}'
```

## Database

You do not need to set up a database to try the project. Without any database settings the API stores
everything in a local SQLite file at `data/local.db` and creates the tables itself the first time it
starts. The Docker setup does the same inside the API container.

The database only holds what the assistant itself owns. Customer, loan, payment and card data stays in
the enterprise systems, which here are the mock services. There are eight tables.

| Table | What it stores |
|---|---|
| `conversations` | One row per conversation, with the customer, channel, enabled packs, model and turn count |
| `messages` | What the customer said and what the assistant replied, with secrets already removed |
| `tool_calls` | Each tool the assistant called in a turn, its arguments and whether it succeeded |
| `turn_metrics` | Timings and token counts for every turn |
| `auth_sessions` | Login sessions, stored as a hash of the token with expiry and revocation times |
| `audit_events` | Sign ins, tool calls, executed actions and errors. Rows can be added but never changed |
| `guardrail_events` | Every guardrail decision, such as a redacted card number or a blocked secret. Add only |
| `voice_metrics` | Timings reported by the browser for voice turns |

Each customer keeps their last five conversations, and older ones are deleted together with their
messages, tool calls and timings. Audit and guardrail events are kept separately and are not deleted.

### Using PostgreSQL

The setup script creates the database, the tables and a separate application user with only the rights
the code needs. To try it locally, start PostgreSQL in Docker:

```bash
docker run -d --name loan-postgres -e POSTGRES_PASSWORD=change-me -p 5432:5432 postgres:16
```

Then add the admin connection to `.env`. `DB_SSL=disable` is needed here because the local container
does not use TLS. Leave it out for a managed database such as Amazon RDS, which requires TLS.

```
DB_HOST=127.0.0.1
DB_PORT=5432
DB_ADMIN_USER=postgres
DB_ADMIN_PASSWORD=change-me
DB_SSL=disable
```

Run the setup once:

```bash
uv run python -m engine.db_setup
```

It creates a database called `blue_machines` and all eight tables, owned by the admin. It then creates
the user `blue_machines_app` with a random password and gives it only what the code needs. That user can
read and write conversations and delete old ones for retention, but it can only add rows to the audit and
guardrail tables, and it cannot create, change or drop any table. Even a compromised application could
not rewrite the audit trail or damage the schema. Other roles on the server are not allowed to connect to
this database.

At the end the script writes `DATABASE_URL` for the application user into `.env`. Remove the
`DB_ADMIN_PASSWORD` line afterwards, since the API never needs it, and restart the API. You can run the
script again at any time. It keeps the existing data and gives the application user a new password.
Calling `/api/health` shows which storage the API is using.

## Repository layout

| Path | Contents |
|---|---|
| `shared/finance.py` | Loan maths: EMI, schedules with rate resets and prepayments, prepayment impact |
| `data/policies/` | Versioned policy clauses with machine readable parameters |
| `datagen/` | Synthetic data generator, demo personas and a validator |
| `mock_systems/` | One FastAPI app per enterprise system, with fault injection and idempotent writes |
| `engine/` | Clients, tools, packs, agent loop, model provider, governance, storage, tracing and the web API |
| `data/governance/policy.json` | Runtime rules for sessions, retention, PII, actions, rate limits and models |
| `evals/` | Graded conversations and the evaluation runner |
| `loadtest/` | The concurrent conversation load test |
| `tests/` | Unit and integration tests |

## The data

All records are dated as of 22 October 2026, and the mock systems treat that day as today. There are five
data files, one per system: customers and their interaction history, loans with full repayment schedules
and a charges ledger, every payment attempt including failures and duplicate debits, service tickets
with their SLAs, and credit cards with statements, transactions and disputes.

The data is consistent across systems. An instalment's status follows from its payment attempts, charges
use the policy version that was in force on the day, failures caused by the bank have their charges
waived, and floating rate schedules follow every change in the benchmark rate.

Four demo customers are built to show specific situations.

| Customer | Phone | What it shows |
|---|---|---|
| Priya Sharma | +91 98000 00001 | Home loan EMI went up in October after the benchmark rate rose. Two failed debits in six months, one charged and one waived because the bank caused it. A 50,000 prepayment with no charge, an eligible bounce charge waiver, and a top up loan she cannot get because of the recent bounce |
| Arjun Mehta | +91 98000 00002 | Personal loan with only seven EMIs paid and October overdue, so prepayment is blocked for two reasons and foreclosure for one. His card autopay failed, he paid four days late and was charged a 950 rupee late fee that can be reversed |
| Kavya Iyer | +91 98000 00003 | Loan against property set to keep the EMI fixed, so a rate rise extended the tenure instead. October's EMI was debited twice and the refund ticket is past its SLA. She also has an open card dispute |
| Rohan Gupta | +91 98000 00004 | Two active loans, so "my loan" is ambiguous. His personal loan is past 24 EMIs, so prepayment is allowed within the free limit |

Another 200 background customers make the volumes realistic, with about 224 loans, 5,700 payments and
7,000 card transactions.

## Simplifications

Interest is calculated on a monthly rest, EMIs are rounded to the rupee and the last instalment absorbs
the rounding. A rate change applies from the first EMI due after its effective date. Charges on a late
instalment are collected with the next EMI. Card finance charges apply only to balances carried forward.
