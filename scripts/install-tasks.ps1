# Register the scheduled tasks so nothing depends on remembering:
#   LifeCRM intake         - daily 07:00 + at logon (text + audio inboxes)
#   LifeCRM doctor         - daily 08:00 (the live database, after intake)
#   LifeCRM backup         - daily 21:30 (tiered retention)
#   LifeCRM verify-restore - weekly Sunday 12:00 (an untested backup is not a backup)
#   LifeCRM review         - weekly Monday 07:30 (the week that just ended, for /review-week)
# Run once, as your own user (no admin needed); re-running replaces them:
#   powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

# The logon trigger and the principal both name this user. A logon trigger
# without -User means "any user logs on", which only an administrator may register:
# a normal session gets "Access is denied" on the first task, and "Stop" then
# ends the script with nothing registered. The WindowsIdentity name is right
# for a Microsoft-account sign-in too, where $env:USERNAME can differ.
$Me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $Me -LogonType Interactive -RunLevel Limited

function Register-LifeCrmTask($Name, $Script, $Triggers) {
    # -WorkingDirectory: without it Task Scheduler starts in System32, and
    # `python -m lifecrm` there fails with "No module named lifecrm".
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Root\scripts\$Script`"" `
        -WorkingDirectory $Root
    # A laptop: the defaults skip a task that comes due on battery and kill
    # one when the charger is pulled - intake and backup would silently not run.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers `
        -Settings $settings -Principal $Principal -Force | Out-Null
    Write-Host "registered: $Name"
}

Register-LifeCrmTask "LifeCRM intake" "transcribe-inbox.ps1" @(
    (New-ScheduledTaskTrigger -Daily -At 07:00),
    (New-ScheduledTaskTrigger -AtLogOn -User $Me)
)
Register-LifeCrmTask "LifeCRM doctor" "doctor.ps1" @(
    (New-ScheduledTaskTrigger -Daily -At 08:00)
)
Register-LifeCrmTask "LifeCRM backup" "backup.ps1" @(
    (New-ScheduledTaskTrigger -Daily -At 21:30)
)
Register-LifeCrmTask "LifeCRM verify-restore" "verify-restore.ps1" @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 12:00)
)
# Monday morning, not Sunday evening: the pack covers the last full Mon-Sun
# week, and until the 05:00 rollover on Monday that is still the week before.
# 07:30 is after the rollover and after intake.
Register-LifeCrmTask "LifeCRM review" "review.ps1" @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 07:30)
)
Write-Host "done - inspect with: Get-ScheduledTask -TaskName 'LifeCRM*'"
Write-Host "logs: data\transcribe.log, data\doctor.log, data\verify-restore.log, data\review.log"
