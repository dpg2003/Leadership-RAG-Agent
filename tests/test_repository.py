import sqlite3

import pytest

from leadership.models import normalize_name
from leadership.storage.db import connect
from leadership.storage.repository import ProvenanceError, Repository, RoleIn

DOMAIN = "example.com"


@pytest.fixture
def repo(tmp_path):
    conn = connect(tmp_path / "test.db")
    yield Repository(conn)
    conn.close()


def _person(repo, company_id, source_id, name, title, function, seniority, location=None):
    pid = repo.upsert_person(company_id, name, source_id, f"{name}, {title}")
    repo.add_fact(pid, "title", title, source_id, f"{name}, {title}")
    if location:
        repo.add_fact(pid, "location", location, source_id, f"based in {location}")
    repo.update_person_resolved(pid, full_name=name, confidence="high", location=location)
    repo.replace_roles(pid, [RoleIn(title, function, seniority)])
    return pid


@pytest.fixture
def seeded(repo):
    cid = repo.upsert_company("Example", DOMAIN, is_public=True, ticker="EXMP")
    site = repo.add_source(cid, "https://example.com/team", "company_site", "2026-01-01", "a")
    news = repo.add_source(cid, "https://news.test/a", "search", "2026-01-02", "b")
    ceo = _person(repo, cid, site, "Ada Lovelace", "CEO", "other", "c_level", "London")
    _person(repo, cid, site, "Grace Hopper", "VP of Engineering", "engineering", "vp")
    _person(repo, cid, news, "Alan Turing", "VP, Marketing", "marketing", "vp")
    _person(repo, cid, news, "Mary Ann Smith", "Head of Design", "design", "head")
    _person(repo, cid, news, "John Smith", "Head of Sales", "sales", "head")
    # Second source for the CEO, so source_count is 2.
    repo.add_fact(ceo, "title", "CEO", news, "CEO Ada Lovelace said")
    return repo, cid


def test_list_leaders_filters_and_orders(seeded):
    repo, _ = seeded
    names = [p["full_name"] for p in repo.list_leaders(DOMAIN)]
    assert names[0] == "Ada Lovelace"  # c_level first
    assert set(names[1:3]) == {"Alan Turing", "Grace Hopper"}  # then VPs
    vps = repo.list_leaders(DOMAIN, seniority="vp")
    assert {p["full_name"] for p in vps} == {"Alan Turing", "Grace Hopper"}
    eng = repo.list_leaders(DOMAIN, function="engineering", seniority="vp")
    assert [p["full_name"] for p in eng] == ["Grace Hopper"]
    assert repo.list_leaders(DOMAIN, function="legal") == []
    assert repo.list_leaders("unknown.com") == []


def test_count_leaders_counts_people_not_roles(seeded):
    repo, cid = seeded
    assert repo.count_leaders(DOMAIN) == 5
    assert repo.count_leaders(DOMAIN, seniority="vp") == 2
    # A person with two current roles still counts once.
    pid = repo.get_person(DOMAIN, "Ada Lovelace")[0]["id"]
    site = repo.add_source(cid, "https://example.com/team", "company_site", "2026-01-01", "a")
    repo.add_fact(pid, "title", "Co-founder", site, "Co-founder Ada")
    repo.replace_roles(
        pid, [RoleIn("CEO", "other", "c_level"), RoleIn("Co-founder", "other", "founder")]
    )
    assert repo.count_leaders(DOMAIN) == 5


def test_source_count_is_computed(seeded):
    repo, _ = seeded
    by_name = {p["full_name"]: p for p in repo.list_leaders(DOMAIN)}
    assert by_name["Ada Lovelace"]["source_count"] == 2
    assert by_name["Grace Hopper"]["source_count"] == 1


def test_get_person_returns_facts_with_sources(seeded):
    repo, _ = seeded
    [ada] = repo.get_person(DOMAIN, "ada lovelace")
    assert ada["location"] == "London"
    assert ada["source_count"] == 2
    assert {f["source"]["source_type"] for f in ada["facts"]} == {"company_site", "search"}
    assert all(f["evidence_snippet"] for f in ada["facts"])


def test_get_person_partial_and_ambiguous(seeded):
    repo, _ = seeded
    assert [p["full_name"] for p in repo.get_person(DOMAIN, "Lovelace")] == ["Ada Lovelace"]
    smiths = {p["full_name"] for p in repo.get_person(DOMAIN, "Smith")}
    assert smiths == {"John Smith", "Mary Ann Smith"}
    assert repo.get_person(DOMAIN, "Nobody") == []
    assert repo.get_person(DOMAIN, "  ") == []


def test_invalid_filter_rejected(seeded):
    repo, _ = seeded
    with pytest.raises(ValueError, match="function"):
        repo.list_leaders(DOMAIN, function="wizardry")
    with pytest.raises(ValueError, match="seniority"):
        repo.count_leaders(DOMAIN, seniority="intern")


def _new_person(repo, cid):
    src = repo.add_source(cid, "https://x.test", "search", "2026-01-01", "c")
    return repo.upsert_person(cid, "New Person", src, "New Person joined"), src


def test_person_requires_evidence(seeded):
    repo, cid = seeded
    src = repo.add_source(cid, "https://x.test", "search", "2026-01-01", "c")
    with pytest.raises(ProvenanceError):
        repo.upsert_person(cid, "Ghost Person", src, "  ")
    assert repo.get_person(DOMAIN, "Ghost Person") == []
    pid = repo.upsert_person(cid, "Real Person", src, "Real Person, CTO")
    [person] = repo.get_person(DOMAIN, "Real Person")
    assert person["id"] == pid
    assert [f["field"] for f in person["facts"]] == ["full_name"]


