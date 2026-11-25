"""`leadership eval`: run eval/questions.yaml through the REAL chat agent and grade it.

Grading is deterministic (regexes and tool-call checks on the last answer), so a pass/fail
is reproducible and explainable; the expected answer in words is kept next to each case for
the human check (D40). Results are written to eval/results.json and eval/RESULTS.md.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from leadership.chat.agent import ChatAgent, Reply
from leadership.config import REPO_ROOT

EVAL_DIR = REPO_ROOT / "eval"
QUESTIONS = EVAL_DIR / "questions.yaml"
CHECK_KEYS = {"include", "exclude", "tools", "no_tools", "cites"}
CASE_KEYS = {"id", "company", "kind", "turns", "expected", "verified_by", "checks"}
KINDS = {"example", "not_found", "follow_up", "conflict", "low_confidence", "flag", "company"}


@dataclass
class Result:
    id: str
    company: str | None
    kind: str
    passed: bool
    failures: list[str]
    expected: str
    verified_by: str | None
    answer: str
    tools: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    error: str | None = None


def load_cases(path: Path = QUESTIONS) -> list[dict[str, Any]]:
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for c in cases:
        if set(c) - CASE_KEYS or {"id", "turns", "checks"} - set(c):
            raise ValueError(f"{path}: bad keys in case {c.get('id')!r}")
        if c["id"] in seen:
            raise ValueError(f"{path}: duplicate id {c['id']!r}")
        seen.add(c["id"])
        if set(c["checks"]) - CHECK_KEYS or not c["checks"]:
            raise ValueError(f"{path}: bad checks in case {c['id']!r}")
        if not c["turns"] or not all(isinstance(t, str) and t.strip() for t in c["turns"]):
            raise ValueError(f"{path}: case {c['id']!r} needs at least one question")
        if c.get("kind") not in KINDS:
            raise ValueError(f"{path}: bad kind in case {c['id']!r}")
        for pattern in c["checks"].get("include", []) + c["checks"].get("exclude", []):
            re.compile(pattern)
    return cases


def _plain(text: str) -> str:
    return text.replace("**", "").replace("__", "")


def grade(case: dict[str, Any], reply: Reply) -> list[str]:
    """Failed checks for the last answer (empty = pass)."""
    checks, text, failures = case["checks"], _plain(reply.text), []
    for pattern in checks.get("include", []):
        if not re.search(pattern, text, re.I | re.S):
            failures.append(f"missing /{pattern}/")
    for pattern in checks.get("exclude", []):
        if m := re.search(pattern, text, re.I | re.S):
            failures.append(f"should not say {m.group(0)!r}")
    for want in checks.get("tools", []):
        args = want.get("args", {})
        if not any(
            s.tool == want["tool"] and all(s.input.get(k) == v for k, v in args.items())
            for s in reply.steps
        ):
            failures.append(f"no {want['tool']}({args}) call")
    if checks.get("no_tools") and reply.steps:
        failures.append(f"called tools: {', '.join(s.tool for s in reply.steps)}")
    if checks.get("cites") and not reply.sources:
        failures.append("cites no tool-result URL")
    return failures


def run_case(case: dict[str, Any], model: str | None = None) -> Result:
    base = {
        "id": case["id"],
        "company": case.get("company"),
        "kind": case["kind"],
        "expected": " ".join(str(case.get("expected", "")).split()),
        "verified_by": case.get("verified_by"),
    }
    agent = ChatAgent(case.get("company"), model=model)
    reply = None
    tools: list[str] = []
    tokens = [0, 0, 0.0]
    try:
        for turn in case["turns"]:
            reply = agent.ask(turn)
            tools += [f"{s.tool}({json.dumps(s.input, ensure_ascii=False)})" for s in reply.steps]
            tokens[0] += reply.prompt_tokens
            tokens[1] += reply.output_tokens
            tokens[2] += reply.cost_usd or 0.0
    except Exception as exc:  # reported as a failed case, never hidden
        return Result(**base, passed=False, failures=[f"error: {exc}"], answer="",
                      tools=tools, error=repr(exc))  # fmt: skip
    if reply is None:  # load_cases rejects empty turns; this keeps the type checker honest
        raise ValueError(f"case {case['id']!r} has no turns")
    failures = grade(case, reply)
    return Result(
        **base, passed=not failures, failures=failures, answer=reply.text, tools=tools,
        prompt_tokens=tokens[0], output_tokens=tokens[1], cost_usd=round(tokens[2], 4),
    )  # fmt: skip


def select(cases: list[dict[str, Any]], domain: str | None, ids: list[str] | None):
    if ids:
        unknown = set(ids) - {c["id"] for c in cases}
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(sorted(unknown))}")
        return [c for c in cases if c["id"] in ids]
    if domain:
        if domain not in {c.get("company") for c in cases}:
            raise ValueError(f"no eval cases for {domain!r}")
        # A company's cases, plus the cases with no company selected (ambiguity checks).
        return [c for c in cases if c.get("company") in (domain, None)]
    return cases


def run(cases: list[dict[str, Any]], model: str | None = None, workers: int = 4) -> list[Result]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda c: run_case(c, model), cases))


def write_results(results: list[Result], model: str, out_dir: Path = EVAL_DIR) -> Path:
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    passed = sum(r.passed for r in results)
    cost = round(sum(r.cost_usd or 0 for r in results), 4)
    data = {"run_at": now, "model": model, "passed": passed, "total": len(results),
            "cost_usd": cost, "results": [asdict(r) for r in results]}  # fmt: skip
    (out_dir / "results.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    lines = [
        "# Eval results",
        "",
        f"Generated by `leadership eval` on {now} with `{model}`: **{passed}/{len(results)} "
        f"passed**, cost ${cost}. Graded by the deterministic checks in "
        "[questions.yaml](questions.yaml); `expected` is the answer in words.",
        "",
        "| Case | Kind | Result | Expected | Agent's answer (first 300 chars) | Tools |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        verdict = "PASS" if r.passed else "FAIL: " + "; ".join(r.failures)
        answer = " ".join(r.answer.split())[:300]
        lines.append(
            "| "
            + " | ".join(
                x.replace("|", "\\|")
                for x in (r.id, r.kind, verdict, r.expected, answer, "<br>".join(r.tools))
            )
            + " |"
        )
    path = out_dir / "RESULTS.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path
