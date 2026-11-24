"""Builds docs/VALIDATION.md from the committed data and the subagent reports (no LLM).

Per company: every stored person with their best evidence, the pipeline's own review lists,
the source-verifier's verdict ("Agent verdict"), and an empty "Verified by Daniel" column
for the hand-check. Hand-written sections (Known misses, notes) live in
docs/validation/<domain>-notes.md and are included verbatim. Daniel's ticks and notes in the
"Verified by Daniel" column are carried over on regeneration, keyed by company, table and
name, so re-running after the hand-check never loses them (D25).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from leadership.config import REPO_ROOT

DOCS = REPO_ROOT / "docs"
log = logging.getLogger(__name__)
VERDICT_RANK = {"UNSUPPORTED": 0, "PARTIAL": 1, "STALE": 2, "SOURCE_UNAVAILABLE": 3, "SUPPORTED": 4}
DANIEL = "✅ / ❌ / note"
# The collection the source-verifier reports were made from (update when it is re-run).
VERIFIER_RUN = "from the collection in commit 60a6431, plus an addendum for the people added since"

REVIEW_TITLES = {
    "seat_conflicts": "Seat conflicts (two current holders of one C-suite seat; not decided)",
    "conflicts": "Conflicts between sources",
    "possible_duplicates": "Possible duplicates (not merged)",
    "merged_by_nickname_rule": "Merged by the nickname rule (D6)",
    "from_blocked_host": "From a bot-blocked company host: check in your browser (D13)",
    "reanchored": "Evidence re-anchored from the source text (D22)",
    "stale_single_source": "Single source older than 18 months (low confidence)",
    "c_level_not_in_proxy_undated": "C-level title not in the latest proxy, source undated",
    "superseded_by_proxy": "Retired by the proxy rule (D16)",
    "superseded_by_promotion": "Superseded by a later title",
    "founder_historical": "Founders without a current operating role (D23)",
}


def _cell(text: Any, limit: int = 160) -> str:
    s = " ".join(str(text if text is not None else "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def parse_verifier(path: Path) -> dict[str, list[tuple[str, str, str]]]:
    """person -> [(field/value, verdict, reason)] from the verifier's Records table."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    if not path.exists():
        return out
    header: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            header = None if not line.strip() else header
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            lowered = [c.lower() for c in cells]
            if "verdict" in lowered and "person" in lowered:
                # "Reason (1 line)" -> "reason": match columns by their first word.
                header = [re.sub(r"\s*\(.*\)$", "", c) for c in lowered]
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        row = dict(zip(header, cells, strict=False))
        verdict = re.sub(r"[^A-Z_]", "", row.get("verdict", "").upper())
        if verdict in VERDICT_RANK and row.get("person"):
            what = f"{row.get('field', '')}: {row.get('stored value', '')}".strip(": ")
            out.setdefault(row["person"], []).append((what, verdict, row.get("reason", "")))
    return out


def _agent_verdict(rows: list[tuple[str, str, str]] | None) -> str:
    if not rows:
        return "not checked"
    worst = min(rows, key=lambda r: VERDICT_RANK[r[1]])
    counts = {v: sum(r[1] == v for r in rows) for v in VERDICT_RANK}
    summary = ", ".join(f"{n} {v}" for v, n in counts.items() if n)
    if worst[1] == "SUPPORTED":
        return summary
    return f"{summary}. Worst: {worst[0]}: {worst[2]}"


