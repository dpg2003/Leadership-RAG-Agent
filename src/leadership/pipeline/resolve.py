"""Deterministic resolution of extracted people (no LLM).

This module currently holds the identity rule: deciding whether two name records for the
same company are the same person. Phase 3 adds fact writing and conflict resolution here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum

from leadership.models import normalize_name

# Small, explicit nickname map. Keys and values are normalized (lowercase, no accents).
# Prefix pairs ("vlad"/"vladimir", "dan"/"daniel") don't need an entry; see _first_names_match.
_NICKNAME_GROUPS = (
    {"robert", "rob", "bob", "bobby", "robbie"},
    {"william", "will", "bill", "billy", "liam"},
    {"james", "jim", "jimmy", "jamie"},
    {"michael", "mike", "mick"},
    {"richard", "rick", "dick", "rich"},
    {"edward", "ed", "eddie", "ted", "ned"},
    {"elizabeth", "liz", "beth", "betsy", "eliza"},
    {"margaret", "maggie", "peggy", "meg"},
    {"katherine", "catherine", "kate", "katie", "kathy", "cathy"},
    {"anthony", "tony"},
    {"andrew", "andy", "drew"},
    {"steven", "stephen", "steve"},
    {"joseph", "joe", "joey"},
    {"thomas", "tom", "tommy"},
    {"charles", "charlie", "chuck"},
    {"john", "jack", "johnny"},
    {"alexander", "alexandra", "alex", "sasha"},
    {"jennifer", "jen", "jenny"},
    {"christopher", "chris"},
    {"susan", "sue", "suzy"},
)
_NICKNAMES: dict[str, frozenset[str]] = {
    name: frozenset(group) for group in _NICKNAME_GROUPS for name in group
}

# A prefix shorter than this is too ambiguous to merge on ("Al" could be Alan or Alice).
_MIN_PREFIX_LEN = 3
# Whole-name similarity at or above this, without a merge, is flagged for human review.
_POSSIBLE_DUPLICATE_RATIO = 0.85
# Same first initial plus last names at least this similar is also flagged.
_SIMILAR_LAST_NAME_RATIO = 0.8


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


class Identity(StrEnum):
    SAME = "same"  # merge automatically
    POSSIBLE_DUPLICATE = "possible_duplicate"  # keep separate, list in VALIDATION.md
    DIFFERENT = "different"


@dataclass(frozen=True)
class NameRecord:
    company_id: int
    full_name: str
    functions: frozenset[str] = field(default_factory=frozenset)

    @property
    def tokens(self) -> list[str]:
        return normalize_name(self.full_name).split()


def _first_names_match(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if len(short) >= _MIN_PREFIX_LEN and long.startswith(short):
        return True
    return b in _NICKNAMES.get(a, frozenset())


def compare_identity(a: NameRecord, b: NameRecord) -> Identity:
    """Narrow, deterministic same-person rule.

    SAME only when all hold: same company; identical normalized names, OR same last name +
    first names that match by prefix (Vlad/Vladimir) or the nickname map + at least one
    overlapping role function. Anything close that misses a condition is
    POSSIBLE_DUPLICATE so a human decides; a wrong merge would mix two people's facts.
    """
    if a.company_id != b.company_id:
        return Identity.DIFFERENT
    ta, tb = a.tokens, b.tokens
    if not ta or not tb:
        return Identity.DIFFERENT
    if ta == tb:
        return Identity.SAME

    same_last = ta[-1] == tb[-1]
    first_match = _first_names_match(ta[0], tb[0])
    function_overlap = bool(a.functions & b.functions)

    if same_last and first_match and function_overlap:
        return Identity.SAME
    same_initial = ta[0][0] == tb[0][0]
    if same_last and (first_match or same_initial):
        return Identity.POSSIBLE_DUPLICATE
    if same_initial and _ratio(ta[-1], tb[-1]) >= _SIMILAR_LAST_NAME_RATIO:
        return Identity.POSSIBLE_DUPLICATE  # e.g. Quirk / Quirke
    ratio = _ratio(" ".join(ta), " ".join(tb))
    if ratio >= _POSSIBLE_DUPLICATE_RATIO:
        return Identity.POSSIBLE_DUPLICATE
    return Identity.DIFFERENT
