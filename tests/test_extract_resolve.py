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
from leadership.sources.base import Company, RawDocument
from leadership.sources.fetch import html_blocks, html_to_text

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


def test_evidence_is_reanchored_within_one_line():
    text = (
        "Chris Koegel, Vice President, Corporate Finance & Investor Relations\n"
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
        "Chris Koegel, Vice President, Corporate Finance & Investor Relations"
    )


def test_reanchor_refuses_name_and_title_on_different_lines():
    """A heading with the name above a line with the title is two blocks: not re-anchored."""
    text = "Chris Koegel\nVice President, Investor Relations\nChris Koegel leads IR."
    d = doc("https://investors.x.test/m/ck", text, "search")
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Chris Koegel", "Vice President, Investor Relations", "Chris Koegel leads IR."),
    ])  # fmt: skip
    claims, (rej,) = validate(d, ex)
    assert not claims and rej.reason == "title not found in the evidence snippet"


def test_reanchor_uses_markdown_table_cells_as_blocks():
    text = "| Shiv Verma, Chief Financial Officer | Ann Lee, Chief Operating Officer |"
    d = doc("https://x.test/t", text, "search")
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Shiv Verma", "Chief Financial Officer", "Shiv Verma - Chief Financial Officer"),
        # Wrong pairing by the model: the neighbouring cell's title.
        person("Shiv Verma", "Chief Operating Officer", "Shiv Verma - Chief Operating Officer"),
    ])  # fmt: skip
    (claim,), (rej,) = validate(d, ex)
    assert claim.reanchored and claim.evidence_snippet == "Shiv Verma, Chief Financial Officer"
    assert rej.title == "Chief Operating Officer"

    far = doc("https://x.test/far", "Jane Doe wrote this.\n" + "filler " * 80 + "\nHead of Sales")
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[person("Jane Doe", "Head of Sales", "Jane Doe, Head of Sales")],
    )
    claims, (rej,) = validate(far, ex)
    assert not claims and rej.reason == "evidence snippet not found in the source text"


def test_team_grid_never_pairs_a_name_with_the_neighbours_title():
    """Cards in a grid: each <li> is one block. The model pairs Bob with Alice's title; the
    old 200-character window would have re-anchored 'Chief Executive Officer Bob Jones'."""
    html = (
        "<html><body><ul class='grid'>"
        "<li><strong>Alice Smith</strong> Chief Executive Officer</li>"
        "<li><strong>Bob Jones</strong> Chief Technology Officer</li>"
        "</ul>"
        "<div class='card'><h3>Carol White</h3><p>Chief Financial Officer</p></div>"
        "</body></html>"
    )
    text, _ = html_to_text(html)
    d = RawDocument("https://x.test/team", "company_site", "t", text, "2026-11-24T00:00:00+00:00",
                    "x.txt", {}, blocks=html_blocks(html))  # fmt: skip
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Bob Jones", "Chief Executive Officer", "Bob Jones, Chief Executive Officer"),
        person("Bob Jones", "Chief Technology Officer", "Bob Jones, Chief Technology Officer"),
        # Name in <h3>, title in <p>: different blocks, so not re-anchored either.
        person("Carol White", "Chief Financial Officer", "Carol White, CFO"),
    ])  # fmt: skip
    claims, rejections = validate(d, ex)
    assert [(c.full_name, c.title, c.evidence_snippet) for c in claims] == [
        ("Bob Jones", "Chief Technology Officer", "Bob Jones Chief Technology Officer")
    ]
    assert sorted((r.full_name, r.title) for r in rejections) == [
        ("Bob Jones", "Chief Executive Officer"),
        ("Carol White", "Chief Financial Officer"),
    ]


