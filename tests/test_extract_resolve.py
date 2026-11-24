"""Offline tests for Phase 3: title rules, evidence checks, currency and confidence rules.

No LLM here: the model's structured output is constructed by hand (`Extraction`), which is
exactly the shape the real call returns. The runtime path always calls the real API.
"""

from __future__ import annotations

from datetime import date

import pytest

from leadership.pipeline.extract import ExtractedPerson, Extraction
from leadership.pipeline.prefilter import select_text
from leadership.pipeline.resolve import resolve_claims
from leadership.pipeline.titles import classify_title
from leadership.pipeline.validate import Claim, Rejection, validate
from leadership.sources.base import RawDocument

TODAY = date(2026, 11, 24)

PROXY_TEXT = """Executive Officers
The names of our current executive officers are set forth in the table below:
Name
Age
Position
Vladimir Tenev
39
Chair and Chief Executive Officer
Shiv Verma
41
Chief Financial Officer
Our named executive officers for 2025 were:
Vladimir Tenev, Chair and Chief Executive Officer;
Jason Warnick, former Chief Financial Officer.
As previously disclosed, Mr. Warnick ceased to serve as Chief Financial Officer effective
February 6, 2026."""

NEWS_TEXT = """Newsroom
March 1, 2021
Robinhood names Jason Warnick Chief Financial Officer.
Robinhood names Gretchen Howard Chief Operating Officer.
Robinhood names Vlad Tenev Chief Executive Officer."""


def doc(url: str, text: str, source_type: str = "company_site", **meta) -> RawDocument:
    return RawDocument(url, source_type, "t", text, "2026-11-24T00:00:00+00:00", "x.txt", meta)


def person(name, title, snippet, status="current", works=True, **kw) -> ExtractedPerson:
    return ExtractedPerson(
        full_name=name,
        title=title,
        employer="Robinhood",
        works_at_target_company=works,
        status=status,
        evidence_snippet=snippet,
        location=kw.get("location"),
        location_snippet=kw.get("location_snippet"),
        bio=kw.get("bio"),
    )


def proxy_claims() -> list[Claim]:
    d = doc(
        "https://sec.test/def14a",
        PROXY_TEXT,
        "sec_filing",
        form="DEF 14A",
        filing_date="2026-04-22",
    )
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[
            person("Vladimir Tenev", "Chair and Chief Executive Officer",
                   "Vladimir Tenev 39 Chair and Chief Executive Officer"),
            person("Shiv Verma", "Chief Financial Officer",
                   "Shiv Verma 41 Chief Financial Officer"),
            # The model is asked for status; even if it says 'current', the snippet says former.
            person("Jason Warnick", "Chief Financial Officer",
                   "Jason Warnick, former Chief Financial Officer.", status="current"),
        ],
    )  # fmt: skip
    claims, rejections = validate(d, ex)
    assert not rejections
    return claims


def news_claims() -> list[Claim]:
    d = doc("https://robinhood.test/news/2021", NEWS_TEXT)
    ex = Extraction(
        page_published_date="2021-03-01",
        published_date_snippet="March 1, 2021",
        people=[
            person("Jason Warnick", "Chief Financial Officer",
                   "Robinhood names Jason Warnick Chief Financial Officer."),
            person("Gretchen Howard", "Chief Operating Officer",
                   "Robinhood names Gretchen Howard Chief Operating Officer."),
            person("Vlad Tenev", "Chief Executive Officer",
                   "Robinhood names Vlad Tenev Chief Executive Officer."),
        ],
    )  # fmt: skip
    claims, rejections = validate(d, ex)
    assert not rejections
    return claims


def by_name(resolution, name):
    return next(p for p in resolution.people if p.full_name == name)


# --------------------------------------------------------------------------- title rules


@pytest.mark.parametrize(
    ("title", "seniority", "function"),
    [
        ("Chair and Chief Executive Officer", "c_level", "other"),
        ("Chief Legal, Compliance, and Corporate Affairs Officer", "c_level", "legal"),
        ("Chief Brokerage Officer", "c_level", "operations"),
        ("CFO", "c_level", "finance"),
        ("President", "c_level", "other"),
        ("General Counsel", "c_level", "legal"),
        ("SVP, Engineering", "vp", "engineering"),
        ("Vice President of Product", "vp", "product"),
        ("Head of Product", "head", "product"),
        ("Head of People", "head", "people"),
        ("Co-founder", "founder", "other"),
    ],
)
def test_classify_title(title, seniority, function):
    tc = classify_title(title)
    assert tc is not None and (tc.seniority, tc.function) == (seniority, function)


