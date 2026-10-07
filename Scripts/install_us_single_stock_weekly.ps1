# Run once in an administrator PowerShell window. Default schedule: Sunday 08:00 local time.
param(
    [ValidateRange(0, 23)] [int] $Hour = 8,
    [ValidateRange(0, 59)] [int] $Minute = 0,
    [ValidateSet('Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday')] [string] $Day = 'Sunday'
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell using Run as administrator, then run this script again.'
}
$taskName = 'US single-stock pipeline'
$time = Get-Date -Hour $Hour -Minute $Minute -Second 0
$action = New-ScheduledTaskAction -Execute 'C:\Fund_Server\Scripts\run_us_single_stock_pipeline.bat' -Argument '--stage all' -WorkingDirectory 'C:\Fund_Server\Code\US_singlestock_complete_pipeline'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $time
$account = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $account -Settings $settings -Description 'Weekly US single-stock Primary, Secondary, and Summary refresh.' -Force | Out-Null
Write-Host "Installed $taskName for every $Day at $($time.ToString('HH:mm')) local time."
