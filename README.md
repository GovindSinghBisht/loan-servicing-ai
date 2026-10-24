# AI Loan Servicing — Blue Machines FDE Challenge

A conversational assistant for a large lender's loan customers: precise, customer-specific answers over
enterprise systems (CRM, loan management, payments, policy, service operations), with authorized
actions and a reusable platform for further workflows.

This repository is the backend: synthetic data, mock enterprise systems, the assistant engine and its
API. The customer web app (text and voice) is in
[blue-machines-fde-web](https://github.com/GovindSinghBisht/blue-machines-fde-web).

## Architecture

```
 Customer ── text / voice ──▶  Agent loop (LLM)     ──▶  Tool layer  ──▶  Enterprise systems
                               plans + explains          computes +        CRM · Loans · Payments
                                                         enforces          Policy · Service Ops · Cards
```

- **Agent loop** (`engine/agent.py`): the LLM decides which tools to call, independent calls run in
  parallel, replies stream token by token. Every turn records time to first token, model time and tool time.
- **Model-agnostic** (`engine/llm.py`): the LLM sits behind a small provider interface. OpenAI is the
  default (`LLM_PROVIDER=openai`, `OPENAI_MODEL`); another vendor plugs in by implementing the same interface. Tools, security,
  confirmations and evaluation are identical whichever model runs.
- **Tool layer** (`engine/tools/`): typed tools that combine systems and do all arithmetic in
  `shared/finance.py`, applying policy rules from the policy system's machine-readable parameters. Each
  result carries the rule checks and source records behind it, so answers are verifiable.
- **Security**: the verified customer id is injected server-side, never supplied by the model; ownership
  is checked on every id; actions return a proposal and only execute after the customer confirms in a
  later turn; every tool call, action and guardrail decision is recorded in the database.
- **Documents** (`mock_systems/statements.py`): repayment statements are produced by the lender, not the
  assistant. The assistant requests one; the mock loan system generates a fixed-format PDF from its own
  records (instalments, principal and interest paid, charges, opening and closing balance), stores it and
  serves it back by reference only to the customer who owns it. The chat shows a download button.
- **Login sessions** (`engine/server.py`): sign-in returns a random bearer token that the server stores only
  as a SHA-256 hash, with idle (30 min) and absolute (8 h) expiry from the governance policy. It travels
  in the `Authorization` header, never in URLs, so it cannot leak into access logs. A refresh or an API
  restart does not sign the customer out: a valid session is restored from the database with its
  conversation. Sign-out revokes the session immediately; revoked sessions are kept for audit.
- **Resilience** (`engine/clients.py`): per-system timeouts, retries with backoff, circuit breaker,
  policy caching. An unavailable system degrades the answer instead of failing the conversation.
- **Packs** (`engine/packs.py`): a workflow is configuration (tool groups + domain instructions) over the
  shared engine. Two ship: loan servicing and credit card servicing (`engine/tools/cards.py`: charge
  explanations, late fee reversal, transaction disputes, card block). A session gets the packs that match
  what the customer holds, so a customer with a loan and a card talks to one assistant. The card workflow
  was one new tools file (~250 lines) and a pack entry; clients, scoping, policy lookup, confirmations,
  tickets, audit, the agent loop, web and voice were reused unchanged.
- **Voice** (server side here; browser side in the web app repo): the same session, tools and
  history as text; only the reply style changes. Streaming speech recognition in the browser, replies
  spoken sentence by sentence as they stream (OpenAI `gpt-4o-mini-tts`, browser voice as fallback).
  Talking over the assistant stops the audio and cancels the turn on the server, which keeps the
  conversation history valid and lets in-flight confirmations finish. Lookups slower than 0.4 s get a
  spoken filler ("Let me check your payment records"), and a second one after 2.5 s. Each voice turn logs
  transcript time, time to first sound and time to first answer audio.
- **Guardrails and governance** (`engine/governance.py`, rules in `data/governance/policy.json`):
  - *Input*: OTP / PIN / CVV / password messages are blocked before the model sees them; card numbers,
    Aadhaar and PAN are redacted; prompt-injection attempts are flagged.
  - *Output*: a grounding check verifies that every amount, percentage and account id in a reply appears in
    data the tools returned; anything else is flagged and shown to the customer as unverified.
  - *Actions*: kill switches, channel restrictions, a per-session action cap and amount limits (e.g.
    prepayments above Rs 10 lakh go to a relationship manager) are enforced in code before a proposal.
  - *Channels and models*: voice requires the customer's call-recording consent; only allow-listed models
    can serve customers. Risk teams change the policy file, not code.
- **Storage** (`engine/storage.py`): conversations, messages, tool calls, per-turn latency, audit and
  guardrail events in PostgreSQL (SQLite when `DATABASE_URL` is unset). Writes go through a background
  queue so the database never adds reply latency. Each customer keeps their **last 5 conversations**
  (retention policy) and can reopen them in the web app; audit and guardrail events are retained
  separately. Secrets are redacted before anything is stored.
- **Observability** (`engine/observability.py`): Langfuse traces per turn, grouped by session and
  customer: model calls with token usage, every tool call, guardrail decisions. PII is masked before data
  leaves the service. Enabled by setting `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`.

## Quick start

Requires Python 3.10+ with [uv](https://docs.astral.sh/uv/). The web app additionally needs Node.js 20+.

```bash
uv sync
cp .env.example .env                      # add OPENAI_API_KEY
uv run python -m datagen.generate         # synthetic data (deterministic)
```

Then, in two terminals:

```bash
uv run python -m mock_systems.run                 # 1: six mock systems on ports 8101-8106
uv run uvicorn engine.server:app --port 8000      # 2: assistant API
```

Then run the [web app](https://github.com/GovindSinghBisht/blue-machines-fde-web) (`npm install && npm run dev`,
http://localhost:3000) and sign in with a demo customer (OTP `123456`). The API allows the web app's
origin via `WEB_ORIGINS` (default `http://localhost:3000`). A terminal client is also available:
`uv run python -m engine.cli --phone +919800000001`.

To work on the UI without an LLM key, run `uv run python -m tests.offline_server` instead of terminal 2:
a keyword-routed stand-in that calls the real tools.

Database: without configuration a local SQLite file (`data/local.db`) is used. For PostgreSQL, put the
server's admin credentials in `.env` (`DB_HOST`, `DB_ADMIN_USER`, `DB_ADMIN_PASSWORD`) and run once:

```bash
uv run python -m engine.db_setup
```

It creates the `blue_machines` database and its tables as the admin, then a least-privilege
`blue_machines_app` user with only the table rights the code needs: read/write on conversations,
retention deletes, and **append-only** access to audit and guardrail events, so even a compromised app
cannot rewrite the audit trail. No CREATE / DROP / ALTER, and other roles cannot connect to the
database. It writes `DATABASE_URL` for the app user into `.env`; remove the admin password afterwards.
`/api/health` reports the active storage backend and whether tracing is on.

Tests and evaluation:

```bash
uv run pytest -q                          # math, data, tools, agent, API, voice, storage, guardrails (no key needed)
uv run python -m datagen.validate         # data integrity + persona stories
uv run python -m evals.run                # 19 graded conversations against the real model + latency report
```

Change a system's behaviour live (e.g. make payments slow and flaky for a demo):

```bash
curl -X PUT localhost:8103/admin/chaos -H 'content-type: application/json' -d '{"p50_ms": 1500, "error_rate": 0.3}'
```

## Layout

| Path | What |
|---|---|
| `shared/finance.py` | Deterministic loan math (EMI, amortization with rate resets and prepayments, prepayment impact). |
| `data/policies/` | Versioned policy clauses with machine-readable `params` (one superseded version on purpose). |
| `datagen/` | Synthetic data generator, demo personas, validator. |
| `mock_systems/` | One FastAPI app per enterprise system, with fault injection and idempotent writes. |
| `engine/` | Resilient clients, tool registry and confirmation gate, tool packs, agent loop, LLM providers, guardrails and governance, storage, tracing, web API (`server.py`), terminal client, database setup. |
| `data/governance/policy.json` | Governance policy: retention, PII handling, action limits and kill switches, voice consent, model allow-list. |
| `evals/` | Graded conversations and the latency/accuracy runner. |
| `tests/` | Unit and integration tests (in-process systems, scripted model). |

## Generated systems

All records are as of **2026-10-22** (`AS_OF`); mock services treat this as "today".

| File | System | Contents |
|---|---|---|
| `crm.json` | Customer CRM | customers, contact details, consents, interaction history |
| `loans.json` | Loan Management | loans with full schedules, rate history, reset preference, NACH mandate; charges ledger |
| `payments.json` | Payments | every debit attempt: NACH presentations, failures with reason codes, customer payments, duplicate debits |
| `service_ops.json` | Service Operations | ticket categories with SLA, tickets with history and SLA breach status |
| `cards.json` | Cards (workflow #2) | cards, monthly statements, transactions, disputes |

Data is consistent across systems, not just random rows. Instalment status follows from the payment
attempts, charges are levied by the policy version in force on the date (`LP-CHG-001` v1 vs v2), bank-side
failures get the charge waived (`LP-CHG-002`), and floating-rate schedules replay every EBLR change.

## Demo personas

| Customer | Login phone | Built to show |
|---|---|---|
| Priya Sharma `CUST100001` | +91-9800000001 | Home loan EMI up in Oct because EBLR rose 8.50 → 9.00; 2 failed debits in the last 6 months (one chargeable, one bank-side and waived); prepay ₹50,000 with no charge; bounce-charge waiver eligible; top-up **not** eligible (recent bounce) |
| Arjun Mehta `CUST100002` | +91-9800000002 | Personal loan with 7 EMIs paid and Oct EMI overdue: part-prepayment blocked for two reasons (lock-in and overdue), foreclosure blocked by lock-in; card autopay failed, paid 4 days late, ₹950 + GST late fee (reversal eligible) |
| Kavya Iyer `CUST100003` | +91-9800000003 | LAP on tenure-reset: rate rose, EMI unchanged, tenure extended; Oct EMI debited twice, refund pending, ticket past SLA; open card dispute |
| Rohan Gupta `CUST100004` | +91-9800000004 | Two active loans ("my loan" is ambiguous); personal loan past 24 EMIs, so part-prepayment allowed with a 25% free limit and 2% foreclosure slab |

Plus 200 background customers (risk-weighted failure rates, closed loans, spread revisions, prepayments)
for realistic volumes: ~224 loans, ~5.7k payments, ~7k card transactions.

## Simplifications (deliberate)

- Monthly-rest interest; EMIs rounded to the rupee with the last instalment absorbing rounding.
- Rate changes apply from the first EMI due after the effective date.
- Charges on a late instalment are collected with the next EMI.
- Card finance charges apply only to balances carried forward, not retroactively to transaction dates.