@pytest.mark.parametrize(
    "title", ["Independent Director", "Chairman", "Director of Engineering", "Software Engineer",
              "Chief of Staff", "Product Manager"],
)  # fmt: skip
def test_out_of_scope_titles(title):
    assert classify_title(title) is None


def test_founder_and_ceo_gets_both_roles():
    tc = classify_title("CEO & Founder")
    assert tc is not None and tc.seniority == "c_level" and tc.also_founder


# --------------------------------------------------------------------------- evidence checks


def test_snippet_must_be_literal_in_source():
    d = doc("https://x.test/a", "Jane Doe is our Chief Technology Officer.\nMore text.")
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Jane Doe", "Chief Technology Officer", "Jane Doe is our Chief Technology Officer."),
        person("Jane Doe", "VP of Engineering", "Jane Doe leads engineering as VP."),
        person("Jane Doe", "CTO", "Jane Doe is our Chief Technology Officer."),
        person("John Roe", "Chief Executive Officer", "John Roe, CEO of Ramp", works=False),
    ])  # fmt: skip
    claims, rejections = validate(d, ex)
    assert len(claims) == 1
    reasons = [r.reason for r in rejections]
    assert "evidence snippet not found in the source text" in reasons
    assert "title not found in the evidence snippet" in reasons
    assert any(r.startswith("not at the target company") for r in reasons)


def test_whitespace_and_curly_quotes_do_not_break_the_literal_check():
    d = doc("https://x.test/b", "Jane  Doe\nHead of Product at Acme’s HQ")
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Jane Doe", "Head of Product", "Jane Doe Head of Product at Acme's HQ"),
    ])  # fmt: skip
    claims, _ = validate(d, ex)
    assert len(claims) == 1


def test_unverified_location_and_date_are_dropped_not_invented():
    d = doc("https://x.test/c", "Jane Doe, VP of Sales, joined last week.")
    ex = Extraction(
        page_published_date="2025-01-01",
        published_date_snippet="January 1, 2025",
        people=[
            person(
                "Jane Doe",
                "VP of Sales",
                "Jane Doe, VP of Sales, joined",
                location="Denver",
                location_snippet="based in Denver",
            )
        ],
    )
    (claim,), _ = validate(d, ex)
    assert claim.location is None and claim.published_at is None


def test_single_first_name_is_rejected():
    d = doc("https://x.test/d", "Thanks to John, our Head of Sales.")
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[person("John", "Head of Sales", "John, our Head of Sales")],
    )
    claims, (rej,) = validate(d, ex)
    assert not claims and isinstance(rej, Rejection) and "full name" in rej.reason


# --------------------------------------------------------------------------- currency rules


def test_warnick_former_cfo_in_proxy_is_never_current():
    """The proxy says 'former Chief Financial Officer'; an old newsroom post says CFO."""
    res = resolve_claims(proxy_claims() + news_claims(), today=TODAY)
    warnick = by_name(res, "Jason Warnick")
    assert not warnick.is_current
    assert all(not r.is_current for r in warnick.roles)
    verma = by_name(res, "Shiv Verma")
    assert verma.is_current and verma.confidence == "high"


def test_warnick_from_proxy_alone_is_not_current():
    res = resolve_claims(proxy_claims(), today=TODAY)
    assert not by_name(res, "Jason Warnick").is_current


def test_older_newsroom_c_level_not_in_proxy_is_not_current():
    res = resolve_claims(proxy_claims() + news_claims(), today=TODAY)
    howard = by_name(res, "Gretchen Howard")
    assert not howard.is_current
    assert "proxy" in howard.roles[0].note
    assert any(e["person"] == "Gretchen Howard" for e in res.review["superseded_by_proxy"])


def test_undated_c_level_not_in_proxy_stays_current_and_is_listed():
    """The proxy lists Section 16 officers only; an undated IR page listing a Chief People
    Officer can't be shown to be older than the proxy."""
    d = doc("https://investors.robinhood.test/leadership", "Connie Schan\nChief People Officer",
            "search")  # fmt: skip
    ex = Extraction(page_published_date=None, published_date_snippet=None,
                    people=[person("Connie Schan", "Chief People Officer",
                                   "Connie Schan Chief People Officer")])  # fmt: skip
    claims, _ = validate(d, ex)
    res = resolve_claims(proxy_claims() + claims, today=TODAY)
    assert by_name(res, "Connie Schan").is_current
    assert res.review["c_level_not_in_proxy_undated"][0]["person"] == "Connie Schan"


