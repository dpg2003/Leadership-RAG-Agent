"""LLM extraction: one structured-output call per pre-filtered document.

The model only *reads*: it returns people with verbatim evidence. Everything that decides what
is stored (evidence check, title rules, dedupe, currency, confidence) is deterministic code in
validate.py and resolve.py. Responses are cached on disk (keyed by model, prompt version and
the exact input), so re-running resolution costs nothing; `--reextract` ignores the cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from leadership.config import require_setting
from leadership.sources.base import Company, RawDocument

log = logging.getLogger(__name__)

PROMPT_VERSION = "1"
MAX_TOKENS = 16_000
EFFORT = "medium"
WORKERS = 4
# USD per million tokens (input, output), standard rates from Anthropic's pricing page
# (https://platform.claude.com/docs/en/about-claude/pricing), as listed on 2026-11-03.
# Unknown models are still run; their cost is reported as null.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class ExtractedPerson(BaseModel):
    full_name: str = Field(description="The person's name exactly as written on the page.")
    title: str = Field(
        description="Their job title copied verbatim from the evidence snippet, without the "
        "company name. Example: 'Chief Financial Officer', 'VP, Engineering', 'Head of Product'."
    )
    employer: str = Field(description="Organization this title is held at, as the page says it.")
    works_at_target_company: bool = Field(
        description="True only if the title is held at the target company (not a customer, "
        "investor, partner, previous employer, or a different organization with a similar name)."
    )
    status: Literal["current", "former", "incoming", "unclear"] = Field(
        description="'former' if the page says they left, were replaced, or uses 'former'/'ex-'/"
        "'previously'; 'incoming' if announced but not started; 'current' only if the page "
        "presents the title as held now (as of the page date)."
    )
    evidence_snippet: str = Field(
        description="A verbatim quote from the page (one or two sentences, under 300 "
        "characters) that contains both the person's name and the title."
    )
    location: str | None = Field(
        description="City/region where the person is based, only if the page states it; else null."
    )
    location_snippet: str | None = Field(
        description="Verbatim quote stating the location; null when location is null."
    )
    bio: str | None = Field(
        description="One verbatim sentence from the page describing their background; else null."
    )


class Extraction(BaseModel):
    page_published_date: str | None = Field(
        description="Publication date shown on the page, as YYYY-MM-DD; null if none is shown."
    )
    published_date_snippet: str | None = Field(
        description="The verbatim text on the page that shows that date; null if none."
    )
    people: list[ExtractedPerson]


SYSTEM = """You extract company leadership from one web page or filing. You read; you never guess.

Rules:
- Target company: {name} (website {domain}). Only include people whose title is held AT the
  target company. Exclude people at other organizations that share the name, customers,
  investors, partners, authors of quoted studies, and people from other companies mentioned in
  the text.
- In scope: C-level officers (Chief ... Officer, CEO, CFO, President, General Counsel),
  founders, Vice Presidents (VP, SVP, EVP), and Heads of a function (Head of X). Skip board-
  only directors, managers, engineers, and other staff.
- Every value must be copied verbatim from the page text: name, title, snippet, location, bio,
  date. Do not translate, expand abbreviations, or fix typos. If something is not stated, use
  null.
- The evidence_snippet must contain both the name and the title, exactly as they appear.
- Mark status 'former' whenever the page says the person left, was replaced, or held the role
  in the past ("former CFO", "previously VP"). A page that announces an appointment presents it
  as current as of the page date.
- If the page lists no in-scope people at the target company, return an empty list.
- Text marked [...] was removed by a filter; only use what you see."""


@dataclass
class ExtractResult:
    doc: RawDocument
    extraction: Extraction | None
    input_tokens: int = 0
    output_tokens: int = 0
    from_cache: bool = False
    error: str = ""


@dataclass
class ExtractStats:
    llm_calls: int = 0
    cached: int = 0
    input_tokens: int = 0  # this run's API calls only
    output_tokens: int = 0
    cached_input_tokens: int = 0  # what the cached responses cost when they were made
    cached_output_tokens: int = 0
    errors: list[dict] = field(default_factory=list)

    def cost_usd(self, model: str, include_cached: bool = False) -> float | None:
        price = PRICES.get(model)
        if price is None:
            return None
        tin, tout = self.input_tokens, self.output_tokens
        if include_cached:
            tin, tout = tin + self.cached_input_tokens, tout + self.cached_output_tokens
        return round((tin * price[0] + tout * price[1]) / 1e6, 4)


def _user_prompt(doc: RawDocument, text: str) -> str:
    meta_date = doc.meta.get("published_date") or doc.meta.get("filing_date") or "unknown"
    return (
        f"Source URL: {doc.url}\nPage title: {doc.title}\n"
        f"Publication date from metadata (may be missing): {meta_date}\n\n"
        f"<page>\n{text}\n</page>"
    )


class Extractor:
    def __init__(self, company: Company, model: str, cache_dir: Path, reextract: bool = False):
        self.company = company
        self.model = model
        self.cache_dir = cache_dir
        self.reextract = reextract
        self.system = SYSTEM.format(name=company.name, domain=company.domain)
        self._client: anthropic.Anthropic | None = None

    def _client_lazy(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=require_setting("ANTHROPIC_API_KEY"), timeout=120.0, max_retries=3
            )
        return self._client

    def _cache_path(self, doc: RawDocument, text: str) -> Path:
        key = hashlib.sha256(
            "\x00".join([self.model, PROMPT_VERSION, self.system, doc.url, text]).encode("utf-8")
        ).hexdigest()[:24]
        return self.cache_dir / f"{key}.json"

    def extract(self, doc: RawDocument, text: str) -> ExtractResult:
        path = self._cache_path(doc, text)
        if path.exists() and not self.reextract:
            data = json.loads(path.read_text(encoding="utf-8"))
            usage = data.get("usage", {})
            return ExtractResult(
                doc,
                Extraction.model_validate(data["extraction"]),
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                from_cache=True,
            )
        try:
            response = self._client_lazy().messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self.system,
                messages=[{"role": "user", "content": _user_prompt(doc, text)}],
                output_format=Extraction,
                output_config={"effort": EFFORT},
            )
        except anthropic.AuthenticationError as exc:
            raise RuntimeError("ANTHROPIC_API_KEY was rejected (HTTP 401)") from exc
        except anthropic.APIError as exc:
            log.warning("extraction failed for %s: %s", doc.url, exc)
            return ExtractResult(doc, None, error=f"API error: {exc}")
        usage = response.usage
        result = ExtractResult(doc, None, usage.input_tokens, usage.output_tokens)
        if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
            result.error = f"no structured output (stop_reason={response.stop_reason})"
            return result
        result.extraction = response.parsed_output
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "url": doc.url,
            "model": self.model,
            "prompt_version": PROMPT_VERSION,
            "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
            "extraction": result.extraction.model_dump(),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return result

    def extract_all(
        self, jobs: list[tuple[RawDocument, str]]
    ) -> tuple[list[ExtractResult], ExtractStats]:
        stats = ExtractStats()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(lambda job: self.extract(*job), jobs))
        for r in results:
            if r.from_cache:
                stats.cached += 1
                stats.cached_input_tokens += r.input_tokens
                stats.cached_output_tokens += r.output_tokens
            else:
                stats.llm_calls += 1
                stats.input_tokens += r.input_tokens
                stats.output_tokens += r.output_tokens
            if r.error:
                stats.errors.append({"url": r.doc.url, "error": r.error})
        return results, stats
