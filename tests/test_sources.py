"""Offline tests for fetching, caching, and source adapters (no network, no keys)."""

import hashlib
import json

import httpx
import pytest

from leadership.pipeline.discover import normalize_domain, site_name_from_html
from leadership.sources.base import Company
from leadership.sources.company_site import _locale_prefix, parse_sitemap, score_url
from leadership.sources.fetch import FetchBlocked, Fetcher, html_to_text, is_banned
from leadership.sources.search import SearchAdapter, _drop_excluded, build_queries, is_excluded
from leadership.sources.sec_edgar import find_section, lookup_cik


def make_fetcher(tmp_path, routes: dict, calls: list | None = None, min_interval_s: float = 0):
    """Fetcher backed by a MockTransport. `routes`: URL -> (status, body[, headers])."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        status, body, *rest = routes.get(str(request.url), (404, "not found"))
        headers = {"content-type": "text/html", **(rest[0] if rest else {})}
        return httpx.Response(status, text=body, headers=headers)

    transport = httpx.MockTransport(handler)
    return Fetcher(tmp_path / "raw", transport=transport, min_interval_s=min_interval_s)


# --------------------------------------------------------------------------- fetcher


def test_robots_disallow_blocks_fetch(tmp_path):
    routes = {"https://ex.test/robots.txt": (200, "User-agent: *\nDisallow: /private")}
    f = make_fetcher(tmp_path, routes)
    with pytest.raises(FetchBlocked, match="robots"):
        f.get("https://ex.test/private/team", "company_site")


def test_robots_403_means_stay_out(tmp_path):
    f = make_fetcher(tmp_path, {"https://ex.test/robots.txt": (403, "")})
    with pytest.raises(FetchBlocked):
        f.get("https://ex.test/team", "company_site")


def test_missing_robots_allows(tmp_path):
    f = make_fetcher(tmp_path, {"https://ex.test/team": (200, "<p>hi</p>")})
    assert f.get("https://ex.test/team", "company_site").ok


def test_linkedin_never_fetched(tmp_path):
    calls: list[str] = []
    f = make_fetcher(tmp_path, {}, calls)
    for url in ("https://www.linkedin.com/in/someone", "https://linkedin.com/company/x"):
        assert is_banned(url)
        with pytest.raises(FetchBlocked, match="banned"):
            f.get(url, "company_site", check_robots=False)
    assert calls == []
    assert not is_banned("https://notlinkedin.com/")


def test_redirect_to_banned_host_is_never_requested(tmp_path):
    calls: list[str] = []
    routes = {
        "https://ex.test/team": (301, "", {"location": "https://www.linkedin.com/company/ex"})
    }
    with pytest.raises(FetchBlocked, match="banned"):
        make_fetcher(tmp_path, routes, calls).get("https://ex.test/team", "company_site")
    assert not any("linkedin" in c for c in calls)
    # The refusal is cached: a warm run raises again without any request.
    warm: list[str] = []
    with pytest.raises(FetchBlocked, match="banned"):
        make_fetcher(tmp_path, routes, warm).get("https://ex.test/team", "company_site")
    assert warm == []


def test_redirect_to_robots_disallowed_path_is_never_requested(tmp_path):
    calls: list[str] = []
    routes = {
        "https://ex.test/robots.txt": (200, "User-agent: *\nDisallow: /private"),
        "https://ex.test/about": (302, "", {"location": "/private/people"}),
        "https://other.test/robots.txt": (200, "User-agent: *\nDisallow: /"),
        "https://ex.test/team": (302, "", {"location": "https://other.test/team"}),
    }
    for start in ("https://ex.test/about", "https://ex.test/team"):
        with pytest.raises(FetchBlocked, match="robots"):
            make_fetcher(tmp_path, routes, calls).get(start, "company_site")
    assert "https://ex.test/private/people" not in calls
    assert "https://other.test/team" not in calls


def test_allowed_redirect_is_followed(tmp_path):
    routes = {
        "https://ex.test/about": (301, "", {"location": "https://ex.test/company/about"}),
        "https://ex.test/company/about": (200, "<p>About</p>"),
    }
    res = make_fetcher(tmp_path, routes).get("https://ex.test/about", "company_site")
    assert res.ok and res.final_url == "https://ex.test/company/about"


def test_robots_5xx_means_stay_out(tmp_path):
    f = make_fetcher(tmp_path, {"https://ex.test/robots.txt": (503, "")})
    with pytest.raises(FetchBlocked, match="robots"):
        f.get("https://ex.test/team", "company_site")


def test_rate_limit_spaces_requests_per_host(tmp_path, monkeypatch):
    clock = {"t": 100.0}
    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        clock["t"] += s

    monkeypatch.setattr("leadership.sources.fetch.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("leadership.sources.fetch.time.sleep", fake_sleep)
    routes = {f"https://ex.test/p{i}": (200, "<p>x</p>") for i in range(3)}
    f = make_fetcher(tmp_path, routes, min_interval_s=1.0)
    for i in range(3):
        f.get(f"https://ex.test/p{i}", "company_site")
    # robots.txt + 3 pages on one host: every request after the first waits ~1 s.
    assert len(sleeps) == 3 and all(abs(s - 1.0) < 1e-9 for s in sleeps)


def test_cache_hit_makes_no_request(tmp_path):
    calls: list[str] = []
    routes = {"https://ex.test/about": (200, "<p>About us</p>")}
    make_fetcher(tmp_path, routes, calls).get("https://ex.test/about", "company_site")
    first = len(calls)
    again = make_fetcher(tmp_path, routes, calls).get("https://ex.test/about", "company_site")
    assert again.from_cache and again.text_body() == "<p>About us</p>"
    assert len(calls) == first  # robots.txt is cached too


def test_errors_are_cached_not_refetched(tmp_path):
    calls: list[str] = []
    make_fetcher(tmp_path, {}, calls).get("https://ex.test/gone", "company_site")
    n = len(calls)
    res = make_fetcher(tmp_path, {}, calls).get("https://ex.test/gone", "company_site")
    assert res.status == 404 and res.from_cache and len(calls) == n


def test_html_to_text_drops_scripts_and_keeps_title():
    html = """<html><head><title>Team | Acme</title><style>.x{}</style></head><body>
      <nav>Home</nav><script>var secret = "Fake Person, CEO";</script>
      <div><h3>Jane Doe</h3><p>Chief   Technology Officer</p></div></body></html>"""
    text, title = html_to_text(html)
    assert title == "Team | Acme"
    assert "Fake Person" not in text
    assert text.splitlines() == ["Home", "Jane Doe", "Chief Technology Officer"]


# --------------------------------------------------------------------------- company site


@pytest.mark.parametrize(
    ("url", "score"),
    [
        ("https://ex.test/about", 10),
        ("https://ex.test/us/en/leadership/", 10),
        ("https://ex.test/newsroom/acme-appoints-jane-doe-as-cfo/", 8),
        ("https://ex.test/blog/jane-joins-as-head-of-sales", 8),
        ("https://ex.test/careers", 3),
        ("https://ex.test/blog/q3-product-update", 2),
        ("https://ex.test/pricing", 0),
        ("https://ex.test/blog", 0),
    ],
)
def test_score_url(url, score):
    assert score_url(url) == score


def test_parse_sitemap_urls_and_children():
    xml = """<urlset><url><loc>https://ex.test/a?x=1&amp;y=2</loc><lastmod>2026-01-02</lastmod>
      </url><url><loc> https://ex.test/b </loc></url></urlset>
      <sitemapindex><sitemap><loc>https://ex.test/sitemap-news.xml</loc></sitemap></sitemapindex>"""
    urls, children = parse_sitemap(xml)
    assert urls == [("https://ex.test/a?x=1&y=2", "2026-01-02"), ("https://ex.test/b", "")]
    assert children == ["https://ex.test/sitemap-news.xml"]


