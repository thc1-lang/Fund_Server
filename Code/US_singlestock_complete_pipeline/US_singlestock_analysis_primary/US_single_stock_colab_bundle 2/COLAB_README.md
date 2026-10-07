# US Single-Stock Analysis — Google Colab bundle

This folder contains the runnable model code in one portable package. The original
`src` package structure is retained because Python imports depend on it.

## Included

- `main.py` — command-line entry point
- `config.py` — model controls and strategy weights
- `src/` — scoring, normalization, institutional gates, Sheets I/O and validation
- `tests/` — regression tests
- `requirements.txt` — Python dependencies
- `US_Single_Stock_Colab.ipynb` — guided Colab launcher

Generated outputs, virtual environments, caches, Word reports and Google service-
account credentials are deliberately excluded.

## Recommended use

1. Upload `US_single_stock_colab_bundle.zip` to Google Colab.
2. Extract it and open `US_Single_Stock_Colab.ipynb`, or copy the notebook cells
   into a new Colab notebook.
3. Install `requirements.txt`.
4. For a live Google Sheets run, make a copy of the workbook for each user and set
   that copy's spreadsheet ID in the notebook.
5. Upload that user's own service-account JSON privately and share the workbook
   with the service account email.

Never place a service-account JSON file in a shared notebook, ZIP file or public
Drive folder.

## Commands

Tests:

```bash
python -m pytest -q
```

Read-only local workbook run:

```bash
python main.py --input-xlsx workbook.xlsx --output-dir outputs/dry_run
```

Live Google Sheets run:

```bash
python main.py --live --output-dir outputs/live
```

The live command writes to the configured destination workbook. Use a workbook
copy unless the user is explicitly authorised to update the shared production file.
