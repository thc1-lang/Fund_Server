# Run once in an administrator PowerShell window as thc1.
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell using Run as administrator, then run this script again.'
}
$activePath = 'C:\Fund_Server\Code\us_complete_pipeline\work\active-pipeline.json'
if (Test-Path -LiteralPath $activePath) {
    $activeRun = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
    if (Get-Process -Id $activeRun.pid -ErrorAction SilentlyContinue) {
        throw 'A pipeline run is active. Wait for completion before restarting its Telegram listener.'
    }
}
$action = New-ScheduledTaskAction -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -Argument '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\Fund_Server\Scripts\run_telegram_listener.ps1"' -WorkingDirectory 'C:\Fund_Server\Code\us_complete_pipeline'
$account = New-ScheduledTaskPrincipal -UserId 'thc1' -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User 'thc1'))
# Restart only the Telegram listener, so its new elevated account takes effect now.
$existing = Get-ScheduledTask -TaskName 'US pipeline Telegram control' -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName 'US pipeline Telegram control'
    Start-Sleep -Seconds 2
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match '\s-m\s+notifications[.]control(?:\s|$)'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Register-ScheduledTask -TaskName 'US pipeline Telegram control' -Action $action -Principal $account -Settings $settings -Trigger $triggers -Description 'Authorised Telegram pipeline commands; start automatically after reboot without an interactive login.' -Force | Out-Null
Start-ScheduledTask -TaskName 'US pipeline Telegram control'
Write-Host 'Telegram controller installed and restarted for startup. The daily pipeline schedule is unchanged.'
