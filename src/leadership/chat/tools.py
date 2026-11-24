"""Typed, read-only chat tools over the stored data (no free-form SQL).

Each tool opens the company's SQLite file read-only, runs the parameterized queries in
storage/repository.py, and returns a JSON-ready dict. Every fact in a result carries its
source URL, date and evidence, so the agent can cite it. Results are compact on purpose:
title facts are grouped by wording, and long lists of sources are capped.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any

from leadership.config import DATA_DIR, db_path
from leadership.models import FUNCTIONS, SENIORITIES
from leadership.pipeline.collect import load_review_flags
from leadership.pipeline.titles import seat_functions
from leadership.storage.repository import Repository

MAX_SOURCES_PER_WORDING = 3
MAX_WORDINGS = 6
SNIPPET_CHARS = 240

SOURCE_TYPE_NAMES = {
    "company_site": "the company's own website (team/about pages, newsroom, blog)",
    "sec_filing": "SEC EDGAR filings (10-K, DEF 14A proxy, 8-K Item 5.02)",
    "search": "web search results (Tavily), from news and other public pages",
}


class ToolError(ValueError):
    """A bad tool argument (unknown company, invalid filter). Returned to the model."""


def available_companies() -> list[dict[str, Any]]:
    """Companies with a collected database under data/, as [{domain, name, is_public}]."""
    companies = []
    for path in sorted(DATA_DIR.glob("*/leadership.db")):
        with closing(_connect_ro(path.parent.name)) as conn:
            row = conn.execute("SELECT name, domain, is_public, ticker FROM companies").fetchone()
        if row:
            companies.append(dict(row) | {"is_public": bool(row["is_public"])})
    return companies


def _connect_ro(domain: str) -> sqlite3.Connection:
    path = db_path(domain)
    if not path.exists():
        raise ToolError(f"no collected data for {domain!r}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _domain(company: str) -> str:
    known = {c["domain"]: c for c in available_companies()}
    key = company.strip().lower().removeprefix("https://").removeprefix("http://")
    key = key.removeprefix("www.").split("/")[0]
    if key in known:
        return key
    by_name = [d for d, c in known.items() if c["name"].lower() == company.strip().lower()]
    if len(by_name) == 1:
        return by_name[0]
    raise ToolError(
        f"no collected data for {company!r}. Collected companies: "
        + ", ".join(f"{c['name']} ({d})" for d, c in known.items())
    )


def _check(name: str, value: str | None, allowed: tuple[str, ...]) -> None:
    if value is not None and value not in allowed:
        raise ToolError(f"{name} must be one of {', '.join(allowed)}; got {value!r}")


# ------------------------------------------------------------------ shared pieces


def _sources_checked(repo: Repository, domain: str) -> dict[str, Any]:
    run = repo.latest_run(domain) or {}
    by_source = run.get("stats", {}).get("discovery", {}).get("documents_by_source", {})
    return {
        "collected_at": run.get("finished_at"),
        "documents_by_source": by_source,
        "source_types": {k: SOURCE_TYPE_NAMES[k] for k in by_source if k in SOURCE_TYPE_NAMES},
    }


def _flags(domain: str, name: str) -> list[dict[str, str]]:
    return [
        {k: f[k] for k in ("flag", "detail", "source_url", "source_date", "decided_by") if k in f}
        for f in load_review_flags(domain)
        if f["person"] == name
    ]


def _cite(fact: dict[str, Any]) -> dict[str, Any]:
    s = fact["source"]
    return {"url": s["url"], "source_type": s["source_type"], "published": s["published_at"]}


def _grouped_titles(
    person: dict[str, Any], limit: int | None = MAX_WORDINGS
) -> list[dict[str, Any]]:
    """Title facts grouped by wording, newest first, with up to 3 cited sources each."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in person["facts"]:
        if f["field"] == "title":
            groups.setdefault(f["value"], []).append(f)
    out = []
    for wording, facts in groups.items():
        facts.sort(key=lambda f: f["source"]["published_at"] or "", reverse=True)
        out.append(
            {
                "wording": wording,
                "newest_source_date": facts[0]["source"]["published_at"],
                "source_count": len({f["source"]["url"] for f in facts}),
                "any_source_says_current": any(f["is_current"] for f in facts),
                "sources": [
                    _cite(f) | {"evidence": f["evidence_snippet"][:SNIPPET_CHARS]}
                    for f in facts[:MAX_SOURCES_PER_WORDING]
                ],
            }
        )
    out.sort(key=lambda g: (g["newest_source_date"] or "", g["source_count"]), reverse=True)
    return out[:limit]


