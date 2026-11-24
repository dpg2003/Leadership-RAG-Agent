"""Tool-calling chat agent over the typed tools in tools.py (real LLM calls).

The agent keeps the whole conversation (including tool calls and results) in `history`, so
follow-ups such as "and the CFO?" or "where is he based?" work. Each `ask()` returns the
answer, the tool steps it took, and the source URLs it could cite, for the CLI and UI.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import anthropic

from leadership.chat.prompts import system_prompt
from leadership.chat.tools import TOOL_SCHEMAS, available_companies, run_tool
from leadership.config import llm_model, require_setting
from leadership.pipeline.extract import PRICES

log = logging.getLogger(__name__)

MAX_TOKENS = 4096
MAX_TOOL_ROUNDS = 8
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


@dataclass
class Step:
    tool: str
    input: dict[str, Any]
    is_error: bool
    output: str


@dataclass
class Reply:
    text: str
    steps: list[Step] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # tool-result URLs cited in the answer
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


class ChatAgent:
    def __init__(
        self,
        company: str | None = None,
        model: str | None = None,
        client: Any = None,
        today: date | None = None,
    ) -> None:
        companies = available_companies()
        domains = {c["domain"] for c in companies}
        if company is not None and company not in domains:
            raise ValueError(
                f"no collected data for {company!r}; collected: {', '.join(sorted(domains))}"
            )
        self.company = company
        self.model = model or llm_model()
        self.system = system_prompt(
            companies, company, (today or datetime.now(UTC).date()).isoformat()
        )
        self.history: list[dict[str, Any]] = []
        self._client = client

    def _client_lazy(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=require_setting("ANTHROPIC_API_KEY"), timeout=120.0, max_retries=3
            )
        return self._client

    def ask(self, question: str) -> Reply:
        start = len(self.history)
        self.history.append({"role": "user", "content": question})
        reply = Reply("")
        tool_urls: set[str] = set()
        try:
            for _ in range(MAX_TOOL_ROUNDS + 1):
                response = self._client_lazy().messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=self.system,
                    tools=TOOL_SCHEMAS,
                    messages=self.history,
                    thinking={"type": "adaptive"},
                )
                reply.input_tokens += response.usage.input_tokens
                reply.output_tokens += response.usage.output_tokens
                self.history.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    output, is_error = run_tool(block.name, dict(block.input))
                    reply.steps.append(Step(block.name, dict(block.input), is_error, output))
                    tool_urls.update(_URL.findall(output))
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output,
                            "is_error": is_error,
                        }
                    )
                self.history.append({"role": "user", "content": results})
            else:
                raise RuntimeError(f"no answer after {MAX_TOOL_ROUNDS} tool rounds")
        except anthropic.AuthenticationError as exc:
            del self.history[start:]
            raise RuntimeError("ANTHROPIC_API_KEY was rejected (HTTP 401)") from exc
        except Exception:
            del self.history[start:]  # keep the history valid for the next question
            raise

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n(The answer was cut off at the length limit.)"
        elif response.stop_reason == "refusal":
            text = text or "The model declined to answer this question."
        reply.text = text
        cited = {u.rstrip(".,;") for u in _URL.findall(text)}
        reply.sources = sorted(u for u in tool_urls if u in cited)
        price = PRICES.get(self.model)
        if price:
            reply.cost_usd = round(
                (reply.input_tokens * price[0] + reply.output_tokens * price[1]) / 1e6, 4
            )
        log.info(
            "chat answer: %d tool calls, %d tokens in / %d out, cost $%s",
            len(reply.steps), reply.input_tokens, reply.output_tokens, reply.cost_usd,
        )  # fmt: skip
        return reply


def steps_as_json(steps: list[Step]) -> str:
    return json.dumps([{"tool": s.tool, "input": s.input} for s in steps], ensure_ascii=False)
