# Telegram group controller on this Windows server

The existing Fund-server bot sends all pipeline notifications and command replies to one private Telegram group. `TELEGRAM_CHAT_ID` is its negative group ID. `TELEGRAM_ALLOWED_USER_IDS` is a comma-separated list of positive numeric user IDs. Private chats and other groups are ignored. `TELEGRAM_ALLOWED_USER_ID` is unsupported.

Run `C:\Server\Scripts\configure_telegram_group.ps1` as Windows user `thc1` to configure or migrate the group. The script stops only the Telegram listener, prompts for one ordinary group message from each authorised person, shows the discovered group and users, and requires `YES` before writing the group and user IDs to the Windows user environment. It reuses the existing bot token without displaying it, checkpoints setup updates, and restarts the existing listener task. The daily pipeline task and credentials for Google and FRED are untouched.

The listener reads process settings first, then saved Windows user settings. Restart the listener after changing a saved setting. Scheduled pipeline runs read the same group chat ID and send notifications there. Do not start a second getUpdates client for this bot.
## Notification details

Every executable entry point sends exactly one start notification and one final
success, failure, or stopped report. The start names the entire pipeline or the
selected part, dataset, and UTC start time. The final report gives duration, finish
time, stage completion and timing, tracked value changes, and affected outputs.
It points to /status or /logs for further details. Stage transitions do not send
notifications, and a successful phone launch no longer sends an extra request
acknowledgement. Responses to explicit status, log, or stop commands remain available.

Change counts come from actual comparisons or successful source sync results:
source indicator inputs, indicator analysis output ranges, and relationship score
grids. Rewriting identical values does not count as a change. Analysis adds a
best-effort read before writing so it can compare old and new values. If that
read fails, normal writing continues and the report labels the comparison as
unavailable. Formatting, helper tables, and uninstrumented source/audit outputs
are not included; a no-change statement is limited to the tracked ranges.
Numeric comparisons allow tiny floating-point differences. Counts describe value
operations during this run, not a complete workbook revision history.

The run keeps a `changes.jsonl` journal and includes its events in `summary.json`.
Failures may follow partial writes: the final report explains this and includes
the available error. Notifications themselves never block a run on delivery
failure. Help and self-tests are silent. A killed process or lost network cannot
guarantee a final message.

For macOS, export `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the launcher's
environment. For Colab, add these names to Secrets and grant notebook access.
No tokens are included in portable exports.

Official references: [Bot creation](https://core.telegram.org/bots/tutorial#obtain-your-bot-token),
[Getting updates](https://core.telegram.org/bots/api#getupdates), and
[Sending messages](https://core.telegram.org/bots/api#sendmessage).

## Run the pipeline from your phone

The Windows server controller accepts `/run`, `/status`, and `/help`. `/run`
starts `C:\Server\Scripts\run_us_pipeline.bat`, the same launcher used by the
daily task, including normal Google Sheets updates. It does not change the daily
trigger. The shared pipeline lock is acquired before setup or calculations, so
phone, scheduled, and manual full-pipeline runs cannot overlap.

Only users named by `TELEGRAM_ALLOWED_USER_IDS` can issue commands in the configured
group. The group ID never authorises a user. Telegram command
text never becomes a shell command, path, or Python argument. Forwarded and edited
messages are ignored; commands older than two minutes expire. Initial startup
discards old queued commands when there is no offset. Processed update IDs are saved before dispatch, so
restarts do not repeat a run request. If a crash occurs between saving and dispatch,
the request may be lost: check `/status` and send a fresh `/run` if idle.

`/status` reports the task state and latest recorded summary, including stage and
durations. Stage summaries update at stage boundaries; this is not a live row
counter. A manual run does not cancel the next daily run.

The listener is installed as task `US pipeline Telegram control`, with startup and
login triggers, S4U logon as `thc1`, and highest privileges. The administrator
installer has been completed on this server. The controller works without an
interactive login, provided the server is on and online. Its PowerShell supervisor
restarts it after a Python exit; Task Scheduler restarts the supervisor on failure.
Logs are in `C:\Server\Logs\telegram_listener.log`.

To reinstall or deploy updates, run this single command in administrator PowerShell
as `thc1`, while the pipeline is idle:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Server\Scripts\install_telegram_startup.ps1
```

This changes only the listener task, leaving the daily pipeline schedule unchanged.
S4U uses local settings and HTTPS; network shares or Windows-integrated network
authentication would require a different task logon setup.

