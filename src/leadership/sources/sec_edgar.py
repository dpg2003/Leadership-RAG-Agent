"""SEC EDGAR adapter (public companies only; authoritative for executive officers).

Uses the free official endpoints with the SEC-required User-Agent (SEC_USER_AGENT):
- company_tickers.json to map ticker or name to CIK,
- the submissions API to find the latest 10-K and DEF 14A,
- the filing's primary document from the Archives.
"""

from __future__ import annotations

import json
import logging
import re

from leadership.config import require_setting
from leadership.sources.base import AdapterReport, Company, RawDocument
from leadership.sources.fetch import FetchBlocked, Fetcher, html_to_text

log = logging.getLogger(__name__)

SOURCE_TYPE = "sec_filing"
NAMESPACE = "sec_filing"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
FORMS = ("10-K", "DEF 14A")


def _sec_get(fetcher: Fetcher, url: str):
    return fetcher.get(url, NAMESPACE, user_agent=require_setting("SEC_USER_AGENT"))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def lookup_cik(fetcher: Fetcher, name: str, ticker: str | None) -> tuple[str, str, str] | None:
    """Return (cik, ticker, sec_title) or None.

    With a ticker, match it exactly. Without one, accept a name match only if exactly one SEC
    registrant's title starts with the company name as a whole word: a generic name
    ("Campfire") must not latch onto an unrelated registrant. Pass --ticker to be explicit.
    """
    res = _sec_get(fetcher, TICKERS_URL)
    if not res.ok:
        raise RuntimeError(f"SEC ticker list unavailable (HTTP {res.status})")
    rows = json.loads(res.text_body()).values()
    if ticker:
        hits = [r for r in rows if r["ticker"].upper() == ticker.upper()]
    else:
        key = _norm(name)
        hits = [r for r in rows if re.match(rf"{re.escape(key)}\b", _norm(r["title"]))]
        if len({r["cik_str"] for r in hits}) != 1:
            if hits:
                titles = sorted({r["title"] for r in hits})[:5]
                log.info("SEC name match for %r is ambiguous (%s); private", name, titles)
            return None
    if not hits:
        return None
    row = hits[0]
    return str(row["cik_str"]).zfill(10), row["ticker"], row["title"]


EXEC_HEADING = (
    r"^(information about (our )?)?executive officers( of the registrant| of the company)?\.?$"
)


def find_section(text: str, heading_pattern: str = EXEC_HEADING, max_len: int = 20_000):
    """Character span of the executive-officers section, or None.

    Headings appear several times (table of contents, page headers, exhibit index), so pick
    the heading followed by the most title lines (lines ending in "Officer") before the next
    heading or 3,000 characters. Only a hint for pre-filtering: extraction still sees, and
    is checked against, the full text.
    """
    matches = list(re.finditer(heading_pattern, text, flags=re.I | re.M))
    best: tuple[int, int] | None = None
    for i, m in enumerate(matches):
        end = m.end() + 3000
        if i + 1 < len(matches):
            end = min(end, matches[i + 1].start())
        score = len(re.findall(r"\bOfficer,?$", text[m.end() : end], flags=re.M))
        if score and (best is None or score >= best[1]):  # ties: later wins (body after TOC)
            best = (m.start(), score)
    if best is None:
        return None
    return best[0], min(len(text), best[0] + max_len)


class SecEdgarAdapter:
    source_type = SOURCE_TYPE

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher

    def discover(self, company: Company) -> AdapterReport:
        report = AdapterReport(SOURCE_TYPE)
        if not company.is_public or not company.cik:
            report.notes.append("not a public company (no SEC registrant matched); skipped")
            return report
        url = SUBMISSIONS_URL.format(cik=company.cik)
        try:
            sub = _sec_get(self.fetcher, url)
        except FetchBlocked as exc:
            report.skip(url, str(exc))
            return report
        if not sub.ok:
            report.skip(url, f"HTTP {sub.status}")
            return report
        recent = json.loads(sub.text_body())["filings"]["recent"]
        latest: dict[str, int] = {}
        for i, form in enumerate(recent["form"]):
            if form in FORMS and form not in latest:
                latest[form] = i  # `recent` is newest first
        for form in FORMS:
            if form not in latest:
                report.notes.append(f"no {form} in recent filings")
                continue
            i = latest[form]
            doc_url = ARCHIVE_URL.format(
                cik=int(company.cik),
                accession=recent["accessionNumber"][i].replace("-", ""),
                document=recent["primaryDocument"][i],
            )
            try:
                res = _sec_get(self.fetcher, doc_url)
            except FetchBlocked as exc:
                report.skip(doc_url, str(exc))
                continue
            if not res.ok:
                report.skip(doc_url, f"HTTP {res.status}")
                continue
            text, _ = html_to_text(res.text_body())
            raw_path = self.fetcher.write_text(NAMESPACE, doc_url, text)
            span = find_section(text)
            if span is None:
                report.notes.append(f"{form}: no executive officers section found")
            report.documents.append(
                RawDocument(
                    url=doc_url,
                    source_type=SOURCE_TYPE,
                    title=f"{company.name} {form} filed {recent['filingDate'][i]}",
                    text=text,
                    fetched_at=res.fetched_at,
                    raw_path=raw_path,
                    meta={
                        "form": form,
                        "filing_date": recent["filingDate"][i],
                        "report_date": recent["reportDate"][i],
                        "executive_officers_span": span,
                    },
                )
            )
        return report
