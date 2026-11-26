"""Tool-calling chat agent over the typed tools in tools.py (real LLM calls).

The agent keeps the conversation in `history`, so follow-ups such as "and the CFO?" or
"where is he based?" work. Once a question is answered, its tool calls and results are
trimmed from the history and only the question and the answer are kept (D39): the answer
already holds the facts and URLs a follow-up needs, and the agent calls tools again for
anything new. The tools and system prompt are cached (prompt caching), and so is the
growing conversation within a question's tool loop.

Each `ask()` returns the answer, the tool steps it took, the source URLs it cites, and its
token use and cost, for the CLI, the UI and the eval.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
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

# Prompt-cache price multipliers on the base input price (Anthropic pricing page, prompt
# caching, 5-minute TTL): writes 1.25x; reads 0.05x on Sonnet 5.5 / Opus 5.5, else 0.1x.
CACHE_WRITE = 1.25
CACHE_READ = {"claude-sonnet-5-5": 0.05, "claude-opus-5-5": 0.05}
_EPHEMERAL = {"type": "ephemeral"}
MAX_SNIPPETS = 3

# ask(on_event=...) callback: ("round", n) when a model call starts, ("text", delta) for
# streamed answer text, ("tool", Step) after each tool call. Text streamed in a round that
# ends in tool calls is the model thinking aloud; the final answer is the last round's text.
OnEvent = Callable[[str, Any], None]


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
    # For each cited URL: source type, date and the stored evidence snippets (from tool results)
    source_details: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0  # uncached input
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None

    @property
    def prompt_tokens(self) -> int:
        """All input tokens sent, cached or not."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


class ChatAgent:
    def __init__(
        self,
        company: str | None = None,
        model: str | None = None,
        client: Any = None,
        today: date | None = None,
        prompt_cache: bool = True,
        trim_history: bool = True,
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
        self.prompt_cache = prompt_cache
        self.trim_history = trim_history
        self.history: list[dict[str, Any]] = []
        self._client = client

    def _client_lazy(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=require_setting("ANTHROPIC_API_KEY"), timeout=120.0, max_retries=3
            )
        return self._client

    def _request(self) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "tools": TOOL_SCHEMAS,
            "messages": self.history,
            "thinking": {"type": "adaptive"},
        }
        if self.prompt_cache:
            # Tools render before the system prompt, so one breakpoint here caches both;
            # the top-level field caches the conversation tail within a tool loop.
            req["system"] = [{"type": "text", "text": self.system, "cache_control": _EPHEMERAL}]
            req["cache_control"] = _EPHEMERAL
        else:
            req["system"] = self.system
        return req

    def _call(self, on_event: OnEvent | None) -> Any:
        client = self._client_lazy()
        if on_event is None:
            return client.messages.create(**self._request())
        with client.messages.stream(**self._request()) as stream:
            for text in stream.text_stream:
                on_event("text", text)
            return stream.get_final_message()

    def ask(self, question: str, on_event: OnEvent | None = None) -> Reply:
        """Answer one question. With `on_event`, the answer is streamed (see OnEvent)."""
        start = len(self.history)
        self.history.append({"role": "user", "content": question})
        reply = Reply("")
        tool_urls: set[str] = set()
        try:
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                if on_event:
                    on_event("round", round_no)
                response = self._call(on_event)
                u = response.usage
                reply.input_tokens += u.input_tokens
                reply.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
                reply.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
                reply.output_tokens += u.output_tokens
                self.history.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    output, is_error = run_tool(block.name, dict(block.input))
                    reply.steps.append(Step(block.name, dict(block.input), is_error, output))
                    if on_event:
                        on_event("tool", reply.steps[-1])
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
        except BaseException:
            # Also on BaseException: a UI rerun (Streamlit's RerunException) can interrupt a
            # streamed answer between a tool_use turn and its tool_result; keep history valid.
            del self.history[start:]
            raise

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n(The answer was cut off at the length limit.)"
        elif response.stop_reason == "refusal":
            text = text or "The model declined to answer this question."
        reply.text = text
        cited = {u.rstrip(".,;") for u in _URL.findall(text)}
        reply.sources = sorted(u for u in tool_urls if u in cited)
        reply.source_details = source_details(reply.steps, reply.sources)
        if self.trim_history:
            self._trim(start, reply)
        reply.cost_usd = self._cost(reply)
        log.info(
            "chat answer: %d tool calls, %d prompt tokens (%d cached read, %d cache write) "
            "/ %d out, cost $%s",
            len(reply.steps), reply.prompt_tokens, reply.cache_read_tokens,
            reply.cache_write_tokens, reply.output_tokens, reply.cost_usd,
        )  # fmt: skip
        return reply

    def _trim(self, start: int, reply: Reply) -> None:
        """Replace this question's tool calls and results with the question and the answer
        (D39). The tools called are noted in the kept answer, so the model knows the facts
        came from tools."""
        calls = "; ".join(
            f"{s.tool}({', '.join(f'{k}={v!r}' for k, v in s.input.items())})" for s in reply.steps
        )
        answer = reply.text or "(no answer)"
        if calls:
            answer = f"[Answered from tool results: {calls}]\n\n{answer}"
        self.history[start + 1 :] = [{"role": "assistant", "content": answer}]

    def _cost(self, reply: Reply) -> float | None:
        price = PRICES.get(self.model)
        if not price:
            return None
        read = CACHE_READ.get(self.model, 0.1)
        usd = (
            reply.input_tokens * price[0]
            + reply.cache_write_tokens * price[0] * CACHE_WRITE
            + reply.cache_read_tokens * price[0] * read
            + reply.output_tokens * price[1]
        ) / 1e6
        return round(usd, 4)


def source_details(steps: list[Step], urls: list[str]) -> list[dict[str, Any]]:
    """For each URL: the source type, date and evidence snippets that the tool results
    returned with it (nothing is looked up again)."""
    found: dict[str, dict[str, Any]] = {u: {"url": u, "evidence": []} for u in urls}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            d = found.get(node.get("url")) if isinstance(node.get("url"), str) else None
            if d is not None:
                for key in ("source_type", "published", "dated_by"):
                    if node.get(key) and key not in d:
                        d[key] = node[key]
                ev = node.get("evidence")
                if isinstance(ev, str) and ev not in d["evidence"]:
                    d["evidence"].append(ev)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for s in steps:
        if not s.is_error:
            try:
                walk(json.loads(s.output))
            except ValueError:
                continue
    for d in found.values():
        d["evidence"] = d["evidence"][:MAX_SNIPPETS]
    return [found[u] for u in urls]


def steps_as_json(steps: list[Step]) -> str:
    return json.dumps([{"tool": s.tool, "input": s.input} for s in steps], ensure_ascii=False)