The listener code is in `notifications/control.py`. It is Windows-specific and
expects the named local scheduled task and server batch launcher; portable exports do not register tasks.

## Full phone command list

Send the exact names below as ordinary messages. A leading slash is optional.
Underscores work instead of hyphens. Telegram's menu uses underscores because
its command names only allow letters, digits and underscores, up to 32 characters.
For the longest name the menu uses `/us_macro_correlation_momentum`; the full
`us-macroanalysis-correlation-momentum` text still works.

| Command | Action |
|---|---|
| `us-macroanalysis-run` | Run the full pipeline |
| `us-macroanalysis-status` | Show running state and latest recorded result |
| `us-macroanalysis-logs` | Watch the server log in a message updated every 30 seconds |
| `us-macroanalysis-stop` | Request stopping this pipeline and its current stage process tree |
| `us-macroanalysis-indicators` | Run importer only |
| `us-macroanalysis-analysis` | Run indicator analysis only |
| `us-macroanalysis-relationships` | Run all relationship calculations |
| `us-macroanalysis-spread` | Run spread only |
| `us-macroanalysis-correlation` | Run correlation only |
| `us-macroanalysis-momentum` | Run momentum only |
| `us-macroanalysis-spread-momentum` | Run spread momentum only |
| `us-macroanalysis-correlation-momentum` | Run correlation momentum only |
| `us-stock-run` | Run the complete US single-stock Primary → Secondary → Summary pipeline |
| `us-stock-primary` | Run the Primary Zacks screen and publication only |
| `us-stock-secondary` | Run the Secondary SEC screen and publication only |
| `us-stock-summary` | Rebuild the single-stock evidence package and human summary only |
| `us-stock-status` | Show single-stock state and latest recorded result |
| `us-stock-logs` | Watch the separate single-stock log; repeat to stop watching |
| `us-stock-stop` | Stop the single-stock pipeline only |
| `pythonstopall` | Force-stop all Python processes on the server, including the bot |

Shortcuts `/run`, `/status`, `/logs`, `/stop`, and `/help` remain available.
Stage runs use dataset `all`, matching the main runner's default, and share the
same lock as full and scheduled runs. All run commands perform actual updates.

Live logs show a bounded tail with known credentials redacted. A watch has no time limit and continues after a run finishes; sending the logs
command again stops watching. It edits one message instead of flooding the chat. Watches are
not restored after a listener restart.

Pipeline-only stop creates a request tied to the active run ID. The pipeline's
own process handles it, so it also works for an elevated scheduled run without
killing other Python programs. During a stage it checks every second and stops
the stage's process tree; during setup it stops at the next checkpoint. Partial
spreadsheet updates are not rolled back. An older run launched before this code
upgrade cannot be remotely stopped by this mechanism.

`pythonstopall` requires an elevated listener. It sends an acknowledgement before
spawning the separate PowerShell termination helper. It does not distinguish this
project from other Python jobs. The supervisor starts a new bot after about 10
seconds plus connection time; stopped calculation jobs are not restarted. This
command does not disable the next daily scheduled run. Global force-stop tests
use mocks; the live server-wide kill was not executed during installation.

## Single-stock notification policy

The single-stock runner sends a start and a final result or failure notification
to this same protected group. It records the published shortlist memberships
locally and names tickers and categories only when a membership has changed.
Unchanged shortlists receive a concise completion message. Its run lock,
scheduled task, stop request, status record, and live log are separate from the
macro pipeline, so commands always affect the intended job.

## Example notification layout (illustrative values)

```text
Pipeline started
Scope: Entire US pipeline
Dataset: All
Started: 19 Sep 2026, 11:00 UTC
You will receive one completion report when this run ends.
```

```text
Pipeline finished
Scope: Entire US pipeline
Dataset: All
Duration: 18m 42s
Finished: 19 Sep 2026, 11:18 UTC
Stages completed: 3/3

Source data: 2 added, 4 updated values.
  Affected: GDP, Industrial production
Indicator analysis: 127 updated values.
  Affected: GDP, Industrial production
Relationship scores: no value changes detected in tracked ranges (8 outputs checked).

Completed:
Macro importer — 4m 20s
Macro analysis — 6m 10s
Spread, correlation and momentum (all) — 8m 12s

Details: /status or /logs
```

Messages also include the computer and a short run ID to match start and end.
