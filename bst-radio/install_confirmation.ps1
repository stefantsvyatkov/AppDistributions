<#
.SYNOPSIS
    Sets up (again) the daily second opinion for BST Radio's stream check on the owner's computer.

.DESCRIPTION
    Registers the Task Scheduler task "BST Radio stream confirmation": every day at 14:00, or as soon as the computer
    is on after a missed time, pythonw runs confirm_dead.py from this folder, without a window. It runs only while
    the owner is logged on, as the GitHub CLI's login is theirs. Run it again after reinstalling Windows, once Python,
    ffmpeg (winget install Gyan.FFmpeg) and the GitHub CLI (winget install GitHub.cli, then gh auth login) are back.
    -Remove takes the task away.
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$name = "BST Radio stream confirmation"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed: $name"
    return
}

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { throw "Python is not installed (pythonw was not found)." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "ffmpeg is not installed: winget install Gyan.FFmpeg" }
$script = Join-Path $PSScriptRoot "confirm_dead.py"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At 14:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "BST Radio: plays from Bulgaria the streams the GitHub check found dead, and publishes the ones that fail here too." `
    -Force | Out-Null
Write-Output "Registered: $name, daily at 14:00 (log: $env:TEMP\bst-radio-confirmation.log)"
