$ErrorActionPreference = 'Stop'
$taskName = 'US pipeline Telegram control'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
if ($task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $taskName
}
Start-Sleep -Seconds 3
$healthPath = 'C:\Fund_Server\Code\us_complete_pipeline\work\telegram-health.json'
if (Test-Path -LiteralPath $healthPath) {
    $listenerPid = [int]((Get-Content -LiteralPath $healthPath -Raw | ConvertFrom-Json).pid)
    if (Get-Process -Id $listenerPid -ErrorAction SilentlyContinue) {
        $migrationPidPath = 'C:\Fund_Server\Code\us_complete_pipeline\work\telegram-migration-stop.pid'
        Set-Content -LiteralPath $migrationPidPath -Value $listenerPid -NoNewline
        Start-ScheduledTask -TaskName $taskName
        for ($attempt = 0; $attempt -lt 10 -and (Test-Path -LiteralPath $migrationPidPath); $attempt++) {
            Start-Sleep -Seconds 1
        }
        if (Test-Path -LiteralPath $migrationPidPath) {
            throw 'The elevated Telegram listener did not stop; no getUpdates setup will start.'
        }
    }
}
Set-Location 'C:\Fund_Server\Code\us_complete_pipeline'
& 'C:\Users\thc1\AppData\Local\Python\pythoncore-3.14-64\python.exe' -X utf8 -u 'tools\configure_telegram_group.py'
if ($LASTEXITCODE -ne 0) { throw 'Telegram group configuration did not complete; listener remains stopped.' }
Start-ScheduledTask -TaskName $taskName
Write-Host 'Group-only Telegram listener started. The daily pipeline task was not changed.'
