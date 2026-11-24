# Claude Code Prompt — Company Leadership RAG Agent

> Paste everything below the line into Claude Code, starting in **Plan Mode** (Shift+Tab twice). Work through the phases one at a time. Don't let it build everything in one pass. The reviewers are grading how you work with the agent, so each phase needs its own checkpoint.

---

## Role & context

You are a senior engineer helping me build a take-home for an AI engineering role. Reviewers will clone the repo and run it. They are evaluating:
1. Data sourcing.
2. Storage design.
3. Agent architecture.
4. How I collaborate with you: **especially how I validate what you produce.**
5. **How honest the system is about what it doesn't know.**

The reviewer explicitly said: **a lean solution that works end to end beats a complex one.** The budget is 4–8 hours. If something would push past that, tell me and propose the simpler option. Optimize for correctness, provenance, and readability over feature count.

## The task

Input: a company domain or URL. Output:
1. **Collection pipeline**: find the company's leadership (C-level, VPs, Heads of departments) from public sources, collect public profile data, and store it with provenance.
2. **Chat interface**: an LLM-powered chat over the stored data that answers questions like "Who's their CTO?", "How many VPs do they have?", "Who heads marketing?", "Where is their CEO based?"

Test companies: `https://meetcampfire.com/` (small private startup) and `https://robinhood.com/` (public company, NASDAQ: HOOD). The scope is these two only. Build generically, but tune and validate against these two.

Hard rules:
- Real LLM calls only (no stubs or mocks in the runtime path).
- The repo must be clone-and-go.
- Commit the collected data for both companies.
- Ship an `.env.example`, never real keys.

## Non-negotiable principles

- **Every fact has a source.** No person, title, or location is stored without a source URL, a short evidence snippet, and a `retrieved_at` timestamp. The chat agent cites sources in its answers.
- **Never invent data.** If there's no evidence, the field stays NULL. The chat agent says "not found in the collected data" and, where useful, says what sources were checked. It never guesses.
- **Surface uncertainty.** Answers state when a fact comes from only one low-trust source, or when sources conflict.
- **Respect sources.** Honor robots.txt, rate-limit, set a descriptive User-Agent. **No LinkedIn scraping** (its ToS prohibits it), and the same goes for any other site whose ToS forbids scraping.
- **Config via env.** Keep the model name configurable (`LLM_MODEL`, default `claude-sonnet-5-5`).

## Data sourcing (priority order)

1. **Company's own site.** Find leadership/team/about/press pages via sitemap.xml, common paths (`/about`, `/team`, `/leadership`, `/company`), and nav links. This is the highest-trust source.
2. **SEC EDGAR** (public companies only; free official API; User-Agent must include a contact email). Use the latest 10-K "Executive Officers" section and/or the DEF 14A proxy. These are authoritative for C-level roles.
3. **One web search API** (Tavily, Brave, or Serper; pick one and justify it). Use targeted queries (`"{company}" VP`, `"{company}" "head of"`, `"{company}" CTO`) to find press releases, news, and conference bios, which is where most VPs and department heads appear.

Each source is a small adapter with the same interface (`discover(company) -> list[RawDocument]`). Cache raw fetched pages to disk so re-running extraction never re-fetches. If a page needs JavaScript to render, tell me before adding a headless browser.

## Storage: SQLite, relational, Postgres-compatible

Rationale (put this in `docs/DECISIONS.md`): the target questions are structured (filter by role, count VPs, look up a location), so a relational model answers them exactly. A vector store would make counts and filters less reliable. SQLite keeps it clone-and-go, and a plain `schema.sql` stays Postgres-compatible.

```
companies       (id, name, domain, is_public, ticker)
people          (id, company_id, full_name, location, bio, profile_url, confidence, updated_at)
roles           (id, person_id, title_raw, function, seniority, is_current)
                -- function: engineering|product|finance|marketing|sales|operations|legal|people|security|design|data|other
                -- seniority: c_level|vp|head|founder
sources         (id, company_id, url, source_type, fetched_at, raw_path)
                -- source_type: company_site|sec_filing|search
facts           (id, person_id, field, value, source_id, evidence_snippet, extracted_at)
                -- every claim + its evidence = the audit trail
collection_runs (id, company_id, started_at, finished_at, stats_json)
```

