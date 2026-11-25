"""Offline checks of the eval set and its grader (no API calls)."""

from types import SimpleNamespace

from leadership.chat.tools import available_companies
from leadership.evaluate import Result, grade, load_cases, select, write_results

CASES = load_cases()


def test_eval_set_is_well_formed_and_covers_the_spec():
    domains = {c["domain"] for c in available_companies()}
    assert all(c["company"] in domains | {None} for c in CASES)
    for d in domains:
        mine = [c for c in CASES if c["company"] == d]
        assert 8 <= len(mine) <= 10, d
        kinds = [c["kind"] for c in mine]
        assert kinds.count("not_found") >= 2 and "follow_up" in kinds
    assert sum(c["kind"] == "not_found" for c in CASES) >= 4
    assert any(c["kind"] in ("conflict", "low_confidence") for c in CASES)
    assert {"no-company", "uncollected-company"} <= {c["id"] for c in CASES}
    examples = {c["turns"][-1] for c in CASES if c["kind"] == "example"}
    assert {"Who's their CTO?", "Who heads marketing?", "Where is their CEO based?"} <= examples
    assert any("How many VPs" in c["turns"][0] for c in CASES)


def test_select_runs_a_company_plus_the_no_company_cases():
    ids = {c["id"] for c in select(CASES, "meetcampfire.com", None)}
    assert "cf-cfo" in ids and "no-company" in ids and "rh-cto" not in ids


def _reply(text, steps=(), sources=()):
    return SimpleNamespace(text=text, steps=list(steps), sources=list(sources))


def _step(tool, **args):
    return SimpleNamespace(tool=tool, input=args)


def test_grader_include_exclude_tools_and_citations():
    case = {"checks": {"include": ["Paul Nichols"], "exclude": ["San Martin is the CTO"],
                       "tools": [{"tool": "list_leaders", "args": {"function": "engineering"}}],
                       "cites": True}}  # fmt: skip
    good = _reply("**Paul Nichols** is CTO.", [_step("list_leaders", company="x",
                  function="engineering")], ["https://a"])  # fmt: skip
    assert grade(case, good) == []
    bad = _reply("San Martin is the CTO.", [_step("get_person", name="x")])
    failures = grade(case, bad)
    assert len(failures) == 4  # missing name, forbidden claim, wrong tool, no citation


def test_results_are_written_with_lf_line_endings(tmp_path):
    r = Result(id="a", company=None, kind="company", passed=True, failures=[], expected="x",
               verified_by=None, answer="line 1\nline 2")  # fmt: skip
    write_results([r], "m", out_dir=tmp_path)
    for name in ("results.json", "RESULTS.md"):
        data = (tmp_path / name).read_bytes()
        assert b"\n" in data and b"\r\n" not in data, name


def test_grader_no_tools():
    case = {"checks": {"include": [r"\?"], "no_tools": True}}
    assert grade(case, _reply("Which company?")) == []
    assert grade(case, _reply("Which company?", [_step("list_leaders")]))
