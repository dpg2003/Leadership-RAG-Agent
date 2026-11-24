# Source verification: meetcampfire.com
Run: 2026-11-23 · Records checked: 54 facts (7 people) · Live URLs spot-checked: 8

## Summary
SUPPORTED: 52 · PARTIAL: 2 · UNSUPPORTED: 0 · STALE: 0 · SOURCE_UNAVAILABLE: 0
Estimated precision (SUPPORTED / total): 96.3% (52/54)

Method: every evidence_snippet was grep-checked against its cached raw_path (all 54 found; whitespace/curly-quote differences only). Context read for each. 8 live fetches: Fernando blog, TokenPost, GTMnow Substack, Fortune, campfire.ai/basecamp, blog/atlar, blog/campfire-accelerates-accounting-with-claude, blog/lease-accounting. linkedin.com not touched. Note that the CFO Dive article itself cites "his LinkedIn profile" (second-hand; not fetched).

## Records
| # | Person | Field | Stored value | Verdict | Reason (1 line) | Source |
|---|---|---|---|---|---|---|
| 1 | Alexandra Roveda | full_name | Alexandra Roveda | SUPPORTED | Snippet present; names her, Campfire, appointment | tokenpost.com/news/people/44993 (live OK) |
| 2 | Alexandra Roveda | title | vice president of enterprise | PARTIAL | Title and company correct and current (Oct 30, 2026); function stored "other" but remit is enterprise sales/GTM; as_of null; single, low-tier aggregator source | tokenpost.com/news/people/44993 |
| 3 | Chris McDonough | bio | "joined 4 companies right before they exploded." | SUPPORTED | Verbatim in podcast blurb | thegtmnewsletter.substack.com/p/gtm-205-... (live OK, 2026-09-10) |
| 4 | Chris McDonough | full_name | Chris McDonough | SUPPORTED | "Chris McDonough, VP of Sales at Campfire" | same |
| 5 | Chris McDonough | title | VP of Sales | SUPPORTED | Same snippet; function sales / seniority vp correct. Single third-party source | same |
| 6 | Erik Leavell | full_name | Erik Leavell | SUPPORTED | Speaker card "Erik Leavell / Head of Finance & Applied AI, Campfire" (cache; repeated 4x on page). Live fetch truncated, inconclusive | campfire.ai/basecamp |
| 7 | Erik Leavell | title | Head of Finance & Applied AI | SUPPORTED | Same; function finance / head correct. Event-speaker page, single source | campfire.ai/basecamp |
| 8 | Fernando San Martin | bio | "He saw an opportunity ..." | SUPPORTED | Pronoun refers to him in prior sentence of same paragraph | campfire.ai/blog/were-coming-out-of-stealth-... |
| 9 | Fernando San Martin | full_name | Fernando San Martin | SUPPORTED | Named by CEO as "my Co-founder / CTO" (live OK) | same |
| 10 | Fernando San Martin | title | Co-founder / CTO | PARTIAL | Said so on 2024-02-27 only; is_current=true not established (21 months old, single source) and conflicts with Paul Nichols as CTO (2025-02 onward). Two role rows with different function/seniority (engineering/c_level and other/founder) | same |
| 11 | John Glasgow | bio | "Prior to Campfire, John served 15+ years ..." | SUPPORTED | Verbatim in author box | blog/from-cpa-to-cfo-ron-wangerin-cfo-trust-and-will |
| 12-28 | John Glasgow | full_name (17 facts) | John Glasgow | SUPPORTED | All 17 snippets found; each names him with Campfire (company blog x8, claude.com, Fortune, Accel, BenchmarkIt, CFO Dive, PRNewswire, YouTube, Basecamp) | see leadership.json |
| 29-45 | John Glasgow | title (17 facts) | CEO and Founder / CEO & Founder / Founder and CEO / CEO / CEO & CFO / founder / CEO, CFO and founder | SUPPORTED | Each snippet states the variant verbatim with Campfire. "CEO & CFO" supported by Doss quote (2026-02-24) and CFO Dive; "founder" (BenchmarkIt) and "CEO" (Aug 2024) are thin but accurate. Fortune 2026-10-16 (live OK) says "founder and CEO" | see leadership.json |
| 46 | Marcio Arnecke | bio | "Most recently, Arnecke served as CMO at Apollo.io ..." | SUPPORTED | Verbatim; correctly prior-employer bio, not a title | fortune.com/press-releases/campfire-appoints-... (live OK) |
| 47 | Marcio Arnecke | full_name | Marcio Arnecke | SUPPORTED | Announcement of his appointment | same |
| 48 | Marcio Arnecke | title | Chief Marketing Officer | SUPPORTED | "said Marcio Arnecke, Chief Marketing Officer, Campfire." Dated 2026-10-16. Single source | same |
| 49-51 | Paul Nichols | full_name (3 facts) | Paul Nichols | SUPPORTED | "led by CTO Paul Nichols" in 3 pages (campfire.ai blog, claude.com, zanovoy.com) | see leadership.json |
| 52-54 | Paul Nichols | title (3 facts) | CTO | SUPPORTED | Same snippets state CTO at Campfire. Oldest 2025-02-06 (live OK); newest is zanovoy.com (third-party, 2026-07-08) restating the Anthropic case study. Currency plausible but not independently shown | see leadership.json |

