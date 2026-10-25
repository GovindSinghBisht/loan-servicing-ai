# Performance and accuracy

These measurements were taken on 24 October 2026 on one MacBook with 8 cores. Every number can be
reproduced with the commands at the end. Times are in milliseconds unless stated otherwise.

## Summary

With the real model, the assistant answered all 19 evaluation conversations correctly while backend
failures were being injected. A customer sees the first word of a reply after about 2.1 seconds on
average, and 4.8 seconds in the slowest 5% of turns. Most of that time is the language model. The tools
and the backend systems together take about 0.4 seconds, because independent calls run at the same time.

One instance of the API handles about 200 concurrent conversations without slowing down. Six instances
handled 1,000 concurrent conversations with every turn completing, in two separate runs. When I killed
one instance in the middle of a run, every conversation still completed because the other instances
restored the sessions from the database. Beyond that, the limit is the language model provider's
throughput, which is a matter of contract and rate limit tier, and then the number of instances. It is
not the database or the backend systems.

## Accuracy

`evals/run.py` plays 19 scripted conversations, 27 turns in total, against the mock systems with fault
injection switched on, using the real model (gpt-4.1). Each turn is graded automatically. The exact
figures must appear in the reply, such as Rs 43,298 or a tenure going from 106 to 110 instalments.
Figures belonging to other customers must not appear. The right tools must be called, and an action may
only run after the customer confirms it.

The conversations cover the main areas of the brief. Ten test the query engine on questions that need
account data, policy and calculation together: why an EMI changed, what a 50,000 prepayment saves, which
payments failed in six months, whether foreclosure is free, what options a loan has, and why a rate rise
extended a tenure instead of raising the EMI. One follows the voice example from the brief, where the
customer asks about the next EMI, switches to the last payment and then asks about paying 20,000 extra.
Three test actions with confirmation, including a waiver, escalating an existing duplicate debit ticket
and a card fee reversal. Three test the credit card workflow on the same engine. Two test security, one
asking for another customer's loan and one checking that the model cannot confirm its own action. Two
test degraded backends, one with the payments system down and one with every system slow.

The first run passed 12 of 19 conversations, and fixing the failures was useful in itself. The card
cases failed because the evaluation runner only gave customers the loans pack, so it now chooses packs
the same way the API does. More seriously, the model once told a customer they could prepay without
checking, so the prompt now forbids stating eligibility unless a tool has checked it. Customers were
sometimes asked to confirm an action twice. The fix has three parts: the payment history now marks bounce
charges that can still get a goodwill waiver, an eligible check tells the model to propose the action in
the same turn, and the model is told never to ask permission just to look something up. The model also
described a tenure change without the numbers and sometimes left out ticket numbers, so the prompt now
asks for before and after figures and reference numbers. Finally, the customer snapshot was missing
ticket descriptions, which it now includes.

After these fixes the full suite passed 19 of 19. Two later runs on fresh systems gave 19 of 19 and 18 of
19. The one miss was a correct answer about a card dispute that left out its reference number. The two
action conversations that had been unreliable, the waiver and the card fee reversal, passed 10 out of 10
times over five fresh runs.

The evaluation also shows how the model works in practice. Turns took 1.9 model steps on average. 20 of
27 turns took two steps, fetching data and then answering, 5 were answered straight from the snapshot,
and none came close to the limit of eight. 76% of input tokens were served from the prompt cache, because
the part of the prompt that is the same for everyone comes first. Fault injection was active throughout,
with 4 retries and 1 unavailable system across 106 backend calls, and none of it produced a wrong answer.

## Conversation latency with the real model

These numbers come from the evaluation run with 27 turns, gpt-4.1 and fault injection on.

| Measure | Median | 95th percentile |
|---|---|---|
| Time to the first word | 2,149 | 4,771 |
| Whole turn | 2,799 | 5,639 |
| Model time per turn | 2,395 | |
| Tool and backend time per turn | 371 | |
| Model time per step | 1,212 | 1,505 |

This is how the backend calls made by the tools performed, next to the latency and errors injected into
each mock system.

| System | Calls | Median | 95th percentile | Injected median and 95th percentile, error rate |
|---|---|---|---|---|
| Policy | 14 | 46 | 86 | 40 and 120, 0.5%, cached for 5 minutes |
| Service ops | 8 | 94 | 335 | 200 and 700, 3% |
| Loans | 55 | 159 | 616 | 120 and 400, 2% |
| Cards | 4 | 167 | 552 | 150 and 500, 2% |
| Payments | 11 | 312 | 846 | 350 and 1,200, 4%, the slow legacy system |

