"""Agent architectures under test.

Three shapes, deliberately: they are the ones teams actually argue about, and the cost/accuracy gap
between them is the thing an eval platform exists to measure.

* `single_shot` — one call. The baseline everything else has to beat.
* `react` — think/act/observe until the model stops calling tools. Flexible, and pays for the whole
  transcript again on every step.
* `planner_executor` — plan once, execute the steps, synthesise. Fewer, shorter calls than ReAct on
  tasks whose shape is known up front.

Prompt templating is `{{variable}}` substitution against the case input. Not Jinja: an eval harness
should not be able to execute arbitrary logic that lives in a dataset row.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from crucible.providers.base import CompletionRequest, Message, ToolSpec
from crucible.runner.tools import ToolBox

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

ARCHITECTURES = ("single_shot", "react", "planner_executor")


@dataclass
class TargetConfig:
    model: str
    architecture: str = "single_shot"
    system: str | None = None
    prompt_template: str = "{{input}}"
    max_tokens: int = 1024
    temperature: float | None = None
    thinking: bool = False
    effort: str | None = None
    max_steps: int = 6
    tools: list[dict[str, Any]] = field(default_factory=list)
    response_format: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("input_schema") or {"type": "object", "properties": {}},
            )
            for t in self.tools
        ]


def render_template(template: str, variables: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        cur: Any = variables
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return match.group(0)  # leave unknown placeholders visible rather than blanking them
        return cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False)

    rendered = _TEMPLATE_RE.sub(replace, template)
    # `{{input}}` with no matching key means "the whole case input".
    if rendered == template and "{{input}}" in template:
        rendered = template.replace("{{input}}", json.dumps(variables, ensure_ascii=False))
    return rendered


@dataclass
class AgentOutput:
    text: str
    steps: int = 1
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_output_dict(self) -> dict[str, Any]:
        """Shape the assertion engine sees. `parsed` is populated when the text is JSON, so
        JSONPath assertions can address fields without every suite repeating a parse step."""
        parsed: Any = None
        text = self.text.strip()
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        return {"text": self.text, "parsed": parsed, "tool_calls": self.tool_calls, "steps": self.steps}


class _Caller(Protocol):
    """Structural type for `executor.LlmCaller`; kept local so agents do not import the executor."""

    async def call(self, request: CompletionRequest, *, name: str, kind: str = "llm") -> Any: ...

    def invoke_tool(self, tools: ToolBox, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


async def run_single_shot(caller: _Caller, config: TargetConfig, case_input: dict[str, Any], _: ToolBox) -> AgentOutput:
    prompt = render_template(config.prompt_template, case_input)
    completion = await caller.call(
        CompletionRequest(
            model=config.model,
            system=config.system,
            messages=[Message(role="user", content=prompt)],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            thinking=config.thinking,
            effort=config.effort,
            response_format=config.response_format,
        ),
        name="answer",
    )
    return AgentOutput(text=completion.text, steps=1)


async def run_react(caller: _Caller, config: TargetConfig, case_input: dict[str, Any], tools: ToolBox) -> AgentOutput:
    """Loop until the model answers instead of calling a tool, or the step budget runs out.

    The whole transcript is resent on every step — that is the cost characteristic that makes ReAct
    expensive, and the benchmark is meant to show it rather than hide it.
    """
    prompt = render_template(config.prompt_template, case_input)
    messages = [Message(role="user", content=prompt)]
    specs = config.tool_specs()
    observed: list[dict[str, Any]] = []

    for step in range(config.max_steps):
        completion = await caller.call(
            CompletionRequest(
                model=config.model,
                system=config.system,
                messages=messages,
                max_tokens=config.max_tokens,
                tools=specs,
                temperature=config.temperature,
                thinking=config.thinking,
                effort=config.effort,
            ),
            name=f"react.step.{step + 1}",
        )
        if not completion.tool_calls:
            return AgentOutput(text=completion.text, steps=step + 1, tool_calls=observed)

        messages.append(Message(role="assistant", content=completion.text or "(tool call)"))
        for call in completion.tool_calls:
            result = caller.invoke_tool(tools, call.name, call.arguments)
            observed.append({"tool": call.name, "arguments": call.arguments, "result": result})
            messages.append(
                Message(role="user", content=f"Tool {call.name} returned: {json.dumps(result, ensure_ascii=False)}")
            )

    final = await caller.call(
        CompletionRequest(
            model=config.model,
            system=config.system,
            messages=[*messages, Message(role="user", content="Give your final answer now, with no further tools.")],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        ),
        name="react.final",
    )
    return AgentOutput(text=final.text, steps=config.max_steps + 1, tool_calls=observed)


async def run_planner_executor(
    caller: _Caller, config: TargetConfig, case_input: dict[str, Any], tools: ToolBox
) -> AgentOutput:
    """Plan once, execute the plan's tool steps, then synthesise a single answer."""
    prompt = render_template(config.prompt_template, case_input)
    plan_completion = await caller.call(
        CompletionRequest(
            model=config.model,
            system=(config.system or "") + "\nFirst produce a short numbered plan. Do not answer yet.",
            messages=[Message(role="user", content=prompt)],
            max_tokens=min(512, config.max_tokens),
            temperature=config.temperature,
        ),
        name="plan",
    )

    observed: list[dict[str, Any]] = []
    specs = config.tool_specs()
    if specs:
        step_completion = await caller.call(
            CompletionRequest(
                model=config.model,
                system=config.system,
                messages=[
                    Message(role="user", content=prompt),
                    Message(role="assistant", content=plan_completion.text),
                    Message(role="user", content="Call the tools your plan needs, all at once."),
                ],
                max_tokens=config.max_tokens,
                tools=specs,
                temperature=config.temperature,
            ),
            name="execute",
        )
        for call in step_completion.tool_calls:
            observed.append(
                {
                    "tool": call.name,
                    "arguments": call.arguments,
                    "result": caller.invoke_tool(tools, call.name, call.arguments),
                }
            )

    evidence = json.dumps(observed, ensure_ascii=False)[:4000] if observed else "(no tools required)"
    final = await caller.call(
        CompletionRequest(
            model=config.model,
            system=config.system,
            messages=[
                Message(role="user", content=prompt),
                Message(role="assistant", content=plan_completion.text),
                Message(role="user", content=f"Tool results:\n{evidence}\n\nGive the final answer."),
            ],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            response_format=config.response_format,
        ),
        name="synthesise",
    )
    return AgentOutput(text=final.text, steps=2 + (1 if specs else 0), tool_calls=observed)


ARCHITECTURE_RUNNERS = {
    "single_shot": run_single_shot,
    "react": run_react,
    "planner_executor": run_planner_executor,
}
