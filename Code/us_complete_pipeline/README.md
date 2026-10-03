# US complete pipeline

Run the macro importer, macro analysis, then spread/correlation and momentum calculations for **both coincident and leading scores**. Each dataset uses its own four relationship workbooks. Every stage must succeed before the next starts.

## Run everything

Double-click **Start.command** on macOS or **Start.bat** on Windows. On macOS/Linux you can also run this from the project folder:

```bash
bash Start.command
```

Requires Python 3.10 or later. The launcher creates `.venv/` and installs missing packages when needed. It prompts for your external Google credential JSON and FRED API key if they are not already configured. Terminal output shows progress, elapsed time and any failure.

Normal runs update the configured Google Sheets and verify the results. There is no dry-run mode.

## Run one part

Use the same launcher with these options:

| What to run | Options after `bash Start.command` |
|---|---|
| Macro importer | `--stage importer` |
| Macro analysis | `--stage analysis` |
| All spread, correlation and momentum calculations | `--stage relationships` |
| Spread only | `--stage spread` |
| Correlation only | `--stage correlation` |
| Both momentum calculations | `--stage momentum` |
| Spread momentum only | `--stage spread-momentum` |
| Correlation momentum only | `--stage correlation-momentum` |

Add `--dataset leading` or `--dataset coincident` to restrict relationship calculations. The default is `--dataset all`.

```bash
bash Start.command --stage relationships --dataset leading
```

Independent stages use the existing upstream data. A full run refreshes it first. Selecting a dataset does not restrict the shared macro import/analysis stages.

## Run in Google Colab or move to another computer

Open [portable/](portable/README.md):

- **US_PIPELINE_COLAB.ipynb** â€” upload to Colab and run its single cell.
- **US_PIPELINE_COLAB.py** â€” copy the entire file into one Colab code cell.
- **us_complete_pipeline_portable.zip** â€” extract on another computer and use its launcher.

The Colab cell defaults to `STAGE = "all"` and `DATASET = "all"`. It contains all the runtime code. Supply your own Google credentials and FRED key; neither is included in the portable files.

## Telegram notifications

The main runner and direct import, indicator analysis, and relationship entry points
send one start notification and one final success, failure, or stopped report. For example:
`Indicators started`, `Indicators finished`, or `Correlation failed`. Alerts identify
the computer and include the selected relationship dataset. Failures include the exception and recent child
process output. Setup failures are covered too. Notification errors never stop the
pipeline; missing credentials print a short skipped message. Offline self-tests do
not send alerts. Forced process termination or a disconnected machine cannot send
a final alert.

Direct commands such as `python3 monthly_indicators.py`,
`python3 us_indicator_analysis.py --all`, and
`python3 run_relationships.py --correlation` now notify too (supply the usual
credentials/options). Relationship flags also support spread and momentum variants.
The same titles apply to `run_us_pipeline.py --stage importer` or `--stage correlation`.
A full run sends exactly one start notification and one final report;
its child commands are suppressed to avoid duplicate alerts. Help and self-tests
are silent. The calculation-library files such as `correlation.py` contain functions,
not executable jobs; use the relationship entry point to run them.

On your Mac, copy the updated project or extract the updated portable ZIP, keeping
the `notifications` folder alongside the scripts. Set `TELEGRAM_CHAT_ID` and
`TELEGRAM_BOT_TOKEN` in the environment of the terminal/scheduler that launches the
job, then run your existing command or `bash Start.command --stage importer`.
Windows environment settings do not transfer to the Mac. A Terminal-only variable
is not automatically available to a Finder-launched process or scheduled job.

The shared helper is `notifications/telegram.py`, using `TELEGRAM_CHAT_ID` and
`TELEGRAM_BOT_TOKEN` from the process environment. Existing Windows user variables
are inherited by newly opened terminals and launchers; restart your terminal if needed.
No secrets are stored in the source or portable exports.

Create a bot with Telegram's @BotFather, then start a chat with the bot (or add
it to your target group). Set the bot token and destination chat ID in the
launching process environment. All alerts use ordinary Telegram notifications.
See https://core.telegram.org/bots/api#sendmessage for the delivery API.

From PowerShell in this folder:

```powershell
$env:TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
$env:TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"
```

For scheduled runs, configure these variables for the account running the task
and restart its launching process so it inherits them.


```powershell
py -3 -m notifications.telegram  # Send one real test alert; no pipeline/Sheets writes
py -3 -m pytest -q tests/test_telegram.py  # Offline mocked notification tests
.\Start.bat  # Normal full run; updates Google Sheets
```

For Colab, add `TELEGRAM_CHAT_ID` and `TELEGRAM_BOT_TOKEN` in the Secrets panel
and enable notebook access for both, then run the updated portable notebook or
entire Python cell. Environment variables take precedence over Colab Secrets.
Alerts cover the extracted pipeline execution after Colab's credential prompts.

## Folder guide

| Location | Purpose |
|---|---|
| `Start.command`, `Start.bat` | Everyday launchers |
| Root `.py` files | Runtime engines and their entry points; kept together for direct execution |
| `portable/` | Ready-to-use Colab files and portable ZIP |
| `docs/` | Configuration, maintenance and verification details |
| `tools/` | Rebuild exports and synchronise the standalone project |
| `tests/` | Offline regression tests |
| `work/` | Generated logs, source archives and recovery backups |
| `.venv/` | Local Python environment; recreated on each machine |

Read [configuration](docs/configuration.md), [maintenance](docs/maintenance.md), or [verification evidence](docs/verification.md) for details. This project runs independently of the other project folders.

Correlation momentum counts genuine paired-data updates, so annual/quarterly histories are not inflated by repeated monthly values. See [mixed-frequency rules](docs/configuration.md#mixed-frequency-momentum) for windows, blank scores and input-date checks.

Telegram alerts include a run ID, computer, dataset, UTC timestamps, and elapsed time.
The final report includes stage durations, completed stages, and tracked value changes.
Stage progress remains available through /status and /logs. Failure alerts include available error
output. Long messages retain their beginning and final error with a shortening marker.
See [Telegram setup](docs/telegram-setup.md) for Windows setup and testing.

## Telegram phone controls on this server

Send `/run`, `/status`, or `/help` to the configured bot. The controller only
accepts the configured chat and authorised user. `/run` launches the same server
batch file used by the daily task; the daily schedule remains unchanged. A shared
lock prevents overlapping full-pipeline runs before setup or calculations begin.
`/status` reports running/idle and the latest recorded stage/result.

The elevated listener task starts at boot or login and runs after sign-out.
See [Telegram setup](docs/telegram-setup.md) for the full command list, live logs,
pipeline-only stopping, and the global `pythonstopall` command.
The controller depends on this Windows server's scheduled task and batch launcher;
copying the portable files alone does not install remote controls.

Run notifications now report value additions, revisions, removals, and affected
outputs rather than treating every rewritten cell as changed. Source input tabs,
indicator analysis output ranges, and relationship score grids are tracked.
Unavailable comparisons are labelled explicitly; formatting and helper-tab edits
are not included in the counts. No intermediate stage messages are sent.