def test_newsroom_c_level_newer_than_proxy_stays_current():
    d = doc(
        "https://robinhood.test/news/2026",
        "June 2, 2026\nRobinhood names Ann Lee Chief Marketing Officer.",
    )
    ex = Extraction(
        page_published_date="2026-06-02",
        published_date_snippet="June 2, 2026",
        people=[
            person(
                "Ann Lee",
                "Chief Marketing Officer",
                "Robinhood names Ann Lee Chief Marketing Officer.",
            )
        ],
    )
    claims, _ = validate(d, ex)
    res = resolve_claims(proxy_claims() + claims, today=TODAY)
    assert by_name(res, "Ann Lee").is_current


def test_vlad_and_vladimir_merge_and_stay_current():
    res = resolve_claims(proxy_claims() + news_claims(), today=TODAY)
    tenev = [p for p in res.people if p.full_name.endswith("Tenev")]
    assert len(tenev) == 1 and tenev[0].is_current
    assert {"names": ["Vlad Tenev", "Vladimir Tenev"]} in res.review["merged_by_nickname_rule"]


def test_uncorroborated_single_source_older_than_18_months_is_low_and_listed():
    d = doc("https://news.test/2023", "May 1, 2023\nAcme hires Bo Park as VP of Data.", "search")
    ex = Extraction(
        page_published_date="2023-05-01",
        published_date_snippet="May 1, 2023",
        people=[person("Bo Park", "VP of Data", "Acme hires Bo Park as VP of Data.")],
    )
    claims, _ = validate(d, ex)
    res = resolve_claims(claims, today=TODAY)
    park = by_name(res, "Bo Park")
    assert park.is_current and park.confidence == "low"
    assert res.review["stale_single_source"][0]["person"] == "Bo Park"


def test_company_site_role_without_date_is_medium():
    d = doc("https://acme.test/team", "Our team\nBo Park\nHead of Design")
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[person("Bo Park", "Head of Design", "Bo Park Head of Design")],
    )
    claims, _ = validate(d, ex)
    assert by_name(resolve_claims(claims, today=TODAY), "Bo Park").confidence == "medium"


def test_blocked_host_records_are_listed_for_review():
    d = doc("https://investors.acme.test/m/bo", "Bo Park, VP of Finance", "search",
            direct_fetch="blocked: investors.acme.test/robots.txt returned HTTP 403")  # fmt: skip
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[person("Bo Park", "VP of Finance", "Bo Park, VP of Finance")],
    )
    claims, _ = validate(d, ex)
    res = resolve_claims(claims, today=TODAY)
    assert res.review["from_blocked_host"] == [
        {"person": "Bo Park", "only_source": True, "urls": ["https://investors.acme.test/m/bo"]}
    ]


# --------------------------------------------------------------------------- pre-filter


def test_prefilter_keeps_role_lines_and_skips_pages_without_them():
    page = doc("https://x.test/p", "\n".join(["nav"] * 50 + ["Jane Doe", "Head of Product"]))
    text, _ = select_text(page)
    assert "Head of Product" in text and text.count("nav") <= 3
    assert select_text(doc("https://x.test/q", "pricing\nfeatures\ncontact us"))[0] == ""


def test_prefilter_uses_the_sec_executive_officers_span():
    text = "x" * 100 + PROXY_TEXT + "y" * 100
    d = doc(
        "https://sec.test/p",
        text,
        "sec_filing",
        executive_officers_span=[100, 100 + len(PROXY_TEXT)],
    )
    assert select_text(d)[0] == PROXY_TEXT


def test_evidence_is_reanchored_to_the_literal_heading_when_the_quote_rewords_the_title():
    text = (
        "Chris Koegel\nVice President, Corporate Finance & Investor Relations\n"
        "Chris Koegel is the Vice President of Corporate Finance and Investor Relations."
    )
    d = doc("https://investors.x.test/m/ck", text, "search")
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Chris Koegel", "Vice President, Corporate Finance & Investor Relations",
               "Chris Koegel is the Vice President of Corporate Finance and Investor Relations."),
    ])  # fmt: skip
    (claim,), rejections = validate(d, ex)
    assert not rejections and claim.reanchored
    assert claim.evidence_snippet == (
        "Chris Koegel Vice President, Corporate Finance & Investor Relations"
    )


