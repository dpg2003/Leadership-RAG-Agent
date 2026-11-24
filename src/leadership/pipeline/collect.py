"""Full collection run: discover -> pre-filter -> extract (LLM) -> validate -> resolve -> store.

Writes data/<domain>/leadership.db (rebuilt each run, D3) and data/<domain>/leadership.json,
a readable export of the same data. Both are committed; the raw cache is not (D14).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from leadership.config import company_dir, db_path, llm_model
from leadership.pipeline.discover import discover, fetcher_for, normalize_domain, resolve_company
from leadership.pipeline.extract import PROMPT_VERSION, Extractor
from leadership.pipeline.prefilter import select_text
from leadership.pipeline.resolve import Resolution, ResolvedPerson, resolve_claims
from leadership.pipeline.validate import Claim, Rejection, validate
from leadership.sources.base import Company
from leadership.sources.company_site import MAX_PAGES
from leadership.storage.db import connect
from leadership.storage.repository import Repository, RoleIn

log = logging.getLogger(__name__)


@dataclass
class CollectResult:
    company: Company
    resolution: Resolution
    stats: dict[str, Any]
    db_path: Path
    json_path: Path


def collect(
    url: str,
    *,
    name: str | None = None,
    ticker: str | None = None,
    private: bool = False,
    max_pages: int = MAX_PAGES,
    refresh: bool = False,
    reextract: bool = False,
    today: date | None = None,
) -> CollectResult:
    started = time.monotonic()
    domain = normalize_domain(url)
    with fetcher_for(domain, refresh) as fetcher:
        company = resolve_company(fetcher, url, name, ticker, private)
        found = discover(company, fetcher, max_pages=max_pages)

    jobs, prefilter_skipped = [], []
    for doc in found.documents:
        text, note = select_text(doc)
        if text:
            jobs.append((doc, text))
        else:
            prefilter_skipped.append({"url": doc.url, "reason": note})
    log.info("%d documents, %d sent to the LLM", len(found.documents), len(jobs))

    model = llm_model()
    extractor = Extractor(company, model, company_dir(domain) / "raw" / "_extractions", reextract)
    results, xstats = extractor.extract_all(jobs)

    claims: list[Claim] = []
    rejections: list[Rejection] = []
    for r in results:
        if r.extraction is not None:
            c, rj = validate(r.doc, r.extraction, company.name)
            claims += c
            rejections += rj
    resolution = resolve_claims(claims, today=today)

    stats: dict[str, Any] = {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "discovery": found.stats,
        "documents": len(found.documents),
        "documents_sent_to_llm": len(jobs),
        "prefilter_skipped": len(prefilter_skipped),
        "llm_calls": xstats.llm_calls,
        "llm_cached_responses": xstats.cached,
        "tokens_in": xstats.input_tokens,
        "tokens_out": xstats.output_tokens,
        "cost_usd": xstats.cost_usd(model),
        "cost_usd_including_cached": xstats.cost_usd(model, include_cached=True),
        "extraction_errors": xstats.errors,
        "claims_accepted": len(claims),
        "claims_rejected": len(rejections),
        "rejections": [vars(r) for r in rejections],
        "people_found": len(resolution.people),
        "people_current": sum(p.is_current for p in resolution.people),
        "review": resolution.review,
        "duration_s": 0.0,
    }
    path = db_path(domain)
    _store(path, company, resolution, stats, started)
    json_path = export_json(path, domain)
    return CollectResult(company, resolution, stats, path, json_path)


def _store(
    path: Path, company: Company, resolution: Resolution, stats: dict, started: float
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    repo = Repository(conn)
    try:
        with repo.transaction():
            company_id = repo.upsert_company(
                company.name, company.domain, company.is_public, company.ticker
            )
            run_id = repo.start_run(company_id)
            repo.clear_people(company_id)
            conn.execute("DELETE FROM sources WHERE company_id = ?", (company_id,))
            for person in resolution.people:
                _store_person(repo, company_id, person)
            stats["duration_s"] = round(time.monotonic() - started, 1)
            repo.finish_run(run_id, stats)
    finally:
        conn.close()


def _source_id(repo: Repository, company_id: int, claim: Claim, cache: dict[str, int]) -> int:
    doc = claim.doc
    if doc.url not in cache:
        cache[doc.url] = repo.add_source(
            company_id, doc.url, doc.source_type, doc.fetched_at, doc.raw_path, claim.published_at
        )
    return cache[doc.url]


def _store_person(repo: Repository, company_id: int, person: ResolvedPerson) -> None:
    sources: dict[str, int] = {}
    best = next(c for c in person.claims if c.full_name == person.full_name)
    person_id = repo.upsert_person(
        company_id,
        person.full_name,
        _source_id(repo, company_id, best, sources),
        best.evidence_snippet,
    )
    for c in person.claims:
        sid = _source_id(repo, company_id, c, sources)
        repo.add_fact(person_id, "full_name", c.full_name, sid, c.evidence_snippet)
        repo.add_fact(
            person_id, "title", c.title, sid, c.evidence_snippet, is_current=c.status != "former"
        )
    if person.location_claim is not None and person.location_claim.location_snippet:
        lc = person.location_claim
        repo.add_fact(
            person_id,
            "location",
            person.location or "",
            _source_id(repo, company_id, lc, sources),
            lc.location_snippet or "",
        )
    if person.bio_claim is not None and person.bio:
        bc = person.bio_claim
        sid = _source_id(repo, company_id, bc, sources)
        repo.add_fact(person_id, "bio", person.bio, sid, person.bio)
    repo.replace_roles(
        person_id,
        [
            RoleIn(r.title, r.function, r.seniority, r.is_current, r.as_of, r.note or None)
            for r in person.roles
        ],
    )
    repo.update_person_resolved(
        person_id,
        full_name=person.full_name,
        confidence=person.confidence,
        location=person.location,
        bio=person.bio,
        profile_url=person.profile_url,
    )


def export_json(path: Path, domain: str) -> Path:
    """Readable, diff-friendly export of the stored data (committed next to the DB)."""
    conn = connect(path)
    try:
        repo = Repository(conn)
        company = repo.get_company(domain) or {}
        people = repo.all_people(domain)
        run = repo.latest_run(domain)
    finally:
        conn.close()
    out = {"company": company, "latest_run": run, "people": people}
    json_path = path.with_name("leadership.json")
    json_path.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return json_path
