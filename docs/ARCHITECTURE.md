# Architecture, decisions and trade-offs

A concise account of how the loan-servicing assistant is built, why it is built that way, what was
traded off, and how it would evolve toward production. Measured results are in
[PERFORMANCE.md](PERFORMANCE.md). Setup and the demo personas are in the [README](../README.md).

## 1. The problem, in one line each

| Brief | What it demands of the design |
|---|---|
| A. Precise answers | Figures must come from the customer's records, the policy in force and real calculation, not from document retrieval or the model's memory |
| B. Voice that feels natural | Interruptions, topic changes and follow-ups, and responsiveness even when a backend is slow |
| C. Requests across systems | Read from several systems, reason over business rules, and act on the customer's behalf, safely |
| D. New workflows in days, not weeks | One engine that a second use case reuses, not a second assistant |

The central idea: **the LLM understands and explains; deterministic code fetches, computes and decides.**
Everything below follows from that split.

## 2. System overview

```
 Web app (Next.js, separate repo)                       Backend (this repo)
 ┌──────────────────────────────┐    REST + SSE    ┌───────────────────────────────────────────────────┐
 │ Chat · account panel · past  │ ───────────────▶ │ API (FastAPI)  sessions · streaming · voice TTS     │
 │ conversations · confirm cards│ ◀─────────────── │      │                                              │
 │ Voice: browser STT, streamed │   words, tools,  │      ▼                                              │
 │ TTS, barge-in, fillers       │   actions, docs  │ Governance ── input guardrails / action limits /    │
 └──────────────────────────────┘                  │      │         output grounding check              │
                                                   │      ▼                                              │
                                                   │ Agent loop ◀──▶ LLM provider (OpenAI, swappable)    │
                                                   │      │   packs choose tools + instructions          │
                                                   │      ▼                                              │
                                                   │ Tools (23) ── loan maths · policy clauses · checks  │
                                                   │      │                                              │
                                                   │      ▼                                              │
                                                   │ Resilient clients: timeout · retry · breaker · cache│
                                                   └──────┼───────────────────────────────┬─────────────┘
                                                          ▼                               ▼
                                   Enterprise systems (mocked, with injected        PostgreSQL: chats,
                                   latency and failures): CRM · Loans · Payments ·  sessions, audit,
                                   Policy · Service Ops · Cards                     metrics  ·  Langfuse
```

**One turn**, for example "Why did my EMI go up?":

1. The bearer token is resolved to a server-side session. The customer ID lives there and never comes from the client or the model.
2. Input guardrails redact or block sensitive data.
3. The model, which already has the customer snapshot loaded at sign-in, calls `explain_emi_change`.
4. The tool checks ownership, fetches the schedule, rate history and rate policy **in parallel**, and finds the instalment, the reset and the benchmark move.
5. It returns facts with their sources.
6. The model explains them, streamed word by word.
7. An output check confirms every rupee figure, percentage and account ID appears in what the tools returned.
8. Messages, tool calls, metrics and audit are written by a background queue, and the turn is traced.

## 3. Key decisions

### 3.1 Tools compute; the model explains (not RAG)

**Decision.** Every customer-specific number comes from a typed tool. A tool combines the systems of
record, applies policy clauses and does all arithmetic in `shared/finance.py` (EMI, amortisation with rate
resets and prepayments, prepayment impact). The model may only restate tool results. Each result carries
its rule checks (`passed`, `detail`) and its sources (`policy:LP-PRE-002 v1`, `loans:HL1000001/schedule#39`).

**Why.** Problem A is exactly where retrieval over documents fails. "What happens if I prepay ₹50,000?"
needs this customer's principal, the clause that applies to *their* product and rate type, charges, and a
recalculated schedule. A vector database can find the clause but can't do the rest.

**Trade-off.** Every new kind of question needs a tool, so coverage is engineered, not emergent. The
model refuses or hands over rather than improvising when no tool fits.

### 3.2 Policy as versioned data

**Decision.** Business rules are clauses with an ID, version, effective dates and scope (product, rate
type, borrower type), with human text **and** machine-readable `params`, e.g. `min_emis_paid: 12` or
charge slabs. The policy system serves the version in force on a date. Tools pick the most specific clause
that matches the loan and read its parameters.

**Why.**
- Answers cite the exact clause and version.
- Historical charges are explained by the rule that applied at the time: the bounce charge changed in April 2025, and both versions are in the data.
- A rule change is a data change, not a code change.

**Trade-off.** Clauses must be structured by someone who understands them. Free-text policies would need
an extraction step, with review, before they can drive decisions.

### 3.3 A small hand-written agent loop, no planner, no framework

**Decision.** A bounded loop (at most 8 steps):
- the model picks tools,
- independent calls run in parallel,
- the results go back to the model,
- it stops when the model answers in text.

