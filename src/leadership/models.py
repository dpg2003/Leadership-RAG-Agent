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
    text = unicodedata.normalize("NFKD", display_name(name))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("’", "'")
    text = re.sub(r"[^a-z\s'-]", " ", text).replace("'", "").replace("-", " ")
    tokens = [t for t in text.split() if t not in _HONORIFICS]
    if len(tokens) > 2:
        tokens = [tokens[0], *[t for t in tokens[1:-1] if len(t) > 1], tokens[-1]]
    return " ".join(tokens)


# Professional credentials after a name ("Stephanie Guild, CFA", "Jane Doe PhD"). Removed
# from the displayed name only; facts keep the name exactly as the source wrote it.
# Generational suffixes (Jr., III) are part of the name and stay.
_CREDENTIAL_SUFFIX = re.compile(
    r"(?:,?\s+(?:CFA|CPA|CFP|CAIA|FRM|CISSP|PhD|Ph\.D\.?|MBA|MD|M\.D\.?|JD|J\.D\.?|Esq\.?"
    r"|PE|P\.E\.?|DDS|LLM|LL\.M\.?))+\s*$"
)


def display_name(name: str) -> str:
    """'Stephanie Guild, CFA' -> 'Stephanie Guild'. Never empties a name."""
    stripped = _CREDENTIAL_SUFFIX.sub("", name.strip()).strip().rstrip(",")
    return stripped or name.strip()


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
