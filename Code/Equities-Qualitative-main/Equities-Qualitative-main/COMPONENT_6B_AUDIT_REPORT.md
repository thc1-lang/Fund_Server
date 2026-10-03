# Component 6B Quality Audit

## AUDIT RESULTS

ZM: 52 final operating outcomes, 610 canonical/derived facts, 5 guidance commitments, 1 material strategic commitment, 5 capital events.

PLTR: 84 final operating outcomes, 490 canonical/derived facts, no current-store guidance, no strategic or capital events.

EXEL: 46 final operating outcomes, 1,204 canonical/derived facts, no current-store guidance, no strategic or capital events.

The original 142 operating outcomes were all valid. The final counts are higher only because defensible GAAP gross-margin, operating-margin, and FCF derivations are now persisted.

## OPERATING OUTCOMES

Before: 142 persisted outcomes.

After: 182 persisted outcomes: ZM 52, PLTR 84, EXEL 46.

Valid: 182. Every record passed company, person, tenure, metric, unit, basis, value-change, attribution, and provenance checks.

Removed: 0 original outcomes.

Corrected: annual/quarter selection and as-of filtering are enforced; derived metrics now carry formula and source-fact provenance.

## SEC FACT NORMALIZATION

Duplicates: 0 duplicate canonical economic keys across 2,304 persisted facts.

Restatements: 1,316 canonical facts retain revision history, with 2,189 discarded candidate observations preserved in revision provenance. No averaging is performed.

Period issues: 0 outcome period mismatches. Annual, quarterly, and YTD facts remain separate by start/end period.

Metric issues: 0. Cash, long-term debt, current debt, period-end shares, and diluted weighted-average shares remain separate metrics.

Derivations: 474 derived facts: gross margin, operating margin, and FCF. FCF is `operating_cash_flow - capital_expenditure`; each record stores both source fact IDs and the formula. No standalone quarter is derived from YTD data.

## TENURE ALIGNMENT

Full period: 182.

Partial period: 0 included in headline outcomes.

Rejected outside tenure: 0 persisted.

Issues: 0. No PRE_TENURE or POST_TENURE fact is included in a snapshot. Duration fields are stored on outcomes.

## GUIDANCE

ZM: 5 commitments. Four remain unresolved and one is `NOT_COMPARABLE` because percentage guidance was compared against a dollar fact candidate. FY2027 revenue preserves an initial and raised revision; Q2 and Q3 revenue guides are separate series.

PLTR: `GUIDANCE_NOT_IN_CURRENT_DOCUMENT_STORE` — no documents or claims are present for PLTR in the current qualitative store.

EXEL: `GUIDANCE_DOCUMENTS_PRESENT_BUT_NOT_EXTRACTED` — 720 documents are present, but no extracted claims are currently available. This is a coverage status, not a claim that management gave no guidance.

Coverage reasons: persisted in `guidance_coverage_diagnostics.jsonl` as `GUIDANCE_AVAILABLE_AND_CAPTURED`, `GUIDANCE_NOT_IN_CURRENT_DOCUMENT_STORE`, or `NO_GUIDANCE_GIVEN`.

Initial vs latest: revision number and revision type are preserved; later guidance does not overwrite the initial record.

Resolved: 0.

Unresolved: 4.

Matching rejections: 1 unit mismatch; future FY2027 actuals remain unresolved; nonnumeric raised guidance remains unresolved.

## STRATEGIC COMMITMENTS

Before: 6 extracted records.

After: 1 material, observable ZM commitment concerning AI monetization and churn reduction.

Resolved: 0.

Unresolved: 1.

Rejected low-value: 5 guidance-like, historical, speculative, or vague statements.

## CAPITAL ALLOCATION

Acquisitions: 2 completed events from distinct source claims: an unnamed mid-July acquisition and Common Room at $250 million. They have separate program IDs.

Buybacks: 1 authorization for $1 billion; no execution was inferred.

Issuance: 0.

Debt: 0 events in the current qualitative claim corpus.

Capex: 1 planned capex event; planned spend remains distinct from realized spend.

Linkage issues: 0. Announcement/completion and authorization/execution remain separate statuses.

## ATTRIBUTION

Direct: 0.

Role-based: 0.

Team: 10: 5 guidance, 1 strategic, and 4 capital-allocation records.

Tenure overlap: 182.

Changes: company-level guidance remains unassigned to individual executives; causal language was not inferred from tenure overlap.

Unsupported rejected: 0.

## PRIOR COMPANIES

Confirmed with dates: 0.

Partial dates: 0.

Unknown dates: 2: AstraZeneca and Gartner. Both identities are source-confirmed, but no defensible dates are available.

Rejected: 2 prior-company mappings are excluded from historical performance analysis because dates are unknown.

## TRACK RECORD COVERAGE

ZM CEO: Eric S. Yuan; 2011–current; 13 operating metrics; 0 guidance, 0 strategic, 0 capital records; direct 0, role-based 0, team 0, tenure-overlap 13; HIGH coverage with person-attribution limitations.

ZM CFO: Michelle Chang; 2024-10–current; 13 operating metrics; 0 guidance, 0 strategic, 0 capital records; tenure-overlap 13; HIGH coverage with person-attribution limitations.

PLTR CEO: Alexander Karp; 2003–current; 14 operating metrics; 0 guidance, 0 strategic, 0 capital records; tenure-overlap 14; HIGH coverage with current-store coverage limitations.

PLTR CFO: David Glazer; 2013–current; 14 operating metrics; 0 guidance, 0 strategic, 0 capital records; tenure-overlap 14; HIGH coverage with current-store coverage limitations.

EXEL CEO: Michael M. Morrissey; 2010–current; 16 operating metrics; 0 guidance, 0 strategic, 0 capital records; tenure-overlap 16; HIGH coverage with current-store coverage limitations.

EXEL CFO: Christopher J. Senner; 2015-07–current; 15 operating metrics; 0 guidance, 0 strategic, 0 capital records; tenure-overlap 15; HIGH coverage with current-store coverage limitations.

## PROVENANCE

Failures: 0. All current facts, outcomes, commitments, events, and tenures retain source-linked provenance. Derived facts retain source fact IDs and formulas.

## IDEMPOTENCY

Second run: 2,304 facts, 182 operating outcomes, 17 tenures, 12 snapshots, 5 guidance outcomes, 5 capital events, 1 strategic commitment, and 3 guidance diagnostics were unchanged.

Duplicates: 0 duplicate stable IDs.

Network requests: 0 additional SEC requests on the cached rerun.

## TESTS

Tests passed: 293.

Tests added: 23 offline Component 6B tests, including restatements, annual/YTD separation, derived FCF/margins, period safety, guidance diagnostics/revisions, strategic filtering, acquisition linkage, prior-company date safeguards, and neutral guidance-coverage safeguards.

## FREEZE RECOMMENDATION

Component 6B ready to freeze: YES

Reason: SEC selection is canonical and revision-safe; period and tenure boundaries are validated; derivations are provenance-complete; guidance coverage gaps are explicit; strategic extraction is material-only; capital events are status-separated and linked; attribution remains conservative; prior-company performance is blocked without dates; and reruns are idempotent.
