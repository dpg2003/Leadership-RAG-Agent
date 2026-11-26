"""Command-line interface: `uv run leadership --help`."""

from __future__ import annotations

import logging
from typing import Any

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from leadership.config import COMPANIES_PATH, REPO_ROOT, MissingSettingError, company_settings
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


def company_options(
    url: str, name: str | None, ticker: str | None, private: bool | None, max_pages: int | None
) -> dict[str, Any]:
    """Flags override data/companies.yaml, which overrides the built-in defaults."""
    from leadership.pipeline.discover import normalize_domain

    flags = {"name": name, "ticker": ticker, "private": private, "max_pages": max_pages}
    configured = company_settings(normalize_domain(url))
    if ticker is not None and private is None:
        flags["private"] = False  # an explicit --ticker means a public lookup, over the file
    opts = {"name": None, "ticker": None, "private": False, "max_pages": MAX_PAGES}
    opts |= configured | {k: v for k, v in flags.items() if v is not None}
    used = {k: v for k, v in configured.items() if flags[k] is None}
    if used:
        shown = ", ".join(f"{k}={v}" for k, v in used.items())
        console.print(f"Settings from {COMPANIES_PATH.relative_to(REPO_ROOT).as_posix()}: {shown}")
    return opts


@app.command()
def discover(
    url: str = typer.Argument(..., help="Company domain or URL, e.g. robinhood.com"),
    name: str | None = typer.Option(None, help="Company name (default: read from the site)"),
    ticker: str | None = typer.Option(None, help="Stock ticker, if public (default: look up)"),
    private: bool | None = typer.Option(None, "--private/--public", help="Skip the SEC lookup"),
    source: list[str] | None = typer.Option(None, help="Only these sources (repeatable)"),  # noqa: B008
    max_pages: int | None = typer.Option(
        None, help=f"Company-site pages to fetch, top-ranked (default {MAX_PAGES})"
    ),
    refresh: bool = typer.Option(False, help="Ignore the cache and re-fetch"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Step 1 only: find and cache source documents (no LLM)."""
    from leadership.pipeline.discover import discover as run_discover
    from leadership.pipeline.discover import fetcher_for, normalize_domain, resolve_company
    from leadership.sources.fetch import FetchBlocked

    _setup_logging(verbose)
    try:
        o = company_options(url, name, ticker, private, max_pages)
        with fetcher_for(normalize_domain(url), refresh) as fetcher:
            company = resolve_company(fetcher, url, o["name"], o["ticker"], o["private"])
            status = (
                f"public ({company.ticker}, CIK {company.cik})" if company.is_public else "private"
            )
            console.print(
                f"[bold]{company.name}[/] · {company.domain} → {company.site_url} · {status}"
            )
            result = run_discover(
                company, fetcher, tuple(source) if source else None, o["max_pages"]
            )
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
    private: bool | None = typer.Option(None, "--private/--public", help="Skip the SEC lookup"),
    max_pages: int | None = typer.Option(
        None, help=f"Company-site pages to fetch, top-ranked (default {MAX_PAGES})"
    ),
    refresh: bool = typer.Option(False, help="Ignore the fetch cache and re-fetch"),
    reextract: bool = typer.Option(False, help="Ignore cached LLM extractions"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Full run: discover, extract with the LLM, validate, resolve, store."""
    from leadership.pipeline.collect import collect as run_collect
    from leadership.sources.fetch import FetchBlocked

    _setup_logging(verbose)
    try:
        opts = company_options(url, name, ticker, private, max_pages)
        result = run_collect(url, **opts, refresh=refresh, reextract=reextract)
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


@app.command()
def chat(
    domain: str | None = typer.Argument(None, help="Company domain, e.g. robinhood.com"),
    question: list[str] | None = typer.Option(  # noqa: B008
        None, "--question", "-q", help="Ask these in order (one conversation), then exit"
    ),
    steps: bool = typer.Option(True, help="Show the tool calls behind each answer"),
) -> None:
    """Chat about a company's leadership, answered only from the collected data."""
    import anthropic
    from rich.markdown import Markdown

    from leadership.chat.agent import ChatAgent

    try:
        agent = ChatAgent(domain)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    def answer(q: str) -> None:
        try:
            reply = agent.ask(q)
        except MissingSettingError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc
        except (RuntimeError, anthropic.APIError) as exc:
            console.print(f"[red]{exc}[/]")
            return
        if steps:
            for s in reply.steps:
                args = ", ".join(f"{k}={v!r}" for k, v in s.input.items())
                console.print(f"[dim]  → {s.tool}({args}){' [error]' if s.is_error else ''}[/]")
        console.print(Markdown(reply.text, hyperlinks=False))
        if reply.sources:
            console.print("[dim]  Sources:[/]")
            for url in reply.sources:
                console.print(f"[dim]  - {url}[/]", soft_wrap=True)
        console.print(
            f"[dim]  {reply.prompt_tokens:,} tokens in ({reply.cache_read_tokens:,} cached) / "
            f"{reply.output_tokens:,} out · "
            f"${reply.cost_usd}[/]\n"
        )

    hint = "" if question else " · Ctrl+C or 'exit' to quit"
    console.print(
        f"[bold]Leadership chat[/] · {domain or 'no company selected'} · model {agent.model}{hint}"
    )
    if question:
        for q in question:
            console.print(f"[bold cyan]> {q}[/]")
            answer(q)
        return
    while True:
        try:
            q = console.input("[bold cyan]> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"exit", "quit"}:
            break
        if q:
            answer(q)


@app.command("eval")
def eval_cmd(
    domain: str | None = typer.Argument(
        None, help="Run this company's cases plus the no-company cases (default: all)"
    ),
    case: list[str] | None = typer.Option(None, "--case", "-c", help="Only these case ids"),  # noqa: B008
    workers: int = typer.Option(4, help="Cases run in parallel"),
    write: bool | None = typer.Option(
        None, help="Write eval/results.json and eval/RESULTS.md (default: only for a full run)"
    ),
) -> None:
    """Run eval/questions.yaml through the real chat agent and print pass/fail."""
    from leadership.config import llm_model, require_setting
    from leadership.evaluate import load_cases, run, select, write_results

    try:
        require_setting("ANTHROPIC_API_KEY")  # fail before running, so no results are written
    except MissingSettingError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    try:
        cases = select(load_cases(), domain, case)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    model = llm_model()
    console.print(f"Running {len(cases)} eval cases with {model} …")
    try:
        results = run(cases, workers=workers)
    except MissingSettingError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    table = Table("case", "kind", "result", "why", "cost")
    for r in results:
        table.add_row(
            r.id, r.kind, "[green]PASS[/]" if r.passed else "[red]FAIL[/]",
            "; ".join(r.failures)[:120], f"${r.cost_usd}",
        )  # fmt: skip
    console.print(table)
    passed = sum(r.passed for r in results)
    unverified = sum(not r.verified_by for r in results)
    cost = round(sum(r.cost_usd or 0 for r in results), 4)
    console.print(
        f"[bold]{passed}/{len(results)} passed[/] · cost ${cost}"
        + (f" · {unverified} expected answers not yet hand-verified" if unverified else "")
    )
    if write is None:  # a partial run never replaces the committed full results
        write = not domain and not case
    if write:
        console.print(f"wrote {write_results(results, model).relative_to(REPO_ROOT).as_posix()}")
    if passed < len(results):
        raise typer.Exit(1)


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


UI_APP = REPO_ROOT / "src" / "leadership" / "ui" / "app.py"


def ui_command(port: int) -> list[str]:
    """The Streamlit command line. Headless, so the first launch never stops at Streamlit's
    interactive email prompt or opens a browser; no usage stats are sent."""
    import sys

    return [sys.executable, "-m", "streamlit", "run", str(UI_APP), "--server.port", str(port),
            "--server.headless", "true", "--browser.gatherUsageStats", "false"]  # fmt: skip


@app.command()
def ui(port: int = typer.Option(8501, help="Port to serve on")) -> None:
    """Start the web UI (Streamlit): chat with sources and steps, and the leadership table."""
    import subprocess

    console.print()
    console.print(
        f"[bold]Leadership UI:[/] open [bold cyan]http://localhost:{port}[/] in your browser"
        "  (Ctrl+C to stop)"
    )
    console.print()
    try:
        # From the repo root, so Streamlit picks up .streamlit/config.toml (theme, no tracebacks).
        code = subprocess.call(ui_command(port), cwd=REPO_ROOT)
    except KeyboardInterrupt:
        code = 0
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