- `people` and `roles` hold the **resolved current view**, and `facts` holds **every claim with evidence**, so conflicts are visible.
- `confidence` is computed deterministically from the source mix (e.g. company site or SEC filing = high, multiple search hits = medium, single search hit = low), not from the LLM's opinion.
- Commit `data/<domain>/leadership.db` plus a readable `data/<domain>/leadership.json` export.

## Agent architecture: as few LLM steps as possible

**Collection pipeline** (deterministic orchestration):
1. **Discover**: run the source adapters and cache raw docs. No LLM.
2. **Extract** (LLM, structured output via tool use + Pydantic): for each relevant chunk, return `{full_name, title_raw, function, seniority, location?, bio?, evidence_snippet}`. Return an empty list rather than guess, and leave out non-leadership roles. Before the LLM call, pre-filter chunks with keyword/heading heuristics to control cost.
3. **Validate & resolve** (no LLM):
   - Reject any extraction whose `evidence_snippet` doesn't literally appear in the source text. This catches hallucinations cheaply.
   - Apply deterministic title rules (CEO/CFO/VP/Head of) that override the LLM's classification on unambiguous titles.
   - Dedupe people by normalized name. Settle conflicts by source priority (company_site > sec_filing > search), then recency.
   - Write `facts`, then resolve into `people`/`roles`.

**Chat agent**: tool-calling over typed tools, not free-form text-to-SQL.
- Tools: `list_leaders(company, function?, seniority?)`, `count_leaders(company, function?, seniority?)`, `get_person(company, name)` (returns facts + sources).
- The system prompt requires:
  - Answer only from tool results.
  - Cite source URLs.
  - State low confidence or conflicts.
  - Say "not found" when the data isn't there.
  - Ask which company when it's ambiguous.
- Keep conversation history so follow-ups work ("where is *she* based?").

## Interface

**Primary interface: a polished Streamlit app** (`uv run leadership ui`). It should look like an internal tool, not a demo.
- **Sidebar:** company selector, plus last-run stats (date, sources used, people found, LLM cost).
- **Chat tab:**
  - Answers stream in.
  - Each answer has a collapsible **Sources** section (links + evidence snippets) and a collapsible **Agent steps** section (tools called and with what arguments).
  - An empty chat shows 4–5 suggested-question buttons.
- **Leadership tab:** a filterable table (name, title, function, seniority, location, confidence, source count), grouped C-level / VP / Head. Low-confidence rows are visibly flagged.
- **Look and errors:** a clean theme in `.streamlit/config.toml` (neutral palette, one accent color). Friendly messages for a missing API key or an LLM failure, and never a raw stack trace.

**CLI** (Typer + Rich): `leadership collect <url>`, `leadership chat <domain>`, `leadership eval <domain>`, `leadership ui`. Collection runs from the CLI, not from the UI.

**Runs in VS Code on Windows, macOS, and Linux/WSL:**
- `uv` for env + deps, and every command runs as `uv run ...`. **No Makefile**, because Windows doesn't have `make` by default.
- VS Code files:
  - `.vscode/extensions.json`: Python, Pylance, Ruff.
  - `.vscode/settings.json`: `.venv` interpreter, Ruff format-on-save, pytest enabled.
  - `.vscode/launch.json`: **Launch UI**, **Collect robinhood.com**, **Collect meetcampfire.com**, **Chat CLI**, **Debug tests**.
- `.gitattributes` with `* text=auto eol=lf`. Use `pathlib` for paths and explicit UTF-8 for all file I/O.
- Load `.env` automatically. If a key is missing, print exactly which one and where to get it.
- **Fresh-clone test:** clone → `uv sync` → copy `.env.example` → F5 **Launch UI** must work against the committed data with only `ANTHROPIC_API_KEY` set.

Dependencies: Python 3.12, `httpx`, `trafilatura` (or `selectolax`), `pydantic`, `anthropic`, `streamlit`, `typer`, `rich`, `python-dotenv`. Justify anything else.

