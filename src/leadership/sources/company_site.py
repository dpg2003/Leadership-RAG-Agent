"""Company website adapter (highest-trust source).

Finds candidate pages from the homepage nav, common paths, and sitemap.xml, ranks them by
URL keywords, and fetches the top MAX_PAGES. No LLM here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from leadership.sources.base import AdapterReport, Company, RawDocument
from leadership.sources.fetch import (
    FetchBlocked,
    Fetcher,
    FetchResult,
    html_blocks,
    html_to_text,
)

log = logging.getLogger(__name__)

SOURCE_TYPE = "company_site"
NAMESPACE = "company_site"
MAX_PAGES = 60
MAX_CHILD_SITEMAPS = 6

COMMON_PATHS = (
    "about", "about-us", "team", "our-team", "leadership", "company", "company/leadership",
    "management", "people", "press", "newsroom", "careers",
)  # fmt: skip

# Path keywords and the score they give a URL. Highest match wins.
_LEADERSHIP_WORDS = r"leadership|team|about|management|executives?|founders?|people|company"
_PEOPLE_NEWS_WORDS = (
    r"appoint|names?|named|joins?|hires?|welcome|chief|officer|president|vp|vice-president|"
    r"head-of|executive|leader|founder|ceo|cto|cfo|coo|cmo|cpo|board"
)
_NEWS_WORDS = r"press|news|newsroom|blog|announce|media"
# Child sitemaps worth opening; product catalogs (stocks, crypto, help center) are not.
_SITEMAP_KEEP = r"newsroom|press|news|blog|company|about|marketing|pages?|main|post"


@dataclass
class Candidate:
    url: str
    score: int
    lastmod: str = ""
    via: str = ""


def _clean(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def score_url(url: str) -> int:
    path = urlsplit(url).path.lower()
    segments = [s for s in path.split("/") if s]
    last = segments[-1] if segments else ""
    if any(re.fullmatch(_LEADERSHIP_WORDS, s) for s in segments):
        return 10
    is_news = any(re.search(_NEWS_WORDS, s) for s in segments)
    if is_news and re.search(rf"(^|-)({_PEOPLE_NEWS_WORDS})(-|$)", last):
        return 8
    if re.search(rf"(^|-)({_LEADERSHIP_WORDS})(-|$)", last):
        return 6
    if "careers" in segments or "jobs" in segments:
        return 3
    if is_news and len(segments) >= 2:
        return 2  # an individual post: bylines often carry author titles
    return 0


def _site_hosts(company: Company) -> set[str]:
    hosts = {urlsplit(company.site_url).hostname or "", company.domain}
    return {h.removeprefix("www.") for h in hosts if h}


def _on_site(url: str, hosts: set[str]) -> bool:
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in hosts)


def parse_sitemap(xml: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ([(url, lastmod)], [child sitemap urls]). Regex, not an XML parser, on purpose:
    sitemaps are simple, and this avoids entity-expansion risks on untrusted XML."""
    urls = []
    for block in re.findall(r"<url>(.*?)</url>", xml, flags=re.S):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, flags=re.S)
        mod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, flags=re.S)
        if loc:
            urls.append((loc.group(1).replace("&amp;", "&"), mod.group(1) if mod else ""))
    children = []
    for block in re.findall(r"<sitemap>(.*?)</sitemap>", xml, flags=re.S):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, flags=re.S)
        if loc:
            children.append(loc.group(1).replace("&amp;", "&"))
    return urls, children


def _locale_prefix(site_url: str) -> str:
    """'/us/en/' for https://robinhood.com/us/en/, else '/'."""
    path = urlsplit(site_url).path
    return path if re.fullmatch(r"/[a-z]{2}/[a-z]{2}/", path) else "/"


