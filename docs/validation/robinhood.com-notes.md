### Known misses (from the blind coverage scout, `docs/validation/robinhood.com-scout.md`)

| Name | Title (scout's source) | Why the pipeline missed it | Verified by Daniel |
|---|---|---|---|
| Erika Dean | Chief Security Officer ([newsroom, 2024-06-10](https://robinhood.com/newsroom/safety-first-a-conversation-between-robinhoods-security-team-leaders/); possibly stale) | Never fetched: the company-site crawler ranks pages by URL keywords and this slug has no role word. Would conflict with Evan McHenry (VP and CISO, investor site). | ✅ / ❌ / note |
| Katelyn (KP) Perna | Crypto Chief Information Security Officer (same 2024-06-10 newsroom post as Erika Dean; possibly stale) | Never fetched, for the same reason as Erika Dean. | ✅ / ❌ / note |
| Dr. Naomi Boyd | Chief Economic Advisor ([newsroom, 2026-02-19](https://robinhood.com/us/en/newsroom/robinhood-welcomes-dr-naomi-boyd-as-chief-economic-advisor)) | Extracted, then rejected by the title rules: "Chief … Advisor" is not a C-level officer title (advisors are out of scope). Scope question, not a bug. | ✅ / ❌ / note |

### Scout vs. pipeline

- **Both found, and they agree:** Vlad Tenev, Shiv Verma, Steve Quirk, Dara Bazzano, Lucas Moskowitz, Johann Kerbrat. Jeff Pinner and Jason Warnick are former in both.
- **They disagree:**
  - Dan Gallagher: the scout and the 2026 proxy have "Chief Legal, Compliance and Corporate Affairs Officer". The pipeline displays the older "Chief Legal Officer", because titles are chosen by source priority (company site first), not recency. Same person and seat; the title is stale.
  - Chris Koegel: the scout reports him leaving for Klarna (secondary aggregator, 2026-11-19; it couldn't fetch a primary source). The pipeline has him current, low confidence, from the investor-site leadership page only.
- **Pipeline-only, so look closer:**
  - The scout couldn't reach the investor-site leadership page (HTTP 403, not worked around), so most VPs there are unconfirmed by it: Fatehpuria, Billings, Baldwin, Mackenzie, Anand, Lee, White, Rao, Guild, Schan, McHenry, Koller, Sinclair, Mehta.
  - Adam Wolff (VP of Engineering, 2020 source) and Christine Brown (2021) are single, old sources and are flagged as stale.
- **The scout found no head of:** engineering/CTO (after Pinner), product, marketing, people, design, data, operations. The pipeline has Connie Schan (Chief People Officer, undated) and Adam Wolff (VP Engineering, stale).
- **Scout candidates it couldn't confirm** (not counted as misses): Sivaa Subramanian (VP Engineering 2024 to about May 2026, likely departed) and Gaetan Thabot (head of crypto partnerships, undated secondary sources).
- **Note:** the source-verifier's line "people_found=36 but 35 people are exported" is wrong. The export has 36 people, and its own records table covers 36.
