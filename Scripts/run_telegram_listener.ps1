$ErrorActionPreference = 'Continue'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$migrationPidPath = 'C:\Fund_Server\Code\us_complete_pipeline\work\telegram-migration-stop.pid'
if (Test-Path -LiteralPath $migrationPidPath) {
    $targetPid = [int](Get-Content -LiteralPath $migrationPidPath -Raw)
    $target = Get-CimInstance Win32_Process -Filter "ProcessId=$targetPid"
    if ($target -and $target.Name -eq 'python.exe' -and $target.CommandLine -match '\s-m\s+notifications[.]control(?:\s|$)') {
        Stop-Process -Id $targetPid -Force -ErrorAction Stop
        Remove-Item -LiteralPath $migrationPidPath -Force
        exit 0
    }
    throw 'Migration target is not the Telegram listener; no process was stopped.'
}
Set-Location 'C:\Fund_Server\Code\us_complete_pipeline'
while ($true) {
    & 'C:\Users\thc1\AppData\Local\Python\pythoncore-3.14-64\python.exe' -X utf8 -u -m notifications.control >> 'C:\Fund_Server\Logs\telegram_listener.log' 2>&1
    Start-Sleep -Seconds 10
}