def test_reanchored_single_source_is_low_and_corroboration_lifts_it():
    page = doc("https://acme.test/team", "Ann Lee, Head of Data\nAnn Lee leads the data team.")
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[person("Ann Lee", "Head of Data", "Ann Lee leads the data team.")],
    )
    claims, _ = validate(page, ex)
    assert claims[0].reanchored
    res = resolve_claims(claims, today=TODAY)
    assert by_name(res, "Ann Lee").roles[0].confidence == "low"  # company site would be medium

    other = doc("https://news.test/a", "Acme's Head of Data, Ann Lee, said...", "search")
    ex2 = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Ann Lee", "Head of Data", "Acme's Head of Data, Ann Lee, said")])  # fmt: skip
    res = resolve_claims(claims + validate(other, ex2)[0], today=TODAY)
    assert by_name(res, "Ann Lee").roles[0].confidence == "high"


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
        ("Senior Vice President, General Counsel and Corporate Secretary", "vp", "legal"),
        ("Deputy General Counsel, VP-Legal and Regulatory", "vp", "legal"),
        ("VP and Deputy General Counsel", "vp", "legal"),
        ("General Counsel", "c_level", "legal"),
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
    d = doc("https://x.test/m", "Ann Lee, Head of Data\nAnn Lee leads the data team.")
    ex = Extraction(
        page_published_date=None,
        published_date_snippet=None,
        people=[person("Ann Lee", "Head of Data", "Ann Lee leads the data team.")],
    )
    claims, _ = validate(d, ex)
    res = resolve_claims(claims, today=TODAY)
    assert res.review["reanchored"][0]["evidence_snippet"] == "Ann Lee, Head of Data"


# --------------------------------------------------------------------- affiliation (D22)

CAMPFIRE = Company("Campfire", "meetcampfire.com", "https://campfire.ai/")


def _one(d, name, title, snippet, company=CAMPFIRE):
    ex = Extraction(page_published_date=None, published_date_snippet=None,
                    people=[person(name, title, snippet)])  # fmt: skip
    return validate(d, ex, company)


def test_byline_with_title_on_company_domain_states_affiliation():
    """Decided by Daniel after Phase 4: an author byline with a title on the company's own
    domain counts (Brendan Doyle's Campfire blog posts)."""
    d = doc("https://campfire.ai/blog/forecast",
            "Brendan Doyle\nHead of Product · September 18, 2026")  # fmt: skip
    claims, rejections = _one(d, "Brendan Doyle", "Head of Product",
                              "Brendan Doyle Head of Product · September 18, 2026")  # fmt: skip
    assert claims and not rejections


def test_quoted_outsider_on_company_blog_is_not_a_byline():
    text = "said Nick Jezienicki, Head of Partnerships at the partner bank."
    claims, (rej,) = _one(doc("https://campfire.ai/blog/atlar", text), "Nick Jezienicki",
                          "Head of Partnerships", text)  # fmt: skip
    assert not claims and rej.reason == "evidence does not state an affiliation with Campfire"
    # The same byline on a third-party site is not enough either.
    other = doc("https://blog.other.test/post", "Brendan Doyle\nHead of Product", "search")
    assert _one(other, "Brendan Doyle", "Head of Product", "Brendan Doyle Head of Product")[1]


@pytest.mark.parametrize(
    ("url", "source_type", "text", "name", "title", "snippet"),
    [
        ("https://news.test/44993", "search",
         "Campfire appointed Alexandra Roveda vice president of enterprise Friday.",
         "Alexandra Roveda", "vice president of enterprise",
         "Campfire appointed Alexandra Roveda vice president of enterprise"),
        ("https://campfire.ai/blog/stealth", "company_site",
         "At the same time, my Co-founder / CTO, Fernando San Martin, led back-end engineering.",
         "Fernando San Martin", "Co-founder / CTO", "my Co-founder / CTO, Fernando San Martin"),
        ("https://campfire.ai/team", "company_site", "Paul Nichols\nCTO",
         "Paul Nichols", "CTO", "Paul Nichols CTO"),
        ("https://investors.campfire.ai/management/paul-nichols", "search", "Paul Nichols\nCTO",
         "Paul Nichols", "CTO", "Paul Nichols CTO"),
        ("https://www.sec.gov/x/def14a.htm", "sec_filing", "Paul Nichols 41 CTO",
         "Paul Nichols", "CTO", "Paul Nichols 41 CTO"),
    ],
)  # fmt: skip
def test_affiliation_stated_by_name_people_page_first_person_or_own_filing(
    url, source_type, text, name, title, snippet
):
    claims, rejections = _one(doc(url, text, source_type), name, title, snippet)
    assert claims and not rejections


