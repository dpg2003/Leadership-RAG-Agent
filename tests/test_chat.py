"""Chat tools over the committed data (offline, deterministic) and the agent loop's
bookkeeping. The loop test uses a scripted stand-in for the API client: test-only, so the
history/tool-result plumbing is checked without a network call. Real-model runs are in
Phase 5's transcripts and the eval (Phase 6)."""

import json
import sqlite3
from contextlib import closing
from datetime import date
from types import SimpleNamespace

import pytest

from leadership.chat import tools
from leadership.chat.agent import ChatAgent
from leadership.pipeline.titles import seat_functions

RH, CF = "robinhood.com", "meetcampfire.com"


def test_cto_seat_is_empty_and_pinner_is_former_with_reason():
    r = tools.list_leaders(RH, "engineering", "c_level", include_former=True)
    assert r["current"] == [] and r["current_count"] == 0
    pinner = next(p for p in r["not_current"] if p["name"] == "Jeff Pinner")
    assert pinner["status"].startswith("NOT CURRENT")
    (role,) = pinner["past_roles"]
    assert role["last_confirmed"] == "2026-05-08" and "8-K" in role["note"]
    assert "also_titled" not in r  # Kerbrat's old CTO wording is already a resolved past role


def test_former_people_are_left_out_unless_asked():
    assert "not_current" not in tools.list_leaders(RH, "engineering", "c_level")


def test_count_has_confidence_breakdown_that_adds_up():
    r = tools.count_leaders(RH)
    assert r["current_total"] == 22
    assert r["confirmed_high_or_medium"] + r["low_confidence"] == r["current_total"]
    assert sum(r["by_confidence"].values()) == r["current_total"]
    assert all("low_confidence_reason" in p for p in r["people"] if p["confidence"] == "low")
    assert tools.count_leaders(CF)["current_total"] == 8


def test_review_flags_surface_in_every_tool():
    flag = "departing Dec 31, 2026 to Klarna as CFO"
    vps = tools.count_leaders(RH, seniority="vp")
    koegel = next(p for p in vps["people"] if p["name"] == "Chris Koegel")
    assert flag in koegel["flags"][0]["flag"]
    (person,) = tools.get_person(RH, "Koegel")["people"]
    assert flag in person["flags"][0]["flag"] and person["status"] == "current"
    listed = tools.list_leaders(RH, "finance")["current"]
    assert flag in next(p for p in listed if p["name"] == "Chris Koegel")["flags"][0]["flag"]


def test_ceo_location_comes_with_evidence():
    (vlad,) = tools.get_person(RH, "Vlad Tenev")["people"]
    assert vlad["location"] == "Bay Area of California"
    (loc,) = vlad["location_facts"]
    assert "lives in the Bay Area" in loc["evidence"] and loc["url"].startswith("https://")


def test_combined_title_is_a_cfo_row_with_both_sources():
    """Campfire: 'CEO & CFO' is split into a CFO row (D37), backed by both wordings."""
    r = tools.list_leaders(CF, "finance", "c_level", include_former=True)
    (glasgow,) = r["current"]
    assert glasgow["name"] == "John Glasgow" and "also_titled" not in r
    cfo = glasgow["roles"][0]
    assert (cfo["title"], cfo["function"], cfo["is_current"]) == ("CEO & CFO", "finance", True)
    assert {s["wording"] for s in cfo["sources"]} == {"CEO & CFO", "CEO, CFO and founder"}
    assert tools.count_leaders(CF)["current_total"] == 8  # people, not roles


def test_founder_filter_includes_a_merged_ceo_and_founder_row():
    """Glasgow's "CEO & Founder" is one C-level row standing for a founder role too (D29)."""
    r = tools.list_leaders(CF, seniority="founder", include_former=True)
    assert [p["name"] for p in r["current"]] == ["John Glasgow"]
    assert r["current"][0]["roles"][0]["title"] == "CEO & Founder"
    assert [p["name"] for p in r["not_current"]] == ["Fernando San Martin"]
    c = tools.count_leaders(CF, seniority="founder")
    assert c["current_total"] == 1 and c["people"][0]["title"] == "CEO & Founder"
    assert tools.list_leaders(CF, seniority="c_level")["current_count"] >= 1
    assert tools.count_leaders(CF)["current_total"] == 8


