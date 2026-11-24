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
