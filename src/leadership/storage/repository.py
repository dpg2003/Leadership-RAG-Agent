"""All database reads and writes. Every query is parameterized.

Provenance invariants enforced here, not just in the pipeline:
- a fact needs a source (of the same company) and a non-blank evidence snippet;
- a person is created together with their `full_name` fact;
- a person's resolved full_name/location/bio and each role title need a matching fact;
- profile_url must be a fetched source URL of the same company.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from leadership.models import (
    CONFIDENCE_LEVELS,
    FACT_FIELDS,
    FUNCTIONS,
    SENIORITIES,
    SOURCE_TYPES,
    display_name,
    normalize_name,
    utc_now_iso,
)


class ProvenanceError(ValueError):
    """A write would store a claim without supporting evidence."""


@dataclass(frozen=True)
class RoleIn:
    title_raw: str
    function: str
    seniority: str
    is_current: bool = True
    as_of: str | None = None
    note: str | None = None


# Module constant interpolated into SQL (never user or LLM input); all values are bound params.
_SENIORITY_ORDER = (
    "CASE r.seniority WHEN 'c_level' THEN 0 WHEN 'founder' THEN 1 WHEN 'vp' THEN 2 ELSE 3 END"
)

# Single definition of a person's source count, shared by list_leaders and get_person.
_SOURCE_COUNT_SQL = "(SELECT COUNT(DISTINCT f.source_id) FROM facts f WHERE f.person_id = p.id)"


def _check_choice(name: str, value: str | None, allowed: tuple[str, ...]) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"Invalid {name} {value!r}; expected one of {', '.join(allowed)}")


def _lastrowid(cur: sqlite3.Cursor) -> int:
    if cur.lastrowid is None:
        raise RuntimeError("INSERT did not return a row id")
    return cur.lastrowid


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------------------------------------------------------------- companies / sources

    def upsert_company(
        self, name: str, domain: str, is_public: bool = False, ticker: str | None = None
    ) -> int:
        self.conn.execute(
            """INSERT INTO companies (name, domain, is_public, ticker) VALUES (?, ?, ?, ?)
               ON CONFLICT (domain) DO UPDATE SET
                 name = excluded.name, is_public = excluded.is_public, ticker = excluded.ticker""",
            (name, domain, int(is_public), ticker),
        )
        row = self.conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
        return row[0]

    def get_company(self, domain: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM companies WHERE domain = ?", (domain,)).fetchone()
        return dict(row) if row else None

    def add_source(
        self,
        company_id: int,
        url: str,
        source_type: str,
        fetched_at: str,
        raw_path: str,
        published_at: str | None = None,
    ) -> int:
        _check_choice("source_type", source_type, SOURCE_TYPES)
        self.conn.execute(
            """INSERT INTO sources
                 (company_id, url, source_type, fetched_at, raw_path, published_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (company_id, url) DO UPDATE SET
                 source_type = excluded.source_type,
                 fetched_at = excluded.fetched_at,
                 raw_path = excluded.raw_path,
                 published_at = excluded.published_at""",
            (company_id, url, source_type, fetched_at, raw_path, published_at),
        )
        return self.conn.execute(
            "SELECT id FROM sources WHERE company_id = ? AND url = ?", (company_id, url)
        ).fetchone()[0]

    # ---------------------------------------------------------------- people / facts / roles

    def upsert_person(
        self,
        company_id: int,
        full_name: str,
        source_id: int,
        evidence_snippet: str,
        confidence: str = "low",
        extracted_at: str | None = None,
    ) -> int:
        """Get or create a person by normalized name, recording the name claim as a fact.

        A person can't exist without evidence: the `full_name` fact is written in the same
        call. Does not overwrite resolved fields of an existing person.
        """
        _check_choice("confidence", confidence, CONFIDENCE_LEVELS)
        full_name = full_name.strip()
        key = normalize_name(full_name)
        if not key:
            raise ValueError(f"Name {full_name!r} normalizes to an empty string")
        if not evidence_snippet or not evidence_snippet.strip():
            raise ProvenanceError(f"Person {full_name!r} has no evidence snippet")
        self._check_source_company(source_id, company_id)
        self.conn.execute(
            """INSERT INTO people (company_id, full_name, normalized_name, confidence, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (company_id, normalized_name) DO NOTHING""",
            (company_id, full_name, key, confidence, extracted_at or utc_now_iso()),
        )
        person_id = self.conn.execute(
            "SELECT id FROM people WHERE company_id = ? AND normalized_name = ?", (company_id, key)
        ).fetchone()[0]
        self.add_fact(person_id, "full_name", full_name, source_id, evidence_snippet, extracted_at)
        return person_id

    def _check_source_company(self, source_id: int, company_id: int) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM sources WHERE id = ? AND company_id = ?", (source_id, company_id)
        ).fetchone()
        if row is None:
            raise ProvenanceError(f"Source {source_id} does not belong to company {company_id}")

    def add_fact(
        self,
        person_id: int,
        field: str,
        value: str,
        source_id: int,
        evidence_snippet: str,
        extracted_at: str | None = None,
        is_current: bool | None = None,
    ) -> int:
        """Record a claim. An identical (person, field, value, source) claim is stored once.

        `is_current` is for title facts: whether that source presented the title as current.
        """
        _check_choice("field", field, FACT_FIELDS)
        if not value or not value.strip():
            raise ProvenanceError(f"Fact {field!r} has an empty value")
        if not evidence_snippet or not evidence_snippet.strip():
            raise ProvenanceError(f"Fact {field}={value!r} has no evidence snippet")
        value = value.strip()
        company = self.conn.execute(
            "SELECT company_id FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        if company is None:
            raise ValueError(f"Unknown person {person_id}")
        self._check_source_company(source_id, company[0])
        existing = self.conn.execute(
            """SELECT id FROM facts
               WHERE person_id = ? AND field = ? AND value = ? AND source_id = ?""",
            (person_id, field, value, source_id),
        ).fetchone()
        if existing is not None:
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO facts (person_id, field, value, source_id, evidence_snippet,
                                  extracted_at, is_current)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                person_id,
                field,
                value,
                source_id,
                evidence_snippet,
                extracted_at or utc_now_iso(),
                None if is_current is None else int(is_current),
            ),
        )
        return _lastrowid(cur)

    def _require_fact(self, person_id: int, field: str, value: str) -> None:
        """The resolved value must come from a stored fact. A displayed name may drop a
        credential suffix ("Stephanie Guild" from the fact "Stephanie Guild, CFA")."""
        values = [
            r[0]
            for r in self.conn.execute(
                "SELECT value FROM facts WHERE person_id = ? AND field = ?", (person_id, field)
            )
        ]
        wanted = value.strip()
        ok = wanted in values or (
            field == "full_name" and any(display_name(v) == wanted for v in values)
        )
        if not ok:
            raise ProvenanceError(f"No {field!r} fact with value {value!r} for person {person_id}")

    def update_person_resolved(
        self,
        person_id: int,
        *,
        full_name: str,
        confidence: str,
        location: str | None = None,
        bio: str | None = None,
        profile_url: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        _check_choice("confidence", confidence, CONFIDENCE_LEVELS)
        full_name = full_name.strip()
        location = location.strip() if location is not None else None
        bio = bio.strip() if bio is not None else None
        self._require_fact(person_id, "full_name", full_name)
        if location is not None:
            self._require_fact(person_id, "location", location)
        if bio is not None:
            self._require_fact(person_id, "bio", bio)
        if profile_url is not None:
            row = self.conn.execute(
                """SELECT 1 FROM sources s JOIN people p ON p.company_id = s.company_id
                   WHERE p.id = ? AND s.url = ?""",
                (person_id, profile_url),
            ).fetchone()
            if row is None:
                raise ProvenanceError(f"profile_url {profile_url!r} is not a fetched source")
        self.conn.execute(
            """UPDATE people SET full_name = ?, location = ?, bio = ?, profile_url = ?,
                 confidence = ?, updated_at = ? WHERE id = ?""",
            (
                full_name,
                location,
                bio,
                profile_url,
                confidence,
                updated_at or utc_now_iso(),
                person_id,
            ),  # fmt: skip
        )

    def replace_roles(self, person_id: int, roles: list[RoleIn]) -> None:
        roles = [
            RoleIn(r.title_raw.strip(), r.function, r.seniority, r.is_current, r.as_of, r.note)
            for r in roles
        ]
        for role in roles:
            _check_choice("function", role.function, FUNCTIONS)
            _check_choice("seniority", role.seniority, SENIORITIES)
            self._require_fact(person_id, "title", role.title_raw)
        self.conn.execute("DELETE FROM roles WHERE person_id = ?", (person_id,))
        self.conn.executemany(
            """INSERT INTO roles
                 (person_id, title_raw, function, seniority, is_current, as_of, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    person_id,
                    r.title_raw,
                    r.function,
                    r.seniority,
                    int(r.is_current),
                    r.as_of,
                    r.note,
                )
                for r in roles
            ],
        )

    def clear_people(self, company_id: int) -> None:
        """Drop people, roles and facts before a full re-resolve.

        Facts (and, in collect.py, sources) are rebuilt every run from the cached raw
        documents and LLM extractions, which are the durable record. See D3, D19.
        """
        self.conn.execute("DELETE FROM people WHERE company_id = ?", (company_id,))

    # ---------------------------------------------------------------- transactions

    @contextmanager
    def transaction(self) -> Generator[None]:
        """Commit on success, roll back on any exception (which is re-raised)."""
        with self.conn:
            yield

    def commit(self) -> None:
        self.conn.commit()

    # ---------------------------------------------------------------- collection runs

    def start_run(self, company_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO collection_runs (company_id, started_at) VALUES (?, ?)",
            (company_id, utc_now_iso()),
        )
        return _lastrowid(cur)

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE collection_runs SET finished_at = ?, stats_json = ? WHERE id = ?",
            (utc_now_iso(), json.dumps(stats, ensure_ascii=False, sort_keys=True), run_id),
        )

    def latest_run(self, domain: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT cr.* FROM collection_runs cr JOIN companies c ON c.id = cr.company_id
               WHERE c.domain = ? AND cr.finished_at IS NOT NULL
               ORDER BY cr.finished_at DESC, cr.id DESC LIMIT 1""",
            (domain,),
        ).fetchone()
        if row is None:
            return None
        run = dict(row)
        run["stats"] = json.loads(run.pop("stats_json") or "{}")
        return run

    # ---------------------------------------------------------------- reads for chat / UI

    def _roles_for(self, person_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"""SELECT title_raw, function, seniority, is_current, as_of, note FROM roles r
                WHERE r.person_id = ? ORDER BY {_SENIORITY_ORDER}, r.title_raw""",
            (person_id,),
        ).fetchall()
        return [dict(r) | {"is_current": bool(r["is_current"])} for r in rows]

    def list_leaders(
        self, domain: str, function: str | None = None, seniority: str | None = None
    ) -> list[dict[str, Any]]:
        """Current leaders with at least one current role matching the filters."""
        _check_choice("function", function, FUNCTIONS)
        _check_choice("seniority", seniority, SENIORITIES)
        rows = self.conn.execute(
            f"""SELECT p.id, p.full_name, p.location, p.confidence, p.profile_url,
                   {_SOURCE_COUNT_SQL}
                     AS source_count,
                   (SELECT MIN({_SENIORITY_ORDER}) FROM roles r
                     WHERE r.person_id = p.id AND r.is_current = 1) AS rank
                FROM people p JOIN companies c ON c.id = p.company_id
                WHERE c.domain = ?
                  AND EXISTS (SELECT 1 FROM roles r WHERE r.person_id = p.id AND r.is_current = 1
                              AND (? IS NULL OR r.function = ?)
                              AND (? IS NULL OR r.seniority = ?))
                ORDER BY rank, p.full_name""",
            (domain, function, function, seniority, seniority),
        ).fetchall()
        leaders = []
        for row in rows:
            person = dict(row)
            person.pop("rank")
            person["roles"] = [r for r in self._roles_for(person["id"]) if r["is_current"]]
            leaders.append(person)
        return leaders

    def count_leaders(
        self, domain: str, function: str | None = None, seniority: str | None = None
    ) -> int:
        """Distinct people (not roles) with a current role matching the filters."""
        _check_choice("function", function, FUNCTIONS)
        _check_choice("seniority", seniority, SENIORITIES)
        return self.conn.execute(
            """SELECT COUNT(DISTINCT p.id) FROM people p
               JOIN companies c ON c.id = p.company_id
               JOIN roles r ON r.person_id = p.id
               WHERE c.domain = ? AND r.is_current = 1
                 AND (? IS NULL OR r.function = ?)
                 AND (? IS NULL OR r.seniority = ?)""",
            (domain, function, function, seniority, seniority),
        ).fetchone()[0]

    def get_person(self, domain: str, name: str) -> list[dict[str, Any]]:
        """People whose normalized name contains every token of `name`, with facts and sources.

        An exact normalized match wins outright; otherwise all token matches are returned so
        the caller can surface ambiguity instead of picking one.
        """
        query = normalize_name(name)
        if not query:
            return []
        candidates = self.conn.execute(
            f"""SELECT p.*, {_SOURCE_COUNT_SQL} AS source_count
               FROM people p JOIN companies c ON c.id = p.company_id
               WHERE c.domain = ?
                 AND EXISTS (SELECT 1 FROM facts f WHERE f.person_id = p.id)
               ORDER BY p.full_name""",
            (domain,),
        ).fetchall()
        exact = [p for p in candidates if p["normalized_name"] == query]
        tokens = query.split()
        matches = exact or [
            p for p in candidates if all(t in p["normalized_name"].split() for t in tokens)
        ]
        return [self._person_detail(dict(p)) for p in matches]

    def all_people(self, domain: str) -> list[dict[str, Any]]:
        """Every stored person (current or not) with roles and facts, for export/validation."""
        rows = self.conn.execute(
            f"""SELECT p.*, {_SOURCE_COUNT_SQL} AS source_count
               FROM people p JOIN companies c ON c.id = p.company_id
               WHERE c.domain = ? ORDER BY p.full_name""",
            (domain,),
        ).fetchall()
        return [self._person_detail(dict(r)) for r in rows]

    def _person_detail(self, person: dict[str, Any]) -> dict[str, Any]:
        facts = self.conn.execute(
            """SELECT f.field, f.value, f.evidence_snippet, f.extracted_at, f.is_current,
                      s.id AS source_id, s.url, s.source_type, s.fetched_at, s.published_at
               FROM facts f JOIN sources s ON s.id = f.source_id
               WHERE f.person_id = ?
               ORDER BY f.field, s.source_type, s.url, f.value, f.id""",
            (person["id"],),
        ).fetchall()
        person.pop("normalized_name", None)
        person["roles"] = self._roles_for(person["id"])
        person["facts"] = [
            {
                "field": f["field"],
                "value": f["value"],
                "evidence_snippet": f["evidence_snippet"],
                "extracted_at": f["extracted_at"],
                "is_current": None if f["is_current"] is None else bool(f["is_current"]),
                "source": {
                    "url": f["url"],
                    "source_type": f["source_type"],
                    "retrieved_at": f["fetched_at"],
                    "published_at": f["published_at"],
                },
            }
            for f in facts
        ]
        return person
