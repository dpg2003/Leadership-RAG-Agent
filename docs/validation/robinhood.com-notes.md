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
  - Adam Wolff (VP of Engineering, 2020 source) and Christine Brown (2021) are single, old sources and are flagged as stale.
- **The scout found no head of:** engineering/CTO (after Pinner), product, marketing, people, design, data, operations. The pipeline has Connie Schan (Chief People Officer, undated) and Adam Wolff (VP Engineering, stale).
- **Scout candidates it couldn't confirm** (not counted as misses): Sivaa Subramanian (VP Engineering 2024 to about May 2026, likely departed) and Gaetan Thabot (head of crypto partnerships, undated secondary sources).
- **Note:** the source-verifier's line "people_found=36 but 35 people are exported" is wrong. The export has 36 people, and its own records table covers 36.
- **Source-verifier verdicts** are from the collection in commit 60a6431. The 9 people added by the crawler fix (D26) were checked afterwards, in the addendum at the end of `robinhood.com-verifier.md`. Changed records (such as titles now using the newest wording) may differ from what the verifier saw.
- **Known limits in this data:**
  - Matt Billings's two wordings of one position are now one row, with both sources (D30; listed under "Role rows merged").
  - "Steph Guild" (Head of Investment Strategy) is stored separately from Stephanie Guild (Chief Investment Officer) and listed as a possible duplicate. If they are the same person, the current count (31) is one too high.
