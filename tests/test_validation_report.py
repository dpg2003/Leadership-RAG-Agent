"""Offline tests for the VALIDATION.md generator (no network, no LLM)."""

from leadership.validation_report import _agent_verdict, parse_verifier


def test_parse_verifier_reads_the_records_table(tmp_path):
    report = tmp_path / "x-verifier.md"
    report.write_text(
        "# Source verification\n\n## Records\n"
        "| # | Person | Field | Stored value | Verdict | Reason (1 line) | Source |\n"
        "|---|---|---|---|---|---|---|\n"
        "| 1 | Jane Doe | title | CFO | SUPPORTED | ok | u |\n"
        "| 2 | Jane Doe | location | SF | **PARTIAL** | interviewer's line | u |\n"
        "\n## Suspected duplicates\n| a | b |\n|---|---|\n| x | y |\n",
        encoding="utf-8",
    )
    rows = parse_verifier(report)
    assert [r[1] for r in rows["Jane Doe"]] == ["SUPPORTED", "PARTIAL"]
    verdict = _agent_verdict(rows["Jane Doe"])
    assert verdict.startswith("1 PARTIAL, 1 SUPPORTED") and "interviewer" in verdict
    assert _agent_verdict(None) == "not checked"
    assert parse_verifier(tmp_path / "missing.md") == {}


def test_daniels_ticks_survive_regeneration():
    from leadership.validation_report import DANIEL, carry_over_ticks

    def doc(cell_a, cell_b):
        return (
            "## Acme (acme.test)\n\n### Current leadership (2)\n\n"
            "| # | Name | Role | Verified by Daniel |\n|---|---|---|---|\n"
            f"| 1 | Jane Doe | CFO | {cell_a} |\n| 2 | Bob Ray | CTO | {cell_b} |\n"
        )

    old = doc("✅", "❌ left in 2025")
    new = doc(DANIEL, DANIEL).replace("(2)", "(3)")  # counts may change between runs
    merged = carry_over_ticks(old, new)
    assert "| 1 | Jane Doe | CFO | ✅ |" in merged
    assert "| 2 | Bob Ray | CTO | ❌ left in 2025 |" in merged


