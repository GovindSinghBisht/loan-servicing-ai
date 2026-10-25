# Architecture and design decisions

This document explains how the loan servicing assistant is built, why I made the main design choices,
what each choice costs, and how the system would move toward production. The measured results are in
[PERFORMANCE.md](PERFORMANCE.md), and setup instructions and the demo customers are in the
[README](../README.md).

## The problem

The brief describes four problems. Customers get generic or outdated answers instead of answers about
their own account. Voice support is slow and rigid. Completing a request means dealing with several
separate systems. And every new use case takes weeks because each one is built from scratch.

Each of these points to something the design has to do. Answers need to come from the customer's records,
the policy that actually applies to them and real calculation, which document search alone cannot
provide. Voice needs to handle interruptions and changes of topic, and stay responsive when a backend is
slow. The assistant has to read from several systems and act on the customer's behalf safely. And a
second workflow should reuse the engine rather than become a second assistant.

The idea that ties the design together is a clear split of responsibilities. The language model
understands the customer and explains the result. Ordinary code fetches the data, does the calculations
and makes the decisions.

## How the system fits together

```
Web app (separate repo)              Backend (this repo)

Chat, account panel,      REST and   API: sessions, streaming, speech
history, confirm cards  ---------->    Governance: input checks, action limits, output check
Voice: browser speech     streamed     Agent loop  <---->  Language model (OpenAI)
recognition, spoken       events       Tools: loan maths, policy rules, eligibility checks
replies, interruptions                 Clients: timeouts, retries, circuit breaker, cache
                                          |                               |
                                 Enterprise systems (mocked)        PostgreSQL and Langfuse
                                 CRM, Loans, Payments, Policy,
                                 Service Ops, Cards
```

Take a customer who asks why their EMI went up. The API first turns the bearer token into a server side
session, and that session holds the customer's ID. Input checks then look for anything sensitive. The
model already has a snapshot of the customer's accounts from sign in, so it knows which loan is meant and
calls the `explain_emi_change` tool straight away. The tool checks that the loan belongs to the customer,
fetches the repayment schedule, the rate history and the relevant policy at the same time, and works out
which instalment changed and why. It returns those facts along with where each one came from. The model
explains them and the words stream to the browser. A final check confirms that every amount, percentage
and account number in the reply appeared in the tool results. The conversation, tool calls, timings and
audit records are then written to the database in the background, and the whole turn is traced.

## Key decisions

### Tools do the work, the model explains

Every number a customer sees comes from a typed tool. A tool combines the systems of record, applies the
policy clauses that fit the customer's product, and does all arithmetic in `shared/finance.py`, which
covers EMIs, schedules with rate resets and prepayments, and the effect of a prepayment. The model is
only allowed to restate what the tools returned. Each tool result carries its rule checks, each with a
pass or fail and a short reason, and its sources, such as a policy clause and version or a specific
instalment in the schedule.

I chose this because the brief's main complaint is exactly where document retrieval breaks down. A
question like "what happens if I prepay 50,000" needs the customer's outstanding principal, the clause
for their product and rate type, the charges and a recalculated schedule. A search over documents can
find the clause but cannot do the rest.

The cost is that every new kind of question needs a tool, so coverage has to be built rather than
appearing on its own. When no tool fits, the assistant says it cannot confirm the answer or offers a
human agent instead of guessing.

### Policy rules are versioned data

Each business rule is a clause with an ID, a version, the dates it applies, and the products, rate types
and borrower types it covers. It has both a human readable text and machine readable parameters, such as
the minimum number of EMIs paid or the foreclosure charge slabs. The policy system returns the version
in force on a given date, and tools pick the most specific clause that matches the loan and read its
parameters.

This gives three things. Answers can cite the exact clause and version. Old charges are explained by the
rule that applied at the time. The bounce charge, for example, changed in April 2025, and both versions
are in the data. And changing a rule means changing data, not code.

The cost is that someone who understands the rules has to write them in this structured form. Policies
that only exist as free text would need an extraction and review step first.

### A small agent loop with no separate planner

The agent loop runs for at most eight steps. In each step the model picks the tools it needs, independent
calls run in parallel, and the results go back to the model. The loop ends when the model answers in
text. Logic that depends on several systems in sequence lives inside the tools, which call the systems
in the right order. Across tools, the model plans one step at a time based on what each result shows.

I wrote the loop myself rather than using a framework because the engine needs control that frameworks
tend to hide. It needs a turn counter for confirmations, timing for every step, token streaming that voice
can share, and a way to repair the conversation history when a customer interrupts. A separate planning
call would add about a second to every turn, which matters a lot on a voice call, and the evaluations
show it isn't needed: turns take 1.9 model steps on average and never more than four.