def test_third_party_page_without_the_company_name_is_rejected():
    d = doc("https://blog.other.test/post", "Paul Nichols, CTO, spoke at the event.", "search")
    claims, (rej,) = _one(d, "Paul Nichols", "CTO", "Paul Nichols, CTO, spoke at the event.")
    assert not claims and "affiliation" in rej.reason


# ------------------------------------------------------------- legal chiefs (D4) and founders


def test_general_counsel_is_c_level_only_when_in_the_proxy():
    proxy = doc("https://sec.test/def14a", "Dan Gallagher 52 Chief Legal Officer", "sec_filing",
                form="DEF 14A", filing_date="2026-04-22")  # fmt: skip
    ir = doc("https://investors.robinhood.test/management/lm",
             "Lucas Moskowitz, Senior Vice President, General Counsel", "search")  # fmt: skip
    claims = validate(proxy, Extraction(page_published_date=None, published_date_snippet=None,
                      people=[person("Dan Gallagher", "Chief Legal Officer",
                                     "Dan Gallagher 52 Chief Legal Officer")]))[0]  # fmt: skip
    claims += validate(
        ir,
        Extraction(
            page_published_date=None,
            published_date_snippet=None,
            people=[
                person(
                    "Lucas Moskowitz",
                    "Senior Vice President, General Counsel",
                    "Lucas Moskowitz, Senior Vice President, General Counsel",
                )
            ],
        ),
    )[0]
    res = resolve_claims(claims, today=TODAY)
    (gallagher,) = by_name(res, "Dan Gallagher").roles
    (moskowitz,) = by_name(res, "Lucas Moskowitz").roles
    assert (gallagher.seniority, gallagher.function) == ("c_level", "legal")
    # "Senior Vice President, General Counsel": not in the proxy, and a VP token (D30).
    assert (moskowitz.seniority, moskowitz.function, moskowitz.is_current) == (
        "vp",
        "legal",
        True,
    )
    assert "seat_conflicts" not in res.review


def test_founder_without_a_current_operating_role_is_historical():
    d = doc("https://acme.test/about", "Baiju Bhatt, Co-founder\nJohn Glasgow, CEO & Founder")
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Baiju Bhatt", "Co-founder", "Baiju Bhatt, Co-founder"),
        person("John Glasgow", "CEO & Founder", "John Glasgow, CEO & Founder"),
    ])  # fmt: skip
    res = resolve_claims(validate(d, ex)[0], today=TODAY)
    baiju, john = by_name(res, "Baiju Bhatt"), by_name(res, "John Glasgow")
    assert not baiju.is_current and "historical" in baiju.roles[0].note
    assert res.review["founder_historical"][0]["person"] == "Baiju Bhatt"
    assert john.is_current and all(r.is_current for r in john.roles)


def test_sec_8k_departure_after_the_proxy_wins():
    """Pinner: listed as CTO in the 2026-04-22 proxy; an 8-K filed 2026-05-08 says he is
    separating. The 8-K is an SEC filing, newer than the proxy: not current."""
    proxy = doc("https://sec.test/def14a", "Jeffrey Pinner 45 Chief Technology Officer",
                "sec_filing", form="DEF 14A", filing_date="2026-04-22")  # fmt: skip
    eightk = doc("https://sec.test/8k", "Jeffrey Pinner, Chief Technology Officer, would be "
                 "separating from his position.", "sec_filing", form="8-K",
                 filing_date="2026-05-08")  # fmt: skip
    claims = validate(
        proxy,
        Extraction(
            page_published_date=None,
            published_date_snippet=None,
            people=[
                person(
                    "Jeffrey Pinner",
                    "Chief Technology Officer",
                    "Jeffrey Pinner 45 Chief Technology Officer",
                )
            ],
        ),
    )[0]
    claims += validate(
        eightk,
        Extraction(
            page_published_date=None,
            published_date_snippet=None,
            people=[
                person(
                    "Jeffrey Pinner",
                    "Chief Technology Officer",
                    "Jeffrey Pinner, Chief Technology Officer, would be separating",
                    status="former",
                )
            ],
        ),
    )[0]
    (role,) = by_name(resolve_claims(claims, today=TODAY), "Jeffrey Pinner").roles
    assert not role.is_current and role.as_of == "2026-05-08" and role.confidence == "high"


