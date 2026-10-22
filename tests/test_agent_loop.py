"""Agent loop with a scripted OpenAI-style stream: no API calls, deterministic.

Tool-call arguments arrive split across chunks, as the real API streams them, so these tests also
cover the provider's stream assembly.
"""
import json
from types import SimpleNamespace as NS

from engine.agent import Agent
from engine.core import tools_for
from engine.llm import OpenAIProvider


def _chunk(content=None, tool_calls=None, finish=None, usage=None):
    choices = [] if usage else [NS(delta=NS(content=content, refusal=None, tool_calls=tool_calls), finish_reason=finish)]
    return NS(choices=choices, usage=usage)


def reply(text):
    half = len(text) // 2
    return [_chunk(content=text[:half]), _chunk(content=text[half:]), _chunk(finish="stop"),
            _chunk(usage=NS(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=NS(cached_tokens=0)))]


def calls(*specs):
    """specs: (id, name, args dict). Each call's JSON arguments are streamed in two pieces."""
    out = []
    for i, (id_, name, args) in enumerate(specs):
        raw = json.dumps(args)
        out.append(_chunk(tool_calls=[NS(index=i, id=id_, function=NS(name=name, arguments=raw[:5]))]))
        out.append(_chunk(tool_calls=[NS(index=i, id=None, function=NS(name=None, arguments=raw[5:]))]))
    return out + [_chunk(finish="tool_calls"),
                  _chunk(usage=NS(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=NS(cached_tokens=0)))]


class FakeOpenAI:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot: the agent keeps appending
        chunks = self.script.pop(0)
        if callable(chunks):
            chunks = chunks()

        async def gen():
            for c in chunks:
                yield c
        return gen()


def agent_with(session, script, customer="CUST100001"):
    fake = FakeOpenAI(script)
    provider = OpenAIProvider(tools_for({"core", "loans"}), model="test-model", client=fake)
    return Agent(session(customer), provider=provider), fake


async def test_parallel_tool_calls_return_results_together(session):
    agent, fake = agent_with(session, [
        calls(("c1", "explain_emi_change", {"loan_id": "HL1000001"}),
              ("c2", "get_payment_history", {"loan_id": "HL1000001"})),
        reply("Your EMI rose by Rs 1,210."),
    ])
    r = await agent.respond("Why did my EMI go up, and did any payment fail?")
    assert r.text == "Your EMI rose by Rs 1,210." and r.metrics.steps == 2
    assert [c["name"] for c in r.tool_calls] == ["explain_emi_change", "get_payment_history"]
    assert all(c["ok"] for c in r.tool_calls)

    second = fake.calls[1]["messages"]
    assistant, results = second[-3], second[-2:]
    assert [tc["function"]["arguments"] for tc in assistant["tool_calls"]] == ['{"loan_id": "HL1000001"}'] * 2
    assert [m["tool_call_id"] for m in results] == ["c1", "c2"]
    assert json.loads(results[0]["content"])["data"]["emi_change"] == 1210
    assert "Customer snapshot" in second[1]["content"]


async def test_model_cannot_confirm_its_own_proposal_in_the_same_turn(session):
    agent = None

    def self_confirm():
        action_id = next(iter(agent.session.pending))
        return calls(("c2", "confirm_action", {"action_id": action_id}))

    agent, _ = agent_with(session, [
        calls(("c1", "escalate_service_request", {"ticket_id": "SR0000001", "reason": "late"})),
        self_confirm,
        reply("Shall I escalate SR0000001?"),
    ], customer="CUST100003")
    r = await agent.respond("My refund is late, please escalate")
    assert [c["status"] or c["error"] for c in r.tool_calls] == ["CONFIRMATION_REQUIRED", "CONFIRMATION_REQUIRED"]
    assert agent.session.pending, "action must still be pending"


async def test_system_outage_is_reported_to_the_model_not_raised(session):
    agent, fake = agent_with(session, [
        calls(("c1", "get_payment_history", {"loan_id": "HL1000001"})),
        reply("Payment history is unavailable right now."),
    ])
    agent.session.systems.payments.breaker.opened_at = 10**12  # force the circuit open
    r = await agent.respond("Show my failed payments")
    out = json.loads(fake.calls[1]["messages"][-1]["content"])
    assert out["error"] == "SYSTEM_UNAVAILABLE" and out["system"] == "payments"
    assert r.tool_calls[0]["error"] == "SYSTEM_UNAVAILABLE"


async def test_malformed_tool_arguments_become_an_error_result(session):
    broken = [_chunk(tool_calls=[NS(index=0, id="c1", function=NS(name="get_loan_details", arguments='{"loan_id": '))]),
              _chunk(finish="tool_calls")]
    agent, fake = agent_with(session, [broken, reply("Sorry, which loan?")])
    r = await agent.respond("Show my loan")
    assert r.tool_calls[0]["error"] == "INVALID_ARGUMENTS"
    assert r.text == "Sorry, which loan?"