About 85% of a turn is the model, and our own stack adds roughly 0.4 seconds. The slowest system,
payments, does not add its delay on top of the others because independent calls run in parallel. Each
system has its own timeout, and the circuit breaker stops a dead system from holding up a turn.

## Voice

Voice turns go through the same API, and the browser reports its own timings for each one: how long
until the final transcript, until the first sound (a filler or the answer), until the first spoken
answer, and whether the customer interrupted. The samples so far come from development testing, seven
turns rather than a full session with a real microphone, so they are only indicative.

When a filler covers a slow lookup, the first sound comes after about 0.4 seconds. The first spoken
answer came after 4.2 to 4.9 seconds. Every interruption in the samples stopped playback and cancelled
the turn on the server.

The time to the first spoken answer is the weakest number in this report. It is the model's time to its
first token, around 2 to 3 seconds, plus speech synthesis for the first sentence. It could be brought down
by using a faster model for voice, starting speech on the first clause instead of the first sentence, or
moving to streaming speech recognition and synthesis on the server, which is also the path to phone calls
and to Safari and mobile. In the meantime the filler keeps the line from going silent.

## Load test

### Method

The load test in `loadtest/` runs the real stack with one change. The API, sign in and sessions,
guardrails, agent loop, tools, policy rules, loan maths, clients, streaming and PostgreSQL storage are all
real, and the mock systems run with fault injection on. Only the language model is replaced, by a stand
in that makes the same kind of tool decisions and calls the real tools. It waits for a random time
calibrated to the real model's measured latency, 1.2 seconds per step on average and 1.5 seconds at the
95th percentile, and streams its reply word by word. This measures what our own system can handle
without running into the model provider's rate limits.

Each virtual customer signs in, which looks the customer up in the CRM and loads a snapshot from four
systems. They then ask three questions that fit the products they hold, pausing about two seconds between
them, and sign out. Customers are drawn from all 204 synthetic profiles. A level of N means N
conversations open at the same time, with sign ins spread over ten seconds. The load is generated from
four processes, and I compared the server's own timings with the client's to make sure the load generator
was not the bottleneck. With several instances, each conversation stays on the instance it signed in to,
the way a load balancer with session affinity would route it.

I did not run the load test against the production database or the live model. The database is a shared
RDS instance, where heavy load would affect other databases and leave audit rows behind that cannot be
removed, and the live model would hit rate limits and cost real money. The test used a throwaway local
PostgreSQL of the same major version.

### Results

| Setup | Concurrent | Success | Turns per second | Sign in median and 95th | First word median and 95th | Turn median, 95th and 99th |
|---|---|---|---|---|---|---|
| 1 instance | 50 | 100% | 5.9 | 462 and 2,188 | 2,348 and 3,552 | 2,543, 3,693 and 4,823 |
| 1 instance | 200 | 100% | 21.9 | 411 and 1,286 | 2,389 and 3,458 | 2,578, 3,636 and 4,431 |
| 1 instance | 500 | 98.2% | 22.7 | 1,509 and 17,211 | 6,915 and 23,221 | 7,859, 24,271 and 30,460 |
| 1 instance | 1,000 | 76.0% | 17.3 | 22,060 and 53,726 | 15,888 and 45,677 | 16,903, 47,042 and 58,554 |
| 4 instances | 1,000 | 100% | 76.0 | 434 and 1,810 | 2,679 and 8,408 | 2,920, 9,180 and 11,711 |
| 6 instances, run 1 | 1,000 | 100% | 93.2 | 463 and 1,104 | 2,528 and 4,636 | 2,756, 4,903 and 6,658 |
| 6 instances, run 2 | 1,000 | 100% | 101.0 | 433 and 1,024 | 2,399 and 3,536 | 2,596, 3,716 and 5,196 |

With 1,000 conversations on six instances, each instance peaked at 50 to 64% of one CPU core and about 95
MB of memory. Backend failures that reached a customer were between 0.00 and 0.01% of calls, because
retries and the circuit breaker absorbed the rest. The server's first word times and the client's agreed
within 1 to 4%.

One Python process serves about 200 concurrent conversations with the same latency as 50. Past that the
event loop runs out of CPU, tool time climbs from 0.4 to 4.5 seconds and new connections start to be
refused. The design scales out by adding instances, because they share nothing except PostgreSQL and any
instance can pick up a session. 1,000 concurrent conversations need about six instances, plus one spare
in case one fails.