def test_locale_prefix():
    assert _locale_prefix("https://robinhood.com/us/en/") == "/us/en/"
    assert _locale_prefix("https://campfire.ai/") == "/"


def test_site_name_and_domain():
    assert site_name_from_html("<title>Campfire | The AI-Native ERP</title>") == "Campfire"
    assert site_name_from_html("<title>Robinhood: 24/5 Trading</title>") == "Robinhood"
    og = '<meta property="og:site_name" content="Acme Corp"><title>Home - x</title>'
    assert site_name_from_html(og) == "Acme Corp"
    assert normalize_domain("https://www.Robinhood.com/us/en/") == "robinhood.com"
    assert normalize_domain("meetcampfire.com") == "meetcampfire.com"
    with pytest.raises(ValueError):
        normalize_domain("not a domain")


# --------------------------------------------------------------------------- SEC


def test_find_section_skips_table_of_contents():
    text = "\n".join(
        [
            "Executive Officers",  # table of contents entry
            "70",
            "Executive Compensation",
            *["Our Chief Executive Officer letter, page filler."] * 10,  # TOC sees officers too
            "Executive Officers",  # real section
            "Jane Doe",
            "Chief Executive Officer",
            "John Roe",
            "Chief Financial Officer",
        ]
    )
    start, _ = find_section(text)
    assert text[start:].startswith("Executive Officers\nJane Doe")
    assert find_section("no such heading here") is None