def _excerpt(snippet: str, name: str, limit: int = 220) -> str:
    """The evidence, shortened around the person's name so the name is always visible."""
    text = " ".join(snippet.split())
    if len(text) <= limit:
        return _cell(text, limit)
    last = name.split()[-1] if name.split() else ""
    at = text.find(last) if last else -1
    start = max(0, min(at - limit // 3, len(text) - limit)) if at >= 0 else 0
    piece = ("…" if start else "") + text[start : start + limit]
    piece += "…" if start + limit < len(text) else ""
    return _cell(piece, limit + 2)


def _evidence(fact: dict | None, name: str) -> str:
    if not fact:
        return ""
    return f'"{_excerpt(fact["evidence_snippet"], name)}" [source]({fact["source"]["url"]})'


def _location(person: dict) -> str:
    if not person.get("location"):
        return "not found"
    for f in person["facts"]:
        if f["field"] == "location" and f["value"] == person["location"]:
            return f"{_cell(person['location'])} ([source]({f['source']['url']}))"
    return _cell(person["location"])


def _former_fact(person: dict) -> dict | None:
    """Prefer a fact whose source called the role former; else the newest dated title."""
    titles = [f for f in person["facts"] if f["field"] == "title"]
    pool = [f for f in titles if f["is_current"] is False] or titles
    return max(pool, key=lambda f: f["source"]["published_at"] or "", default=None)


def _manual(person: dict) -> list[str]:
    """Hand-curated flags (D28), e.g. 'reported departing (secondary source, unconfirmed)'."""
    return [
        f"FLAG: {f['flag']} ([source]({f['source_url']}), {f.get('source_date', 'undated')})"
        for f in person.get("review_flags", [])
    ]


# Words in the verifier's reason that mean "is this still current?" rather than "is the
# evidence right?" or "is the title classified right?". A heuristic, labelled as such.
_CURRENCY = re.compile(
    r"not current|is_current|still current|currently|no longer|stale|proxy|inferred|"
    r"\bformer\b|\bleft\b|depart|separat|18 months|outdated|absent|retired|old source|"
    r"\b(?:19|20)\d\d source\b|nothing (?:shows|establishes)",
    re.IGNORECASE,
)
_CLASSIFICATION = re.compile(
    r"function|seniority|classif|c_level|\bhead\b|\bvp\b|stored title|title is|older title",
    re.IGNORECASE,
)


def verifier_summary(
    verdicts: dict[str, list[tuple[str, str, str]]], caveat: str = ""
) -> list[str]:
    """Split the verifier's verdicts so a low 'precision' is explained: was the evidence
    wrong, or is only the currency (or the classification) disputed?"""
    rows = [r for rs in verdicts.values() for r in rs]
    if not rows:
        return ["Source-verifier: no report."]
    supported = [r for r in rows if r[1] == "SUPPORTED"]
    rest = [r for r in rows if r[1] != "SUPPORTED"]
    # Classification first: "title and company correct and current; function should be
    # sales" is a classification dispute, not a currency one.
    classification = [r for r in rest if _CLASSIFICATION.search(r[2])]
    currency = [r for r in rest if r not in classification and _CURRENCY.search(r[2])]
    unavailable = [r for r in rest if r[1] in ("SOURCE_UNAVAILABLE", "STALE") and r not in currency]
    evidence = [r for r in rest if r not in currency + classification + unavailable]
    n = len(rows)
    return [
        f"Source-verifier, {n} records{caveat}:",
        "",
        f"- **Evidence supported** (the quoted evidence is in the source and states this "
        f"person and title): {n - len(evidence)} of {n}.",
        f"  - fully SUPPORTED (evidence, classification and currency all agree): {len(supported)}",
        f"  - **currency disputed** (evidence fine; the verifier doubts the role is still "
        f"current, or that it ended, e.g. the proxy and 18-month rules): {len(currency)}",
        f"  - classification or displayed title disputed: {len(classification)}",
        f"  - live source unavailable: {len(unavailable)}",
        f"- **Evidence not establishing the claim:** {len(evidence)}"
        + (": " + "; ".join(_cell(f"{r[0]} ({r[2]})", 120) for r in evidence) if evidence else ""),
        "",
        "The split is a keyword heuristic over the verifier's one-line reasons; the per-person"
        " verdicts are in the tables.",
    ]


def _flags(name: str, review: dict[str, list[dict]]) -> list[str]:
    flags = []
    for key, entries in review.items():
        for e in entries:
            names = e.get("names") or [h.get("person") for h in e.get("holders", [])]
            if e.get("person") == name or name in (names or []):
                flags.append(key)
                break
    return flags


def _best_title_fact(person: dict) -> dict | None:
    titles = [f for f in person["facts"] if f["field"] == "title"]
    current = [r["title_raw"] for r in person["roles"] if r["is_current"]]
    for f in titles:
        if f["value"] in current and f["is_current"] is not False:
            return f
    return titles[0] if titles else None


def company_section(domain: str) -> str:
    data = json.loads((REPO_ROOT / "data" / domain / "leadership.json").read_text("utf-8"))
    stats, review = data["collection"], data["collection"].get("review", {})
    verdicts = parse_verifier(DOCS / "validation" / f"{domain}-verifier.md")
    people = data["people"]
    current = [p for p in people if any(r["is_current"] for r in p["roles"])]
    former = [p for p in people if p not in current]

    out = [f"## {data['company'].get('name', domain)} ({domain})", ""]
    out.append(
        f"Documents: {stats.get('documents')} ({stats.get('documents_sent_to_llm')} sent to the "
        f"LLM) · claims accepted {stats.get('claims_accepted')}, rejected "
        f"{stats.get('claims_rejected')} · people {len(people)} ({len(current)} current) · "
        f"extraction cost ${stats.get('cost_usd_including_cached')} · model {stats.get('model')}"
    )
    unchecked = [p["full_name"] for p in people if p["full_name"] not in verdicts]
    caveat = (
        f" ({VERIFIER_RUN}"
        + (f"; {len(unchecked)} people added since are not checked" if unchecked else "")
        + ")"
    )
    out += ["", *verifier_summary(verdicts, caveat)]
    out += ["", f"### Current leadership ({len(current)})", ""]
    out.append(
        "| # | Name | Current role(s) | Seniority / function | Confidence | Sources | "
        "Evidence (best) | Location | Review flags | Agent verdict | Verified by Daniel |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, p in enumerate(current, 1):
        roles = [r for r in p["roles"] if r["is_current"]]
        evidence = _evidence(_best_title_fact(p), p["full_name"])
        out.append(
            f"| {i} | {_cell(p['full_name'])} | "
            f"{_cell('; '.join(r['title_raw'] for r in roles))} | "
            f"{_cell('; '.join(r['seniority'] + '/' + r['function'] for r in roles))} | "
            f"{p['confidence']} | {p['source_count']} | {evidence} | {_location(p)} | "
            f"{_cell(', '.join(_flags(p['full_name'], review) + _manual(p)), 300)} | "
            f"{_cell(_agent_verdict(verdicts.get(p['full_name'])), 220)} | {DANIEL} |"
        )
    out += ["", f"### Not current (kept for history) ({len(former)})", ""]
    out.append(
        "| # | Name | Role(s) | Why not current | As of | Evidence | Agent verdict | "
        "Verified by Daniel |"
    )
    out.append("|---|---|---|---|---|---|---|---|")
    for i, p in enumerate(former, 1):
        out.append(
            f"| {i} | {_cell(p['full_name'])} | "
            f"{_cell('; '.join(r['title_raw'] for r in p['roles']))} | "
            f"{_cell('; '.join(sorted({r['note'] or 'former' for r in p['roles']})))} | "
            f"{_cell(max((r['as_of'] or '' for r in p['roles']), default='') or 'undated')} | "
            f"{_evidence(_former_fact(p), p['full_name'])} | "
            f"{_cell(_agent_verdict(verdicts.get(p['full_name'])), 220)} | {DANIEL} |"
        )

    out += ["", "### Review lists from the pipeline", ""]
    for key, title in REVIEW_TITLES.items():
        entries = review.get(key)
        if not entries:
            if key == "possible_duplicates":  # always shown: "none" is a result too
                out += [f"**{title}** (0): none found by the pipeline's identity rule.", ""]
            continue
        out += [f"**{title}** ({len(entries)})", ""]
        for e in entries:
            out.append(f"- {_cell(_describe(key, e), 400)}")
        out.append("")

    stored = {p["full_name"] for p in people}
    rejected: dict[str, list[dict]] = {}
    for r in stats.get("rejections", []):
        rejected.setdefault(r["full_name"], []).append(r)
    dropped = {n: rs for n, rs in rejected.items() if n not in stored and not _merged(n, stored)}
    out += [f"### People with every claim rejected ({len(dropped)})", ""]
    out.append("| Name | Titles claimed | Reason(s) | Example source | Verified by Daniel |")
    out.append("|---|---|---|---|---|")
    for n, rs in sorted(dropped.items()):
        out.append(
            f"| {_cell(n)} | {_cell('; '.join(sorted({r['title'] for r in rs})))} | "
            f"{_cell('; '.join(sorted({r['reason'] for r in rs})))} | [{len(rs)} claim(s)]"
            f"({rs[0]['url']}) | {DANIEL} |"
        )
    notes = DOCS / "validation" / f"{domain}-notes.md"
    if notes.exists():
        out += ["", notes.read_text(encoding="utf-8").strip()]
    return "\n".join(out) + "\n"


def _merged(name: str, stored: set[str]) -> bool:
    """A rejected name that is the same person (or a possible duplicate) as a stored one,
    e.g. "Jeffrey Pinner" vs "Jeff Pinner": not a dropped person."""
    from leadership.pipeline.resolve import Identity, NameRecord, compare_identity

    a = NameRecord(0, name)
    return any(compare_identity(a, NameRecord(0, s)) is not Identity.DIFFERENT for s in stored)


def _describe(key: str, e: dict) -> str:
    if key == "seat_conflicts":
        holders = "; ".join(
            f"{h['person']} ({h['title']}, as of {h['as_of'] or 'undated'})" for h in e["holders"]
        )
        return f"{e.get('seat')}: {holders}"
    if "names" in e:
        return " / ".join(e["names"])
    parts = [e.get("person", ""), e.get("title", ""), e.get("field", "")]
    if "values" in e:
        parts.append(" vs ".join(e["values"]))
    if e.get("note"):
        parts.append(e["note"])
    if "evidence_snippet" in e:
        parts.append(f'"{e["evidence_snippet"]}"')
    if "only_source" in e:
        parts.append("only source" if e["only_source"] else "also has other sources")
    urls = e.get("urls") or ([e["url"]] if e.get("url") else [])
    parts.append(" ".join(urls[:3]) + (f" (+{len(urls) - 3} more)" if len(urls) > 3 else ""))
    return " · ".join(p for p in parts if p)


HEADER = r"""# Validation

Generated by `leadership validation-report` from `data/<domain>/leadership.json` and the
subagent reports in `docs/validation/`. **Agent verdict** is the source-verifier's verdict
on that person's facts, an input, not an approval. **Verified by Daniel** is for the
hand-check: ✅ correct, ❌ wrong, or a note.

How to hand-check: open the evidence link, find the quoted snippet, and confirm the person,
the title, and that it is current. Records from investors.robinhood.com must be checked in a
browser (the host blocks automated clients; D13). A `|` inside a note is fine: it is kept
in the note and written back escaped as `\|`.
"""


_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _split_row(line: str, width: int | None) -> list[str]:
    r"""Cells of a markdown table row. `\|` is a literal pipe. If a hand-written note in the
    last column contains a bare `|`, the extra pieces are joined back into that cell."""
    cells = [c.strip() for c in _UNESCAPED_PIPE.split(line.strip().strip("|"))]
    if width and len(cells) > width:
        cells = cells[: width - 1] + [" | ".join(cells[width - 1 :])]
    return cells


def _row_keys(text: str):
    """Yield (company, table, name, line_index, cells) for rows of tables that have a
    'Verified by Daniel' column. The table heading is stripped of its count."""
    company = table = ""
    header: list[str] | None = None
    for i, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            company = line
        elif line.startswith("### "):
            table = re.sub(r"\s*\(\d+\)$", "", line)
        if not line.startswith("|"):
            header = None
            continue
        if header is None:
            header = _split_row(line, None)
            continue
        cells = _split_row(line, len(header))
        if header[-1] != "Verified by Daniel" or set("".join(cells)) <= set("-: "):
            continue
        name = next((c for c in cells if c and not c.isdigit()), "")
        yield company, table, name, i, cells


def _escape_pipes(text: str) -> str:
    return _UNESCAPED_PIPE.sub(r"\\|", text)


def carry_over_ticks(old: str, new: str) -> str:
    """Put Daniel's edited cells from the old file into the regenerated one.

    Matched by (company, table, name); if the person moved to another table (e.g. from
    current to not current after a fix), by (company, name) when that name is unique.
    A note that can't be placed is logged, never silently dropped from the log."""
    exact: dict[tuple[str, str, str], str] = {}
    by_name: dict[tuple[str, str], list[str]] = {}
    for company, table, name, _, cells in _row_keys(old):
        if cells[-1] != DANIEL:
            exact[(company, table, name)] = cells[-1]
            by_name.setdefault((company, name), []).append(cells[-1])
    lines = new.splitlines()
    used: set[tuple[str, str]] = set()
    rows = list(_row_keys(new))
    new_names: dict[tuple[str, str], int] = {}
    for company, _, name, _, _ in rows:
        new_names[(company, name)] = new_names.get((company, name), 0) + 1
    for company, table, name, i, cells in rows:
        if cells[-1] != DANIEL:
            continue
        value = exact.get((company, table, name))
        if value is None and new_names[(company, name)] == 1:
            candidates = by_name.get((company, name), [])
            value = candidates[0] if len(candidates) == 1 else None
        if value is not None:
            lines[i] = lines[i].rstrip()[: -len(f" {DANIEL} |")] + f" {_escape_pipes(value)} |"
            used.add((company, name))
    for company, name in sorted(set(by_name) - used):
        log.warning("hand-check note not carried over: %s / %s -> %s", company, name,
                    by_name[(company, name)])  # fmt: skip
    return "\n".join(lines) + "\n"


def build(domains: list[str]) -> Path:
    text = HEADER + "".join("\n" + company_section(d) for d in domains)
    path = DOCS / "VALIDATION.md"
    if path.exists():
        text = carry_over_ticks(path.read_text(encoding="utf-8"), text)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
