# AI Loan Servicing — Blue Machines FDE Challenge

A conversational assistant for a large lender's loan customers: precise, customer-specific answers over
enterprise systems (CRM, loan management, payments, policy, service operations), with authorized
actions and a reusable platform for further workflows.

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
  later turn; every tool call and action is audit-logged (`logs/audit.jsonl`).
- **Resilience** (`engine/clients.py`): per-system timeouts, retries with backoff, circuit breaker,
  policy caching. An unavailable system degrades the answer instead of failing the conversation.
- **Packs** (`engine/packs.py`): a workflow is configuration (tool groups + domain instructions) over the
  shared engine.

## Quick start

Requires Python 3.10+ with [uv](https://docs.astral.sh/uv/), and Node.js 20+.

```bash
uv sync
cp .env.example .env                      # add OPENAI_API_KEY
uv run python -m datagen.generate         # synthetic data (deterministic)
```

Then, in three terminals:

```bash
uv run python -m mock_systems.run                 # 1: six mock systems on ports 8101-8106
uv run uvicorn engine.server:app --port 8000      # 2: assistant API
cd web && npm install && npm run dev              # 3: web app on http://localhost:3000
```

Sign in with a demo customer (OTP `123456`). A terminal client is also available:
`uv run python -m engine.cli --phone +919800000001`.

To work on the UI without an LLM key, run `uv run python -m tests.offline_server` instead of terminal 2:
a keyword-routed stand-in that calls the real tools.

Tests and evaluation:

```bash
uv run pytest -q                          # math, data, tools, agent loop (no API key needed)
uv run python -m datagen.validate         # data integrity + persona stories
uv run python -m evals.run                # 16 graded conversations against the real model + latency report
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
| `engine/` | Resilient clients, tool registry and confirmation gate, tool packs, agent loop, LLM providers, web API (`server.py`), terminal client. |
| `web/` | Customer web app (Next.js, React, Tailwind): chat with live progress, sources, confirmations, account panel. |
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