def test_prefilter_sends_a_whole_8k():
    d = doc("https://sec.test/8k", "Item 5.02 Departure of Officers\nJane Doe ...\nSIGNATURES",
            "sec_filing", form="8-K")  # fmt: skip
    text, note = select_text(d)
    assert text.startswith("Item 5.02") and "8-K" in note


# ------------------------------------------------------- display names and deterministic export


def test_credential_suffix_is_dropped_from_the_display_name_but_kept_in_facts(tmp_path):
    from dataclasses import replace

    from leadership.pipeline.collect import _store, export_json
    from leadership.storage.db import connect
    from leadership.storage.repository import Repository

    d = doc("https://investors.robinhood.test/management/sg",
            "Stephanie Guild, CFA, Chief Investment Officer", "search")  # fmt: skip
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Stephanie Guild, CFA", "Chief Investment Officer",
               "Stephanie Guild, CFA, Chief Investment Officer")])  # fmt: skip
    claims = [replace(c, extracted_at="2026-11-01T00:00:00+00:00") for c in validate(d, ex)[0]]
    res = resolve_claims(claims, today=TODAY)
    assert res.people[0].full_name == "Stephanie Guild"

    company = Company("Robinhood", "robinhood.test", "https://robinhood.test/")
    db = tmp_path / "leadership.db"
    outputs = []
    for _ in range(2):
        _store(db, company, res, {"duration_s": 0.0, "cost_usd": 0.1, "claims_accepted": 1}, 0)
        outputs.append(export_json(db, "robinhood.test").read_bytes())
    assert outputs[0] == outputs[1]  # unchanged data: byte-identical export

    conn = connect(db)
    try:
        (p,) = Repository(conn).all_people("robinhood.test")
    finally:
        conn.close()
    assert p["full_name"] == "Stephanie Guild"
    assert {f["value"] for f in p["facts"] if f["field"] == "full_name"} == {"Stephanie Guild, CFA"}
    assert p["updated_at"] == "2026-11-01T00:00:00+00:00"
    assert b'"cost_usd"' not in outputs[0] and b'"duration_s"' not in outputs[0]


def test_seat_is_the_c_title_not_the_function():
    from leadership.pipeline.resolve import c_seat

    assert c_seat("Co-founder / CTO") == c_seat("Chief Technology Officer")
    assert c_seat("Chief Financial Officer") != c_seat("Chief Accounting Officer")
    assert c_seat("Chairman and Chief Executive Officer") is None


def test_older_s1_found_by_search_is_the_companys_own_filing():
    rh = Company("Robinhood", "robinhood.com", "https://robinhood.com/", True, "HOOD", "0001783879")
    d = doc("https://www.sec.gov/Archives/edgar/data/1783879/000162828021013986/s1.htm",
            "Aparna Chennapragada 37 Chief Product Officer", "search")  # fmt: skip
    snippet = "Aparna Chennapragada 37 Chief Product Officer"
    claims, rejections = _one(d, "Aparna Chennapragada", "Chief Product Officer", snippet, rh)
    assert claims and not rejections
    other = doc("https://www.sec.gov/Archives/edgar/data/99/x/s1.htm", d.text, "search")
    assert _one(other, "Aparna Chennapragada", "Chief Product Officer",
                "Aparna Chennapragada 37 Chief Product Officer", company=rh)[1]  # fmt: skip


def test_flattened_leadership_line_never_pairs_a_name_with_a_neighbours_title():
    """The Robinhood IR leadership page arrives from search as one long line."""
    text = (
        "Vlad Tenev Chairman & Chief Executive Officer Pooja Anand Vice President, Enterprise "
        "Risk Management and Internal Audit Robb Baldwin Vice President and General Manager, "
        "TradePMR"
    )
    d = doc("https://investors.robinhood.test/governance/leadership", text, "search")
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Robb Baldwin", "Vice President, Enterprise Risk Management and Internal Audit",
               "Robb Baldwin - VP"),
        person("Pooja Anand", "Vice President and General Manager, TradePMR", "Pooja Anand - VP"),
        person("Robb Baldwin", "Vice President and General Manager, TradePMR", "Robb Baldwin - VP"),
        person("Pooja Anand", "Vice President, Enterprise Risk Management and Internal Audit",
               "Pooja Anand - VP"),
    ])  # fmt: skip
    claims, rejections = validate(d, ex)
    assert sorted((c.full_name, c.evidence_snippet) for c in claims) == [
        ("Pooja Anand",
         "Pooja Anand Vice President, Enterprise Risk Management and Internal Audit"),
        ("Robb Baldwin", "Robb Baldwin Vice President and General Manager, TradePMR"),
    ]  # fmt: skip
    assert len(rejections) == 2


