"""Shared types for source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Company:
    name: str  # display name used in search queries, e.g. "Robinhood"
    domain: str  # the input domain; names the data folder, e.g. "meetcampfire.com"
    site_url: str  # where the site actually lives after redirects, e.g. "https://campfire.ai/"
    is_public: bool = False
    ticker: str | None = None
    cik: str | None = None  # SEC Central Index Key, zero-padded to 10 digits


@dataclass
class RawDocument:
    url: str
    source_type: str  # company_site | sec_filing | search
    title: str
    text: str  # plain text; evidence snippets are checked against exactly this
    fetched_at: str
    raw_path: str  # repo-relative path of the cached text file (`text` above)
    meta: dict = field(default_factory=dict)
    # Innermost HTML block texts (fetch.html_blocks) for HTML sources; None means each
    # line (and each markdown table cell) of `text` is a block. Re-anchoring only.
    blocks: list[str] | None = None


@dataclass
class AdapterReport:
    """What an adapter tried, so empty or blocked sources are visible, never silent."""

    source_type: str
    documents: list[RawDocument] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {"url", "reason"}
    notes: list[str] = field(default_factory=list)

    def skip(self, url: str, reason: str) -> None:
        self.skipped.append({"url": url, "reason": reason})


class SourceAdapter(Protocol):
    source_type: str

    def discover(self, company: Company) -> AdapterReport: ...
