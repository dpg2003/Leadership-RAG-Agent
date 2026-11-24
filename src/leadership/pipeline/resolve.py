"""Deterministic resolution of extracted people (no LLM).

- Identity: which claims are the same person (narrow rule, D6).
- Roles: one role per (seniority, function) per person; the best source names it.
- Currency: 'former' claims, and the proxy rule (D16): for C-level roles, the latest DEF 14A
  beats older newsroom/search claims that it doesn't confirm.
- Confidence: from source types, corroboration and age (D16).
- Review lists for VALIDATION.md: merges, possible duplicates, conflicts, stale roles.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from leadership.models import SOURCE_PRIORITY, normalize_name

if TYPE_CHECKING:
    from leadership.pipeline.validate import Claim

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


# ---------------------------------------------------------------------------- resolution

STALE_AFTER_DAYS = 548  # 18 months
PROXY_FORM = "DEF 14A"
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_SENIORITY_RANK = {"c_level": 0, "founder": 1, "vp": 2, "head": 3}


def _ordinal(iso: str | None) -> int:
    return date.fromisoformat(iso).toordinal() if iso else 0


def _claim_sort_key(c: Claim) -> tuple:
    """Best evidence first: source priority, then newest dated, then URL for stability."""
    return (SOURCE_PRIORITY[c.doc.source_type], -_ordinal(c.published_at), c.doc.url, c.title)


def _is_proxy(c: Claim) -> bool:
    return c.doc.source_type == "sec_filing" and c.doc.meta.get("form") == PROXY_FORM


def _blocked_host(c: Claim) -> bool:
    return str(c.doc.meta.get("direct_fetch", "")).startswith("blocked")


@dataclass
class ResolvedRole:
    title: str
    function: str
    seniority: str
    is_current: bool
    as_of: str | None
    confidence: str
    claims: list[Claim]
    note: str = ""


@dataclass
class ResolvedPerson:
    full_name: str
    claims: list[Claim]
    roles: list[ResolvedRole]
    confidence: str
    profile_url: str | None = None
    location: str | None = None
    location_claim: Claim | None = None
    bio: str | None = None
    bio_claim: Claim | None = None

    @property
    def is_current(self) -> bool:
        return any(r.is_current for r in self.roles)


@dataclass
class Resolution:
    people: list[ResolvedPerson]
    review: dict[str, list[dict[str, Any]]]


def cluster_claims(claims: list[Claim], company_id: int = 0) -> list[list[Claim]]:
    """Group claims into people with compare_identity (SAME only), best evidence first."""
    clusters: list[list[Claim]] = []
    for claim in sorted(claims, key=_claim_sort_key):
        rec = NameRecord(company_id, claim.full_name, frozenset({claim.title_class.function}))
        for cluster in clusters:
            functions = frozenset(c.title_class.function for c in cluster)
            if compare_identity(rec, NameRecord(company_id, cluster[0].full_name, functions)) is (
                Identity.SAME
            ):
                cluster.append(claim)
                break
        else:
            clusters.append([claim])
    return clusters


def _role_keys(c: Claim) -> list[tuple[str, str]]:
    keys = [(c.title_class.seniority, c.title_class.function)]
    if c.title_class.also_founder:
        keys.append(("founder", "other"))
    return keys


def role_confidence(claims: list[Claim]) -> str:
    """high: an SEC filing, or 2+ sources incl. the company site. medium: the company site,
    or 2+ sources. low: a single search result."""
    urls = {c.doc.url for c in claims}
    types = {c.doc.source_type for c in claims}
    if "sec_filing" in types or (len(urls) >= 2 and "company_site" in types):
        return "high"
    if "company_site" in types or len(urls) >= 2:
        return "medium"
    return "low"


def _resolve_role(
    key: tuple[str, str],
    claims: list[Claim],
    proxy_date: str | None,
    proxy_keys: set[tuple[str, str]],
    today: date,
) -> ResolvedRole:
    seniority, function = key
    claims = sorted(claims, key=_claim_sort_key)
    current = [c for c in claims if c.status != "former"]
    former = [c for c in claims if c.status == "former"]
    newest_current = max((_ordinal(c.published_at) for c in current), default=-1)
    newest_former = max((_ordinal(c.published_at) for c in former), default=-1)
    note = ""
    retired_by_proxy = False
    # A 'former' statement wins unless a current claim is strictly newer (undated = oldest).
    is_current = bool(current) and (not former or newest_current > newest_former)
    if former and current:
        note = "sources disagree on whether this role is current"

    # D16 proxy rule: a dated C-level newsroom/search claim older than the latest proxy, for a
    # role the proxy doesn't list for this person, is not current. SEC claims are exempt.
    # Undated claims can't be shown to be older: they stay current and are listed for review
    # (the proxy lists only Section 16 officers, so e.g. a Chief People Officer may be absent).
    if (
        is_current
        and seniority == "c_level"
        and proxy_date is not None
        and key not in proxy_keys
        and not any(c.doc.source_type == "sec_filing" for c in current)
    ):
        if newest_current <= 0:
            note = f"C-level title not in the {proxy_date} proxy; source is undated"
        elif newest_current < _ordinal(proxy_date):
            is_current = False
            retired_by_proxy = True
            note = f"not listed in the {proxy_date} proxy, which is newer than this claim"

    supporting = current if is_current or retired_by_proxy else (former or claims)
    dated = [c.published_at for c in supporting if c.published_at]
    as_of = max(dated) if dated else None
    confidence = role_confidence(supporting)
    single = len({c.doc.url for c in supporting}) == 1
    if (
        is_current
        and single
        and as_of
        and (today - date.fromisoformat(as_of)).days > STALE_AFTER_DAYS
    ):
        confidence = "low"
        note = note or f"single source, last dated {as_of} (older than 18 months)"
    return ResolvedRole(
        supporting[0].title, function, seniority, is_current, as_of, confidence, claims, note
    )


def _apply_promotions(roles: list[ResolvedRole]) -> None:
    """A person's older title is superseded by a newer, dated title in the same function
    (VP of Operations in 2018, then COO from 2019). Founder roles are never superseded."""
    for r in roles:
        if not r.is_current or r.seniority == "founder":
            continue
        newest = max((_ordinal(c.published_at) for c in r.claims), default=0)
        newer = [
            o
            for o in roles
            if o is not r
            and o.seniority != "founder"
            and o.function == r.function
            and newest
            and min((_ordinal(c.published_at) for c in o.claims if c.published_at), default=0)
            > newest
        ]
        if newer:
            r.is_current = False
            r.note = (
                f"superseded by a later title: {newer[0].title} ({newer[0].as_of or 'undated'})"
            )


def _review_role(review: dict[str, list[dict[str, Any]]], name: str, r: ResolvedRole) -> None:
    if r.seniority == "founder" and r.claims[0].title_class.seniority != "founder":
        return  # the founder half of "Co-founder & CEO": already listed under the main role
    entry = {
        "person": name,
        "title": r.title,
        "note": r.note,
        "urls": sorted({c.doc.url for c in r.claims}),
    }
    if "disagree" in r.note:
        review["conflicts"].append(entry | {"field": "is_current", "resolved": r.is_current})
    elif "source is undated" in r.note:
        review["c_level_not_in_proxy_undated"].append(entry)
    elif "proxy" in r.note:
        review["superseded_by_proxy"].append(entry)
    elif "superseded by a later title" in r.note:
        review["superseded_by_promotion"].append(entry)
    elif "18 months" in r.note:
        review["stale_single_source"].append(entry)


def _seat_conflicts(people: list[ResolvedPerson], review: dict[str, list[dict[str, Any]]]):
    """Two people both current in the same C-level seat (e.g. two CTOs). Listed, not decided:
    the older one may have left, or titles may overlap. Board chairs and 'other' are skipped."""
    seats: dict[str, list[tuple[ResolvedPerson, ResolvedRole]]] = defaultdict(list)
    for p in people:
        for r in p.roles:
            if r.is_current and r.seniority == "c_level" and r.function != "other":
                seats[r.function].append((p, r))
    for function, holders in sorted(seats.items()):
        if len({p.full_name for p, _ in holders}) > 1:
            review["seat_conflicts"].append(
                {
                    "function": function,
                    "holders": [
                        {"person": p.full_name, "title": r.title, "as_of": r.as_of}
                        for p, r in sorted(holders, key=lambda h: h[1].as_of or "", reverse=True)
                    ],
                }
            )


def resolve_claims(
    claims: list[Claim], today: date | None = None, company_id: int = 0
) -> Resolution:
    today = today or date.today()
    proxy_claims = [c for c in claims if _is_proxy(c) and c.status != "former"]
    proxy_date = max((c.published_at for c in proxy_claims if c.published_at), default=None)

    review: dict[str, list[dict[str, Any]]] = defaultdict(list)
    people: list[ResolvedPerson] = []
    for cluster in cluster_claims(claims, company_id):
        names = sorted({c.full_name for c in cluster})
        if len({normalize_name(n) for n in names}) > 1:
            review["merged_by_nickname_rule"].append({"names": names})
        proxy_keys = {k for c in cluster if c in proxy_claims for k in _role_keys(c)}

        by_key: dict[tuple[str, str], list[Claim]] = defaultdict(list)
        for c in cluster:
            for key in _role_keys(c):
                by_key[key].append(c)
        roles = [_resolve_role(k, cs, proxy_date, proxy_keys, today) for k, cs in by_key.items()]
        _apply_promotions(roles)
        roles.sort(key=lambda r: (not r.is_current, _SENIORITY_RANK[r.seniority], r.title))
        live = [r for r in roles if r.is_current] or roles
        confidence = max((r.confidence for r in live), key=_CONFIDENCE_RANK.__getitem__)
        live_claims = [c for r in live for c in r.claims if c.status != "former"] or cluster
        best = min(live_claims, key=_claim_sort_key)
        person = ResolvedPerson(best.full_name, cluster, roles, confidence, best.doc.url)

        ordered = sorted(cluster, key=_claim_sort_key)
        located = [c for c in ordered if c.location]
        if located:
            person.location, person.location_claim = located[0].location, located[0]
            values = sorted({c.location for c in located if c.location})
            if len({v.casefold() for v in values}) > 1:
                review["conflicts"].append(
                    {"person": person.full_name, "field": "location", "values": values}
                )
        bios = [c for c in ordered if c.bio]
        if bios:
            person.bio, person.bio_claim = bios[0].bio, bios[0]

        for r in roles:
            _review_role(review, person.full_name, r)
        for c in cluster:
            if c.reanchored:
                review["reanchored"].append(
                    {"person": person.full_name, "title": c.title, "url": c.doc.url,
                     "evidence_snippet": c.evidence_snippet}
                )  # fmt: skip
        if any(_blocked_host(c) for c in cluster):
            review["from_blocked_host"].append(
                {
                    "person": person.full_name,
                    "only_source": all(_blocked_host(c) for c in cluster),
                    "urls": sorted({c.doc.url for c in cluster if _blocked_host(c)}),
                }
            )
        people.append(person)

    _seat_conflicts(people, review)
    for i, a in enumerate(people):
        for b in people[i + 1 :]:
            ra = NameRecord(company_id, a.full_name, frozenset(r.function for r in a.roles))
            rb = NameRecord(company_id, b.full_name, frozenset(r.function for r in b.roles))
            if compare_identity(ra, rb) is Identity.POSSIBLE_DUPLICATE:
                review["possible_duplicates"].append({"names": sorted([a.full_name, b.full_name])})
    people.sort(key=lambda p: p.full_name)
    return Resolution(people, dict(review))