def test_html_sentence_with_another_name_between_is_not_reanchored():
    html = "<p>Jane Doe thanked Bob Ray, our Head of Sales.</p>"
    text, _ = html_to_text(html)
    d = RawDocument("https://acme.test/news", "company_site", "t", text, "2026-11-24", "x.txt",
                    {}, blocks=html_blocks(html))  # fmt: skip
    ex = Extraction(page_published_date=None, published_date_snippet=None, people=[
        person("Jane Doe", "Head of Sales", "Jane Doe, Head of Sales"),
        person("Bob Ray", "Head of Sales", "Bob Ray, our Head of Sales."),
    ])  # fmt: skip
    claims, (rej,) = validate(d, ex)
    assert [c.full_name for c in claims] == ["Bob Ray"] and rej.full_name == "Jane Doe"


@pytest.mark.parametrize(
    ("url", "snippet"),
    [
        ("https://campfire.ai/blog/atlar", "Nick Jezienicki, Head of US Partnerships"),
        (
            "https://campfire.ai/blog/about-atlar-integration",
            "Nick Jezienicki, Head of Partnerships",
        ),
        ("https://campfire.ai/company/blog/atlar", "Nick Jezienicki, Head of Partnerships"),
    ],
)
def test_affiliation_loopholes_are_closed(url, snippet):
    quote = f"{snippet}, told the audience."  # a quote, not a byline
    claims, (rej,) = _one(doc(url, quote), "Nick Jezienicki", snippet.split(", ", 1)[1], quote)
    assert not claims and "affiliation" in rej.reason


def test_stale_reanchored_role_is_listed_as_stale():
    d = doc(
        "https://acme.test/news/old", "April 22, 2021\nChristine Brown, VP of Product Operations"
    )
    ex = Extraction(page_published_date="2021-04-22", published_date_snippet="April 22, 2021",
                    people=[person("Christine Brown", "VP of Product Operations",
                                   "Christine Brown leads product ops")])  # fmt: skip
    res = resolve_claims(validate(d, ex)[0], today=TODAY)
    assert res.review["stale_single_source"][0]["person"] == "Christine Brown"
    assert res.review["reanchored"][0]["person"] == "Christine Brown"


def test_credential_suffixes_do_not_split_a_person():
    from leadership.models import display_name, normalize_name

    for name in ("Jane Doe, JD", "Jane Doe, M.D.", "Jane Doe CFP", "Jane Doe, PhD"):
        assert normalize_name(name) == "jane doe" and display_name(name) == "Jane Doe"
    assert display_name("John Smith Jr.") == "John Smith Jr."


def test_advisor_titles_are_out_of_scope():
    for title in ("Chief Economic Advisor", "Senior Advisor to the CEO", "Strategic Adviser"):
        assert classify_title(title) is None
    assert classify_title("Vice President, Total Rewards").function == "people"


def _single(d, name, title, snippet, status="current", date=None, date_text=None):
    ex = Extraction(page_published_date=date, published_date_snippet=date_text,
                    people=[person(name, title, snippet, status=status)])  # fmt: skip
    return validate(d, ex)[0]


