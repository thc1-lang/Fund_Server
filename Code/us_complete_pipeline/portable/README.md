# Portable files

Choose one:

| File | Use |
|---|---|
| `US_PIPELINE_COLAB.ipynb` | Upload to Google Colab and run the single code cell. |
| `US_PIPELINE_COLAB.py` | Copy the whole file into one Colab code cell. All source code is embedded. |
| `us_complete_pipeline_portable.zip` | Extract on another computer and run `Start.command` or `Start.bat`. |

In Colab, set `STAGE` to the calculation you need and `DATASET` to `all`, `leading` or `coincident`. Both default to `all`. Upload your Google service-account JSON when prompted. The full run and importer also need a FRED API key, entered at a masked prompt or supplied through Colab Secrets as `FRED_API_KEY`.

The service account must have access to the existing configured workbooks. These files transfer the code, not the Google workbooks or their permissions. A fresh Colab runtime needs credentials again; keep the session running until completion and download any audit files you need before discarding it.

For start, completion, and failure notifications, add
`TELEGRAM_CHAT_ID` and `TELEGRAM_BOT_TOKEN` to Colab Secrets and enable notebook
access for both. Existing environment variables take precedence. The embedded
shared helper sends alerts for the pipeline after credential collection; missing
secrets or delivery errors do not block calculations. No secrets are bundled.

Alerts are tailored to the selected job: for example, `Indicators started` /
`Indicators finished` or `Correlation failed`. The portable ZIP supports the same
notifications when individual entry points are run directly on a Mac or Windows.
Keep the included `notifications` folder with the scripts and set both Telegram
environment variables on that computer. A full pipeline sends one start notification and one final report; its child processes do not send duplicate alerts.

These are generated files. Edit the maintained source, then rebuild all three from the project folder:

```bash
python3 tools/build_colab.py
```

The ZIP excludes credentials, `.venv/`, caches and `work/`. It does not contain a copy of itself. See the [main README](../README.md) for stage options.

Telegram alerts include a run ID, computer, dataset, UTC timestamps, and elapsed time.
The final report includes elapsed time, completed stages, and tracked value changes. Failure alerts include available error
output. Long messages retain their beginning and final error with a shortening marker.
See [Telegram setup](../docs/telegram-setup.md) for Windows setup and testing.
