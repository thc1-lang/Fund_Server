# Leading dataset integration — verified 14 September 2026

At the time of this verified run, both projects calculated coincident and leading datasets in eight separate workbooks. Default runs include both; `--dataset leading` or `--dataset coincident` restricts relationship calculations. Macro import and analysis still precede them in a full pipeline run.

## Live leading run

All four new workbooks were published and read back successfully. Every output has 666 pairs, zero invalid mappings and latest calculated date 31 August 2026. Numerical controls were retained: window 24 and momentum lookback 3.

| Output | Numeric score cells | Matrix cells verified, including blanks |
|---|---:|---:|
| leading_correlation | 205,031 | 779,886 |
| leading_spread | 168,792 | 779,220 |
| leading_spread_momentum | 166,866 | 779,220 |
| leading_correlation_momentum | 187,736 | 779,886 |

The four Calculation Helpers were independently reconstructed and verified too. Score output layouts were visually inspected in Google Sheets. Date/header formulas, source anchors and controls were read back through the connector. Existing coincident workbooks were outside the live run's write allowlist.

The leading base workbooks already imported Leading Score. The copied spread date formula still imported Coincident Score; it now uses its local leading input dates. Both copied momentum source IDs/imports were corrected to the corresponding leading base workbooks, and the copied helper titles were refreshed. The leading spread-to-momentum import required Google's initial Allow access connection; it was completed and the imported values verified before publication.

The run took approximately 245 seconds. Its reports and pre-write/source-link backups are under `us_complete_pipeline/work/20260913T235840.837787Z/`. Missing, insufficient-history and mathematically undefined observations remain blank; matrix-cell counts include those verified blanks.

## Code validation

- Complete project: 144 tests passed.
- Standalone relationship project: 131 tests passed.
- Shared engines and shared regression tests are byte-identical across the two projects.
- Standalone Colab source extraction, notebook/source parity and 144 tests in an unrelated temporary directory passed.
- Tests cover exact target IDs, dataset-qualified momentum sources, write isolation, controls, prior-window calculations and stale/wrong-dataset source rejection.

The original calculation definitions were preserved. No full macro re-import was run for this update: the leading analysis used the data already in Sheets, as requested. Colab browser execution itself was not repeated; the self-contained export was extracted and tested locally.


This records the verified leading run before the folder cleanup. The cleanup changes packaging and documentation; the calculation engines remain unchanged. See [maintenance](maintenance.md) for current commands and layout.