class CompanySiteAdapter:
    source_type = SOURCE_TYPE

    def __init__(self, fetcher: Fetcher, max_pages: int = MAX_PAGES) -> None:
        self.fetcher = fetcher
        self.max_pages = max_pages

    def _get(self, url: str, report: AdapterReport) -> FetchResult | None:
        try:
            res = self.fetcher.get(url, NAMESPACE)
        except FetchBlocked as exc:
            report.skip(url, str(exc))
            return None
        if not res.ok:
            report.skip(url, f"HTTP {res.status}" if res.status else "network error")
            return None
        return res

    def candidates(self, company: Company, report: AdapterReport) -> list[Candidate]:
        hosts = _site_hosts(company)
        root = f"{urlsplit(company.site_url).scheme}://{urlsplit(company.site_url).netloc}"
        found: dict[str, Candidate] = {}

        def add(url: str, via: str, lastmod: str = "", score: int | None = None) -> None:
            url = _clean(url)
            if not url.startswith("http") or not _on_site(url, hosts):
                return
            s = score_url(url) if score is None else score
            prev = found.get(url)
            if prev is None or s > prev.score:
                found[url] = Candidate(url, s, lastmod or (prev.lastmod if prev else ""), via)
            elif lastmod and not prev.lastmod:
                prev.lastmod = lastmod

        add(company.site_url, "homepage", score=9)
        prefixes = {"/", _locale_prefix(company.site_url)}
        for prefix in prefixes:
            for path in COMMON_PATHS:
                add(urljoin(root, prefix + path), "common_path")

        home = self._get(company.site_url, report)
        if home is not None:
            for a in HTMLParser(home.text_body()).css("a[href]"):
                add(urljoin(home.final_url, a.attributes.get("href") or ""), "nav")

        sitemap_urls = [*self.fetcher.sitemaps_for(root), urljoin(root, "/sitemap.xml")]
        sitemap_urls = list(dict.fromkeys(sitemap_urls))
        children_opened = 0
        queue = list(sitemap_urls)
        while queue:
            sm_url = queue.pop(0)
            if sm_url.endswith(".gz"):
                report.notes.append(f"skipped gzipped sitemap {sm_url}")
                continue
            res = self._get(sm_url, report)
            if res is None:
                continue
            urls, children = parse_sitemap(res.text_body())
            for url, lastmod in urls:
                add(url, "sitemap", lastmod)
            for child in children:
                if re.search(_SITEMAP_KEEP, child.lower()) and children_opened < MAX_CHILD_SITEMAPS:
                    if _on_site(child, hosts):
                        queue.append(child)
                        children_opened += 1
                else:
                    report.notes.append(f"skipped child sitemap {child}")

        ranked = sorted(
            (c for c in found.values() if c.score > 0),
            key=lambda c: (-c.score, _neg_date(c.lastmod), c.url),
        )
        dropped = len(ranked) - self.max_pages
        if dropped > 0:
            report.notes.append(f"{dropped} lower-ranked candidate URLs not fetched (cap)")
        return ranked[: self.max_pages]

    def discover(self, company: Company) -> AdapterReport:
        report = AdapterReport(SOURCE_TYPE)
        seen_final: set[str] = set()
        for cand in self.candidates(company, report):
            res = self._get(cand.url, report)
            if res is None:
                continue
            final = _clean(res.final_url)
            if final in seen_final:
                continue  # e.g. /about redirecting to the homepage
            seen_final.add(final)
            if "html" not in res.content_type:
                report.skip(cand.url, f"not HTML ({res.content_type or 'unknown type'})")
                continue
            html = res.text_body()
            text, title = html_to_text(html)
            if len(text) < 200:
                report.skip(cand.url, "almost no text (soft 404, or rendered by JavaScript)")
                continue
            raw_path = self.fetcher.write_text(NAMESPACE, cand.url, text)
            report.documents.append(
                RawDocument(
                    url=final,
                    source_type=SOURCE_TYPE,
                    title=title,
                    text=text,
                    fetched_at=res.fetched_at,
                    raw_path=raw_path,
                    meta={"score": cand.score, "via": cand.via, "lastmod": cand.lastmod},
                    blocks=html_blocks(html),
                )
            )
        return report


def _neg_date(lastmod: str) -> str:
    # Sort newest first with an ascending sort: invert digits of an ISO date prefix.
    digits = re.sub(r"\D", "", lastmod)[:8].ljust(8, "0")
    return "".join(str(9 - int(d)) for d in digits)