Conditional, multi-system logic lives *inside* tools, in dependency order. Across tools, the model plans
step by step from what each result shows.

**Why.**
- The engine needs things frameworks hide: the turn counter that gates confirmations, per-step timing, token streaming shared with voice, and repairing the conversation history after an interruption.
- A separate planning call would add about a second to every turn, which hurts voice.
- Evals average 1.9 steps per turn and never more than 4.

**Trade-off.** Long compound requests ("compare prepay vs foreclose, then raise the cheaper one") rely on
the model sequencing well. If evals or traces showed failures there, I would add a router that sends only
those through an explicit plan step.

### 3.4 Safety enforced in code, not in the prompt

- **Identity:** the customer ID is injected from the session into every tool. Any ID the model passes is ownership-checked. A foreign ID returns `NOT_FOUND`, which doesn't reveal that the record exists.
- **Actions are proposals.** An action tool returns a summary and a deferred write. The write runs only through `confirm_action`, and only if the proposal was made in an **earlier turn** than the confirmation: the model cannot confirm on its own.
- **Writes are safe to retry and to interrupt.** Writes carry idempotency keys, so a retry can't create a second ticket. A confirmation that has started finishes even if the customer barges in.
- **Governance limits apply before a proposal exists:** kill switches, channel restrictions, a per-session action cap and amount caps.

**Why.** Prompts guide behaviour; they don't guarantee it. Each of these properties holds whatever the
model says, and each has a test.

### 3.5 Assume the backends are slow and unreliable

**Decision.**
- Every system call goes through one client layer:
  - a timeout set for each system,
  - jittered retries (writes only with an idempotency key),
  - a circuit breaker,
  - a 5-minute cache for policy only,
  - a latency span per call.
- Failures become explicit `SYSTEM_UNAVAILABLE` results. The prompt tells the model to share what it has, name what's missing and never fill the gap.

The mock systems inject log-normal latency, 503s and hangs, which can be changed at runtime
(`PUT /admin/chaos`), so this is demonstrated rather than claimed.

**Trade-off.** Retries add latency on bad days. Timeouts are tuned for each system, and the breaker caps
the worst case.

### 3.6 The reusable capability: workflow packs

**Decision.** A workflow is configuration: which tool groups it exposes, plus domain instructions. A
session gets the packs matching what the customer holds, so a loan-and-card customer talks to one
assistant. Everything else is shared:
- the agent loop,
- clients and resilience,
- identity scoping and the confirmation gate,
- tickets, audit and governance,
- storage and tracing,
- streaming and voice.

**Evidence.** The credit card workflow was one tools file (7 tools, 3 of them actions), 6 policy clauses,
a mock card system and one pack entry. The engine, the web app and voice were unchanged, and 3 of the 19
evals cover cards, including a mixed card-and-loan conversation.

**Trade-off.** Packs share one prompt as they're combined, so a customer with many products gets a longer
prompt. Fine for two packs; at five or more I would route by intent first and load only the relevant pack.

### 3.7 Transport: REST with server-sent events, not WebSockets

**Decision.** Each message is a POST whose response streams events:
- `tool` (progress),
- `text` (words),
- `action` (a confirm card),
- `document` (a PDF ready to download),
- `filler` (voice),
- `done` (metrics and sources).

Closing the request cancels the turn.

**Why.** A turn is a request with a streamed reply. SSE keeps every request independent, so any instance
can serve it, it passes through proxies and load balancers unchanged, and auth is an ordinary header. The
brief doesn't prescribe a transport.

**Trade-off.** There's no server-initiated push, and audio doesn't travel to the server (see 3.8).

### 3.8 Voice: optimise the interaction, keep the engine

**Decision.** Voice is a channel, not a second system. It uses the same session, tools and history as
text; only the reply style changes (short sentences, no lists or codes).
- The browser does speech recognition and sends text.
- Replies are spoken sentence by sentence as they stream, using OpenAI TTS with the browser voice as a fallback.
- **Barge-in** stops playback and cancels the server turn, and the engine repairs the history so the model knows what was actually heard.
- Lookups slower than 0.4 s get a spoken filler.
- Every voice turn reports transcript time, time to first sound and time to first answer audio.

**Why.** The brief evaluates the interaction (interruptions, changing intent, responsiveness under slow
backends), so that's where the effort went.

**Trade-off.** Browser speech recognition is best in Chrome, limited on Safari and iOS, and not usable for
telephony. The production path is in section 5.

### 3.9 Storage: only what the assistant owns

**Decision.** PostgreSQL holds conversations, messages, tool calls, per-turn metrics, hashed login
sessions, and append-only audit and guardrail events. Business data stays in the systems of record.
- **Writes go through a write-behind queue**, batched into one transaction, so a database 200–380 ms away never delays a reply, and a storage failure is logged, never fatal.
- **Retention:** the last 5 conversations per customer. Audit is kept separately.
- **Least privilege:** the app's database user can't create, alter or drop anything, and it can only insert into audit tables.

