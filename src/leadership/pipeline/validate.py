"""Deterministic checks on what the model extracted (no LLM).

A claim is accepted only if its evidence is literally in the cached document text, the title
is literally in the evidence, the title is in scope by the title rules, and the model says the
role is held at the target company. Optional fields (location, bio, page date) are dropped,
not the whole claim, when their own evidence fails. Every rejection is kept with a reason.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from leadership.models import normalize_name
from leadership.pipeline.extract import ExtractedPerson, Extraction
from leadership.pipeline.titles import TitleClass, classify_title, mentions_former
from leadership.sources.base import RawDocument

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


REANCHOR_MAX_GAP = 200


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


def reanchor(full_name: str, title: str, doc_norm: str) -> str | None:
    """Shortest span of the (normalized) source text holding the exact name and the exact
    title (case-insensitive) at most REANCHOR_MAX_GAP characters apart. Literal, no LLM."""
    name, ttl = norm_text(full_name), norm_text(title).casefold()
    if not name or not ttl:
        return None
    folded = doc_norm.casefold()
    best: tuple[int, int] | None = None
    for m in re.finditer(re.escape(name), doc_norm):
        lo = max(0, m.start() - REANCHOR_MAX_GAP - len(ttl))
        hi = min(len(doc_norm), m.end() + REANCHOR_MAX_GAP + len(ttl))
        for t in re.finditer(re.escape(ttl), folded[lo:hi]):
            ts, te = lo + t.start(), lo + t.end()
            gap = max(ts - m.end(), m.start() - te, 0)
            if gap > REANCHOR_MAX_GAP:
                continue
            span = (min(m.start(), ts), max(m.end(), te))
            if best is None or span[1] - span[0] < best[1] - best[0]:
                best = span
    if best is None:
        return None
    return doc_norm[best[0] : best[1]]


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
    company_name: str | None = None,
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
        span = reanchor(p.full_name, p.title, doc_norm)
        if span is None:
            return reject(problem)
        snippet, reanchored = span, True
    title_class = classify_title(p.title, company_name)
    if title_class is None:
        return reject("title out of scope (not C-level, founder, VP or Head)")

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
    doc: RawDocument, extraction: Extraction, company_name: str | None = None
) -> tuple[list[Claim], list[Rejection]]:
    doc_norm = norm_text(doc.text)
    published_at = page_date(doc, extraction, doc_norm)
    claims: list[Claim] = []
    rejections: list[Rejection] = []
    for person in extraction.people:
        result = check_person(doc, person, doc_norm, published_at, company_name)
        (claims if isinstance(result, Claim) else rejections).append(result)  # type: ignore[arg-type]
    return claims, rejections
