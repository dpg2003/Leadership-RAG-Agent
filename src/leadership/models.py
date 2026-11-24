"""Shared vocabulary: enum values, source priority, name normalization, timestamps."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

FUNCTIONS = (
    "engineering",
    "product",
    "finance",
    "marketing",
    "sales",
    "operations",
    "legal",
    "people",
    "security",
    "design",
    "data",
    "other",
)
SENIORITIES = ("c_level", "vp", "head", "founder")
SOURCE_TYPES = ("company_site", "sec_filing", "search")
FACT_FIELDS = ("full_name", "title", "location", "bio")
CONFIDENCE_LEVELS = ("high", "medium", "low")

# Lower number wins a conflict. Derived from sources.source_type; never stored.
SOURCE_PRIORITY = {"company_site": 0, "sec_filing": 1, "search": 2}

_HONORIFICS = {
    "mr",
    "mrs",
    "ms",
    "dr",
    "prof",
    "jr",
    "sr",
    "ii",
    "iii",
    "iv",
    "phd",
    "md",
    "mba",
    "cfa",
    "cpa",
    "esq",
}


def normalize_name(name: str) -> str:
    """Dedupe key for a person's name.

    Strips accents, case, punctuation, honorifics/suffixes, and single-letter middle initials:
    "Jason S. Warnick" and "jason warnick" map to the same key. Nicknames ("Vlad" vs
    "Vladimir") are deliberately not merged here.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("’", "'")
    text = re.sub(r"[^a-z\s'-]", " ", text).replace("'", "").replace("-", " ")
    tokens = [t for t in text.split() if t not in _HONORIFICS]
    if len(tokens) > 2:
        tokens = [tokens[0], *[t for t in tokens[1:-1] if len(t) > 1], tokens[-1]]
    return " ".join(tokens)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
