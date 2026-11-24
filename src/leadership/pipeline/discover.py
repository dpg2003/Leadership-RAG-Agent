"""Step 1 of collection: resolve the company and run every source adapter. No LLM."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from leadership.config import company_dir
from leadership.models import SOURCE_PRIORITY, utc_now_iso
from leadership.sources.base import AdapterReport, Company, RawDocument, SourceAdapter
from leadership.sources.company_site import CompanySiteAdapter
from leadership.sources.fetch import Fetcher
from leadership.sources.search import SearchAdapter
from leadership.sources.sec_edgar import SecEdgarAdapter, lookup_cik

log = logging.getLogger(__name__)


def normalize_domain(url_or_domain: str) -> str:
    text = url_or_domain.strip()
    if "://" not in text:
        text = "https://" + text
    host = (urlsplit(text).hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        raise ValueError(f"Not a domain or URL: {url_or_domain!r}")
    return host


def site_name_from_html(html: str) -> str | None:
    tree = HTMLParser(html)
    og = tree.css_first('meta[property="og:site_name"]')
    if og and (og.attributes.get("content") or "").strip():
        return og.attributes["content"].strip()
    title = tree.css_first("title")
    if title:
        first = re.split(r"\s+[|:\-–—]\s+|:\s", title.text(strip=True))[0].strip()
        return first or None
    return None


def fetcher_for(domain: str, refresh: bool = False) -> Fetcher:
    return Fetcher(company_dir(domain) / "raw", refresh=refresh)


def resolve_company(
    fetcher: Fetcher,
    url_or_domain: str,
    name: str | None = None,
    ticker: str | None = None,
    private: bool = False,
) -> Company:
    domain = normalize_domain(url_or_domain)
    home = fetcher.get(f"https://{domain}/", "company_site")
    if not home.ok:
        raise RuntimeError(f"Homepage https://{domain}/ returned HTTP {home.status}")
    site_url = home.final_url
    name = name or site_name_from_html(home.text_body()) or domain.split(".")[0].title()
    company = Company(name=name, domain=domain, site_url=site_url)
    if not private:
        match = lookup_cik(fetcher, name, ticker)
        if match:
            company.cik, company.ticker, sec_title = match
            company.is_public = True
            log.info("SEC registrant: %s (%s, CIK %s)", sec_title, company.ticker, company.cik)
        elif ticker:
            raise RuntimeError(f"Ticker {ticker!r} not found in SEC company_tickers.json")
    return company


@dataclass
class DiscoveryResult:
    company: Company
    documents: list[RawDocument]
    reports: list[AdapterReport]
    stats: dict = field(default_factory=dict)


def discover(company: Company, fetcher: Fetcher, sources: tuple[str, ...] | None = None):
    adapters: list[SourceAdapter] = [
        CompanySiteAdapter(fetcher),
        SecEdgarAdapter(fetcher),
        SearchAdapter(fetcher),
    ]
    if sources:
        adapters = [a for a in adapters if a.source_type in sources]
    reports = [a.discover(company) for a in adapters]

    # One document per URL; the higher-priority source keeps it.
    by_url: dict[str, RawDocument] = {}
    for report in sorted(reports, key=lambda r: SOURCE_PRIORITY[r.source_type]):
        for doc in report.documents:
            by_url.setdefault(doc.url, doc)
    documents = list(by_url.values())

    search = next((a for a in adapters if isinstance(a, SearchAdapter)), None)
    stats = {
        "documents_by_source": {
            r.source_type: sum(d.source_type == r.source_type for d in documents) for r in reports
        },
        "skipped_by_source": {r.source_type: len(r.skipped) for r in reports},
        "network_requests": fetcher.network_requests,
        "search_api_calls": search.api_calls if search else 0,
    }
    result = DiscoveryResult(company, documents, reports, stats)
    write_manifest(result)
    return result


def write_manifest(result: DiscoveryResult) -> None:
    """Human-readable record of what discovery found and skipped (committed with the data)."""
    path = company_dir(result.company.domain) / "raw" / "manifest.json"
    manifest = {
        "generated_at": utc_now_iso(),
        "company": asdict(result.company),
        "stats": result.stats,
        "documents": [
            {k: v for k, v in asdict(d).items() if k != "text"} | {"chars": len(d.text)}
            for d in result.documents
        ],
        "skipped": {r.source_type: r.skipped for r in result.reports},
        "notes": {r.source_type: r.notes for r in result.reports},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
