"""Deterministic resolution of extracted people (no LLM).

- Identity: which claims are the same person (narrow rule, D6).
- Roles: one role per (seniority, function) per person; the best source names it.
- Currency: 'former' claims, and the proxy rule (D16): for C-level roles, the latest DEF 14A
  beats older newsroom/search claims that it doesn't confirm.
- Confidence: from source types, corroboration and age (D16); re-anchored evidence alone
  is low (D22).
- General Counsel / Chief Legal: C-level only if in the latest proxy, else Head (D4).
- Founders: current leadership only with a current operating role (D23).
- Review lists for VALIDATION.md: merges, possible duplicates, conflicts, stale roles.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from leadership.models import SOURCE_PRIORITY, display_name, normalize_name
from leadership.pipeline.titles import seat_functions

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
    return (SOURCE_PRIORITY[c.doc.source_type], -_ordinal(c.effective_date), c.doc.url, c.title)


def _is_proxy(c: Claim) -> bool:
    return c.doc.source_type == "sec_filing" and c.doc.meta.get("form") == PROXY_FORM


def _cite(c: Claim) -> str:
    """'robinhood.com/newsroom/x (2021-03-01)' for role notes the chat agent can quote."""
    parts = urlsplit(c.doc.url)
    where = (parts.hostname or "").removeprefix("www.") + parts.path.rstrip("/")
    kind = c.doc.meta.get("form") or c.doc.source_type.replace("_", " ")
    if c.published_at:
        when = c.published_at
    elif c.roster:
        when = f"current roster, retrieved {c.effective_date}"
    else:
        when = "undated"
    return f"{kind} {where} ({when})"


def _newest(claims: list[Claim]) -> Claim:
    return max(
        claims, key=lambda c: (_ordinal(c.effective_date), -SOURCE_PRIORITY[c.doc.source_type])
    )


# An undated page whose LAST path segment is a people word (/governance/leadership, /team,
# /about) is the company's current listing: for choosing a title, it counts as newer than
# any dated claim. /management/<name> profiles and /about/<product> pages are not listings.
_LISTING_PATH = re.compile(
    r"/(?:team|our-team|about|about-us|leadership|people|management|executives?|founders|"
    r"who-we-are)/?$"
)


# Deputy / Associate / Assistant General Counsel are not the legal chief.
_DEPUTY = re.compile(r"\b(?:deputy|associate|assistant)\b", re.IGNORECASE)


def _title_claim(claims: list[Claim]) -> Claim:
    """The claim whose wording is displayed: the newest one (a 2026 proxy beats a 2021
    newsroom post; an undated current leadership listing beats a 2020 announcement), then
    source priority, then the fuller wording ("..., Robinhood Financial" keeps its scope).
    Superseded wording stays in the facts."""

    def key(c: Claim) -> tuple:
        listing = c.roster or (
            not c.published_at and bool(_LISTING_PATH.search(urlsplit(c.doc.url).path))
        )
        return (
            listing,
            _ordinal(c.published_at),
            -SOURCE_PRIORITY[c.doc.source_type],
            len(c.title),
            c.doc.url,
        )

    return max(claims, key=key)


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
    name_claim: Claim | None = None  # the claim the displayed name comes from

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


def _role_keys(c: Claim, in_proxy: bool = False) -> list[tuple[str, str]]:
    """D4: General Counsel / Chief Legal is C-level only for a person the latest proxy lists
    as an executive officer; otherwise VP if the title has a VP token (D30), else Head."""
    if c.title_class.legal_chief and in_proxy:
        keys = [("c_level", "legal")]
    elif c.title_class.legal_chief:
        keys = [("vp" if c.title_class.seniority == "vp" else "head", "legal")]
    else:
        keys = [(c.title_class.seniority, c.title_class.function)]
    # D37: a C-level title naming several seats ("CEO & CFO", "CEO, CFO and founder") is one
    # role row per seat, each backed by the claims whose wording names it.
    if c.title_class.seniority == "c_level" and not c.title_class.legal_chief:
        for function in sorted(seat_functions(c.title) - {f for _, f in keys}):
            keys.append(("c_level", function))
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
    # D38: a search copy of the company's own roster page is medium (it is the company's
    # page, fetched via search because the host blocks bots); corroboration raises it.
    if "company_site" in types or len(urls) >= 2 or any(c.roster for c in claims):
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
    # D38: a roster listing counts as dated by its retrieval date (effective_date).
    newest_current = max((_ordinal(c.effective_date) for c in current), default=-1)
    newest_former = max((_ordinal(c.effective_date) for c in former), default=-1)
    newest_dated_current = max((_ordinal(c.published_at) for c in current), default=-1)
    note = ""
    retired_by_proxy = False
    # A 'former' statement wins unless a current claim is strictly newer (undated = oldest).
    is_current = bool(current) and (not former or newest_current > newest_former)
    if former and current:
        note = (
            f"sources disagree on whether this role is current: former per "
            f"{_cite(_newest(former))}; current per {_cite(_newest(current))}"
        )
    elif former:
        note = f"stated as former in {_cite(_newest(former))}"

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
        roster = [c for c in current if c.roster]
        if roster:
            note = (
                f"C-level title not in the {proxy_date} proxy; listed on the company's current "
                f"roster: {_cite(roster[0])}"
            )
        elif newest_dated_current <= 0:
            note = (
                f"C-level title not in the {proxy_date} proxy; source is undated: "
                f"{_cite(current[0])}"
            )
        elif newest_dated_current < _ordinal(proxy_date):
            is_current = False
            retired_by_proxy = True
            note = (
                f"not listed in the {proxy_date} proxy, which is newer than this claim; "
                f"last confirmed in {_cite(_newest(current))}"
            )

    supporting = current if is_current or retired_by_proxy else (former or claims)
    dated = [c.effective_date for c in supporting if c.effective_date]
    as_of = max(dated) if dated else None
    confidence = role_confidence(supporting)
    single = len({c.doc.url for c in supporting}) == 1
    # The 18-month single-source rule (D32) is applied once, in retire_stale(), after rows
    # are deduped and merged, so pooled sources count as confirmation.
    # D22: evidence re-anchored from the source (not the model's own quote) is low unless
    # another source corroborates the role. (Listed in review.reanchored either way.)
    if single and all(c.affiliation == "byline" for c in supporting):
        confidence = "low"
        note = note or (
            f"affiliation rests on a single author byline on the company's site: "
            f"{_cite(supporting[0])}"
        )
    if single and all(c.reanchored for c in supporting):
        confidence = "low"
        note = note or (
            f"evidence re-anchored by name and title in one block; single source: "
            f"{_cite(supporting[0])}"
        )
    title = _title_claim(supporting).title
    return ResolvedRole(title, function, seniority, is_current, as_of, confidence, claims, note)


def _title_key(title: str) -> str:
    """'CEO & Founder' and 'ceo and founder' are the same title."""
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", title.lower().replace("&", " and ")).split())


def dedupe_roles(roles: list[ResolvedRole]) -> list[ResolvedRole]:
    """D29: one row per (normalized title, function) per person; sources are merged.

    "CEO & Founder" resolves to a C-level role and a founder role (D4/D23); shown as two
    identical rows. The merged row keeps the most senior seniority, is current if either
    was, and keeps every claim, the newest as_of, the best confidence, and both notes."""
    groups: dict[tuple[str, str], list[ResolvedRole]] = {}
    for r in roles:
        groups.setdefault((_title_key(r.title), r.function), []).append(r)
    out = []
    for group in groups.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        lead = min(group, key=lambda r: (not r.is_current, _SENIORITY_RANK[r.seniority]))
        claims = list({id(c): c for r in group for c in r.claims}.values())
        notes = [r.note for r in group if r.note]
        seniorities = sorted({r.seniority for r in group}, key=_SENIORITY_RANK.__getitem__)
        if len(seniorities) > 1:
            notes.append(f"one row for: {', '.join(seniorities)}")
        out.append(
            ResolvedRole(
                lead.title,
                lead.function,
                lead.seniority,
                any(r.is_current for r in group),
                max((r.as_of for r in group if r.as_of), default=None),
                max((r.confidence for r in group), key=_CONFIDENCE_RANK.__getitem__),
                claims,
                "; ".join(dict.fromkeys(notes)),
            )
        )
    return out


_TITLE_STOP = frozenset({"and", "of", "the", "llc", "inc", "for", "at", "to", "a"})
OVERLAP_MIN = 0.6  # Jaccard similarity of title words for "the wording mostly overlaps"


# Seniority words don't count toward overlap: "VP of Product" and "VP of Product Marketing"
# share only "product", so they stay two roles.
_SENIORITY_WORDS = frozenset(
    {"vp", "svp", "evp", "avp", "senior", "executive", "chief", "officer", "head"}
)


def _title_words(title: str) -> frozenset[str]:
    t = _title_key(title)
    t = re.sub(r"\bvice president\b", "vp", t)
    return frozenset(w for w in t.split() if w not in _TITLE_STOP | _SENIORITY_WORDS)


def title_overlap(a: str, b: str) -> float:
    wa, wb = _title_words(a), _title_words(b)
    if len(wa) < 2 or len(wb) < 2:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def merge_overlapping_roles(
    roles: list[ResolvedRole],
) -> tuple[list[ResolvedRole], list[dict[str, Any]]]:
    """D30: rows of one person with the same seniority whose wording mostly overlaps are one
    role (Matt Billings: "VP and President of Robinhood Financial LLC and Robinhood
    Securities, LLC" and "Vice President, Brokerage and President, Robinhood Financial and
    Robinhood Securities"). The newest wording is kept (with its function); sources merged.
    Returns the rows and one record per merge, for VALIDATION.md."""
    rows = list(roles)
    merges: list[dict[str, Any]] = []
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(rows):
            for b in rows[i + 1 :]:
                # Same seniority and the same currency: a former role is history, not a
                # duplicate of the current one.
                if (
                    a.seniority != b.seniority
                    or a.seniority == "founder"
                    or a.is_current != b.is_current
                ):
                    continue
                score = title_overlap(a.title, b.title)
                if score < OVERLAP_MIN:
                    continue
                claims = list({id(c): c for c in a.claims + b.claims}.values())
                newest = _title_claim(claims)
                keep = a if any(c is newest for c in a.claims) else b
                # The merged row has the sources of both: a "single source" note (18-month or
                # re-anchored) no longer applies, and confidence is recomputed from them.
                supporting = [c for c in claims if c.status != "former"] or claims
                multi = len({c.doc.url for c in supporting}) > 1
                notes = [
                    n for n in (a.note, b.note)
                    if n and not (multi and "single source" in n)
                ]  # fmt: skip
                notes.append(
                    f"merged from 2 rows with overlapping wording (D30): {a.title} / {b.title}"
                )
                confidence = max(
                    (a.confidence, b.confidence, role_confidence(supporting)),
                    key=_CONFIDENCE_RANK.__getitem__,
                )
                merged = ResolvedRole(
                    newest.title,
                    keep.function,
                    keep.seniority,
                    a.is_current,
                    max((r.as_of for r in (a, b) if r.as_of), default=None),
                    confidence,
                    claims,
                    "; ".join(dict.fromkeys(notes)),
                )
                merges.append(
                    {"kept": newest.title, "merged": sorted({a.title, b.title} - {newest.title}),
                     "overlap": round(score, 2), "seniority": keep.seniority}
                )  # fmt: skip
                rows = [r for r in rows if r is not a and r is not b] + [merged]
                changed = True
                break
            if changed:
                break
    return rows, merges


def retire_stale(roles: list[ResolvedRole], today: date) -> None:
    """D32, from Daniel's hand-check (all 8 such Robinhood records had departed): a current
    role whose only source is older than 18 months is NOT current unless a newer source
    confirms it. Runs after rows are merged, so a merged row's pooled sources count as
    confirmation. Founder roles are exempt (D23 decides their currency); undated sources
    can't be shown to be old and are left alone."""
    for r in roles:
        if not r.is_current or r.seniority == "founder" or not r.as_of:
            continue
        supporting = [c for c in r.claims if c.status != "former"] or r.claims
        if len({c.doc.url for c in supporting}) != 1:
            continue
        if (today - date.fromisoformat(r.as_of)).days <= STALE_AFTER_DAYS:
            continue
        r.is_current = False
        r.confidence = "low"
        stale = (
            f"single source, last confirmed {r.as_of} in {_cite(supporting[0])}; no newer "
            f"confirmation (older than 18 months)"
        )
        r.note = f"{stale}; {r.note}" if r.note else stale


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
    elif "source is undated" in r.note or ("current roster" in r.note and "proxy" in r.note):
        review["c_level_not_in_proxy_undated"].append(entry)
    elif "proxy" in r.note:
        review["superseded_by_proxy"].append(entry)
    elif "superseded by a later title" in r.note:
        review["superseded_by_promotion"].append(entry)
    elif "18 months" in r.note:
        review["stale_single_source"].append(entry)


_SEAT_ACRONYMS = {
    "cfo": "chief financial officer",
    "cto": "chief technology officer",
    "coo": "chief operating officer",
    "cmo": "chief marketing officer",
    "cpo": "chief product officer",
    "ciso": "chief information security officer",
    "cro": "chief revenue officer",
    "clo": "chief legal officer",
    "chro": "chief human resources officer",
    "cio": "chief information officer",
    "cdo": "chief data officer",
}


def c_seat(title: str) -> str | None:
    """The C-suite seat a title names: 'Co-founder / CTO' and 'Chief Technology Officer' are
    one seat; CFO and Chief Accounting Officer are two (same finance function)."""
    t = " ".join(re.sub(r"[^a-z ]", " ", title.lower()).split())
    m = re.search(r"\bchief (?!executive)(?:[a-z]+ ){1,4}?officer\b", t)
    if m:
        return m.group(0)
    for word in t.split():
        if word in _SEAT_ACRONYMS:
            return _SEAT_ACRONYMS[word]
    return None


def _seat_conflicts(people: list[ResolvedPerson], review: dict[str, list[dict[str, Any]]]):
    """Two people both current in the same C-level seat (e.g. two CTOs). Listed, not decided:
    the older one may have left, or titles may overlap. CEO/President/'other' are skipped."""
    seats: dict[str, list[tuple[ResolvedPerson, ResolvedRole]]] = defaultdict(list)
    for p in people:
        for r in p.roles:
            seat = c_seat(r.title) if r.is_current and r.seniority == "c_level" else None
            if seat:
                seats[seat].append((p, r))
    for seat, holders in sorted(seats.items()):
        if len({p.full_name for p, _ in holders}) > 1:
            review["seat_conflicts"].append(
                {
                    "seat": seat,
                    "holders": [
                        {"person": p.full_name, "title": r.title, "as_of": r.as_of}
                        for p, r in sorted(holders, key=lambda h: h[1].as_of or "", reverse=True)
                    ],
                }
            )


def apply_identity_merges(
    clusters: list[list[Claim]],
    merges: list[dict[str, Any]],
    review: dict[str, list[dict[str, Any]]],
) -> list[list[Claim]]:
    """D33: hand-curated "same person" decisions the narrow rule (D6) can't make, e.g. a
    nickname with no overlapping function. Each merge names both people by name; the merged
    person keeps every claim (and so every fact and source)."""
    for m in merges:
        key_from, key_into = normalize_name(m["merge"]), normalize_name(m["into"])
        src = next((c for c in clusters if any(normalize_name(x.full_name) == key_from
                                               for x in c)), None)  # fmt: skip
        dst = next((c for c in clusters if any(normalize_name(x.full_name) == key_into
                                               for x in c)), None)  # fmt: skip
        if src is not None and src is dst:
            continue  # already the same person
        if src is None or dst is None:
            review["identity_merges_not_applied"].append(
                {"names": [m["merge"], m["into"]], "note": "a name matches no stored person"}
            )
            continue
        dst.extend(src)
        dst.sort(key=_claim_sort_key)
        clusters = [c for c in clusters if c is not src]
        review["merged_by_decision"].append(
            {"names": sorted([m["merge"], m["into"]]), "note": m.get("evidence", ""),
             "decided_by": m.get("decided_by", "")}
        )  # fmt: skip
    return clusters


def resolve_claims(
    claims: list[Claim],
    today: date | None = None,
    company_id: int = 0,
    identity_merges: list[dict[str, Any]] | None = None,
) -> Resolution:
    today = today or date.today()
    proxy_claims = [c for c in claims if _is_proxy(c) and c.status != "former"]
    proxy_date = max((c.published_at for c in proxy_claims if c.published_at), default=None)

    review: dict[str, list[dict[str, Any]]] = defaultdict(list)
    people: list[ResolvedPerson] = []
    clusters = apply_identity_merges(
        cluster_claims(claims, company_id), identity_merges or [], review
    )
    into_names = {normalize_name(m["into"]) for m in identity_merges or []}
    merged_keys = [
        {normalize_name(m["merge"]), normalize_name(m["into"])} for m in identity_merges or []
    ]
    for cluster in clusters:
        names = sorted({c.full_name for c in cluster})
        # Names joined only by a hand-curated merge (D33) are not a D6 nickname merge:
        # collapse each such pair to one name before counting distinct names.
        keys = {normalize_name(n) for n in names}
        for pair in merged_keys:
            if pair <= keys:
                keys = (keys - pair) | {min(pair)}
        if len(keys) > 1:
            review["merged_by_nickname_rule"].append({"names": names})
        in_proxy = any(c in proxy_claims for c in cluster)
        proxy_keys = {k for c in cluster if c in proxy_claims for k in _role_keys(c, True)}

        by_key: dict[tuple[str, str], list[Claim]] = defaultdict(list)
        for c in cluster:
            for key in _role_keys(c, in_proxy):
                by_key[key].append(c)
        roles = [_resolve_role(k, cs, proxy_date, proxy_keys, today) for k, cs in by_key.items()]
        _apply_promotions(roles)
        roles = dedupe_roles(roles)
        roles, role_merges = merge_overlapping_roles(roles)
        retire_stale(roles, today)
        # D4 applies to current roles only: a past General Counsel / Chief Legal Officer role
        # (e.g. before the company had a proxy) stays historical C-level.
        for r in roles:
            if (
                not r.is_current
                and (r.seniority, r.function) == ("head", "legal")
                and all(c.title_class.legal_chief for c in r.claims)
                and not any(_DEPUTY.search(c.title) for c in r.claims)
            ):
                r.seniority = "c_level"
                r.note = (r.note + "; " if r.note else "") + (
                    "historical C-level (the General Counsel rule applies to current roles only)"
                )
        # D23: a founder is current leadership only while holding a current operating role.
        historical: list[ResolvedRole] = []
        if not any(r.is_current for r in roles if r.seniority != "founder"):
            for r in roles:
                if r.seniority == "founder" and r.is_current:
                    r.is_current = False
                    r.note = "founder (historical): no current operating role in the collected data"
                    historical.append(r)
        roles.sort(key=lambda r: (not r.is_current, _SENIORITY_RANK[r.seniority], r.title))
        live = [r for r in roles if r.is_current] or roles
        confidence = max((r.confidence for r in live), key=_CONFIDENCE_RANK.__getitem__)
        live_claims = [c for r in live for c in r.claims if c.status != "former"] or cluster
        # A hand-curated merge (D33) names the person by its "into" name.
        preferred = [c for c in live_claims if normalize_name(c.full_name) in into_names]
        best = min(preferred or live_claims, key=_claim_sort_key)
        person = ResolvedPerson(
            display_name(best.full_name), cluster, roles, confidence, best.doc.url, name_claim=best
        )

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

        for m in role_merges:
            review["merged_role_rows"].append({"person": person.full_name, **m})
        for r in historical:
            review["founder_historical"].append(
                {"person": person.full_name, "title": r.title,
                 "urls": sorted({c.doc.url for c in r.claims})}
            )  # fmt: skip
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
