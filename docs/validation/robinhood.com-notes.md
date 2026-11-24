### Errors found in hand-check (Daniel, 2026-11-23)

| # | Error | Cause | Fix |
|---|---|---|---|
| 1 | 8 people shown as current had left: Adam Wolff, Christine Brown, Rich Cho, Mark Seigle, Christina Lai, Jim Swartwout, Rich Jackson, Smeeta Ramarathnam | Each rested on a single source older than 18 months. The old rule (D16) only lowered their confidence; it didn't retire the role. | D32: such a role is not current unless a newer source confirms it. The note says "last confirmed <date>; no newer confirmation". |
| 2 | "Steph Guild" and "Stephanie Guild" were two people | The identity rule (D6) needs an overlapping function, and "Head of Investment Strategy" (2022) and "Chief Investment Officer" share none | D33: hand-curated merge in `data/robinhood.com/identity_merges.json`, backed by her investor-site bio |
| 3 | Vlad Tenev's location was "San Francisco" | A CNBC caption ("… joins us with CEO Vlad Tenev in San Francisco") was accepted as a location; the check only required the snippet to be literal | D34: the location evidence must say where the person lives or works. "Bay Area of California" (board-page bio) is kept. |
| 4 | Anne Hoge's historical Chief Legal Officer role looked like `head` | Already stored as `c_level` since D4's current-only change (0e22cca). VALIDATION.md's not-current table didn't show seniority, and the verifier's older verdict said "head". | The not-current table now has a seniority column (Anne Hoge: c_level/legal) |
| 5 | Chris Koegel's flag said "unconfirmed" | Hand-check confirmed it from multiple sources | Flag now reads "departing Dec 31, 2026 to Klarna as CFO (multiple sources)" (D28). He stays current. |

**Live page check:** Daniel opened the live investors.robinhood.com/governance/leadership page in a browser (Nov 23, 2026). It matched our current list: every VP and above is in the data, with no misses. Taylor Brown (Chief of Staff) and the Senior Directors are correctly excluded (out of scope). Norm Ashkenas is not on the page, so "not current" is correct.

**AI-assisted research:** during the hand-check, AI-assisted research (claude.ai) flagged two people as departed, based on stale search snippets. Daniel's check of the live page showed both were current. Search snippets are not evidence of currency; the live page is.

**LinkedIn:** some checks used LinkedIn, manually, outside the pipeline. LinkedIn is never a pipeline source (D1), and no LinkedIn URL is stored.

### Known misses (from the blind coverage scout, `docs/validation/robinhood.com-scout.md`)

The scout ran against the collection in commit 60a6431. After the general crawler fix (D26):

| Name | Title (scout's source) | Status now | Verified by Daniel |
|---|---|---|---|
| Erika Dean | Chief Security Officer ([newsroom, 2024-06-10](https://robinhood.com/newsroom/safety-first-a-conversation-between-robinhoods-security-team-leaders/)) | **Found** after the crawler fix. Not current by the proxy rule (2024 post; C-level title not in the 2026 proxy). Would conflict with Evan McHenry (VP and CISO, investor site). | ✅ / ❌ / note |
| Katelyn (KP) Perna | Crypto Chief Information Security Officer (same post) | **Found** after the crawler fix. Not current by the proxy rule, for the same reason. | ✅ / ❌ / note |
| Dr. Naomi Boyd | Chief Economic Advisor ([newsroom, 2026-02-19](https://robinhood.com/us/en/newsroom/robinhood-welcomes-dr-naomi-boyd-as-chief-economic-advisor)) | Excluded by rule: advisor titles are out of scope (D27). Not a miss. | ✅ / ❌ / note |

No known misses remain.

### Scout vs. pipeline

- **Both found, and they agree:** Vlad Tenev, Shiv Verma, Steve Quirk, Dara Bazzano, Lucas Moskowitz, Johann Kerbrat. Jeff Pinner and Jason Warnick are former in both.
- **They disagree:**
  - Chris Koegel: the scout reports him leaving for Klarna (secondary aggregator, 2026-11-19; it couldn't fetch a primary source). The pipeline has him current, low confidence, from the investor-site leadership page only. Flagged "reported departing (secondary source, unconfirmed)" (D28); he stays current until you confirm.
  - Dan Gallagher: the scout has "Chief Legal, Compliance and Corporate Affairs Officer". This was stale in the first run and is fixed: the pipeline now displays the current investor-site listing's wording (`/governance/leadership`), which counts as newest (D27). The proxy's version differs only by a comma.
- **Pipeline-only, so look closer:**
  - The scout couldn't reach the investor-site leadership page (HTTP 403, not worked around), so most VPs there are unconfirmed by it: Fatehpuria, Billings, Baldwin, Mackenzie, Anand, Lee, White, Rao, Guild, Schan, McHenry, Koller, Sinclair, Mehta.
  - Adam Wolff (VP of Engineering, 2020 source) and Christine Brown (2021) rested on single, old sources. Both had left (hand-check), and D32 now marks them not current.
- **The scout found no head of:** engineering/CTO (after Pinner), product, marketing, people, design, data, operations. The pipeline has Connie Schan (Chief People Officer, undated, confirmed on the live page). Adam Wolff (VP Engineering) is not current (D32).
- **Scout candidates it couldn't confirm** (not counted as misses): Sivaa Subramanian (VP Engineering 2024 to about May 2026, likely departed) and Gaetan Thabot (head of crypto partnerships, undated secondary sources).
- **Note:** the source-verifier's line "people_found=36 but 35 people are exported" is wrong. The export has 36 people, and its own records table covers 36.
- **Source-verifier verdicts** are from the collection in commit 60a6431. The 9 people added by the crawler fix (D26) were checked afterwards, in the addendum at the end of `robinhood.com-verifier.md`. Changed records (such as titles now using the newest wording) may differ from what the verifier saw.
- **Known limits in this data:**
  - Matt Billings's two wordings of one position are now one row, with both sources (D30; listed under "Role rows merged").
  - "Steph Guild" is merged into Stephanie Guild (D33, Daniel's decision). Her 2022 "Head of Investment Strategy" role is kept as history.