```
src/leadership/
  sources/    company_site.py  sec_edgar.py  search.py
  pipeline/   discover.py  extract.py  resolve.py
  storage/    schema.sql  db.py  repository.py
  chat/       agent.py  tools.py  prompts.py
  ui/         app.py
  cli.py
data/meetcampfire.com/  data/robinhood.com/
eval/questions.yaml
tests/
docs/DECISIONS.md  docs/VALIDATION.md  docs/initial-prompt.md
docs/validation/  # subagent reports: <domain>-verifier.md, <domain>-scout.md
.claude/agents/   # project subagents (already present, don't modify)
session/          # Claude Code session export
README.md  .env.example  .gitattributes  pyproject.toml  .vscode/  .streamlit/
```

## Validation (a main grading criterion, so treat it as a feature)

- **`docs/VALIDATION.md`**: after collection, generate a checklist for each company: every person, title, location, and source URL, with a blank "Verified by Daniel: ✅ / ❌ / note" column. I'll hand-check each row against its source. Then record the errors I found, what caused each one, and what we changed to fix it. Also record **known misses**: leaders I found manually that the pipeline didn't.
- **`eval/questions.yaml`**: 8–10 Q&A per company, with answers I've verified by hand. Include:
  - the four example questions,
  - at least two questions the data **can't** answer, where the correct answer is "not found",
  - one follow-up question,
  - one question where sources conflict or confidence is low.
- `leadership eval` runs them through the real agent and prints pass/fail.
- **Unit tests** (offline, no keys): title rules, the evidence-snippet check, dedupe/resolve, and repository queries.
- Log token usage and cost per run in `collection_runs.stats_json`.

## README must include

- Quickstart: VS Code (F5) and terminal.
- Screenshots of both UI tabs.
- A Mermaid architecture diagram.
- Storage rationale.
- Sourcing and ethics notes (LinkedIn ban).
- How I validated the output, linking `VALIDATION.md` and eval results.
- Honest known limitations and coverage gaps.
- Cost per run.
- What I'd do with more time.
- **AI tools used:** Claude Code for all development (session export in `session/`), plus Claude (claude.ai) to draft this initial prompt, saved at `docs/initial-prompt.md`.

## How we work (follow this exactly)

1. **Phase 0, plan:** Before writing code, propose the plan, schema, and open questions. Wait for my approval.
2. Then build one phase at a time:
   1. Storage + schema.
   2. Source adapters + raw cache.
   3. Extract + validate/resolve.
   4. Run on both companies. Then:
      - Run `coverage-scout` for each company (blind).
      - Run `source-verifier` for each company.
      - Diff the scout list against our data.
      - Generate `VALIDATION.md` with an "Agent verdict" column next to my blank "Verified by Daniel" column, and a "Known misses" section.
      - **Stop while I hand-check the data.**
   5. Chat agent + tools.
   6. Eval + tests.
   7. UI polish.
   8. README/docs.
3. At the end of each phase:
   - Run it and show me real output.
   - Run the `phase-reviewer` subagent on the phase's diff, and fix any BLOCK or should-fix items (re-run the reviewer until it returns PASS).
   - List what you're unsure about, then stop for my review. Make small, focused commits with clear messages.
4. Record each non-trivial decision and the rejected alternative in `docs/DECISIONS.md`. The first entry is the LinkedIn ban: we rejected LinkedIn as a source because its ToS prohibits scraping, and we accept the resulting gaps in VP/department-head coverage.
5. If a source is blocked or empty, or a result looks wrong, tell me. Don't paper over it.
6. Before I submit, run `fresh-clone-tester`. Fix everything it reports under Critical and README gaps, then run it again until it returns READY TO SUBMIT.

## Subagents (in `.claude/agents/`)

| Agent | When | What it does |
|---|---|---|
| `phase-reviewer` | End of every phase | Read-only code review against these rules, with a PASS / PASS WITH FIXES / BLOCK verdict |
| `coverage-scout` | Phase 4 | Blind, independent web research on each company's leadership. Measures what the pipeline missed. **Never give it our data.** |
| `source-verifier` | Phase 4, after collection | Checks every stored fact against its cached and live source and assigns a verdict per record |
| `fresh-clone-tester` | Before submission | Clones the committed repo into a temp folder, follows the README literally, and runs tests, eval, and a UI health check |

Rules for using them:
- Subagent reports are **inputs to my review, not approvals**. Summarize each one for me, and never act on a subagent's findings by changing data without telling me.
- Don't create additional subagents, and don't use subagents to write production code.
- Mention the subagent workflow in the README's "How I validated" section.
