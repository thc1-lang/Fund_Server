# US COT import

Downloads the CFTC annual **futures-only Disaggregated** and **Traders in Financial Futures (TFF)** reports, audits each contract, and updates the **COT Reports** Google spreadsheet.

## Files

| File | Purpose |
| --- | --- |
| `us_COT.py` | Downloads, validates, audits, and uploads the reports. |
| `run_us_COT.cmd` | Runs the import with the installed Python and service account, writing a log. |
| `requirements.txt` | Python packages needed on a new machine. |
| `tests/test_us_COT.py` | Checks date parsing and the main audit edge cases. |

The scheduled task uses `C:\Fund_Server\Scripts\run_us_COT.cmd`, a small forwarding file that calls the runner here. Keep both runner files in place.

## What each run does

1. Selects the current UTC year for each report. If a current-year annual file is unavailable, it searches for the newest older annual file and prints a warning. `--year` requests one specific year and does not fall back.
2. Downloads and validates both CFTC files before changing the spreadsheet. It checks the report year and the expected report schema.
3. Builds one audit row per contract code. Dates missing **between** a contract's first and last observations count as internal gaps; later source dates count as lag. This is an audit of the selected annual file, not the full historical archive.
4. Clears and replaces four tabs in the configured spreadsheet:

   | Raw tab | Audit tab |
   | --- | --- |
   | `COT` | `COT_Audit` |
   | `COT2` | `COT2_Audit` |

The raw tabs contain the downloaded CFTC rows. The audit tabs include status, source dates, coverage, missing dates, duplicate dates, and source lag. `SOURCE_DELAYED` means the latest source observation is older than the configured threshold; it does not prove a CFTC publication delay. A market absent from the entire selected file cannot be found by this audit.

For a normal upload, the existing pipeline Telegram bot sends exactly two messages to the same configured group chat: **COT import started**, followed by **COT import finished** with duration, source dates, row and contract counts, audit totals, computer name, and matching run ID. A failed run sends **COT import failed** with the error and log location. Telegram delivery is best effort and never changes the import result. `--dry-run` and `--no-telegram` suppress these messages.

**Upload behavior:** the four tabs are updated one after another. If an API error occurs during an upload, previously updated tabs can contain the new data and a tab being written can be incomplete. Check the log's final exit code before treating a run as complete.

## Setup on this machine

- Python: `C:\Users\thc1\AppData\Local\Python\pythoncore-3.14-64\python.exe`
- Credential file: `C:\Fund_Server\Code\us_complete_pipeline\google_credentials.json`
- Destination spreadsheet: **COT Reports** (ID configured in `us_COT.py`)
- Log: `C:\Fund_Server\Logs\us_COT.log`

The service account JSON is **not** stored in this folder. The runner and the script's default path both point to the existing file used by the main pipeline. If that file is moved, update the path in `run_us_COT.cmd` and pass `--service-account-path` for direct runs. The service account must have edit access to the spreadsheet.

On another machine, install dependencies with `python -m pip install -r requirements.txt`, then update the Python executable and credential path in the runner.

## Run and check

From PowerShell:

```powershell
python C:\Fund_Server\Code\us_cot_import\us_COT.py --dry-run
```

The dry run downloads and audits both reports without using Google credentials, changing the spreadsheet, or sending Telegram messages. To perform the upload, run:

```powershell
& C:\Fund_Server\Code\us_cot_import\run_us_COT.cmd
```

The runner accepts the same options as the Python script. For example, `& C:\Fund_Server\Code\us_cot_import\run_us_COT.cmd --dry-run` tests the exact scheduled launcher without uploading.

The runner appends output to `C:\Fund_Server\Logs\us_COT.log`. To watch it live after starting a run:

```powershell
Get-Content C:\Fund_Server\Logs\us_COT.log -Tail 20 -Wait
```

Press **Ctrl+C** to stop watching the log. A complete run prints four `Uploaded ... rows` lines and ends with `Exit code 0`. An error and a nonzero exit code mean the run needs attention. A successful 26 September 2026 run uploaded 10,336 Disaggregated rows, 3,342 TFF rows, and their audit tabs.

## Saturday schedule

Windows Task Scheduler has a task named **US COT report**, scheduled weekly on **Saturday at 10:30 local time**. Its action is `C:\Fund_Server\Scripts\run_us_COT.cmd`. To change the time, open **Task Scheduler → Task Scheduler Library → US COT report → Properties → Triggers**. To test it, right-click the task and choose **Run**, then check the log and **Last Run Result** (`0x0` means success).

You can also run it from the authorised Telegram group using `/us_cot_import` (short form: `/cot`). The bot launches the same runner and sends the normal start and completion reports. Use `/us_cot_status` (short form: `/cot_status`) to see whether it is running and the latest logged exit result. The controller rejects unauthorised users, expired or forwarded commands, commands from another chat, and a second request while a COT import is active.

The machine must be on and able to reach CFTC and Google when the task runs. The Windows account running the task must be able to read the Python executable and service account file. If you move or delete `C:\Server`, the current task is unaffected because its action points to `C:\Fund_Server`.

## Options and troubleshooting

Run `python C:\Fund_Server\Code\us_cot_import\us_COT.py --help` for all options. Useful examples:

```powershell
python C:\Fund_Server\Code\us_cot_import\us_COT.py --year 2025 --dry-run
python C:\Fund_Server\Code\us_cot_import\us_COT.py --stale-after-days 14 --dry-run
```

- **Credential file not found:** confirm the path in the runner or pass `--service-account-path`.
- **Google 403:** confirm that the service account has edit access to the spreadsheet.
- **CFTC download error:** retry later; the script does not silently treat outages or access errors as a missing annual file.
- **No `Exit code 0`:** read the error above the final line in `us_COT.log`, fix it, and rerun.