def test_past_general_counsel_stays_historical_c_level():
    """D4 applies to current roles only: Anne Hoge, CLO before the company had a proxy."""
    proxy = doc("https://sec.test/def14a", "Dan Gallagher 52 Chief Legal Officer", "sec_filing",
                form="DEF 14A", filing_date="2026-04-22")  # fmt: skip
    news = doc("https://robinhood.test/news/hoge",
               "May 1, 2020\nAnne Hoge, Chief Legal Officer, is leaving Robinhood.")  # fmt: skip
    claims = _single(proxy, "Dan Gallagher", "Chief Legal Officer",
                     "Dan Gallagher 52 Chief Legal Officer")  # fmt: skip
    claims += _single(news, "Anne Hoge", "Chief Legal Officer",
                      "Anne Hoge, Chief Legal Officer, is leaving Robinhood.", status="former",
                      date="2020-05-01", date_text="May 1, 2020")  # fmt: skip
    (hoge,) = by_name(resolve_claims(claims, today=TODAY), "Anne Hoge").roles
    assert (hoge.seniority, hoge.function, hoge.is_current) == ("c_level", "legal", False)
    assert "historical C-level" in hoge.note and "2020-05-01" in hoge.note


def test_role_notes_name_the_source_and_date():
    res = resolve_claims(proxy_claims() + news_claims(), today=TODAY)
    (howard,) = by_name(res, "Gretchen Howard").roles
    assert "not listed in the 2026-04-22 proxy" in howard.note
    assert "last confirmed in company site robinhood.test/news/2021 (2021-03-01)" in howard.note


def test_displayed_title_is_the_newest_wording():
    long_title = "Chief Legal, Compliance and Corporate Affairs Officer"
    proxy = doc("https://sec.test/def14a", f"Dan Gallagher 52 {long_title}", "sec_filing",
                form="DEF 14A", filing_date="2026-04-22")  # fmt: skip
    news = doc(
        "https://robinhood.test/news/dg", "Robinhood names Dan Gallagher Chief Legal Officer."
    )
    claims = _single(proxy, "Dan Gallagher", long_title, f"Dan Gallagher 52 {long_title}")
    claims += _single(news, "Dan Gallagher", "Chief Legal Officer",
                      "Robinhood names Dan Gallagher Chief Legal Officer.")  # fmt: skip
    (role,) = by_name(resolve_claims(claims, today=TODAY), "Dan Gallagher").roles
    assert role.title == long_title


def test_current_listing_title_beats_an_old_announcement():
    """Moskowitz: 'VP and Deputy General Counsel' in a 2020 post; the undated IR leadership
    listing says 'Senior Vice President, General Counsel and Corporate Secretary'."""
    old = doc("https://robinhood.test/newsroom/legal-team",
              "August 19, 2020\nsaid Lucas Moskowitz, VP and Deputy General Counsel at "
              "Robinhood.")  # fmt: skip
    listing = doc("https://investors.robinhood.test/governance/leadership",
                  "Lucas Moskowitz Senior Vice President, General Counsel and Corporate Secretary",
                  "search")  # fmt: skip
    claims = _single(old, "Lucas Moskowitz", "VP and Deputy General Counsel",
                     "said Lucas Moskowitz, VP and Deputy General Counsel at Robinhood.",
                     date="2020-08-19", date_text="August 19, 2020")  # fmt: skip
    claims += _single(listing, "Lucas Moskowitz",
                      "Senior Vice President, General Counsel and Corporate Secretary",
                      "Lucas Moskowitz Senior Vice President, General Counsel and Corporate "
                      "Secretary")  # fmt: skip
    roles = by_name(resolve_claims(claims, today=TODAY), "Lucas Moskowitz").roles
    assert [r.title for r in roles if r.is_current] == [
        "Senior Vice President, General Counsel and Corporate Secretary"
    ]


def test_listing_wording_beats_a_profile_page_and_keeps_its_scope():
    """Koller: the IR leadership listing has '..., Robinhood Financial'; his /management/
    profile page shortens it. The listing (last segment 'leadership') wins."""
    listing = doc("https://investors.robinhood.test/governance/leadership",
                  "Walter Koller Vice President and Chief Operating Officer, Robinhood Financial",
                  "search")  # fmt: skip
    profile = doc("https://investors.robinhood.test/management/walter-koller",
                  "Walter Koller Vice President and Chief Operating Officer", "search")  # fmt: skip
    rh = Company("Robinhood", "robinhood.test", "https://robinhood.test/")
    long_t = "Vice President and Chief Operating Officer, Robinhood Financial"
    claims = _one(listing, "Walter Koller", long_t, f"Walter Koller {long_t}", rh)[0]
    claims += _one(profile, "Walter Koller", "Vice President and Chief Operating Officer",
                   "Walter Koller Vice President and Chief Operating Officer", rh)[0]  # fmt: skip
    (role,) = by_name(resolve_claims(claims, today=TODAY), "Walter Koller").roles
    assert role.title == "Vice President and Chief Operating Officer, Robinhood Financial"


