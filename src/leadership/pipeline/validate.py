"""Deterministic checks on what the model extracted (no LLM).

A claim is accepted only if its evidence is literally in the cached document text, the title
is literally in the evidence, the title is in scope by the title rules, the model says the
role is held at the target company, and the evidence itself states that affiliation (D22).
Optional fields (location, bio, page date) are dropped, not the whole claim, when their own
evidence fails. Every rejection is kept with a reason.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from leadership.models import normalize_name
from leadership.pipeline.extract import ExtractedPerson, Extraction
from leadership.pipeline.titles import TitleClass, classify_title, mentions_former
from leadership.sources.base import Company, RawDocument
from leadership.sources.search import is_company_host

MAX_SNIPPET_CHARS = 500

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\xa0": " "})


def norm_text(text: str) -> str:
    """Whitespace, quote and dash differences don't count; everything else must match."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_QUOTES).split())


def in_text(needle: str, haystack_norm: str, casefold: bool = False) -> bool:
    n = norm_text(needle)
    if not n:
        return False
    return n.casefold() in haystack_norm.casefold() if casefold else n in haystack_norm


@dataclass(frozen=True)
class Claim:
    """One accepted (person, title) statement from one document."""

    doc: RawDocument
    full_name: str
    title: str
    title_class: TitleClass
    status: str  # current | former | incoming | unclear
    evidence_snippet: str
    published_at: str | None  # YYYY-MM-DD, verified on the page or from source metadata
    location: str | None = None
    location_snippet: str | None = None
    bio: str | None = None
    reanchored: bool = False  # evidence span taken from the source, not the model's quote
    extracted_at: str | None = None  # when the LLM extraction was made (stable on re-runs)


@dataclass(frozen=True)
class Rejection:
    url: str
    full_name: str
    title: str
    reason: str


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


# The company's own pages whose purpose is listing its people (team, leadership, IR
# management pages): there the page itself states the affiliation.
_PEOPLE_PAGE = re.compile(
    r"^/(?:team|our-team|about|about-us|leadership|people|management|executives?|founders"
    r"|company|who-we-are|governance)(?:\.html?)?(?:/[^/]+)?/?$",
    re.IGNORECASE,
)
# First person on the company's own page: "my co-founder", "our CTO", "we hired".
# ("us" is left out: it also matches "US" in "Head of US Partnerships".)
_FIRST_PERSON = re.compile(r"\b(?:our|my|we|we're|we've)\b", re.IGNORECASE)


def _company_names(company: Company) -> set[str]:
    stems = {company.name}
    for host in (company.domain, urlsplit(company.site_url).hostname or ""):
        stem = host.lower().removeprefix("www.").split(".")[0]
        if len(stem) >= 3:
            stems.add(stem)
    return {norm_text(s).casefold() for s in stems if s.strip()}


def _own_sec_filing(doc: RawDocument, company: Company) -> bool:
    """A search hit that is one of the company's own SEC filings (an older S-1, say)."""
    if not company.cik:
        return False
    parts = urlsplit(doc.url)
    host = (parts.hostname or "").lower()
    return host.endswith("sec.gov") and parts.path.startswith(
        f"/Archives/edgar/data/{int(company.cik)}/"
    )


def affiliation_problem(doc: RawDocument, snippet: str, company: Company | None) -> str:
    """D22: the evidence must itself tie the person to the company. Accepted when the source is
    one of the company's own SEC filings; the snippet names the company; or the page is the
    company's own and is a people page (team, leadership, IR management) or speaks in the
    first person. A byline on a company blog post ("Jane Doe, Head of Partnerships") is not
    enough: guest and partner authors are common."""
    if company is None or doc.source_type == "sec_filing" or _own_sec_filing(doc, company):
        return ""
    s = norm_text(snippet).casefold()
    if any(
        re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", s) for n in _company_names(company)
    ):
        return ""
    own = doc.source_type == "company_site" or is_company_host(doc.url, company)
    if own and (_PEOPLE_PAGE.search(urlsplit(doc.url).path) or _FIRST_PERSON.search(snippet)):
        return ""
    return f"evidence does not state an affiliation with {company.name}"


def _snippet_problem(snippet: str, last_name: str, title: str, doc_norm: str) -> str:
    if len(snippet) > MAX_SNIPPET_CHARS:
        return "evidence snippet too long"
    if not in_text(snippet, doc_norm):
        return "evidence snippet not found in the source text"
    if last_name not in normalize_name(snippet).split():
        return "evidence snippet does not contain the person's last name"
    if not in_text(title, norm_text(snippet), casefold=True):
        return "title not found in the evidence snippet"
    return ""


def doc_blocks(doc: RawDocument) -> list[str]:
    """Innermost HTML blocks when the adapter kept them; otherwise each line of the text, split
    at markdown table cells ("| Alice, CEO | Bob, CTO |" is two blocks)."""
    if doc.blocks is not None:
        return doc.blocks
    return [cell for line in doc.text.splitlines() for cell in line.split("|") if cell.strip()]


# Between a name and its title in flattened text: only separators ("Jane Doe, CFO").
_SEPARATORS = re.compile(r"[\s,:;|·•\-–—()]*")


