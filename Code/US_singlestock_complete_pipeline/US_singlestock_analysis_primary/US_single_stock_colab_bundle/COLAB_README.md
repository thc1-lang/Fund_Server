# Current primary screener distribution

This distribution is generated from the canonical project source, model `primary_snapshot_v7`.
Use `US_Single_Stock_Colab_Standalone.py` for a single-file run. Supply your own workbook ID and credentials; neither is embedded.

```python
!python US_Single_Stock_Colab_Standalone.py --spreadsheet-id YOUR_SHEET_ID --credentials-file service_account.json --output-dir outputs
```

The command above reads Sheets and creates local audit files. Add `--live` only to refresh generated workbook outputs. Dataset is never modified. `--short-only` limits a live refresh to Short.

For a reproducible offline calculation, use `--input-snapshot outputs/source_snapshot.json --output-dir outputs/replay` without connection arguments.

The unpacked `main.py`, `config.py`, `src/` and tests are synchronized by the builder. Read README.md for the scoring and selection policies. The separate directory ending in `bundle 2` and older exports are historical copies.
