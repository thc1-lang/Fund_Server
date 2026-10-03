# Component 5B — Ownership Baseline Validation

The latest official SEC definitive proxies were parsed and persisted separately from Section 16 positions.

| Ticker | Proxy accession | Filing date | Beneficial ownership date | Rows |
| --- | --- | --- | --- | ---: |
| ZM | 0001628280-26-028866 | 2026-04-30 | 2026-03-31 | 15 |
| PLTR | 0001321655-26-000019 | 2026-04-24 | 2026-04-06 | 16 |
| EXEL | 0000939767-26-000046 | 2026-04-15 | 2026-02-27 | 23 |

Proxy rows retain class breakdowns, reported percentage displays, voting power, row markers, table context, and SEC provenance. Institutional and other 5% holders are stored as major beneficial owners and are not silently classified as management.

The baseline store is `ownership_baselines.jsonl`; conservative joins to Section 16 evidence are in `ownership_reconciliations.jsonl`. A second cache-only run made zero network requests and produced 54 unique baselines and 47 unique reconciliation records.

The full test suite passes: 258 tests.