_NOTES = [
    ("c_level", "one row for: c_level, founder", True),
    ("c_level", "dated 2026; one row for: c_level, founder", True),
    ("c_level", "one row for: c_level, founder; merged from 2 rows (D30): A / B", True),
    (
        "c_level",
        "one row for: c_level, vp; merged from 2 rows (D30): Co-Founder & CTO / CTO",
        False,
    ),
    ("c_level", "founder of a club", False),
    ("vp", "one row for: vp, head", False),
    ("c_level", "one row for: c_level, founders", False),
    ("c_level", None, False),
    ("founder", None, True),
]


@pytest.mark.parametrize(("seniority", "note", "is_founder"), _NOTES)
def test_founder_filter_sql_and_python_agree(seniority, note, is_founder):
    from leadership.storage.repository import _SENIORITY_MATCH_SQL, covers_seniority

    role = {"seniority": seniority, "note": note}
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE roles (seniority TEXT, note TEXT)")
        conn.execute("INSERT INTO roles VALUES (?, ?)", (seniority, note))
        for want in ("founder", "c_level", "vp", None):
            sql = conn.execute(
                f"SELECT COUNT(*) FROM roles r WHERE {_SENIORITY_MATCH_SQL}", (want,) * 3
            ).fetchone()[0]
            assert bool(sql) == covers_seniority(role, want), (note, want)
    assert covers_seniority(role, "founder") is is_founder


def test_roster_listing_is_dated_by_retrieval_and_medium_confidence():
    """D38: the search copy of investors.robinhood.com/governance/leadership."""
    rows = tools.count_leaders(RH, seniority="vp")
    assert rows["current_total"] == 15 and rows["low_confidence"] == 0
    (abhishek,) = tools.get_person(RH, "Abhishek Fatehpuria")["people"]
    assert abhishek["confidence"] == "medium"
    (role,) = abhishek["roles"]
    assert role["last_confirmed"] and role["sources"][0]["dated_by"].startswith("search date")


def test_seat_functions():
    assert seat_functions("CEO & CFO") == {"other", "finance"}
    assert seat_functions("Chief Legal, Compliance and Corporate Affairs Officer") == {"legal"}
    assert seat_functions("Chairman & Chief Executive Officer") == {"other"}
    assert seat_functions("VP of Sales") == set()


def test_not_found_and_bad_arguments():
    r = tools.get_person(RH, "Nobody Atall")
    assert r["found"] is False and r["sources_checked"]["documents_by_source"]["sec_filing"] == 4
    out, is_error = tools.run_tool("list_leaders", {"company": "apple.com"})
    assert is_error and "robinhood.com" in json.loads(out)["error"]
    out, is_error = tools.run_tool("count_leaders", {"company": RH, "function": "hacking"})
    assert is_error
    out, is_error = tools.run_tool("get_person", {"company": RH, "nam": "x"})
    assert is_error  # unexpected argument
    assert tools.run_tool("drop_table", {})[1]


def test_company_by_name_or_url():
    assert tools.list_leaders("Robinhood")["company"] == RH
    assert tools.list_leaders("https://www.robinhood.com/us/en/")["company"] == RH


def test_tools_open_the_database_read_only():
    with closing(tools._connect_ro(RH)) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM people")


# ------------------------------------------------------------------ agent loop


def _block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


