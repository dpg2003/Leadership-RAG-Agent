"""Keyword pre-filter (no LLM): which parts of a document are worth sending to the model.

Pages are long (nav, footers, product copy); leaders appear in a few lines. We keep windows of
lines around leadership keywords and skip documents with no hit at all, so the LLM sees less
text and costs less. The evidence check (validate.py) still runs against the *full* text.
"""

from __future__ import annotations

import re

from leadership.sources.base import RawDocument

# A line that mentions a leadership role ("Head of Product", "names Jane Doe CFO").
ROLE_LINE = re.compile(
    r"\b(?:chief|officer|ceo|cfo|cto|coo|president|vice president|vp|head of|founder|counsel)\b",
    re.IGNORECASE,
)
# Publication dates ("March 3, 2025", "2025-03-03"), so the model can report the page date.
DATE_LINE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+(?:19|20)\d\d\b"
    r"|\b(?:19|20)\d\d-\d\d-\d\d\b",
    re.IGNORECASE,
)
MAX_DATE_LINES = 3
WINDOW_LINES = 3
MAX_CHARS = 24_000  # per document sent to the model; the rest is dropped with a note


def select_text(doc: RawDocument) -> tuple[str, str]:
    """(text for the model, note). Empty text means: no leadership signal, skip the LLM."""
    text = doc.text
    if doc.source_type == "sec_filing":
        if doc.meta.get("form") == "8-K":
            return text[:MAX_CHARS], "SEC 8-K (Item 5.02), whole document"
        span = doc.meta.get("executive_officers_span")
        if span:
            return text[span[0] : span[1]], "SEC executive officers section"
        sig = text.rfind("SIGNATURES")
        if sig >= 0:
            return text[sig : sig + MAX_CHARS], "SEC signatures section"
        return "", "SEC filing without an executive officers or signatures section"

    lines = text.splitlines()
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if ROLE_LINE.search(line):
            keep.update(range(max(0, i - WINDOW_LINES), min(len(lines), i + WINDOW_LINES + 1)))
    if not keep:
        return "", "no leadership keywords"
    dates = [i for i, line in enumerate(lines) if DATE_LINE.search(line)][:MAX_DATE_LINES]
    keep.update(dates)

    chunks: list[str] = []
    prev = -2
    for i in sorted(keep):
        if i != prev + 1 and chunks:
            chunks.append("[...]")
        chunks.append(lines[i])
        prev = i
    selected = "\n".join(chunks)
    note = f"{len(keep)} of {len(lines)} lines"
    if len(selected) > MAX_CHARS:
        selected = selected[:MAX_CHARS]
        note += f", truncated to {MAX_CHARS} chars"
    return selected, note