## Rejections (collection.rejections), reviewed from cached sources
- Brendan Doyle, "Head of Product": 10 rejected claims, all from campfire.ai blog bylines (Forecast, BYO CSV, Migration Agent, August Product Update, AskEmber in Slack, Lease Accounting, Multi-Book, category page), dated May to Sept 2026. Verdict from sources: looks like a Campfire employee, not an outside author. The byline appears on Campfire's own domain, repeated across about 9 posts, and the posts speak in Campfire's voice ("Campfire now includes a native Leases module"). No cached page states his employer in words. Live fetch of the Lease Accounting page confirmed the byline but no employer line.
- Nick Jezienicki, "Head of Technology Partnerships": 1 rejected claim, campfire.ai/blog/atlar (2026-05-12). Probably a Campfire employee. The post is on Campfire's blog, tied to a Campfire + Atlar integration, and speaks as Campfire. Less certain than Doyle because it is one post about a partner and no page names his employer, so he could be partner-side. A single byline is insufficient to settle it.
- Paul Nichols via paulcnichols.com: rejection correct (personal site, snippet lacks surname). John Glasgow rejections (about 70, incl. multiples.vc) are harmless because he is accepted via other sources.
- No verdict rule changed. Neither Doyle nor Jezienicki is in the people list, so they are not scored.

## Suspected duplicates
None found among the 7 people. Glasgow and Fernando each have two role rows for the same title string (see below), which is a row-level duplicate.

## Classification corrections (title_raw → suggested function/seniority)
- "vice president of enterprise" (function other / vp) → function sales (GTM remit), seniority vp.
- "CEO & CFO" (other / c_level) and "CEO & Founder" (other / founder) for Glasgow: function "other" looks wrong for a CEO (executive/general management if the taxonomy has it). He should have one c_level row; "founder" is a flag, not a replacement for c_level seniority.
- "Co-founder / CTO" for Fernando: second row (other / founder) duplicates the first (engineering / c_level); the function should be engineering on both, and seniority is ambiguous for a dual title.
- Correct as stored: VP of Sales (sales/vp), Head of Finance & Applied AI (finance/head), Chief Marketing Officer (marketing/c_level), CTO Nichols (engineering/c_level).

## Pipeline bugs suspected (patterns, not one-offs)
1. Affiliation check ignores host domain. First-party campfire.ai bylines (Doyle, Jezienicki, Glasgow on about 70 pages) were rejected for "no affiliation with Campfire", so likely true employees are lost. Consider accepting when the page host equals the company domain and the person is a blog author.
2. Unresolved seat conflict. CTO is held both by Paul Nichols (2026-07-08) and Fernando San Martin (2024-02-27). Fernando is marked is_current=true from a single 21-month-old post, and the pipeline's own review flagged both. Dated sources should not default to current.
3. Role as_of is inconsistent with its evidence. Glasgow "CEO & CFO" as_of 2026-10-16, but its evidence dates 2026-02-24 and an undated article. The as_of seems to take the latest page date for any role.
4. Confidence "high" for Paul Nichols and John Glasgow counts derivative pages (a third-party review that restates the Anthropic case study) as independent sources. Nichols has effectively one origin.
5. Timestamps: retrieved_at/extracted_at are 2026-11-24 UTC while the run date is 2026-11-23. Likely a timezone artifact; confirm.
6. Function "other" is over-used for executive and sales roles.