def _field_facts(person: dict[str, Any], field: str) -> list[dict[str, Any]]:
    return [
        {"value": f["value"]} | _cite(f) | {"evidence": f["evidence_snippet"][:SNIPPET_CHARS]}
        for f in person["facts"]
        if f["field"] == field
    ]


def _bare(url: str) -> str:
    return url.split("://", 1)[-1].removeprefix("www.").rstrip("/")


def _cited_in(url: str, note: str) -> bool:
    """The note cites this URL as a whole token ("site robinhood.com/x (2021-10-01)"), not
    as a prefix of a longer one."""
    return bool(re.search(rf"(?<![\w./-]){re.escape(_bare(url))}/?(?=[\s);,]|$)", note))


def _role_sources(person: dict[str, Any], r: dict[str, Any]) -> list[dict[str, Any]]:
    """The stored sources behind one role, with their exact URLs: facts with this exact
    wording, the source(s) the note cites, and the source dated `last_confirmed`. The note's
    URLs are shortened, so the model must cite these instead (it would rebuild them wrongly)."""
    titles = [f for f in person["facts"] if f["field"] == "title"]
    note = r["note"] or ""
    wording = [f for f in titles if f["value"].casefold() == r["title_raw"].casefold()]
    wording.sort(key=lambda f: f["source"]["published_at"] or "", reverse=True)
    cited = [f for f in titles if _cited_in(f["source"]["url"], note)]
    dated = [f for f in titles if r["as_of"] and f["source"]["published_at"] == r["as_of"]]
    out: dict[str, dict[str, Any]] = {}
    for f in wording[:MAX_SOURCES_PER_WORDING] + cited + dated[:1]:
        out.setdefault(
            f["source"]["url"],
            _cite(f) | {"wording": f["value"], "evidence": f["evidence_snippet"][:SNIPPET_CHARS]},
        )
    return list(out.values())


def _role(person: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": r["title_raw"],
        "function": r["function"],
        "seniority": r["seniority"],
        "is_current": r["is_current"],
        # Newest dated evidence for the role; that source may use another wording.
        "last_confirmed": r["as_of"],
        "note": r["note"],
        "sources": _role_sources(person, r),
    }


def _low_reason(person: dict[str, Any]) -> str:
    notes = [r["note"] for r in person["roles"] if r["is_current"] and r["note"]]
    if notes:
        return "; ".join(notes)
    if person["source_count"] == 1:
        undated = all(not f["source"]["published_at"] for f in person["facts"])
        return "single source" + (" (undated)" if undated else "")
    return "low confidence (no corroborating dated source)"


def _summary(domain: str, person: dict[str, Any], current_only: bool) -> dict[str, Any]:
    roles = [r for r in person["roles"] if r["is_current"] or not current_only]
    out = {
        "name": person["full_name"],
        "confidence": person["confidence"],
        "location": person["location"],
        "roles": [_role(person, r) for r in roles],
        "title_sources": _grouped_titles(person),
        "flags": _flags(domain, person["full_name"]),
    }
    if person["confidence"] == "low":
        out["low_confidence_reason"] = _low_reason(person)
    return out


def _matches(r: dict[str, Any], function: str | None, seniority: str | None) -> bool:
    return (function is None or r["function"] == function) and (
        seniority is None or r["seniority"] == seniority
    )


