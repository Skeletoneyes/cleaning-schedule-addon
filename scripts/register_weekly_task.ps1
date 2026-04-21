# Registers a Windows scheduled task that runs the weekly reconciliation
# pull every Monday at 08:00 local time. Run this once as the current user.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_weekly_task.ps1
#
# To remove: Unregister-ScheduledTask -TaskName "CleaningReconcilePull" -Confirm:$false

$repoRoot = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repoRoot "scripts\reconcile_weekly.bat"

$action    = New-ScheduledTaskAction -Execute $bat
$trigger   = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 8:00am
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName "CleaningReconcilePull" `
    -Description "Weekly pull of HA add-on snapshot, Airbnb iCal, and GCal iCal for reconciliation." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force

Write-Host "Registered task 'CleaningReconcilePull' — Mondays at 08:00."
Write-Host "First run logs to .secrets\pulls\weekly.log"
