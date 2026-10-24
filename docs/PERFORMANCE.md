# Performance and accuracy report

Measured on 24 Oct 2026 on one MacBook (8 cores). Everything here can be re-run with the commands at the
end. Numbers are in milliseconds unless stated.

## Summary

| Question | Result |
|---|---|
| Are answers correct and grounded? | **19/19 conversations, 27/27 turns** pass with the real model (gpt-4.1), with backend failures injected |
| How fast does a customer see the answer start? | **2.1 s median, 4.8 s p95** to the first word with the real model; 2.8 s / 5.6 s for the whole turn |
| Where does the time go? | The model: **2.4 s** of a typical turn. Tools and backends: **0.37 s** (several systems called in parallel) |
| How far does one instance go? | About **200 concurrent conversations** with flat latency; beyond that it becomes CPU-bound |
| Can it reach 1,000 concurrent conversations? | **Yes, on 6 instances**: 100% success in two runs, first word 2.4–2.5 s median, 3.5–4.6 s p95 |
| What if an instance crashes? | **Every conversation still completed**: sessions are restored from Postgres on the remaining instances |
| What limits scale beyond that? | LLM vendor throughput (a contract and rate-limit tier), then instance count; not the database or the backends |

## 1. Accuracy: conversational evaluation with the real model

`evals/run.py` plays 19 scripted conversations (27 turns) against the mock systems with fault injection
on, using the real model. Each turn is graded deterministically. The checks: the exact figures must
appear (e.g. Rs 43,298, 106 → 110 instalments), figures from other customers must not, the right tools
must be called, and actions may run only after the customer confirms.

| Area | Conversations | Examples |
|---|---|---|
| Query engine (data + policy + calculation) | 10 | Why the EMI changed; prepay 50,000 and the interest saved; failed payments in 6 months; foreclosure eligibility; options on a loan; an EMI that stayed the same while the tenure grew |
| Voice-style topic changes | 1 | "What's my next EMI?" → "Actually, check my last payment first" → "Would 20,000 extra reduce my tenure?" |
| Actions with confirmation | 3 | Propose a waiver, then confirm next turn; find an existing duplicate-debit ticket and escalate it; card fee reversal |
| Second workflow (cards) on the same engine | 3 | Late fee explanation; fee reversal; card and loan questions in one conversation |
| Security | 2 | Another customer's loan is refused; the model cannot confirm its own action |
| Degraded backends | 2 | Payments system down (partial, honest answer); slow backends (still correct) |

**Result: 19/19 conversations, 27/27 turns.** The first run scored 12/19. What fixing it found:

| Failure | Cause | Fix |
|---|---|---|
| Card cases failed | The eval harness gave every customer only the loans pack | Harness now chooses packs the same way the API does |
| "You can prepay Rs 50,000" said **without checking** | The model answered from the snapshot | Rule: never state eligibility unless a tool checked it in this conversation |
| Customer had to confirm twice | The model asked before proposing the action | Rule plus tool descriptions: propose in the same turn, so the customer confirms once |
| Tenure change described without numbers | Vague wording | Rule: state the before and after figures the tool returns |
| Ticket number not quoted | — | Rule: always quote service request numbers |
| Disputed amount missing | Snapshot dropped ticket descriptions | Snapshot now includes them |

**How the model works in practice:**
- **1.9 model steps per turn on average.** 20 of 27 turns took two steps (fetch, then answer), 5 took one (answered from the snapshot), and none came near the 8-step limit. This supports not having a separate planning step.
- **76% of input tokens were served from the prompt cache** (123k of 161k), because the stable system prompt comes first.
- **Fault injection was active:** 4 backend retries and 1 unavailable system across 106 backend calls, all absorbed without a wrong answer.

## 2. Conversational latency with the real model

From the final eval run (27 turns, gpt-4.1, fault injection on):

| Metric | p50 | p95 |
|---|---|---|
| Time to first word (what the customer feels) | 2,149 | 4,771 |
| Whole turn | 2,799 | 5,639 |
| Model time per turn | 2,395 | — |
| Tools and backends per turn | 371 | — |
| Model time per step | 1,212 | 1,505 |

Backend calls made by the tools (with injected latency and failures):

| System | Calls | p50 | p95 | Injected profile (p50 / p95, errors) |
|---|---|---|---|---|
| Policy | 14 | 46 | 86 | 40 / 120, 0.5%; cached for 5 min |
| Service ops | 8 | 94 | 335 | 200 / 700, 3% |
| Loans | 55 | 159 | 616 | 120 / 400, 2% |
| Cards | 4 | 167 | 552 | 150 / 500, 2% |
| Payments | 11 | 312 | 846 | 350 / 1,200, 4% (the slow legacy system) |