def _also_titled(
    domain: str,
    people: list[dict[str, Any]],
    listed: set[str],
    function: str | None,
    seniority: str | None,
) -> list[dict[str, Any]]:
    """Current people outside the result whose source wording names a C-suite seat in the
    requested function ("CEO & CFO" for finance). Their role row is filed under another seat
    (D36), so the agent reports the wording with its sources instead of missing it."""
    if function is None or seniority not in (None, "c_level"):
        return []
    out = []
    for p in people:
        current = [r for r in p["roles"] if r["is_current"]]
        if p["full_name"] in listed or not current:
            continue
        # A seat already resolved for this person (current or past) is not a hidden one.
        if any(r["seniority"] == "c_level" and r["function"] == function for r in p["roles"]):
            continue
        hits = [
            g for g in _grouped_titles(p, limit=None)
            if g["any_source_says_current"] and function in seat_functions(g["wording"])
        ]  # fmt: skip
        if hits:
            out.append(
                {
                    "name": p["full_name"],
                    "resolved_current_roles": [_role(p, r) for r in current],
                    "wordings_naming_this_seat": hits,
                    "flags": _flags(domain, p["full_name"]),
                }
            )
    return out


# ------------------------------------------------------------------ the tools


def list_leaders(
    company: str,
    function: str | None = None,
    seniority: str | None = None,
    include_former: bool = False,
) -> dict[str, Any]:
    domain = _domain(company)
    _check("function", function, FUNCTIONS)
    _check("seniority", seniority, SENIORITIES)
    with closing(_connect_ro(domain)) as conn:
        repo = Repository(conn)
        current_ids = [p["id"] for p in repo.list_leaders(domain, function, seniority)]
        people = repo.all_people(domain)
        checked = _sources_checked(repo, domain)
    by_id = {p["id"]: p for p in people}
    current = [_summary(domain, by_id[i], current_only=True) for i in current_ids]
    for s in current:  # keep only the roles that match the filters, first
        s["roles"].sort(key=lambda r: not _matches(r, function, seniority))
    result: dict[str, Any] = {
        "company": domain,
        "filters": {"function": function, "seniority": seniority},
        "current_count": len(current),
        "current": current,
    }
    if include_former:
        result["not_current"] = [
            {
                "name": p["full_name"],
                "status": (
                    "STILL CURRENT in another role (see current_roles)"
                    if any(r["is_current"] for r in p["roles"])
                    else "NOT CURRENT (no current role in the data)"
                ),
                "past_roles": [
                    _role(p, r) for r in p["roles"]
                    if not r["is_current"] and _matches(r, function, seniority)
                ],
                "current_roles": [_role(p, r) for r in p["roles"] if r["is_current"]],
                "flags": _flags(domain, p["full_name"]),
            }
            for p in people
            if any(not r["is_current"] and _matches(r, function, seniority) for r in p["roles"])
        ]  # fmt: skip
    listed = {s["name"] for s in current}
    also = _also_titled(domain, people, listed, function, seniority)
    if also:
        result["also_titled"] = also
    result["sources_checked"] = checked
    return result


def count_leaders(
    company: str, function: str | None = None, seniority: str | None = None
) -> dict[str, Any]:
    domain = _domain(company)
    _check("function", function, FUNCTIONS)
    _check("seniority", seniority, SENIORITIES)
    with closing(_connect_ro(domain)) as conn:
        repo = Repository(conn)
        total = repo.count_leaders(domain, function, seniority)
        ids = [p["id"] for p in repo.list_leaders(domain, function, seniority)]
        people = {p["id"]: p for p in repo.all_people(domain)}
        checked = _sources_checked(repo, domain)
    if total != len(ids):
        raise RuntimeError(f"count_leaders ({total}) and list_leaders ({len(ids)}) disagree")
    rows = []
    for i in ids:
        p = people[i]
        role = next(r for r in p["roles"] if r["is_current"] and _matches(r, function, seniority))
        srcs = _role_sources(p, role)
        row = {
            "name": p["full_name"],
            "title": role["title_raw"],
            "confidence": p["confidence"],
            "source_url": srcs[0]["url"] if srcs else p["profile_url"],
        }
        if p["confidence"] == "low":
            row["low_confidence_reason"] = _low_reason(p)
        if flags := _flags(domain, p["full_name"]):
            row["flags"] = flags
        rows.append(row)
    by_conf = {c: sum(r["confidence"] == c for r in rows) for c in ("high", "medium", "low")}
    return {
        "company": domain,
        "filters": {"function": function, "seniority": seniority},
        "counts_people_not_roles": True,
        "current_total": total,
        "confirmed_high_or_medium": by_conf["high"] + by_conf["medium"],
        "low_confidence": by_conf["low"],
        "by_confidence": by_conf,
        "people": rows,
        "sources_checked": checked,
    }