def test_company_section_lists_every_person_and_includes_notes(tmp_path, monkeypatch):
    import json

    import leadership.validation_report as vr

    data = tmp_path / "data" / "acme.test"
    data.mkdir(parents=True)
    (tmp_path / "docs" / "validation").mkdir(parents=True)
    source = {"url": "https://acme.test/team", "published_at": None}
    fact = {"field": "title", "value": "CFO", "evidence_snippet": "Jane Doe, CFO",
            "is_current": True, "source": source}  # fmt: skip
    people = [
        {"full_name": "Jane Doe", "confidence": "medium", "source_count": 1, "location": None,
         "roles": [{"title_raw": "CFO", "seniority": "c_level", "function": "finance",
                    "is_current": True, "as_of": None, "note": None}], "facts": [fact]},
        {"full_name": "Old Timer", "confidence": "low", "source_count": 1, "location": None,
         "roles": [{"title_raw": "COO", "seniority": "c_level", "function": "operations",
                    "is_current": False, "as_of": "2019-01-01", "note": "former"}],
         "facts": [fact | {"value": "COO", "is_current": False}]},
    ]  # fmt: skip
    (data / "leadership.json").write_text(
        json.dumps({"company": {"name": "Acme"}, "collection": {}, "people": people}), "utf-8"
    )
    (tmp_path / "docs" / "validation" / "acme.test-notes.md").write_text(
        "### Known misses\n\nHAND WRITTEN", "utf-8"
    )
    monkeypatch.setattr(vr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(vr, "DOCS", tmp_path / "docs")
    text = vr.company_section("acme.test")
    assert "| 1 | Jane Doe |" in text and "| 1 | Old Timer |" in text
    assert "HAND WRITTEN" in text and "Possible duplicates (not merged)** (0)" in text
    assert text == vr.company_section("acme.test")  # deterministic


def test_tick_follows_a_person_who_moved_to_another_table():
    from leadership.validation_report import DANIEL, carry_over_ticks

    head = "## Acme (acme.test)\n\n"
    row = "| # | Name | Role | Verified by Daniel |\n|---|---|---|---|\n"
    old = head + "### Current leadership (1)\n\n" + row + "| 1 | Jane Doe | CFO | ❌ left |\n"
    new = (head + "### Current leadership (0)\n\n" + row
           + "\n### Not current (kept for history) (1)\n\n" + row
           + f"| 1 | Jane Doe | CFO | {DANIEL} |\n")  # fmt: skip
    assert "| 1 | Jane Doe | CFO | ❌ left |" in carry_over_ticks(old, new)


def test_pipe_in_a_note_is_kept_and_escaped():
    from leadership.validation_report import DANIEL, carry_over_ticks

    def doc(cell):
        return ("## Acme (acme.test)\n\n### Current leadership (1)\n\n"
                "| # | Name | Role | Verified by Daniel |\n|---|---|---|---|\n"
                f"| 1 | Jane Doe | CFO | {cell} |\n")  # fmt: skip

    merged = carry_over_ticks(doc("❌ CFO | now COO"), doc(DANIEL))
    assert "| 1 | Jane Doe | CFO | ❌ CFO \\| now COO |" in merged
    # Already-escaped notes round-trip unchanged.
    assert carry_over_ticks(merged, doc(DANIEL)) == merged


def test_verifier_summary_separates_currency_from_evidence():
    from leadership.validation_report import verifier_summary

    verdicts = {
        "A": [("title: CFO", "SUPPORTED", "ok")],
        "B": [("title: VP Eng", "UNSUPPORTED", "2020 source; nothing shows it is current")],
        "C": [("title: COO", "PARTIAL", "is_current=false only inferred from the proxy")],
        "D": [("title: GC", "PARTIAL", "seniority should be vp for an SVP")],
        "F": [
            (
                "title: VP",
                "PARTIAL",
                "title and company correct and current; function should be sales",
            )
        ],
        "E": [("title: CTO", "UNSUPPORTED", "snippet not in source")],
    }
    text = "\n".join(verifier_summary(verdicts))
    assert "**Evidence supported** (the quoted evidence is in the source and states this " \
        "person and title): 5 of 6" in text  # fmt: skip
    currency = next(line for line in text.splitlines() if "**currency disputed**" in line)
    assert currency.endswith(": 2")
    classified = next(line for line in text.splitlines() if "classification or" in line)
    assert classified.endswith(": 2")
    assert "snippet not in source" in text


def test_review_flag_without_a_source_is_an_error(tmp_path, monkeypatch):
    import json

    import pytest

    import leadership.pipeline.collect as collect

    monkeypatch.setattr(collect, "company_dir", lambda d: tmp_path)
    (tmp_path / "review_flags.json").write_text(
        json.dumps([{"person": "Jane Doe", "flag": "reported departing"}]), "utf-8"
    )
    with pytest.raises(ValueError, match="source_url"):
        collect.load_review_flags("x.test")


def test_review_flags_reach_the_export_and_the_report(tmp_path, monkeypatch):
    import json

    import leadership.pipeline.collect as collect
    import leadership.validation_report as vr
    from leadership.pipeline.extract import ExtractedPerson, Extraction
    from leadership.pipeline.resolve import resolve_claims
    from leadership.pipeline.validate import validate
    from leadership.sources.base import Company, RawDocument

    monkeypatch.setattr(collect, "company_dir", lambda d: tmp_path)
    (tmp_path / "review_flags.json").write_text(json.dumps([{
        "person": "Jane Doe", "flag": "reported departing (secondary source, unconfirmed)",
        "source_url": "https://news.test/x", "source_date": "2026-11-19", "decided_by": "Daniel",
    }]), "utf-8")  # fmt: skip
    d = RawDocument("https://acme.test/team", "company_site", "t", "Jane Doe, CFO",
                    "2026-11-24T00:00:00+00:00", "x.txt", {})  # fmt: skip
    p = ExtractedPerson(full_name="Jane Doe", title="CFO", employer="Acme",
                        works_at_target_company=True, status="current",
                        evidence_snippet="Jane Doe, CFO", location=None, location_snippet=None,
                        bio=None)  # fmt: skip
    claims = validate(d, Extraction(page_published_date=None, published_date_snippet=None,
                                    people=[p]))[0]  # fmt: skip
    db = tmp_path / "leadership.db"
    collect._store(db, Company("Acme", "acme.test", "https://acme.test/"),
                   resolve_claims(claims), {}, 0)  # fmt: skip
    out = json.loads(collect.export_json(db, "acme.test").read_text("utf-8"))
    assert out["people"][0]["review_flags"][0]["flag"].startswith("reported departing")

    data = tmp_path / "data" / "acme.test"
    data.mkdir(parents=True)
    (data / "leadership.json").write_text(json.dumps(out), "utf-8")
    monkeypatch.setattr(vr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(vr, "DOCS", tmp_path / "docs")
    assert "FLAG: reported departing (secondary source, unconfirmed)" in vr.company_section(
        "acme.test"
    )