**Reading:** about 85% of a turn is the model, and our own stack adds roughly 0.4 s. The slowest system
(payments, p95 1.2 s) doesn't add up with the others, because independent calls run in parallel. Each
system has its own timeout, and the circuit breaker stops a dead system from stalling the turn.

## 3. Voice

Voice turns use the same API, with timings reported by the browser (`/api/voice-metrics`):
- speech end → final transcript,
- speech end → first sound (a filler or the answer),
- speech end → first answer audio,
- request → first token,
- whether the customer interrupted.

The samples recorded so far come from development testing (7 turns), not a real-microphone session, so
they are indicative only:

| Metric | Observed |
|---|---|
| First sound, when a filler covers a slow lookup | ~0.4 s |
| First answer audio | 4.2–4.9 s |
| Interruptions | Stopped playback and cancelled the server turn in every interrupted sample |

First answer audio is the weakest number. It is model time to first token (~2–3 s) plus the first
sentence of TTS. Ways to reduce it:
- use a faster model for the voice channel,
- start TTS on the first clause instead of the first sentence,
- move to streaming server-side STT and TTS (WebSocket or WebRTC), which is also the path to telephony and Safari or mobile.

The filler keeps the line from going silent in the meantime.

## 4. Load test: concurrent conversations

### Method

`loadtest/` runs **the real stack** with one substitution:

- **Real:** API, sign-in and hashed sessions, guardrails, agent loop, tools, policy rules, loan maths, resilient clients, SSE streaming, Postgres 17 storage.
- **Mock systems** run **with fault injection on**.
- **Substituted:** the LLM is a stand-in that makes the same kind of tool decisions (a keyword router calling real tools). It waits a log-normal time **calibrated to the real model above** (1.2 s p50, 1.5 s p95 per step) and streams its reply word by word.

This measures our capacity without hitting the LLM vendor's rate limits (see section 6).

Each virtual customer:
- signs in (CRM lookup plus a snapshot from four systems),
- asks 3 questions suited to what they hold, pausing about 2 s between them,
- then signs out.

Customers are drawn from all 204 synthetic profiles. "N concurrent" means N conversations open at the
same time, with sign-ins spread over 10 s. Load comes from 4 generator processes, and server-side timings
are compared with client-side ones to confirm the generator isn't the bottleneck. With several instances,
each conversation stays on the instance it signed in to (session affinity, as a load balancer would route it).

We did **not** load test against the production database (a shared RDS instance, where load would affect
other databases and leave append-only audit rows behind) or the live LLM (rate limits and cost). The run
uses a throwaway local Postgres of the same major version.

### Results

| Setup | Concurrent | Success | Turns/s | Sign-in p50 / p95 | First word p50 / p95 | Turn p50 / p95 / p99 |
|---|---|---|---|---|---|---|
| 1 instance | 50 | 100% | 5.9 | 462 / 2,188 | 2,348 / 3,552 | 2,543 / 3,693 / 4,823 |
| 1 instance | 200 | 100% | 21.9 | 411 / 1,286 | 2,389 / 3,458 | 2,578 / 3,636 / 4,431 |
| 1 instance | 500 | 98.2% | 22.7 | 1,509 / 17,211 | 6,915 / 23,221 | 7,859 / 24,271 / 30,460 |
| 1 instance | 1,000 | 76.0% | 17.3 | 22,060 / 53,726 | 15,888 / 45,677 | 16,903 / 47,042 / 58,554 |
| 4 instances | 1,000 | 100% | 76.0 | 434 / 1,810 | 2,679 / 8,408 | 2,920 / 9,180 / 11,711 |
| **6 instances (run 1)** | **1,000** | **100%** | 93.2 | 463 / 1,104 | **2,528 / 4,636** | 2,756 / 4,903 / 6,658 |
| **6 instances (run 2)** | **1,000** | **100%** | 101.0 | 433 / 1,024 | **2,399 / 3,536** | 2,596 / 3,716 / 5,196 |

- At 1,000 conversations on 6 instances, each instance peaked at **50–64% of one core and about 95 MB of memory**.
- Backend failures that reached a customer: **0.00–0.01%** of calls. Retries and the circuit breaker absorbed the rest.
- Server-side and client-side first-word times agreed within 1–4%.

**Reading:**
- One Python process serves about 200 concurrent conversations with latency no worse than at 50. Past that, the event loop is CPU-bound: tool time climbs from 0.4 s to 4.5 s and connections start being refused.
- The design scales out by adding instances, because they share nothing except Postgres. Sessions survive on any instance.
- **1,000 concurrent conversations need about 6 instances**, plus one spare for failure (below).