def test_source_must_belong_to_same_company(seeded):
    repo, cid = seeded
    other = repo.upsert_company("Other", "other.com")
    foreign = repo.add_source(other, "https://other.com/team", "company_site", "2026-01-01", "d")
    with pytest.raises(ProvenanceError):
        repo.upsert_person(cid, "Cross Person", foreign, "Cross Person, CEO")
    pid, _ = _new_person(repo, cid)
    with pytest.raises(ProvenanceError):
        repo.add_fact(pid, "title", "CEO", foreign, "CEO")


def test_identical_fact_stored_once(seeded):
    repo, cid = seeded
    pid, src = _new_person(repo, cid)
    first = repo.add_fact(pid, "title", "CTO", src, "New Person, CTO")
    assert repo.add_fact(pid, "title", " CTO ", src, "New Person, CTO") == first


def test_fact_requires_evidence(seeded):
    repo, cid = seeded
    pid, src = _new_person(repo, cid)
    with pytest.raises(ProvenanceError):
        repo.add_fact(pid, "title", "CTO", src, "   ")
    with pytest.raises(ProvenanceError):
        repo.add_fact(pid, "title", "", src, "snippet")
    with pytest.raises(ValueError, match="field"):
        repo.add_fact(pid, "salary", "1", src, "snippet")


def test_resolved_fields_require_backing_fact(seeded):
    repo, cid = seeded
    pid, src = _new_person(repo, cid)
    with pytest.raises(ProvenanceError):
        repo.update_person_resolved(pid, full_name="New Person", confidence="low", location="Paris")
    with pytest.raises(ProvenanceError):  # resolved name must match a full_name fact
        repo.update_person_resolved(pid, full_name="Someone Else", confidence="low")
    with pytest.raises(ProvenanceError):  # profile_url must be a fetched source
        repo.update_person_resolved(
            pid, full_name="New Person", confidence="low", profile_url="https://made.up/x"
        )
    with pytest.raises(ProvenanceError):
        repo.replace_roles(pid, [RoleIn("CTO", "engineering", "c_level")])
    # With backing facts (and stray whitespace) the same writes succeed.
    repo.add_fact(pid, "location", "Paris", src, "based in Paris")
    repo.add_fact(pid, "title", "CTO", src, "New Person, CTO")
    repo.update_person_resolved(
        pid,
        full_name="New Person ",
        confidence="low",
        location=" Paris",
        profile_url="https://x.test",
    )
    repo.replace_roles(pid, [RoleIn(" CTO", "engineering", "c_level")])
    [person] = repo.get_person(DOMAIN, "New Person")
    assert person["location"] == "Paris"
    assert person["roles"][0]["title_raw"] == "CTO"


def test_unique_person_per_normalized_name(seeded):
    repo, cid = seeded
    [grace] = repo.get_person(DOMAIN, "Grace Hopper")
    site = repo.add_source(cid, "https://example.com/team", "company_site", "2026-01-01", "a")
    assert repo.upsert_person(cid, "grace  HOPPER", site, "grace HOPPER") == grace["id"]
    assert (
        repo.upsert_person(cid, "Dr. Grace M. Hopper", site, "Dr. Grace M. Hopper") == grace["id"]
    )


def test_transaction_persists_and_rolls_back(tmp_path):
    path = tmp_path / "tx.db"
    repo = Repository(connect(path))
    with repo.transaction():
        repo.upsert_company("Kept", "kept.com")
    with pytest.raises(RuntimeError), repo.transaction():
        repo.upsert_company("Dropped", "dropped.com")
        raise RuntimeError("boom")
    repo.conn.close()
    reopened = Repository(connect(path))
    assert reopened.get_company("kept.com") is not None
    assert reopened.get_company("dropped.com") is None
    reopened.conn.close()


def test_schema_check_constraints(repo):
    cid = repo.upsert_company("X", "x.com")
    with pytest.raises(sqlite3.IntegrityError):
        repo.conn.execute(
            "INSERT INTO sources (company_id, url, source_type, fetched_at, raw_path)"
            " VALUES (?, 'u', 'linkedin', 't', 'p')",
            (cid,),
        )
    with pytest.raises(sqlite3.IntegrityError):  # foreign key enforced
        repo.conn.execute(
            "INSERT INTO roles (person_id, title_raw, function, seniority)"
            " VALUES (999, 'a', 'other', 'vp')"
        )


def test_runs_round_trip(seeded):
    repo, cid = seeded
    run = repo.start_run(cid)
    repo.finish_run(run, {"people_found": 5, "cost_usd": 0.12})
    latest = repo.latest_run(DOMAIN)
    assert latest["stats"] == {"cost_usd": 0.12, "people_found": 5}
    assert latest["finished_at"]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Jason S. Warnick", "jason warnick"),
        ("José Álvarez", "jose alvarez"),
        ("Conor O’Brien", "Conor O'Brien"),
        ("Mary-Kate Lee Jr.", "mary kate lee"),
    ],
)
def test_normalize_name_equivalences(a, b):
    assert normalize_name(a) == normalize_name(b)


def test_normalize_name_keeps_distinct_people_apart():
    assert normalize_name("Vlad Tenev") != normalize_name("Vladimir Tenev")
    assert normalize_name("Mary Ann Smith") != normalize_name("Mary Smith")