def test_reanchor_handles_table_cells_and_refuses_distant_matches():
    text = (
        "| Date: | May 8, 2026 |\n| | | | Name: Shiv Verma |\n"
        "| | | | Title: Chief Financial Officer |"
    )
    d = doc("https://sec.test/8k", text, "search")
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Shiv Verma", "Chief Financial Officer",
               "Name: Shiv Verma | | | | Title: Chief Financial Officer"),
    ])  # fmt: skip
    (claim,), _ = validate(d, ex)
    assert claim.reanchored and "Shiv Verma" in claim.evidence_snippet

    far = doc("https://x.test/far", "Jane Doe wrote this.\n" + "filler " * 80 + "\nHead of Sales")
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[person("Jane Doe", "Head of Sales", "Jane Doe, Head of Sales")],
    )
    claims, (rej,) = validate(far, ex)
    assert not claims and rej.reason == "evidence snippet not found in the source text"


def test_promotion_supersedes_older_title_in_same_function():
    vp = doc("https://rh.test/2018", "November 29, 2018\nGretchen Howard is joining us as "
             "Vice President of Operations.")  # fmt: skip
    coo = doc("https://rh.test/2019", "June 24, 2019\nGretchen Howard has been elevated to "
              "Chief Operating Officer.")  # fmt: skip
    claims = []
    for d, title, date_text, iso in [
        (vp, "Vice President of Operations", "November 29, 2018", "2018-11-29"),
        (coo, "Chief Operating Officer", "June 24, 2019", "2019-06-24"),
    ]:
        snippet = d.text.split("\n", 1)[1]
        ex = Extraction(page_published_date=iso, published_date_snippet=date_text,
                        people=[person("Gretchen Howard", title, snippet)])  # fmt: skip
        claims += validate(d, ex)[0]
    res = resolve_claims(claims, today=TODAY)
    roles = {r.title: r for r in by_name(res, "Gretchen Howard").roles}
    assert not roles["Vice President of Operations"].is_current
    assert "superseded" in roles["Vice President of Operations"].note
    assert roles["Chief Operating Officer"].is_current  # no proxy in this test


def test_plural_shared_titles_classify():
    tc = classify_title("Co-Founders and Co-CEOs")
    assert tc is not None and tc.seniority == "c_level" and tc.also_founder


@pytest.mark.parametrize(
    ("title", "seniority", "function"),
    [
        ("Vice President and Chief Operating Officer, Robinhood Financial", "vp", "operations"),
        ("Vice President and Chief Information Security Officer", "vp", "security"),
        ("President of Robinhood UK and General Manager of Bitstamp UK", "vp", "other"),
        ("Senior Vice President, General Counsel and Corporate Secretary", "c_level", "legal"),
        ("President and Chief Executive Officer", "c_level", "other"),
    ],
)
def test_subsidiary_and_vp_qualified_titles(title, seniority, function):
    tc = classify_title(title, "Robinhood")
    assert tc is not None and (tc.seniority, tc.function) == (seniority, function)


def test_canonical_url_merges_locale_www_and_trailing_slash():
    from leadership.pipeline.discover import canonical_url

    a = "https://robinhood.com/newsroom/robinhood-welcomes-jeff-pinner-as-chief-technology-officer/"
    b = "https://www.robinhood.com/us/en/newsroom/robinhood-welcomes-jeff-pinner-as-chief-technology-officer"
    assert canonical_url(a) == canonical_url(b)
    assert canonical_url("https://x.test/a") != canonical_url("https://x.test/b")


def test_proxy_retired_role_reports_the_retired_claims():
    res = resolve_claims(proxy_claims() + news_claims(), today=TODAY)
    role = by_name(res, "Gretchen Howard").roles[0]
    assert not role.is_current and role.as_of == "2021-03-01"


def test_reanchored_claims_are_listed_for_review():
    d = doc("https://x.test/m", "Ann Lee\nHead of Data\nAnn Lee leads the data team.")
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[person("Ann Lee", "Head of Data", "Ann Lee leads the data team.")],
    )
    claims, _ = validate(d, ex)
    res = resolve_claims(claims, today=TODAY)
    assert res.review["reanchored"][0]["evidence_snippet"] == "Ann Lee Head of Data"