def reanchor(
    full_name: str,
    title: str,
    doc: RawDocument,
    doc_norm: str,
    other_names: tuple[str, ...] = (),
) -> str | None:
    """Shortest span holding the exact name and the exact title (case-insensitive) inside ONE
    block (D18, tightened in D22), literal in the document text, and never containing another
    person's name from the same page.

    - HTML pages: the block is the innermost block element; name and title may be in either
      order within it (a sentence in one <p>).
    - Text without HTML structure (search results): a line can be a whole flattened team page
      ("Vlad Tenev Chairman & CEO Pooja Anand Vice President, ..."), so the title must
      directly follow the name, with only separators between them.
    """
    name, ttl = norm_text(full_name), norm_text(title).casefold()
    if not name or not ttl:
        return None
    flattened = doc.blocks is None
    others = [norm_text(o) for o in other_names if norm_text(o) and norm_text(o) != name]
    best: str | None = None
    for block in doc_blocks(doc):
        b = norm_text(block)
        folded = b.casefold()
        for m in re.finditer(re.escape(name), b):
            for t in re.finditer(re.escape(ttl), folded):
                if flattened and not (
                    t.start() >= m.end() and _SEPARATORS.fullmatch(b[m.end() : t.start()])
                ):
                    continue
                span = b[min(m.start(), t.start()) : max(m.end(), t.end())]
                if any(o in span for o in others):
                    continue
                if (best is None or len(span) < len(best)) and span in doc_norm:
                    best = span
    return best


def page_date(doc: RawDocument, extraction: Extraction, doc_norm: str) -> str | None:
    """Filing date (SEC) > page-stated date with verified snippet > search metadata date."""
    if doc.source_type == "sec_filing":
        return _iso_date(doc.meta.get("filing_date"))
    stated = _iso_date(extraction.page_published_date)
    snippet = extraction.published_date_snippet
    if stated and snippet and in_text(snippet, doc_norm) and stated[:4] in snippet:
        return stated
    return _iso_date(doc.meta.get("published_date"))


def check_person(
    doc: RawDocument,
    p: ExtractedPerson,
    doc_norm: str,
    published_at: str | None,
    company: Company | None = None,
    other_names: tuple[str, ...] = (),
) -> Claim | Rejection:
    def reject(reason: str) -> Rejection:
        return Rejection(doc.url, p.full_name, p.title, reason)

    tokens = normalize_name(p.full_name).split()
    if len(tokens) < 2:
        return reject("name is not a full name")
    if not p.works_at_target_company:
        return reject(f"not at the target company (employer: {p.employer!r})")
    if not in_text(p.full_name, doc_norm):
        return reject("name not found in the source text")
    model_snippet = p.evidence_snippet.strip()
    snippet = model_snippet
    problem = _snippet_problem(model_snippet, tokens[-1], p.title, doc_norm)
    reanchored = False
    if problem:
        # The model often quotes a sentence but takes the title from a nearby heading. If the
        # exact name and title sit close together in the source, quote that span instead.
        span = reanchor(p.full_name, p.title, doc, doc_norm, other_names)
        if span is None:
            return reject(problem)
        snippet, reanchored = span, True
    title_class = classify_title(p.title, company.name if company else None)
    if title_class is None:
        return reject("title out of scope (not C-level, founder, VP or Head)")
    affiliation = affiliation_problem(doc, snippet, company)
    if affiliation:
        return reject(affiliation)

    status = p.status
    former_re = rf"\bformer(?:ly)?\b[^.]{{0,40}}{re.escape(p.title)}"
    if mentions_former(p.title) or any(
        re.search(former_re, norm_text(s), re.I) for s in {snippet, model_snippet}
    ):
        status = "former"

    location = location_snippet = None
    if (
        p.location
        and p.location_snippet
        and in_text(p.location_snippet, doc_norm)
        and in_text(p.location, norm_text(p.location_snippet), casefold=True)
    ):
        location, location_snippet = p.location.strip(), p.location_snippet.strip()
    bio = None
    if p.bio and len(p.bio) <= 1000 and in_text(p.bio, doc_norm):
        bio = p.bio.strip()

    return Claim(
        reanchored=reanchored,
        doc=doc,
        full_name=p.full_name.strip(),
        title=p.title.strip(),
        title_class=title_class,
        status=status,
        evidence_snippet=snippet,
        published_at=published_at,
        location=location,
        location_snippet=location_snippet,
        bio=bio,
    )


def validate(
    doc: RawDocument, extraction: Extraction, company: Company | None = None
) -> tuple[list[Claim], list[Rejection]]:
    """Checks every extracted person. `company` enables the affiliation check (D22) and the
    company-name rules in titles.py; collect.py always passes it."""
    doc_norm = norm_text(doc.text)
    published_at = page_date(doc, extraction, doc_norm)
    claims: list[Claim] = []
    rejections: list[Rejection] = []
    names = tuple(sorted({p.full_name for p in extraction.people}))
    for person in extraction.people:
        result = check_person(doc, person, doc_norm, published_at, company, names)
        (claims if isinstance(result, Claim) else rejections).append(result)  # type: ignore[arg-type]
    return claims, rejections