class ScriptedClient:
    """Returns canned responses in order and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw["messages"], default=vars)))
        return self.responses.pop(0)


def _resp(stop, *content):
    return SimpleNamespace(
        stop_reason=stop, content=list(content),
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )  # fmt: skip


def test_agent_runs_tools_keeps_history_and_collects_cited_sources():
    url = "https://investors.robinhood.com/governance/board-of-directors"
    client = ScriptedClient([
        _resp("tool_use", _block("tool_use", id="t1", name="get_person",
                                 input={"company": RH, "name": "Vlad Tenev"})),
        _resp("end_turn", _block("text", text=f"Bay Area of California ([board]({url}))")),
        _resp("end_turn", _block("text", text="Not found in the collected data.")),
    ])  # fmt: skip
    agent = ChatAgent(RH, model="test", client=client, today=date(2026, 11, 23))
    reply = agent.ask("Where is their CEO based?")
    assert [s.tool for s in reply.steps] == ["get_person"] and not reply.steps[0].is_error
    assert reply.sources == [url] and reply.input_tokens == 20
    # the tool result went back to the model, paired with its tool_use id
    tool_result = client.requests[1][-1]["content"][0]
    assert tool_result["tool_use_id"] == "t1" and "Bay Area" in tool_result["content"]
    agent.ask("What's his salary?")
    # D39: q1's tool turns are trimmed to the answer: q1, answer, q2
    assert len(client.requests[2]) == 3
    kept = client.requests[2][1]
    assert kept["role"] == "assistant" and "get_person(" in kept["content"]
    assert "Bay Area of California" in kept["content"]


def test_untrimmed_history_keeps_tool_turns_and_cache_markers():
    client = ScriptedClient([
        _resp("end_turn", _block("text", text="a")), _resp("end_turn", _block("text", text="b")),
    ])  # fmt: skip
    agent = ChatAgent(RH, model="test", client=client, trim_history=False, prompt_cache=False)
    agent.ask("q1")
    agent.ask("q2")
    assert len(client.requests[1]) == 3  # nothing to trim without tools, still q1, a, q2


def test_prompt_cache_marks_system_and_conversation():
    seen = {}

    class Capture(ScriptedClient):
        def create(self, **kw):
            seen.update(kw)
            return self.responses.pop(0)

    agent = ChatAgent(
        RH, model="test", client=Capture([_resp("end_turn", _block("text", text="x"))])
    )
    agent.ask("hi")
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["cache_control"] == {"type": "ephemeral"}


def test_agent_rolls_back_history_on_error():
    class Failing(ScriptedClient):
        def create(self, **kw):
            raise RuntimeError("boom")

    agent = ChatAgent(RH, model="test", client=Failing([]))
    with pytest.raises(RuntimeError):
        agent.ask("hi")
    assert agent.history == []


def test_unknown_company_is_rejected():
    with pytest.raises(ValueError, match="no collected data"):
        ChatAgent("apple.com", model="test", client=object())


def test_person_current_in_another_seat_is_not_called_former():
    r = tools.list_leaders(RH, "engineering", "c_level", include_former=True)
    kerbrat = next(p for p in r["not_current"] if p["name"] == "Johann Kerbrat")
    assert kerbrat["status"].startswith("STILL CURRENT")
    assert kerbrat["current_roles"][0]["title"].startswith("Senior Vice President")
    pinner = next(p for p in r["not_current"] if p["name"] == "Jeff Pinner")
    assert pinner["status"].startswith("NOT CURRENT") and pinner["current_roles"] == []


def test_roles_carry_exact_stored_source_urls():
    """Notes shorten URLs; each role gives the stored URLs so none get rebuilt."""
    r = tools.list_leaders(RH, "engineering", "c_level", include_former=True)
    kerbrat = next(p for p in r["not_current"] if p["name"] == "Johann Kerbrat")
    (role,) = kerbrat["past_roles"]
    urls = {s["url"] for s in role["sources"]}
    url = "https://robinhood.com/newsroom/robinhood-and-circle-join-forces-to-bring-usdc-to-customers/"
    assert url in urls


def test_role_sources_include_the_exact_title_wording():
    (vlad,) = tools.get_person(RH, "Vlad Tenev")["people"]
    ceo = next(r for r in vlad["roles"] if r["title"] == "Chairman & Chief Executive Officer")
    wordings = {s["url"]: s["wording"] for s in ceo["sources"]}
    assert wordings.get("https://investors.robinhood.com/governance/leadership") == ceo["title"]


def test_count_rows_have_a_source_url():
    rows = tools.count_leaders(RH, seniority="vp")["people"]
    assert all(r["source_url"].startswith("https://") for r in rows)


def test_note_citation_matches_whole_urls_only():
    note = "last confirmed in company site robinhood.com/newsroom/a-post (2021-10-01)"
    assert tools._cited_in("https://robinhood.com/newsroom/a-post/", note)
    assert not tools._cited_in("https://robinhood.com/", note)
    assert not tools._cited_in("https://robinhood.com/newsroom/a", note)
