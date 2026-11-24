### Errors found in hand-check (Daniel, 2026-11-23)

| # | Error | Cause | Fix |
|---|---|---|---|
| 1 | Fernando San Martin was shown as current CTO, in a seat conflict with Paul Nichols | His CTO role rested on a single 2024 blog post ("my Co-founder / CTO"), and the old 18-month rule only lowered confidence. Paul Nichols has been CTO since Jun 2024; Fernando is now co-founder at Thyme. | D32 retires the stale CTO role. His co-founder role is historical (D23, no current operating role). The seat conflict is resolved. |

Confirmed in the hand-check, not errors: Paul Nichols is the current CTO. Nick Jezienicki is at Campfire (Partnerships, checked manually on LinkedIn; no LinkedIn URL is stored). Brendan Doyle, Chris McDonough, Erik Leavell, Marcio Arnecke, Alexandra Roveda and John Glasgow are all correct.

### Known misses (from the blind coverage scout, `docs/validation/meetcampfire.com-scout.md`)

None. Everyone the scout found is in the pipeline's data: John Glasgow, Marcio Arnecke, and Alexandra Roveda. The scout also confirms Roveda from a Fortune press release dated 2026-10-30, so she is the right Campfire.

Restored after Phase 4: a byline with a title on the company's own domain now counts as affiliation (D22, changed):

| Name | Title | Evidence | Verifier's earlier view | Verified by Daniel |
|---|---|---|---|---|
| Brendan Doyle | Head of Product | bylines on 7 campfire.ai blog posts plus the blog index | Very likely a Campfire employee | ✅ / ❌ / note |
| Nick Jezienicki | Head of Technology Partnerships | one byline, on the Atlar partner post | Probably Campfire, less certain | ✅ / ❌ / note |

### Scout vs. pipeline

- **Pipeline-only, so look closer:**
  - Paul Nichols (CTO), Fernando San Martin (Co-founder / CTO, 2024): the CTO seat conflict was resolved by D32 and the hand-check. Paul Nichols is the current CTO; Fernando is a historical co-founder. The scout saw a co-founder "Fernando" on Y Combinator, with no surname or title.
  - Chris McDonough (VP of Sales, GTM newsletter).
  - Erik Leavell (Head of Finance & Applied AI, campfire.ai/basecamp).
- **CFO:** John Glasgow is also called "CEO & CFO". The Daily Upside (2026-11-09) reports the CFO role is open, with a hire expected soon. That may need a re-collect later.
- campfire.ai has no team or about page, so coverage depends on the blog, press, and search.
- **Source-verifier verdicts** are from the collection in commit 60a6431. Brendan Doyle and Nick Jezienicki were rejected then; they were checked afterwards, in the addendum at the end of `meetcampfire.com-verifier.md`.