def get_person(company: str, name: str) -> dict[str, Any]:
    domain = _domain(company)
    if not name.strip():
        raise ToolError("name is required")
    with closing(_connect_ro(domain)) as conn:
        repo = Repository(conn)
        matches = repo.get_person(domain, name)
        checked = _sources_checked(repo, domain)
    if not matches:
        return {"company": domain, "query": name, "found": False, "sources_checked": checked}
    people = []
    for p in matches:
        s = _summary(domain, p, current_only=False)
        current = any(r["is_current"] for r in p["roles"])
        s["status"] = "current" if current else "NOT CURRENT (no current role in the data)"
        s["location_facts"] = _field_facts(p, "location")
        s["bio_facts"] = _field_facts(p, "bio")
        s["profile_url"] = p["profile_url"]
        s["source_count"] = p["source_count"]
        people.append(s)
    return {
        "company": domain,
        "query": name,
        "found": True,
        "ambiguous": len(people) > 1,
        "people": people,
        "sources_checked": checked,
    }


# ------------------------------------------------------------------ schemas + dispatch

_COMPANY = {
    "type": "string",
    "description": "Company domain from the collected list, e.g. robinhood.com",
}
_FUNCTION = {
    "type": "string",
    "enum": list(FUNCTIONS),
    "description": "Function filter. The CEO, presidents and general managers are 'other'.",
}
_SENIORITY = {"type": "string", "enum": list(SENIORITIES), "description": "Seniority filter."}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_leaders",
        "description": (
            "List a company's CURRENT leaders (C-level, founders, VPs, Heads), optionally "
            "filtered by function and/or seniority, with roles, confidence, location, review "
            "flags, and cited title sources. Set include_former=true to also get people whose "
            "matching role is NOT current, with the reason and last-confirmed date. "
            "'also_titled' lists current people whose source wording names a C-suite seat in "
            "this function although their role is filed under another seat (e.g. 'CEO & CFO')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company": _COMPANY,
                "function": _FUNCTION,
                "seniority": _SENIORITY,
                "include_former": {"type": "boolean", "default": False},
            },
            "required": ["company"],
            "additionalProperties": False,
        },
    },
    {
        "name": "count_leaders",
        "description": (
            "Count a company's CURRENT leaders (distinct people, not roles) matching optional "
            "function/seniority filters. Returns the total, the confirmed (high or medium "
            "confidence) and low-confidence split with reasons, and each person's title."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"company": _COMPANY, "function": _FUNCTION, "seniority": _SENIORITY},
            "required": ["company"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_person",
        "description": (
            "Look up one person by name (full or partial) at a company: every role (current or "
            "not, with reasons and dates), location and bio with evidence, review flags, and "
            "sources. Returns found=false if no stored person matches, or several people if "
            "the name is ambiguous."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company": _COMPANY,
                "name": {"type": "string", "description": "Person's name, e.g. 'Vlad Tenev'"},
            },
            "required": ["company", "name"],
            "additionalProperties": False,
        },
    },
]

TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_leaders": list_leaders,
    "count_leaders": count_leaders,
    "get_person": get_person,
}


def run_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run a tool by name. Returns (JSON text, is_error)."""
    fn = TOOLS.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool {name!r}"}), True
    try:
        return json.dumps(fn(**args), ensure_ascii=False), False
    except (ToolError, TypeError) as exc:
        return json.dumps({"error": str(exc)}), True