@pytest.mark.parametrize(
    ("snippet", "title"),
    [
        ("Jane Doe, Founder at Atlar · May 1, 2026", "Founder at Atlar"),
        ("Jane Doe CEO, Atlar", "CEO, Atlar"),
        ("Jane Doe, Head of Partnerships at Atlar", "Head of Partnerships at Atlar"),
        ("Jane Doe · VP of Engineering at Acme Bank", "VP of Engineering at Acme Bank"),
    ],
)
def test_byline_naming_another_employer_is_rejected(snippet, title):
    d = doc("https://campfire.ai/blog/partner-post", snippet)
    claims, (rej,) = _one(d, "Jane Doe", title, snippet)
    assert not claims and "affiliation" in rej.reason


def test_byline_naming_the_company_or_a_function_is_accepted():
    for snippet, title in [
        ("Jane Doe, Head of Product at Campfire", "Head of Product at Campfire"),
        ("Jane Doe · VP, Engineering · May 1, 2026", "VP, Engineering"),
        ("Jane Doe, Head of Sales, EMEA", "Head of Sales, EMEA"),
        ("Jane Doe · VP, EMEA", "VP, EMEA"),
    ]:
        claims, rejections = _one(doc("https://campfire.ai/blog/p", snippet), "Jane Doe",
                                  title, snippet)  # fmt: skip
        assert claims and not rejections, snippet


def test_single_byline_is_low_confidence():
    d = doc("https://campfire.ai/blog/atlar", "Nick Jezienicki\nHead of Technology Partnerships")
    claims, _ = _one(d, "Nick Jezienicki", "Head of Technology Partnerships",
                     "Nick Jezienicki Head of Technology Partnerships")  # fmt: skip
    assert claims[0].affiliation == "byline"
    (role,) = by_name(resolve_claims(claims, today=TODAY), "Nick Jezienicki").roles
    assert role.confidence == "low" and "single author byline" in role.note


def test_former_deputy_general_counsel_is_not_restored_to_c_level():
    d = doc("https://robinhood.test/news/legal", "August 19, 2020\nChristina Lai, VP and Deputy "
            "General Counsel, has left Robinhood.")  # fmt: skip
    claims = _single(d, "Christina Lai", "VP and Deputy General Counsel",
                     "Christina Lai, VP and Deputy General Counsel, has left Robinhood.",
                     status="former", date="2020-08-19", date_text="August 19, 2020")  # fmt: skip
    (role,) = by_name(resolve_claims(claims, today=TODAY), "Christina Lai").roles
    assert (role.seniority, role.is_current) == ("vp", False)  # VP token (D30), not C-level


def test_same_title_and_function_is_one_role_row_with_merged_sources():
    """D29: 'CEO & Founder' (C-level + founder) on two pages is one row, both sources kept."""
    a = doc("https://acme.test/team", "John Glasgow, CEO & Founder")
    b = doc("https://acme.test/blog/x", "John Glasgow, CEO and Founder")
    claims = _single(a, "John Glasgow", "CEO & Founder", "John Glasgow, CEO & Founder")
    claims += _single(b, "John Glasgow", "CEO and Founder", "John Glasgow, CEO and Founder")
    person = by_name(resolve_claims(claims, today=TODAY), "John Glasgow")
    (role,) = person.roles
    assert (role.seniority, role.function, role.is_current) == ("c_level", "other", True)
    assert {c.doc.url for c in role.claims} == {
        "https://acme.test/team",
        "https://acme.test/blog/x",
    }
    assert "founder" in role.note


def test_different_titles_stay_separate_rows():
    from leadership.pipeline.resolve import ResolvedRole, dedupe_roles

    def role(title, function):
        return ResolvedRole(title, function, "vp", True, None, "low", [], "")

    rows = dedupe_roles([role("VP of Sales", "sales"), role("VP of Product", "product"),
                         role("vp of sales", "sales")])  # fmt: skip
    assert sorted(r.title for r in rows) == ["VP of Product", "VP of Sales"]


