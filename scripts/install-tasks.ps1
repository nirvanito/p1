# Register the three scheduled tasks so nothing depends on remembering:
#   LifeCRM intake         - daily 07:00 + at logon (text + audio inboxes)
#   LifeCRM backup         - daily 21:30 (tiered retention)
#   LifeCRM verify-restore - weekly Sunday 12:00 (an untested backup is not a backup)
# Run once, as your own user (no admin needed):
#   powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Register-LifeCrmTask($Name, $Script, $Triggers) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Root\scripts\$Script`""
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers `
        -Settings $settings -Force | Out-Null
    Write-Host "registered: $Name"
}

Register-LifeCrmTask "LifeCRM intake" "transcribe-inbox.ps1" @(
    (New-ScheduledTaskTrigger -Daily -At 07:00),
    (New-ScheduledTaskTrigger -AtLogOn)
)
Register-LifeCrmTask "LifeCRM backup" "backup.ps1" @(
    (New-ScheduledTaskTrigger -Daily -At 21:30)
)
Register-LifeCrmTask "LifeCRM verify-restore" "verify-restore.ps1" @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 12:00)
)
Write-Host "done - inspect with: Get-ScheduledTask -TaskName 'LifeCRM*'"