**Trade-off.** A crash can lose the last few queued chat rows. Audit-critical writes (sign-in sessions)
are written synchronously.

### 3.10 Sessions that survive restarts

**Decision.** Sign-in returns a random token, stored only as a SHA-256 hash, with an idle expiry (30 min)
and an absolute expiry (8 h) taken from the governance policy. Live conversation state is held in the
instance's memory for speed. If a request reaches an instance without it (after a restart or a crash), the
session and its conversation are restored from Postgres.

**Why.** Memory makes every turn fast, and restore makes instances replaceable: the load test killed an
instance mid-run and every conversation completed.

**Trade-off.** Restoring costs about 0.5 s (it re-reads the customer snapshot), and pending action
proposals aren't carried over, so the customer is asked again.

### 3.11 Governance as a file risk teams own

`data/governance/policy.json` covers:
- session times,
- retention,
- which PII to redact,
- whether secrets are blocked,
- the grounding tolerance,
- action caps and kill switches,
- voice consent,
- the model allow-list,
- PII masking in traces.

Every guardrail decision is a stored, traced event. Risk teams change behaviour without a deploy.

### 3.12 Observability

- **Langfuse:** one trace per turn (model steps with tokens, tool calls, guardrail decisions), grouped by session and customer, with PII masked before export.
- **Our own measurements:** client spans for every backend call and per-turn metrics in the database, which feed the performance report.

## 4. Trade-offs and known limits

| Area | Current state | Consequence |
|---|---|---|
| Coverage | Questions are answered only through tools | Unusual questions get "I can't confirm that" or a hand-over, not a guess |
| Output grounding | Checks figures and IDs after the reply has streamed | Ungrounded figures are flagged and recorded, not prevented mid-sentence |
| Voice | Browser STT; first answer audio ~4–5 s in development samples | Works in Chrome; Safari, mobile and phone need the streaming gateway |
| Authentication | Mock OTP (fixed code) | Fine for a demo; production uses the lender's OTP or identity service |
| Session state | In instance memory, restorable | Needs session affinity at the load balancer; pending proposals are lost on failover |
| Empty conversations | A sign-in with no questions still takes a retention slot | Known bug; fix is to create the conversation row on the first message |
| Mock systems | In-memory; writes reset on restart | Good for repeatable demos; real systems replace them by URL |
| Scale evidence | One laptop; LLM replaced by a timed stand-in in the load test | Proves our stack; vendor throughput is a separate, contractual limit |

## 5. Path to production

| Area | Production step |
|---|---|
| Deployment | Containers behind a load balancer with session affinity; autoscale on active conversations (~200 per instance measured); N+1 headroom |
| LLM | An enterprise or provisioned rate-limit tier sized for ~400 model calls/s at 1,000 conversations; a faster model for voice; a second provider behind the same interface for failover |
| Voice | A streaming audio gateway (WebSocket or WebRTC; SIP from the telephony provider) with server-side STT and TTS, feeding the same engine; voice consent and recording governed as now |
| Identity | The lender's OTP or SSO; step-up verification for high-value actions; device binding |
| Integrations | Swap mock URLs for real system APIs behind the client layer; mTLS and service credentials from a secrets manager; contract tests per system |
| Data | Postgres in a private subnet, encrypted at rest; a shared cache (e.g. Redis) for snapshots and policy so failover is cheap; retention and audit policies agreed with compliance |
| Safety | Grounding check promoted from flagging to blocking for actions; red-team prompt-injection tests; human review queue for flagged turns |
| Operations | Dashboards and alerts on first-word latency, turn errors, breaker state and guardrail rates; eval suite in CI on every prompt or tool change; canary releases for model changes |
| New workflows | Insurance or collections as packs: tools, clauses, a mock or real system, a pack entry and eval cases, with no engine changes (below) |

## 6. Adding a workflow (or the interview's new requirement)

1. **Policy:** add clauses with `params` to `data/policies/`.
2. **System:** point a client at the real API, or add a mock in `mock_systems/` using `make_app` (you get fault injection and idempotency for free).
3. **Tools:** write typed tools in `engine/tools/<pack>.py` with `@tool(..., packs=("<pack>",))`.
   - Read tools return facts, checks and sources.
   - Action tools return a `Proposal`.
4. **Pack:** add an entry in `engine/packs.py` (tool groups + domain instructions), and a rule in `packs_for` for which customers get it.
5. **Evals:** add graded conversations to `evals/cases.py`.

The loop, confirmations, governance, storage, tracing, streaming and voice need no change. A new rule on
an existing workflow is usually step 1 alone. A new action is step 3 plus an eval.
