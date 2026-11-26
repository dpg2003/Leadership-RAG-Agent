"""Streamlit UI: `leadership ui`. Presentation only. Every answer comes from the Phase 5 chat
agent, and every table and number from the chat tools (leadership.chat.tools)."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
import pandas as pd
import streamlit as st

from leadership.chat import tools
from leadership.chat.agent import ChatAgent, Reply, Step
from leadership.config import ENV_PATH, MissingSettingError, get_setting

log = logging.getLogger("leadership.ui")

SOURCE_LABELS = {
    "company_site": "Company site",
    "search": "Web search",
    "sec_filing": "SEC filings",
}
SENIORITY_LABELS = {"c_level": "C-level", "vp": "VP", "head": "Head", "founder": "Founder"}
GROUP_ORDER = ["c_level", "vp", "head", "founder"]
CONFIDENCE_ORDER = ["high", "medium", "low"]
# Suggested questions on an empty chat: the eval's single-question example cases.
MAX_SUGGESTIONS = 5


# ------------------------------------------------------------------ data (cached reads)


@st.cache_data(show_spinner=False)
def companies() -> list[dict[str, Any]]:
    return tools.available_companies()


@st.cache_data(show_spinner=False)
def last_run(domain: str) -> dict[str, Any]:
    return tools.last_run(domain)


@st.cache_data(show_spinner=False)
def leaders(domain: str) -> dict[str, Any]:
    return tools.list_leaders(domain, include_former=True)


@st.cache_data(show_spinner=False)
def suggestions(domain: str) -> list[str]:
    from leadership.evaluate import load_cases

    seen: list[str] = []
    for c in load_cases():
        single = c.get("company") == domain and c["kind"] == "example" and len(c["turns"]) == 1
        if single and c["turns"][0] not in seen:
            seen.append(c["turns"][0])
    return seen[:MAX_SUGGESTIONS]


# ------------------------------------------------------------------ helpers


def _call(step: Step) -> str:
    args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in step.input.items())
    return f"{step.tool}({args})"


def _primary(roles: list[dict[str, Any]]) -> dict[str, Any]:
    """The most senior current role, used to group a person (a founder row sorts last)."""
    return min(roles, key=lambda r: GROUP_ORDER.index(r["seniority"]))


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, MissingSettingError):
        return f"Chat needs an Anthropic API key: {exc}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API. Check your connection and try again."
    if isinstance(exc, anthropic.RateLimitError):
        return "The Anthropic API is rate-limiting requests right now. Wait a moment and retry."
    if isinstance(exc, anthropic.APIStatusError):
        return f"The Anthropic API returned an error (HTTP {exc.status_code}). Please try again."
    if isinstance(exc, RuntimeError):  # e.g. a rejected key, or no answer within the tool budget
        return f"Couldn't answer that: {exc}."
    return "Something went wrong while answering. Please try again."


# ------------------------------------------------------------------ sidebar


def sidebar() -> dict[str, Any] | None:
    found = companies()
    with st.sidebar:
        st.markdown("### Leadership Agent")
        if not found:
            st.warning("No collected data found under data/. Run `leadership collect <domain>`.")
            return None
        company = st.selectbox(
            "Company",
            found,
            format_func=lambda c: f"{c['name']} ({c['domain']})",
            key="company",
        )
        run = last_run(company["domain"])
        st.caption(
            ("Public" + (f" · {company['ticker']}" if company.get("ticker") else ""))
            if company["is_public"]
            else "Private company"
        )
        st.markdown("**Last collection run**")
        when = (run["collected_at"] or "unknown")[:10]
        st.caption(f"{when} · model {run['model'] or 'n/a'}")
        a, b = st.columns(2)
        a.metric("People found", run["people_found"] or 0)
        b.metric("Current", run["people_current"] or 0)
        st.markdown("**Sources by type**")
        for key, label in SOURCE_LABELS.items():
            st.caption(f"{label}: {run['documents_by_source'].get(key, 0)}")
        cost = run["extraction_cost_usd"]
        if cost is not None:
            spent = run["run_cost_usd"] or 0
            st.caption(
                f"Extraction cost: ${cost:.2f}"
                + (" (last run served from cache: $0.00)" if not spent else f" · run ${spent:.2f}")
            )
        st.divider()
        if st.button("New conversation", width="stretch"):
            st.session_state.pop(f"chat:{company['domain']}", None)
            st.rerun()
    return company


# ------------------------------------------------------------------ chat tab


def _render_answer(msg: dict[str, Any]) -> None:
    st.markdown(msg["text"])
    details = msg.get("sources", [])
    if details:
        with st.expander(f"Sources ({len(details)})"):
            for d in details:
                meta = " · ".join(
                    x for x in (SOURCE_LABELS.get(d.get("source_type", ""), d.get("source_type")),
                                d.get("published") or d.get("dated_by")) if x
                )  # fmt: skip
                st.markdown(f"[{d['url']}]({d['url']})" + (f"  \n:gray[{meta}]" if meta else ""))
                for ev in d.get("evidence", []):
                    st.text(ev)  # stored source text, shown verbatim (not as markdown)
    steps = msg.get("steps", [])
    if steps:
        with st.expander(f"Agent steps ({len(steps)})"):
            st.code("\n".join(f"{s}" for s in steps), language=None, wrap_lines=True)
    if msg.get("cost") is not None:
        st.caption(f"{msg['tokens']:,} tokens ({msg['cached']:,} cached) · ${msg['cost']:.4f}")


def _ask(chat: dict[str, Any], company: dict[str, Any], question: str) -> None:
    chat["messages"].append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        status = st.status("Looking it up in the collected data…", expanded=False)
        out = st.empty()
        buf: list[str] = []
        calls: list[str] = []

        def on_event(kind: str, data: Any) -> None:
            if kind == "round":  # a new model call: text from the last round was not the answer
                buf.clear()
                out.empty()
            elif kind == "text":
                buf.append(data)
                out.markdown("".join(buf) + " ▌")
            elif kind == "tool":
                calls.append(_call(data) + (" [error]" if data.is_error else ""))
                status.write(f"`{calls[-1]}`")
                status.update(label=f"Checked the data ({len(calls)} tool calls)…")

        try:
            if chat.get("agent") is None:
                chat["agent"] = ChatAgent(company["domain"])
            reply: Reply = chat["agent"].ask(question, on_event=on_event)
        except Exception as exc:  # shown as a friendly message; details go to the console log
            log.warning("chat failed: %r", exc, exc_info=True)
            status.update(label="No answer", state="error")
            chat["messages"].append({"role": "error", "text": _friendly_error(exc)})
            return
        status.update(label=f"Checked the data ({len(calls)} tool calls)", state="complete")
    chat["messages"].append(
        {
            "role": "assistant",
            "text": reply.text,
            "sources": reply.source_details,
            "steps": [_call(s) + (" [error]" if s.is_error else "") for s in reply.steps],
            "tokens": reply.prompt_tokens + reply.output_tokens,
            "cached": reply.cache_read_tokens,
            "cost": reply.cost_usd,
        }
    )


def chat_tab(company: dict[str, Any]) -> None:
    domain = company["domain"]
    chat = st.session_state.setdefault(f"chat:{domain}", {"agent": None, "messages": []})
    has_key = get_setting("ANTHROPIC_API_KEY") is not None
    if not has_key:
        st.info(
            "Chat needs an Anthropic API key. Add `ANTHROPIC_API_KEY` to "
            f"`{ENV_PATH.name}` at the repo root (copy `.env.example`), then restart "
            "`leadership ui`. The Leadership tab works without a key.",
            icon=":material/key:",
        )

    for msg in chat["messages"]:
        if msg["role"] == "error":
            st.error(msg["text"], icon=":material/error:")
            continue
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["text"])
            else:
                _render_answer(msg)

    pending = st.session_state.pop("pending_question", None)
    if not chat["messages"] and not pending:
        st.markdown(f"#### Ask about {company['name']}'s leadership")
        st.caption("Answers come only from the collected data, with sources.")
        cols = st.columns(2)
        for i, q in enumerate(suggestions(domain)):
            if cols[i % 2].button(q, key=f"suggest:{domain}:{i}", disabled=not has_key,
                                  width="stretch"):  # fmt: skip
                st.session_state["pending_question"] = q
                st.rerun()

    typed = st.chat_input(
        f"Ask about {company['name']}'s leadership", disabled=not has_key, key=f"input:{domain}"
    )
    question = pending or typed
    if question and has_key:
        _ask(chat, company, question)
        st.rerun()


# ------------------------------------------------------------------ leadership tab


def _current_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for p in data["current"]:
        roles = [r for r in p["roles"] if r["is_current"]]
        lead = _primary(roles)
        titles = list(dict.fromkeys(r["title"] for r in roles))
        rows.append(
            {
                "group": lead["seniority"],
                "Name": p["name"],
                "Title": "; ".join(titles),
                "Function": lead["function"],
                "Seniority": SENIORITY_LABELS[lead["seniority"]],
                "Location": p["location"] or "",
                "Confidence": p["confidence"],
                "Sources": p["source_count"],
                "Flags": "; ".join(f["flag"] for f in p["flags"]),
            }
        )
    return rows


def _former_row(p: dict[str, Any]) -> dict[str, Any]:
    """A not-current person's newest past role and why it is not current."""
    last = max(p["past_roles"], key=lambda r: r["last_confirmed"] or "")
    return {
        "Name": p["name"],
        "Last title": last["title"],
        "Last confirmed": last["last_confirmed"] or "undated",
        "Why not current": last["note"] or "no current source",
    }


