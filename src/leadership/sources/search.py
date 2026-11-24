"""Web search adapter (Tavily). Finds press releases, news, and bios, which is where most VPs
and department heads appear. Why Tavily: see DECISIONS.md D7.

Tavily fetches the pages and returns their text (`include_raw_content`), so we don't fetch
result pages ourselves. Excluded at query time and again on results: LinkedIn and
people-data sites whose terms forbid scraping.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from leadership.config import require_setting
from leadership.models import utc_now_iso
from leadership.sources.base import AdapterReport, Company, RawDocument
from leadership.sources.fetch import Fetcher

log = logging.getLogger(__name__)

SOURCE_TYPE = "search"
NAMESPACE = "search"
TAVILY_URL = "https://api.tavily.com/search"
MAX_RESULTS = 8
# ToS forbids scraping (LinkedIn, X, Meta), or the site resells scraped people/company
# profiles. Applied to cached results too, so changing this list needs no new API calls.
EXCLUDED_DOMAINS = (
    "linkedin.com", "x.com", "twitter.com", "facebook.com", "instagram.com",
    "zoominfo.com", "rocketreach.co", "crunchbase.com", "glassdoor.com", "apollo.io",
    "signalhire.com", "contactout.com", "lusha.com", "theorg.com", "comparably.com",
    "salary.com", "dealroom.co", "clay.com", "exa.ai", "tavily.com",
)  # fmt: skip
# tavily.com/knowledge, clay.com/dossier and exa.ai/websets are profile aggregators (the
# Tavily page cites LinkedIn follower counts), not primary sources.
QUERY_TEMPLATES = (
    '"{name}" CEO',
    '"{name}" CTO',
    '"{name}" CFO',
    '"{name}" "chief" officer',
    '"{name}" VP',
    '"{name}" "vice president"',
    '"{name}" "head of"',
    '"{name}" appoints OR hires OR names',
    '"{name}" leadership team',
    '"{name}" founder',
)


def build_queries(company: Company) -> list[str]:
    # The site host disambiguates generic names ("Campfire" -> "Campfire campfire.ai").
    host = (urlsplit(company.site_url).hostname or company.domain).removeprefix("www.")
    return [f"{t.format(name=company.name)} {host}" for t in QUERY_TEMPLATES]


def is_excluded(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in EXCLUDED_DOMAINS)


def _drop_excluded(data: dict) -> bool:
    """Remove excluded-domain results from a cached response. Returns True if any removed.
    Their URLs are kept in `excluded_urls` so the skip is still reported."""
    results = data["response"].get("results", [])
    kept = [r for r in results if not is_excluded(r.get("url", ""))]
    if len(kept) == len(results):
        return False
    dropped = [r.get("url", "") for r in results if is_excluded(r.get("url", ""))]
    data["response"]["results"] = kept
    data["excluded_urls"] = sorted({*data.get("excluded_urls", []), *dropped})
    return True


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def is_company_host(url: str, company: Company) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    roots = {company.domain.lower(), (urlsplit(company.site_url).hostname or "").lower()}
    roots = {r.removeprefix("www.") for r in roots if r}
    return any(host == r or host.endswith("." + r) for r in roots)


class SearchAdapter:
    source_type = SOURCE_TYPE

    def __init__(self, fetcher: Fetcher, max_results: int = MAX_RESULTS) -> None:
        self.fetcher = fetcher
        self.max_results = max_results
        self.api_calls = 0

    def _search(self, query: str) -> dict:
        key = hashlib.sha256(query.encode("utf-8")).hexdigest()[:20]
        path = self.fetcher.cache_dir / NAMESPACE / "_queries" / f"{key}.json"
        if path.exists() and not self.fetcher.refresh:
            data = json.loads(path.read_text(encoding="utf-8"))
            if _drop_excluded(data):
                _write_json(path, data)  # purge text from newly excluded domains
            return data
        payload = {
            "query": query,
            "search_depth": "basic",
            "max_results": self.max_results,
            "include_raw_content": "text",
            "exclude_domains": list(EXCLUDED_DOMAINS),
        }
        headers = {"Authorization": f"Bearer {require_setting('TAVILY_API_KEY')}"}
        self.api_calls += 1
        resp = httpx.post(TAVILY_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code != 200:
            # Don't cache failures; the error body never contains the key.
            raise RuntimeError(f"Tavily search failed (HTTP {resp.status_code}): {resp.text[:200]}")
        data = {"query": query, "fetched_at": utc_now_iso(), "response": resp.json()}
        _drop_excluded(data)
        _write_json(path, data)
        return data

    def discover(self, company: Company) -> AdapterReport:
        report = AdapterReport(SOURCE_TYPE)
        seen: set[str] = set()
        for query in build_queries(company):
            data = self._search(query)
            results = data["response"].get("results", [])
            for url in data.get("excluded_urls", []):
                if url not in seen:
                    seen.add(url)
                    report.skip(url, "excluded domain (ToS)")
                    self.fetcher.text_path(NAMESPACE, url).unlink(missing_ok=True)
            if not results:
                report.notes.append(f"no results for {query!r}")
            for r in results:
                url = r.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                if is_excluded(url):
                    report.skip(url, "excluded domain (ToS)")
                    # Don't keep (or commit) text from a domain excluded after it was cached.
                    self.fetcher.text_path(NAMESPACE, url).unlink(missing_ok=True)
                    continue
                direct_fetch = ""
                if is_company_host(url, company):
                    direct_fetch = self._direct_fetch_status(url)
                    if direct_fetch.startswith("disallowed"):
                        report.skip(url, f"company robots.txt {direct_fetch} (D13)")
                        self.fetcher.text_path(NAMESPACE, url).unlink(missing_ok=True)
                        continue
                text = (r.get("raw_content") or r.get("content") or "").strip()
                if len(text) < 100:
                    report.skip(url, "search result has no usable text")
                    continue
                lines = (" ".join(line.split()) for line in text.splitlines())
                text = "\n".join(line for line in lines if line)
                raw_path = self.fetcher.write_text(NAMESPACE, url, text)
                report.documents.append(
                    RawDocument(
                        url=url,
                        source_type=SOURCE_TYPE,
                        title=r.get("title", ""),
                        text=text,
                        fetched_at=data["fetched_at"],
                        raw_path=raw_path,
                        meta={
                            "query": query,
                            "score": r.get("score"),
                            "published_date": r.get("published_date"),
                            "raw_content": bool(r.get("raw_content")),
                        }
                        | ({"direct_fetch": direct_fetch} if direct_fetch else {}),
                    )
                )
        return report

    def _direct_fetch_status(self, url: str) -> str:
        """For a result on the company's own hosts: could we have fetched it ourselves?

        'disallowed ...' means robots.txt explicitly forbids the path: the result is dropped.
        'blocked ...' means the host refuses bots wholesale (robots.txt itself is 401/403/5xx
        or unreachable): the result is kept at search trust and flagged in VALIDATION.md.
        '' means a direct fetch would have been allowed. Only robots.txt is requested.
        """
        status, allowed = self.fetcher.robots_verdict(url)
        host = urlsplit(url).hostname
        if status == 0:
            return f"blocked: {host}/robots.txt unreachable"
        if status in (401, 403) or status >= 500:
            return f"blocked: {host}/robots.txt returned HTTP {status} (bot protection)"
        return "" if allowed else f"disallowed by {host}/robots.txt"
