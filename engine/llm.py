"""LLM providers behind one small interface, so the engine is model-agnostic.

The agent loop only needs: stream one step (text + tool calls), and append assistant turns and tool
results to a history kept in the provider's own message format. Select with LLM_PROVIDER
(openai); the model with OPENAI_MODEL. Another vendor plugs in by implementing Provider.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from .core import ToolSpec

Stop = Literal["end", "tool_use", "max_tokens", "refusal"]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict | None  # None when the model produced arguments that are not a JSON object


@dataclass
class Step:
    text: str
    tool_calls: list[ToolCall]
    stop: Stop
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    assistant_message: Any = None  # provider-native message to append to history


@dataclass
class Prompt:
    stable: str   # identical across customers and turns: cacheable prefix
    dynamic: str  # per-customer snapshot


class Provider(Protocol):
    name: str
    model: str

    async def step(self, prompt: Prompt, history: list, on_text: Callable[[str], None] | None) -> Step: ...
    def user_message(self, text: str) -> list: ...
    def tool_results(self, calls: list[ToolCall], outputs: list[dict]) -> list: ...


def _parse_args(raw: str) -> dict | None:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


@dataclass
class OpenAIProvider:
    tools: list[ToolSpec]
    model: str = field(default_factory=lambda: os.environ.get("OPENAI_MODEL", "gpt-4.1"))
    client: Any = None
    name: str = "openai"

    def __post_init__(self):
        if self.client is None:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI()
        self._tools = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                         "parameters": t.input_model.model_json_schema()}}
                       for t in self.tools]

    async def step(self, prompt: Prompt, history: list, on_text=None) -> Step:
        # Stable system prompt first so OpenAI's automatic prefix caching can reuse it across turns.
        messages = [{"role": "system", "content": prompt.stable},
                    {"role": "system", "content": prompt.dynamic}, *history]
        stream = await self.client.chat.completions.create(
            model=self.model, messages=messages, tools=self._tools, parallel_tool_calls=True,
            stream=True, stream_options={"include_usage": True}, max_completion_tokens=4000,
        )
        text: list[str] = []
        calls: dict[int, dict] = {}
        finish, usage, refused = None, None, False
        async for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                text.append(delta.content)
                if on_text:
                    on_text(delta.content)
            if delta.refusal:
                refused = True
            for tc in delta.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    c["id"] = tc.id
                if tc.function and tc.function.name:
                    c["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    c["args"] += tc.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason

        ordered = [calls[i] for i in sorted(calls)]
        tool_calls = [ToolCall(c["id"], c["name"], _parse_args(c["args"])) for c in ordered]
        assistant: dict = {"role": "assistant", "content": "".join(text) or None}
        if ordered:
            assistant["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                       for c in ordered]
        stop: Stop = ("refusal" if refused or finish == "content_filter" else
                      "max_tokens" if finish == "length" else "tool_use" if tool_calls else "end")
        details = getattr(usage, "prompt_tokens_details", None)
        return Step("".join(text), tool_calls, stop,
                    input_tokens=usage.prompt_tokens if usage else 0,
                    output_tokens=usage.completion_tokens if usage else 0,
                    cached_tokens=(details.cached_tokens or 0) if details else 0,
                    assistant_message=[assistant])

    def user_message(self, text: str) -> list:
        return [{"role": "user", "content": text}]

    def tool_results(self, calls: list[ToolCall], outputs: list[dict]) -> list:
        return [{"role": "tool", "tool_call_id": c.id, "content": json.dumps(o, default=str)}
                for c, o in zip(calls, outputs)]


def make_provider(tools: list[ToolSpec], name: str | None = None) -> Provider:
    name = (name or os.environ.get("LLM_PROVIDER", "openai")).lower()
    if name == "openai":
        return OpenAIProvider(tools)
    raise ValueError(f"Unknown LLM_PROVIDER {name!r} (expected openai)")