def leadership_tab(company: dict[str, Any]) -> None:
    data = leaders(company["domain"])
    rows = _current_rows(data)
    flagged = [r for r in rows if r["Flags"]]
    by_conf = {c: sum(r["Confidence"] == c for r in rows) for c in CONFIDENCE_ORDER}
    st.markdown(
        f"**{len(rows)} current leaders** · "
        + " · ".join(f"{n} {c}" for c, n in by_conf.items())
        + " confidence"
    )
    for r in flagged:
        st.warning(f"**{r['Name']}**: {r['Flags']}", icon=":material/flag:")

    f1, f2, f3 = st.columns([2, 2, 2])
    query = f1.text_input("Search name or title", key="lead_query")
    functions = sorted({r["Function"] for r in rows})
    pick_fn = f2.multiselect("Function", functions, key="lead_fn")
    pick_conf = f3.multiselect("Confidence", CONFIDENCE_ORDER, key="lead_conf")
    q = query.strip().lower()
    shown = [
        r for r in rows
        if (not q or q in r["Name"].lower() or q in r["Title"].lower())
        and (not pick_fn or r["Function"] in pick_fn)
        and (not pick_conf or r["Confidence"] in pick_conf)
    ]  # fmt: skip

    config = {
        "Sources": st.column_config.NumberColumn(help="Distinct sources backing this person"),
        "Confidence": st.column_config.TextColumn(help="high / medium / low (see DECISIONS.md)"),
        "Flags": st.column_config.TextColumn(help="Review flags, e.g. an announced departure"),
    }
    if not shown:
        st.caption("No current leaders match these filters.")
    for g in GROUP_ORDER:
        group = [r for r in shown if r["group"] == g]
        if not group:
            continue
        st.markdown(f"##### {SENIORITY_LABELS[g]} · {len(group)}")
        df = pd.DataFrame(group).drop(columns="group")
        st.dataframe(df, hide_index=True, width="stretch", column_config=config)

    if st.toggle("Show people who are not current", key="lead_former"):
        former = [_former_row(p) for p in data.get("not_current", [])
                  if p["status"].startswith("NOT CURRENT") and p["past_roles"]]  # fmt: skip
        if former:
            st.dataframe(pd.DataFrame(former), hide_index=True, width="stretch")
        else:
            st.caption("Everyone in the data is current.")


# ------------------------------------------------------------------ page


def main() -> None:
    st.set_page_config(page_title="Leadership Agent", page_icon="🧭", layout="wide")
    company = sidebar()
    if company is None:
        return
    st.title(company["name"], anchor=False)
    chat, table = st.tabs(["Chat", "Leadership"])
    with table:
        leadership_tab(company)
    with chat:
        chat_tab(company)


main()