def test_lookup_cik_refuses_ambiguous_name(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")
    tickers = {
        "0": {"cik_str": 1, "ticker": "HOOD", "title": "Robinhood Markets, Inc."},
        "1": {"cik_str": 2, "ticker": "RVI", "title": "Robinhood Ventures Fund I"},
        "2": {"cik_str": 3, "ticker": "ACME", "title": "Acme Widgets Inc"},
        "3": {"cik_str": 4, "ticker": "ACMX", "title": "Acmeology Corp"},
    }
    url = "https://www.sec.gov/files/company_tickers.json"
    f = make_fetcher(tmp_path, {url: (200, json.dumps(tickers))})
    assert lookup_cik(f, "Robinhood", None) is None  # two registrants: refuse to guess
    assert lookup_cik(f, "Robinhood", "hood") == ("0000000001", "HOOD", "Robinhood Markets, Inc.")
    assert lookup_cik(f, "Acme", None) == ("0000000003", "ACME", "Acme Widgets Inc")  # whole word
    assert lookup_cik(f, "Campfire", None) is None


# --------------------------------------------------------------------------- search


def test_search_exclusions():
    for url in (
        "https://www.linkedin.com/in/x",
        "https://x.com/vladtenev",
        "https://www.zoominfo.com/p/x",
    ):
        assert is_excluded(url)
    assert not is_excluded("https://www.prnewswire.com/news-releases/x")


def test_drop_excluded_purges_cached_text():
    data = {
        "response": {
            "results": [
                {"url": "https://x.com/a", "raw_content": "tweet text"},
                {"url": "https://news.test/b", "raw_content": "article"},
            ]
        }
    }
    assert _drop_excluded(data) is True
    assert [r["url"] for r in data["response"]["results"]] == ["https://news.test/b"]
    assert data["excluded_urls"] == ["https://x.com/a"]
    assert _drop_excluded(data) is False


def test_search_adapter_purges_excluded_text_from_cache(tmp_path):
    """Seeded cache only: no network and no API key needed."""
    company = Company("Acme", "acme.test", "https://acme.test/")
    fetcher = make_fetcher(tmp_path, {})
    adapter = SearchAdapter(fetcher)
    queries = build_queries(company)
    stale = fetcher.write_text("search", "https://x.com/acme", "cached before exclusion")
    for q in queries:
        key = hashlib.sha256(q.encode("utf-8")).hexdigest()[:20]
        path = fetcher.cache_dir / "search" / "_queries" / f"{key}.json"
        results = [
            {"url": "https://x.com/acme", "raw_content": "tweet text " * 20},
            {"url": "https://news.test/acme-cto", "raw_content": "Acme names Jane Doe CTO. " * 10},
        ]
        data = {
            "query": q,
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "response": {"results": results},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    report = adapter.discover(company)
    assert adapter.api_calls == 0
    assert [d.url for d in report.documents] == ["https://news.test/acme-cto"]
    assert {"url": "https://x.com/acme", "reason": "excluded domain (ToS)"} in report.skipped
    assert (
        not (tmp_path / stale).exists()
        and not fetcher.text_path("search", "https://x.com/acme").exists()
    )
    cached = (fetcher.cache_dir / "search" / "_queries").glob("*.json")
    assert all("tweet text" not in p.read_text(encoding="utf-8") for p in cached)


def test_build_queries_disambiguate_with_site_host():
    company = Company("Campfire", "meetcampfire.com", "https://campfire.ai/")
    queries = build_queries(company)
    assert all(q.endswith(" campfire.ai") for q in queries)
    assert '"Campfire" "head of" campfire.ai' in queries
