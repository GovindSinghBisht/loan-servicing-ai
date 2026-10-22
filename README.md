# AI Loan Servicing — Blue Machines FDE Challenge

Work in progress. Day 1: synthetic enterprise data.

## Quick start

```bash
python3 -m datagen.generate      # writes data/generated/*.json (deterministic, ~3s)
python3 -m datagen.validate      # integrity checks + demo persona stories
python3 -m pytest -q tests
```

Requires Python 3.10+, no third-party packages for data generation.

## Layout

| Path | What |
|---|---|
| `shared/finance.py` | Deterministic loan math (EMI, amortization with rate resets and prepayments, prepayment impact). Used by the generator now and by the query engine's tools later, so the LLM never does arithmetic. |
| `data/policies/` | Hand-authored policy clauses with IDs, versions and machine-readable `params` (one superseded version included on purpose). Served by the Policy Knowledge system. |
| `datagen/` | Generator, demo personas, validator. |
| `data/generated/` | One file per backend system (git-ignored; regenerate any time). |

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
