"""Command-line interface: `uv run leadership --help`."""

from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from leadership.config import MissingSettingError
from leadership.sources.company_site import MAX_PAGES

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()


@app.callback()
def main() -> None:
    """Company leadership collection and chat."""


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def discover(
    url: str = typer.Argument(..., help="Company domain or URL, e.g. robinhood.com"),
    name: str | None = typer.Option(None, help="Company name (default: read from the site)"),
    ticker: str | None = typer.Option(None, help="Stock ticker, if public (default: look up)"),
    private: bool = typer.Option(False, help="Skip the SEC lookup"),
    source: list[str] | None = typer.Option(None, help="Only these sources (repeatable)"),  # noqa: B008
    max_pages: int = typer.Option(MAX_PAGES, help="Company-site pages to fetch (top-ranked)"),
    refresh: bool = typer.Option(False, help="Ignore the cache and re-fetch"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Step 1 only: find and cache source documents (no LLM)."""
    from leadership.pipeline.discover import discover as run_discover
    from leadership.pipeline.discover import fetcher_for, normalize_domain, resolve_company
    from leadership.sources.fetch import FetchBlocked

    _setup_logging(verbose)
    try:
        with fetcher_for(normalize_domain(url), refresh) as fetcher:
            company = resolve_company(fetcher, url, name, ticker, private)
            status = (
                f"public ({company.ticker}, CIK {company.cik})" if company.is_public else "private"
            )
            console.print(
                f"[bold]{company.name}[/] · {company.domain} → {company.site_url} · {status}"
            )
            result = run_discover(company, fetcher, tuple(source) if source else None, max_pages)
    except MissingSettingError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    except (FetchBlocked, RuntimeError, ValueError) as exc:
        console.print(f"[red]Discovery failed: {exc}[/]")
        raise typer.Exit(1) from exc

    table = Table("source", "documents", "skipped", "notes")
    for r in result.reports:
        table.add_row(
            r.source_type,
            str(result.stats["documents_by_source"][r.source_type]),
            str(len(r.skipped)),
            "; ".join(r.notes[:3]) + (" …" if len(r.notes) > 3 else ""),
        )
    console.print(table)
    console.print(
        f"network requests: {result.stats['network_requests']} · "
        f"search API calls: {result.stats['search_api_calls']} · "
        f"manifest: data/{company.domain}/raw/manifest.json"
    )


@app.command()
def collect(
    url: str = typer.Argument(..., help="Company domain or URL, e.g. robinhood.com"),
    name: str | None = typer.Option(None, help="Company name (default: read from the site)"),
    ticker: str | None = typer.Option(None, help="Stock ticker, if public (default: look up)"),
    private: bool = typer.Option(False, help="Skip the SEC lookup"),
    max_pages: int = typer.Option(MAX_PAGES, help="Company-site pages to fetch (top-ranked)"),
    refresh: bool = typer.Option(False, help="Ignore the fetch cache and re-fetch"),
    reextract: bool = typer.Option(False, help="Ignore cached LLM extractions"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Full run: discover, extract with the LLM, validate, resolve, store."""
    from leadership.pipeline.collect import collect as run_collect
    from leadership.sources.fetch import FetchBlocked

    _setup_logging(verbose)
    try:
        result = run_collect(
            url,
            name=name,
            ticker=ticker,
            private=private,
            max_pages=max_pages,
            refresh=refresh,
            reextract=reextract,
        )
    except MissingSettingError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    except (FetchBlocked, RuntimeError, ValueError) as exc:
        console.print(f"[red]Collection failed: {exc}[/]")
        raise typer.Exit(1) from exc

    s = result.stats
    table = Table("name", "current roles", "confidence", "sources")
    for p in result.resolution.people:
        current = list(dict.fromkeys(r.title for r in p.roles if r.is_current))
        if current:
            table.add_row(p.full_name, "; ".join(current), p.confidence,
                          str(len({c.doc.url for c in p.claims})))  # fmt: skip
    console.print(table)
    former = [p.full_name for p in result.resolution.people if not p.is_current]
    if former:
        console.print(f"Not current (kept for history): {', '.join(former)}")
    console.print(
        f"documents {s['documents']} · sent to LLM {s['documents_sent_to_llm']} · "
        f"LLM calls {s['llm_calls']} (cached {s['llm_cached_responses']}) · "
        f"tokens {s['tokens_in']:,} in / {s['tokens_out']:,} out · cost ${s['cost_usd']} "
        f"(incl. cached: ${s['cost_usd_including_cached']})"
    )
    console.print(
        f"claims accepted {s['claims_accepted']} · rejected {s['claims_rejected']} · "
        f"people {s['people_found']} ({s['people_current']} current) · "
        + " · ".join(f"{k} {len(v)}" for k, v in sorted(s["review"].items()))
    )
    if s["extraction_errors"]:
        console.print(f"[yellow]{len(s['extraction_errors'])} extraction errors (see stats)[/]")
    console.print(f"wrote {result.db_path.as_posix()} and {result.json_path.name}")


@app.command("validation-report")
def validation_report(
    domains: list[str] = typer.Argument(  # noqa: B008
        None, help="Company domains (default: robinhood.com meetcampfire.com)"
    ),
) -> None:
    """Regenerate docs/VALIDATION.md from the committed data and docs/validation/ reports."""
    from leadership.validation_report import build

    path = build(domains or ["robinhood.com", "meetcampfire.com"])
    console.print(f"wrote {path}")


if __name__ == "__main__":
    app()