The risk is long compound requests, such as comparing prepayment with foreclosure and then raising
whichever is cheaper, where the model has to sequence the steps well. If evaluations or traces showed
problems there, I would add a router that sends only those requests through an explicit planning step.

### Safety is enforced in code

Several protections hold regardless of what the model says. The customer's ID comes from the session and
is passed to every tool by the server, and any account ID the model passes is checked for ownership. A
foreign ID gets a "not found" answer so the system does not reveal that the record exists.

Actions are proposals. An action tool returns a summary and a write that has not happened yet. The write
only runs through `confirm_action`, and only if the proposal was made in an earlier turn than the
confirmation, so the model cannot confirm its own proposal. Writes carry idempotency keys, which means a
retry cannot create a second ticket, and a confirmation that has started will finish even if the customer
interrupts. Governance limits such as kill switches, channel rules, a cap per session and amount limits
are checked before a proposal can exist. Each of these has a test.

### Backends are assumed to be slow and unreliable

All calls to the enterprise systems go through one client layer. Each system gets its own timeout.
Temporary failures are retried with a short random delay, and writes are only retried because they carry
idempotency keys. A circuit breaker stops calling a system that keeps failing, so it fails in
milliseconds instead of making every customer wait. Policy data is cached for five minutes, and every call
is timed. When a system is unavailable the tool returns an explicit `SYSTEM_UNAVAILABLE` result, and the
prompt tells the model to share what it has, say what is missing and never fill the gap with a guess.

The mock systems inject realistic delays, errors and hangs, and these can be changed while the system is
running, so this behaviour can be shown rather than just described. The trade-off is that retries add
latency on a bad day, which the per system timeouts and the circuit breaker keep within limits.

### The reusable capability is the workflow pack

A workflow is configuration. A pack names the tool groups it exposes and adds instructions for its
domain. A session gets the packs that match what the customer holds, so someone with a loan and a credit
card talks to a single assistant. Everything else is shared: the agent loop, the clients, identity
checks, the confirmation gate, tickets, audit, governance, storage, tracing, streaming and voice.

The credit card workflow shows that this works. It took one tools file with seven tools, three of which
are actions, six policy clauses, a mock card system and one pack entry. The engine, the web app and voice
did not change, and three of the 19 evaluation conversations cover cards, including one that mixes card
and loan questions.

As more packs are combined for a customer, the prompt gets longer. That is fine for two packs. With five
or more I would first work out the customer's intent and load only the pack that fits.

### REST with streamed events instead of WebSockets

Each message is a normal POST request, and the response streams events back as server sent events. The
events cover tool progress, the words of the reply, confirmation cards, documents ready to download,
voice fillers and a final event with timings and sources. Closing the request cancels the turn on the
server.

A turn is a request followed by a streamed reply, and server sent events fit that exactly. Each request
stands on its own, so any instance can serve it. It passes through proxies and load balancers without
special handling, and authentication is an ordinary header. The brief does not ask for a particular
transport. The trade-off is that the server cannot push messages on its own, and audio does not travel to
the server.

### Voice is a channel on the same engine

Voice uses the same session, tools and history as text, and only the style of the reply changes, to
short sentences with no lists or codes. The browser turns speech into text. Replies are spoken sentence
by sentence as they stream, using OpenAI text to speech with the browser's own voice as a fallback. When
the customer talks over the assistant, playback stops and the server cancels the turn, and the engine
records what the customer actually heard so the next answer makes sense. A lookup that takes longer than
0.4 seconds gets a spoken filler, and every voice turn reports how long the customer waited for the
transcript, for the first sound and for the first spoken answer.

The brief says it will evaluate the interaction itself, meaning interruptions, changes of mind and
responsiveness when backends are slow, so that is where I put the effort. The trade-off is that browser
speech recognition works best in Chrome, is limited on Safari and iOS, and cannot be used for phone
calls. The production path for that is described below.

### The database holds only what the assistant owns

PostgreSQL stores conversations, messages, tool calls, timings for each turn, login sessions as hashed
tokens, and audit and guardrail events, which can only be added to. Business data stays in the systems of
record. Writes go through a queue that batches them into one transaction, so a database that is 200 to
380 milliseconds away never delays a reply, and a storage failure is logged rather than breaking the
conversation. Each customer keeps their last five conversations, while audit records are kept separately.
The application's database user cannot create, change or drop tables and can only insert into the audit
tables.

The cost of the queue is that a crash can lose the last few chat rows that had not been written yet. Login
sessions are written straight away because they have to survive a restart.

### Sessions survive restarts

