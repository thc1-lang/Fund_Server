# Component 6A — Person and Career Intelligence Audit

This audit covers the official SEC-backed management and board store for ZM, PLTR, and EXEL as of 2026-09-24. Component 6B was not built.

## AUDIT RESULTS

The validation store contains 37 global people, 68 role assertions, 56 career records, 34 board records, 37 issuer relationships, 18 education records, 37 non-destructive SEC insider links, and 3 Item 5.02 role-change events. Every stored fact record has an SEC source ID, URL, accession/form metadata, and a local cached path. The clean store is at `artifacts/management_intelligence/`.

| Issuer | People | Current executives | Current issuer directors | Role assertions | Career records | Prior career records | Confirmed public prior employers | Board records | Education | Insider links | 8-K role changes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ZM | 12 | 4 | 8 | 23 | 24 | 11 | 2 | 12 | 9 | 13 | 3 |
| PLTR | 10 | 5 | 7 | 21 | 13 | 3 | 0 | 7 | 7 | 10 | 0 |
| EXEL | 15 | 5 | 11 | 24 | 19 | 4 | 0 | 15 | 2 | 14 | 0 |

### ZM:

- Current leadership includes Eric S. Yuan as President and Chief Executive Officer and Chairman, Michelle Chang as Chief Financial Officer, and Aparna Bawa as Chief Operating Officer. The proxy also supplies the current director and lead-independent-director assertions.
- William R. McDermott is historical for the Zoom board from 2026-07-27. This is controlled by the official Item 5.02 filing dated 2026-07-28: [SEC 8-K accession 0001628280-26-050155](https://www.sec.gov/Archives/edgar/data/1585521/000162828026050155/zm-20260727.htm).
- Jeff Epstein is current from 2026-08-31, based on the later [SEC 8-K accession 0001585521-26-000126](https://www.sec.gov/Archives/edgar/data/1585521/000158552126000126/zm-20260831.htm). Jonathan Chadwick has a reported resignation effective 2026-11-19, so he remains current as of the audit date and is separately marked as a future departure.
- The proxy source is [DEF 14A accession 0001628280-26-028866](https://www.sec.gov/Archives/edgar/data/1585521/000162828026028866/zm-20260430.htm). The later 8-Ks are additive provenance; they do not overwrite the historical proxy evidence.
- One founder assertion is present, but it is explicitly scoped to another company. No issuer-founder claim is accepted for ZM.

### PLTR:

- Current leadership includes Alexander Karp as Co-Founder and Chief Executive Officer, Stephen Cohen as Co-Founder and President, Peter Thiel as Co-Founder and Chairman, David Glazer as Chief Financial Officer, and Shyam Sankar as Chief Technology Officer.
- Alexander Karp, Stephen Cohen, and Peter Thiel are confirmed issuer founders from structured proxy role text. Eric Woersching and Alexander Moore have founder language tied to other companies and are not counted as Palantir founders.
- No post-proxy Item 5.02 appointment or departure was found in the cached official 8-K set. The canonical proxy is [DEF 14A accession 0001321655-26-000019](https://www.sec.gov/Archives/edgar/data/1321655/000132165526000019/pltr-20260423.htm).

### EXEL:

- Current leadership includes Michael M. Morrissey as President and Chief Executive Officer, Christopher J. Senner as Executive Vice President and Chief Financial Officer, and Stelios Papadopoulos as Independent Chair.
- Stelios Papadopoulos is the confirmed issuer founder. No lead-independent-director role is asserted in the proxy data.
- The 2026-05-26 8-K contains Item 5.02, but its subject is the equity incentive plan; the parser correctly emits no person appointment or departure. No later cached Item 5.02 role change was found.
- The canonical proxy is [DEF 14A accession 0000939767-26-000046](https://www.sec.gov/Archives/edgar/data/939767/000093976726000046/exel-20260415.htm).

## IDENTITY ARCHITECTURE

Canonical human identity is global and independent of ticker: `person:<hash of normalized name tokens>`. The issuer relationship is a separate `PersonCompanyRelationship` keyed by issuer CIK. Compatibility `ticker` and `company_name` fields remain on the person record, but they no longer participate in the canonical identity key.

SEC reporting-owner CIKs and official same-person biography evidence can support a cross-company merge. Same-name people without that evidence are assigned a deterministic issuer-disambiguated ID and marked ambiguous. A management person and an SEC insider person remain separate canonical records; `InsiderIdentityLink` records preserve the match method, confidence, reporting-owner CIK, and source evidence.

The offline tests cover global keys across employers, unsupported same-name disambiguation, same-issuer idempotent reconciliation, surname-first insider names, and high-confidence owner-CIK links. The final three-company store has 37 unique person IDs for 37 records and zero unresolved identity collisions.

## CAREER, ROLE, AND BOARD AUDIT

Current employment and board membership are separate records. A current issuer career record uses the issuer CIK/ticker and `PUBLIC_COMPANY_CONFIRMED`; prior employers are mapped only when the official SEC company-tickers cache provides an exact or strong title match. Unresolved names remain `UNRESOLVED` and are never assigned a guessed ticker. The run has 2 confirmed public prior employers for ZM, 0 for PLTR, and 0 for EXEL; the remaining unresolved mappings are a conservative coverage limitation rather than fabricated data.

Role start dates preserve the precision present in the source (`year`, `month`, or `day`). Board start dates are held on `BoardMembership`, so a board start does not become an employment start. Current and historical assertions can coexist, and an effective end date is applied only when the official source makes the change effective. Future-effective departures remain current until their effective date.

Career and board entries have functional areas and industry categories derived from explicit role and biography evidence. Marketing language and generic claims do not create a role, employer, board, founder status, or education record. Education extraction requires an explicit degree/institution statement and keeps the original evidence text; no graduation year is inferred when absent.

Quality checks for all three issuers report:

- zero duplicate global people;
- zero duplicate career keys;
- zero chronology errors;
- zero missing provenance records across people, roles, career, boards, relationships, and education;
- zero stale current board records for changes effective on or before 2026-09-24; and
- one explicitly reported future departure, Jonathan Chadwick on 2026-11-19.

The manual 12-profile review covered four profiles per issuer:

| Issuer | Profiles reviewed | Checks |
| --- | --- | --- |
| ZM | Eric S. Yuan; Michelle Chang; Jeff Epstein; Jonathan Chadwick | CEO/CFO role identity, current board status, appointment/departure dates, insider links, source paths |
| PLTR | Alexander Karp; David Glazer; Peter Thiel; Stephen Cohen | founder scope, CEO/CFO/Chair/President roles, board starts, education, insider links |
| EXEL | Michael M. Morrissey; Christopher J. Senner; Stelios Papadopoulos; Julie Anne Smith | CEO/CFO/Chair/director roles, founder scope, board history, education, insider links |

All twelve profiles had source-backed person records, and all relevant assertions had local cached provenance. The role-change review specifically checked the ZM departure and appointment 8-Ks and the EXEL Item 5.02 false-positive case.

## SOURCE COVERAGE AND IDEMPOTENCY

The registry contains 16 official cached SEC sources: three definitive proxies and thirteen post-proxy 8-Ks. The SEC provider reuses unchanged local filings and records accession, filing date, source URL, and local path on every normalized fact. No search engine or third-party biography source is used.

A second acquisition pass over ZM, PLTR, and EXEL produced zero additions and zero updates for people, roles, career, boards, relationships, education, insider links, and role changes. The offline suite now has 270 passing tests, including the new Component 6A regression tests for identity, 8-K parsing, date precision, organization mapping, provenance, and audit invariants.

## ACCEPTANCE CHECK

The requested Component 6A criteria pass: global identity is separate from issuer relationship; cross-company identity is conservative; SEC insider links are non-destructive; current/former status is source-dated; CEO/CFO/role classification is present; founder evidence is scoped to the issuer or another company; career chronology and precision are retained; prior-company mapping is exact-cache-only; board history is separate from employment; education is explicit and evidenced; functional and industry categories are evidence-derived; marketing language is excluded; duplicates and chronology are checked; provenance is record-level; 8-K appointments/departures supersede stale current proxy claims; and reruns are idempotent.

## FREEZE RECOMMENDATION

Component 6A ready to freeze: **YES**

Reason: the final official-source run meets the coverage, provenance, chronology, identity, current-status, idempotency, and regression-test requirements. The only open classifications are deliberately conservative `UNRESOLVED` employer mappings where the SEC ticker cache does not provide an exact match; they are surfaced as quality metadata and do not affect current-role correctness.