### Instance failure

During a run of 1,000 conversations on six instances, I killed one instance with SIGKILL 14 seconds in,
to simulate a crash rather than a clean shutdown. Every conversation still completed. 161 requests moved
to the remaining instances, 114 of them streams that were cut off mid turn and 47 refused connections. The
next instance accepted the same token, rebuilt the session from PostgreSQL with its conversation history,
and answered. A second run with seven instances, so that one was spare, gave the same result, with 134
requests moved and every conversation completed.

What these runs did not measure well is how long a restore takes during the rush. Restored turns took 22
to 27 seconds at the median, but the whole laptop was at 99% CPU at the time, running seven API
instances, the mock systems, PostgreSQL and four load generator processes on eight cores. That number
says more about the laptop than about the design. Measured on its own, a restore adds about half a second
on an idle system and 0.4 to 0.8 seconds under steady load, mostly because it reloads the customer's
snapshot from four systems. In production I would keep a spare instance, add a little random delay to
client retries, and cache snapshots in a shared store so a restore does not hit four systems at once.

A turn that was streaming when its instance died is retried in full. That is safe because writes only
happen through `confirm_action` with idempotency keys, so a repeated request cannot create a second
ticket.

### What the load test changed

The test found three problems, and all three are fixed. Sign ins failed with a connection pool limit of 5
plus 10, so the pool is now configurable and defaults to 10 plus 20. The storage flush waited for the
write queue to be completely empty, which might never happen under steady load, so it now only waits for
writes queued before it was called. And SQLite gave up immediately when the database was locked, so it
now waits for the lock. That last one only matters for local development, because production uses
PostgreSQL.

## Why the latency stays low

A few design choices explain these numbers. The customer's account snapshot is loaded once at sign in, so
many questions are answered in a single model step. Tools are called in parallel, and so are the backend
calls inside each tool, so a turn waits for the slowest system rather than for all of them added up.
Everything streams, so the customer sees text at the first word rather than at the end of the turn.
Conversation logging goes through a background queue and never waits for the database, which matters
when the database is far away. The round trip from India to the RDS instance is 190 to 380 milliseconds.
The part of the prompt that is the same for every customer comes first so the provider can cache it. Each
system has its own timeout, retries are limited, a circuit breaker cuts off a dead system and policy is
cached, so a slow or failing system costs at most its timeout. And on voice, a lookup slower than 0.4
seconds gets a spoken filler so the line never goes quiet.

## Limits and what production would need

| Limit | Plan |
|---|---|
| Language model throughput. 1,000 conversations at about one turn every 5 seconds is about 200 turns and 400 model calls per second | A provisioned or enterprise rate limit tier, or a dedicated deployment. This is the real constraint and it is a commercial one |
| CPU per instance, about 200 conversations | More instances behind a load balancer with session affinity, scaled on active conversations |
| Session state in instance memory | Restoring from PostgreSQL already works. A shared cache for snapshots would make failover cheaper |
| Voice first answer, about 4 to 5 seconds | A faster voice model, starting speech earlier, and a streaming speech gateway |
| Mock systems run in one process, 86% CPU at 1,000 conversations | Real enterprise systems scale independently, and the client layer already isolates their latency and failures |
| All numbers come from one laptop | Repeat on cloud instances next to the database, then a soak test over several hours to check memory and connections |

## Reproducing the numbers

The accuracy and latency evaluation needs `OPENAI_API_KEY`. Restart the mock systems before each run so
writes from an earlier run do not carry over.

```bash
uv run python -m mock_systems.run
```
```bash
uv run python -m evals.run --concurrency 4
```

The load test uses a local PostgreSQL on port 5433. Start one or more API instances, then run the load
generator against them.

```bash
for p in 8001 8002 8003 8004 8005 8006; do PORT=$p LOADTEST_DATABASE_URL=postgresql+asyncpg://loadtest@127.0.0.1:5433/loadtest uv run python -m loadtest.server & done
```
```bash
uv run python -m loadtest.run --api http://127.0.0.1:8001 http://127.0.0.1:8002 http://127.0.0.1:8003 http://127.0.0.1:8004 http://127.0.0.1:8005 http://127.0.0.1:8006 --procs 4 --levels 500 1000
```

Add `--failover` and kill one instance during the run to repeat the crash test. Results are written to
`evals/results/` and `loadtest/results/`, which are not committed.