Sign in returns a random token that is stored only as a SHA-256 hash. Sessions expire after 30 minutes of
inactivity or 8 hours in total, and both values come from the governance policy. The live state of a
conversation is kept in the instance's memory so turns are fast. If a request reaches an instance that
does not have it, because of a restart or a crash, the session and its conversation are rebuilt from the
database.

Memory keeps turns fast and the restore makes instances replaceable. In the load test I killed an
instance in the middle of a run and every conversation still completed. A restore costs about half a
second because it reloads the customer's snapshot, and proposals that were waiting for confirmation are
not carried over, so the customer is asked again.

### Governance is a file that risk teams own

The file `data/governance/policy.json` holds the runtime rules: session lifetimes, retention, which
personal data to redact, whether secrets are blocked, the tolerance for the output check, action caps and
kill switches, rate limits, voice consent, the list of approved models and whether traces mask personal
data. Every guardrail decision is stored and traced, and a risk team can change behaviour without a
deployment.

### Observability

Langfuse receives one trace per turn with the model steps and their token counts, the tool calls and the
guardrail decisions, grouped by session and customer, with personal data masked before it leaves the
server. Separately, the system records its own timing for every backend call and every turn in the
database, and those numbers feed the performance report.

## Trade-offs and known limits

| Area | Where it stands | What that means |
|---|---|---|
| Coverage | Questions are answered only through tools | Unusual questions get "I can't confirm that" or a handover, not a guess |
| Output check | Runs after the reply has streamed | A figure that was not grounded is flagged and recorded, not stopped mid sentence |
| Voice | Browser speech recognition, first spoken answer around 4 to 5 seconds in development samples | Works in Chrome. Safari, mobile and phone calls need a streaming audio gateway |
| Sign in | The OTP is a fixed demo code | Production would use the lender's OTP or identity service |
| Session state | Held in memory and restorable from the database | Needs session affinity at the load balancer, and pending proposals are lost on failover |
| Empty conversations | A sign in with no questions still uses a retention slot | Known bug. The fix is to create the conversation record on the first message |
| Mock systems | In memory, so writes reset on restart | Good for repeatable demos. Real systems replace them through configuration |
| Scale evidence | One laptop, with the model replaced by a timed stand in during the load test | Shows what our stack can do. The model vendor's throughput is a separate, contractual limit |

## Path to production

For deployment, I would run the containers behind a load balancer with session affinity and scale on the
number of active conversations. I measured about 200 conversations per instance, and I would keep one
spare instance on top of what the load needs.

The language model is the biggest dependency. At 1,000 conversations the system needs about 400 model
calls per second, which means buying a rate limit tier of that size. I would also use a faster model for
voice and add a second provider behind the same interface so the assistant keeps working if one provider
has an outage.

Voice would move to a streaming audio gateway in front of the engine, using WebSocket or WebRTC for the
web and SIP from the telephony provider for phone calls, with speech recognition and synthesis done on the
server. Consent and recording would stay under the same governance rules.

Sign in would use the lender's own OTP or single sign on service, with extra verification before high
value actions and sessions tied to a device.

For integrations, the client layer would point at the real system APIs instead of the mocks, using mutual
TLS and service credentials from a secrets manager, with contract tests for each system.

PostgreSQL would sit in a private subnet with encryption at rest. A shared cache such as Redis for
snapshots and policy would make failover cheap, and the retention and audit rules would be agreed with
compliance.

On safety, the output check would block replies that lead to an action instead of only flagging them.
I would test regularly against prompt injection and send flagged turns to a queue for human review.

For operations, I would add dashboards and alerts on first word latency, turn errors, circuit breaker
state and guardrail rates, run the evaluation suite in CI on every change to a prompt or tool, and roll out
model changes to a small share of traffic first.

New workflows such as insurance or collections would each be a pack made of tools, policy clauses, a mock
or real system, a pack entry and evaluation cases, without changes to the engine.

## Adding a workflow

A new workflow takes five steps. First add its policy clauses with their parameters to `data/policies/`.
Then point a client at the real system's API, or add a mock in `mock_systems/` using `make_app`, which
brings fault injection and idempotent writes with it. Next write typed tools in `engine/tools/` and tag
them with the pack name. Read tools return facts, checks and sources, and action tools return a
`Proposal`. After that add an entry in `engine/packs.py` with the tool groups and the domain instructions,
and a rule in `packs_for` for which customers get the pack. Finally add graded conversations to
`evals/cases.py`.

The loop, confirmations, governance, storage, tracing, streaming and voice need no changes. A new rule
for an existing workflow is usually just the first step, and a new action is a tool plus an evaluation
case.