### Instance failure

During 1,000 conversations on 6 instances, one instance was killed with SIGKILL 14 s into the run (a crash, not a graceful shutdown):

- **100% of conversations completed.** 161 requests were moved to the remaining instances: 114 streams cut off mid-turn and 47 refused connections.
- The next instance accepted the same token, **restored the session from Postgres** (conversation history included) and answered.
- A repeat with 7 instances (one spare) gave the same result: 100% completed, 134 requests moved.

What the crash test **didn't** measure well is restore latency during the burst. Restored turns took
22–27 s at p50, but the whole machine was at 99% CPU during these runs. That's 7 API instances, the mock
systems, Postgres and 4 generator processes on 8 cores, so the number reflects the laptop, not the design.

Measured restore cost on its own:
- **+0.5 s** on an idle system,
- **0.4–0.8 s** under steady load.

It costs that much because a restore re-reads the customer snapshot from four systems.

Production measures for this:
- one spare instance (N+1),
- jittered client retry,
- caching the snapshot in a shared cache so a restore doesn't hit four systems at once.

A turn that was mid-stream when its instance died is retried in full. That's safe because writes run only
through `confirm_action` with idempotency keys, so a repeated request can't create a second ticket.

### What the load test changed in the code

| Found | Fix |
|---|---|
| Sign-ins failed with `QueuePool limit of size 5 overflow 10 reached` | The pool is configurable (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`), default 10 + 20 |
| `flush()` waited for an empty write queue, which may never happen under steady load | It now waits only for writes queued before the call (bounded) |
| SQLite failed immediately with `database is locked` | `busy_timeout` set (local development only; production uses Postgres) |

## 5. How latency is kept low (design choices behind the numbers)

- **Snapshot at sign-in:** the customer overview is loaded once, so many questions are answered in one model step.
- **Parallel tool calls and parallel backend calls inside tools:** a turn waits for the slowest system, not the sum.
- **Streaming everywhere:** words go to the browser as the model writes them, so the customer sees text at the first word, not the end of the turn.
- **Write-behind storage:** conversation logging never waits on the database. That matters with a remote database (the RDS round trip from India is 190–380 ms).
- **Prompt caching:** the stable prompt prefix is identical for every customer (76% of input tokens cached).
- **Per-system timeouts, bounded retries, a circuit breaker and a 5-minute policy cache:** a slow or failing system costs at most its timeout, and a dead one fails in about 0 ms.
- **Voice fillers:** a lookup slower than 0.4 s gets a spoken filler, so the line is never silent.

## 6. Limits and what production would need

| Limit | Plan |
|---|---|
| **LLM throughput.** 1,000 conversations at ~1 turn per 5 s ≈ 200 turns/s ≈ 400 model calls/s | A provisioned or enterprise rate-limit tier (or a dedicated deployment). The real constraint, and a commercial one |
| CPU per instance (~200 conversations) | Horizontal scaling behind a load balancer with session affinity; autoscale on active conversations |
| Session state in instance memory | Restore from Postgres already works; a shared cache (e.g. Redis) for snapshots would make failover cheaper |
| Voice first-answer latency (~4–5 s) | Faster voice model, earlier TTS start, streaming STT/TTS gateway (WebSocket/WebRTC/SIP) |
| Mock systems run in one process (86% CPU at 1,000) | Real enterprise systems scale independently; the client layer already isolates their latency and failures |
| Numbers come from one laptop | Repeat on cloud instances next to the database, then a soak test (hours) to check memory and connection stability |

## Reproducing

Accuracy and real-model latency (needs `OPENAI_API_KEY`; restart the mock systems first so earlier runs' writes don't carry over):

```bash
uv run python -m mock_systems.run
```
```bash
uv run python -m evals.run --concurrency 4
```

Load test (local Postgres on port 5433; use one instance or several):

```bash
for p in 8001 8002 8003 8004 8005 8006; do PORT=$p LOADTEST_DATABASE_URL=postgresql+asyncpg://loadtest@127.0.0.1:5433/loadtest uv run python -m loadtest.server & done
```
```bash
uv run python -m loadtest.run --api http://127.0.0.1:8001 http://127.0.0.1:8002 http://127.0.0.1:8003 http://127.0.0.1:8004 http://127.0.0.1:8005 http://127.0.0.1:8006 --procs 4 --levels 500 1000
```

Add `--failover` and kill one instance during the run to repeat the crash test. Results are written to
`evals/results/` and `loadtest/results/` (not committed).