def test_general_counsel_without_vp_token_and_not_in_proxy_is_head():
    d = doc("https://acme.test/team", "Ann Lee, General Counsel")
    (role,) = by_name(resolve_claims(_single(d, "Ann Lee", "General Counsel",
                                             "Ann Lee, General Counsel"), today=TODAY),
                      "Ann Lee").roles  # fmt: skip
    assert (role.seniority, role.function) == ("head", "legal")


def test_overlapping_wording_merges_rows_and_keeps_the_newest():
    """D30: Matt Billings, two wordings of one position with different functions."""
    from leadership.pipeline.resolve import title_overlap

    old_t = "VP and President of Robinhood Financial LLC and Robinhood Securities, LLC"
    new_t = "Vice President, Brokerage and President, Robinhood Financial and Robinhood Securities"
    assert title_overlap(old_t, new_t) >= 0.6
    assert title_overlap("VP of Engineering", "VP of Product") < 0.6
    assert title_overlap("VP of Product", "VP of Product Marketing") < 0.6
    assert (
        title_overlap("Chief Operating Officer", "Chief Operating Officer, Robinhood Crypto") < 0.6
    )
    rh = Company("Robinhood", "robinhood.test", "https://robinhood.test/")
    post = doc("https://robinhood.test/newsroom/billings",
               f"We welcome Matt Billings as {old_t}.", published_date="2023-09-18")  # fmt: skip
    listing = doc("https://investors.robinhood.test/governance/leadership",
                  f"Matt Billings {new_t}", "search")  # fmt: skip
    claims = _one(post, "Matt Billings", old_t, f"We welcome Matt Billings as {old_t}.", rh)[0]
    claims += _one(listing, "Matt Billings", new_t, f"Matt Billings {new_t}", rh)[0]
    assert {c.title_class.function for c in claims} == {"other", "operations"}
    res = resolve_claims(claims, today=TODAY)
    (role,) = by_name(res, "Matt Billings").roles
    assert role.title == new_t and role.is_current
    assert {c.doc.url for c in role.claims} == {post.url, listing.url}
    (merge,) = res.review["merged_role_rows"]
    assert merge["person"] == "Matt Billings" and merge["merged"] == [old_t]
    # Two sources now: no "single source" note, not in the stale list.
    assert "single source" not in role.note and "merged from 2 rows" in role.note
    assert role.confidence == "high"  # 2 sources incl. the company site (role_confidence)
    assert "stale_single_source" not in res.review


def test_different_roles_of_one_person_are_not_merged():
    d = doc("https://acme.test/team", "Ann Lee, VP of Engineering\nAnn Lee, VP of Product")
    claims = _single(d, "Ann Lee", "VP of Engineering", "Ann Lee, VP of Engineering")
    claims += _single(d, "Ann Lee", "VP of Product", "Ann Lee, VP of Product")
    res = resolve_claims(claims, today=TODAY)
    assert len(by_name(res, "Ann Lee").roles) == 2 and "merged_role_rows" not in res.review


def test_svp_general_counsel_listed_in_the_proxy_is_c_level():
    """D4 overrides the VP token (D30) for an executive officer named in the latest proxy."""
    title = "Senior Vice President, General Counsel"
    proxy = doc("https://sec.test/def14a", f"Lucas Moskowitz 45 {title}", "sec_filing",
                form="DEF 14A", filing_date="2026-04-22")  # fmt: skip
    claims = _single(proxy, "Lucas Moskowitz", title, f"Lucas Moskowitz 45 {title}")
    (role,) = by_name(resolve_claims(claims, today=TODAY), "Lucas Moskowitz").roles
    assert (role.seniority, role.function) == ("c_level", "legal")


def test_former_row_is_not_merged_into_a_current_one():
    from leadership.pipeline.resolve import ResolvedRole, merge_overlapping_roles

    old = ResolvedRole("VP, Brokerage Operations", "operations", "vp", False, "2020-01-01",
                       "low", [], "stated as former")  # fmt: skip
    new = ResolvedRole("VP, Brokerage and Operations", "other", "vp", True, None, "medium", [],
                       "")  # fmt: skip
    rows, merges = merge_overlapping_roles([old, new])
    assert len(rows) == 2 and merges == []
