"""Per-company collection settings (data/companies.yaml) and how `collect` applies them."""

import pytest
from typer.testing import CliRunner

from leadership import cli
from leadership.config import company_settings
from leadership.sources.company_site import MAX_PAGES


def test_committed_config_reproduces_the_committed_runs():
    assert company_settings("robinhood.com") == {"ticker": "HOOD"}
    assert company_settings("meetcampfire.com") == {"private": True, "max_pages": 120}
    assert company_settings("example.com") == {}


def test_bad_setting_is_rejected(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("x.com:\n  max_pages: lots\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_pages"):
        company_settings("x.com", path)
    path.write_text("x.com:\n  tikker: X\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tikker"):
        company_settings("x.com", path)
    path.write_text("x.com: {}\ny.com:\n  private: maybe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="y.com"):  # every company is checked
        company_settings("x.com", path)
    for bad in ("- x.com\n", "x.com: just-a-string\n"):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            company_settings("x.com", path)


def _collect_kwargs(monkeypatch, *args):
    """Run `leadership collect ...` and return what it passed to the pipeline."""
    seen = {}

    def fake_collect(url, **kwargs):
        seen.update(kwargs, url=url)
        raise ValueError("stop here")  # the CLI reports it and exits 1; nothing is fetched

    monkeypatch.setattr("leadership.pipeline.collect.collect", fake_collect)
    result = CliRunner().invoke(cli.app, ["collect", *args])
    assert result.exit_code == 1 and "stop here" in result.output
    return seen


def test_bare_collect_reads_the_config(monkeypatch):
    rh = _collect_kwargs(monkeypatch, "robinhood.com")
    assert (rh["ticker"], rh["private"], rh["max_pages"]) == ("HOOD", False, MAX_PAGES)
    cf = _collect_kwargs(monkeypatch, "https://www.meetcampfire.com/")
    assert (cf["ticker"], cf["private"], cf["max_pages"]) == (None, True, 120)


def test_flags_override_the_config(monkeypatch):
    cf = _collect_kwargs(monkeypatch, "meetcampfire.com", "--public", "--max-pages", "10")
    assert (cf["private"], cf["max_pages"]) == (False, 10)
    # An explicit --ticker overrides a configured private: true (unless --private is given too).
    cf = _collect_kwargs(monkeypatch, "meetcampfire.com", "--ticker", "XYZ")
    assert (cf["ticker"], cf["private"]) == ("XYZ", False)
    cf = _collect_kwargs(monkeypatch, "meetcampfire.com", "--ticker", "XYZ", "--private")
    assert cf["private"] is True
    other = _collect_kwargs(monkeypatch, "example.com")
    assert (other["ticker"], other["private"], other["max_pages"]) == (None, False, MAX_PAGES)
